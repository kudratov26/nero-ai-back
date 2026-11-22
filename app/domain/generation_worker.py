"""The Generation worker: claim a queued job, render it, and settle its Credits.

A job is claimed in one short transaction with `SELECT ... FOR UPDATE SKIP
LOCKED`, so two workers never take the same row and neither blocks the other.
The claim stamps `locked_by`/`locked_at`, increments `attempts`, and moves the row
to `running`. Rendering then happens outside any transaction, because a slow
provider must not hold a connection, and progress is reported in its own short
transaction each time.

Success is atomic: the output Assets, the Credit commit, and the `succeeded`
status commit together. A terminal failure marks the row `failed` and refunds in
one transaction, so a reservation is either consumed or returned and never both.
A job cancelled while it was running is discarded without touching Credits,
because `cancel_generation` already refunded it.

Retryable provider failures leave the row `queued` with its reservation intact,
so the next claim tries again. When `attempts` reaches `max_attempts`, the job
fails instead. A lock older than `stale_after` belongs to a worker that died, so
the next claim takes it over rather than leaving the job stuck forever.

Logs carry ids, counts, and durations. They never carry the prompt or the output.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ErrorCode
from app.core.metrics import METRICS
from app.db.models import Generation
from app.db.models.common import utcnow
from app.db.session import session_scope
from app.domain import credits
from app.domain.assets import add_asset
from app.providers.base import (
    GenerationJob,
    GenerationOutput,
    GenerationProvider,
    ProviderCancelledError,
    ProviderError,
    ProviderModel,
)
from app.storage.local import LocalStorage

log = structlog.get_logger(__name__)

#: Failure codes a Generation can record. They are stable strings, not problem
#: enum values, because a worker failure outlives the request that created it.
ERROR_INTERNAL = ErrorCode.INTERNAL_ERROR.value

SUCCESS_CREDIT_REASON = "Completed generation"
FAILED_CREDIT_REASON = "Failed generation"


@dataclass(frozen=True)
class ClaimedJob:
    """A Generation this worker owns until it finishes, retries, or fails."""

    generation_id: UUID
    user_id: UUID
    modality: str
    model_id: str
    prompt: str
    params: dict[str, object]
    cost: int
    attempts: int
    max_attempts: int
    worker_id: str


def _to_job(row: Generation, *, worker_id: str) -> ClaimedJob:
    return ClaimedJob(
        generation_id=row.id,
        user_id=row.user_id,
        modality=row.modality,
        model_id=row.model_id,
        prompt=row.prompt,
        params=dict(row.params),
        cost=row.cost,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        worker_id=worker_id,
    )


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    stale_after: timedelta,
) -> ClaimedJob | None:
    """Claim one job, or return `None` when the queue is empty.

    The row is locked only for the claim itself. The returned `ClaimedJob` is a
    flat copy, so no lazy attribute can touch a closed session later.
    """
    async with session_scope(session_factory) as session:
        # The candidate id is chosen and locked in one statement. `SKIP LOCKED`
        # makes a concurrent claim skip a row another worker holds instead of
        # waiting for it, and `session.get` re-reads the locked row for the update.
        candidates = (
            select(Generation.id)
            .where(
                or_(
                    Generation.status == "queued",
                    (
                        (Generation.status == "running")
                        & (
                            (Generation.locked_at.is_(None))
                            | (Generation.locked_at < utcnow() - stale_after)
                        )
                    ),
                ),
            )
            .order_by(Generation.status.desc(), Generation.created_at, Generation.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        generation_id = await session.scalar(candidates)
        if generation_id is None:
            return None

        row = await session.get(Generation, generation_id, with_for_update=True)
        if row is None:  # pragma: no cover - the row was just locked
            return None

        if row.attempts >= row.max_attempts:
            # No attempts are left, so rendering would be pointless. Fail it and
            # refund in the claim transaction rather than stranding the Credits.
            await _mark_failed(session, row, error_code=ERROR_INTERNAL, reason="exhausted")
            return None

        row.status = "running"
        row.locked_by = worker_id
        row.locked_at = utcnow()
        row.attempts += 1
        if row.started_at is None:
            row.started_at = utcnow()
        job = _to_job(row, worker_id=worker_id)

    log.info(
        "generation.claimed",
        generation_id=str(job.generation_id),
        modality=job.modality,
        model_id=job.model_id,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
        worker_id=worker_id,
    )
    return job


async def report_progress(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: ClaimedJob,
    percent: int,
) -> bool:
    """Record progress for a job this worker still owns.

    Returns `False` when the job is no longer owned: it finished, or another
    worker recovered the lock after this one went stale. Progress only moves
    forward, so a late callback cannot rewind a completed bar.
    """
    bounded = max(0, min(100, percent))
    async with session_scope(session_factory) as session:
        statement = (
            update(Generation)
            .where(
                Generation.id == job.generation_id,
                Generation.status == "running",
                Generation.locked_by == job.worker_id,
            )
            # `greatest` keeps progress monotonic even if a late callback repeats a
            # lower value, and still reports the row so ownership stays decidable.
            .values(progress=func.greatest(Generation.progress, bounded))
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await session.execute(statement))
    return (result.rowcount or 0) > 0


async def _settle_success(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    *,
    job: ClaimedJob,
    outputs: Sequence[GenerationOutput],
) -> bool:
    """Insert the outputs, commit the Credits, and mark the job `succeeded`.

    Runs in one transaction so a half-written result cannot be observed. Returns
    `False` when the worker no longer owns the job — a concurrent cancel won — in
    which case the outputs are discarded and Credits stay with the cancel.
    """
    if not outputs:
        raise ProviderError("the provider returned no output", retryable=False)

    async with session_scope(session_factory) as session:
        row = await session.scalar(
            select(Generation)
            .where(
                Generation.id == job.generation_id,
                Generation.locked_by == job.worker_id,
            )
            .with_for_update()
        )
        if row is None or row.status != "running":
            if row is not None:
                # Release the lock without touching Credits: the cancel route
                # already refunded, or another worker settled this row.
                row.locked_by = None
                row.locked_at = None
            log.info(
                "generation.discarded",
                generation_id=str(job.generation_id),
                reason="not_owned",
            )
            return False

        for output in outputs:
            await add_asset(
                session,
                storage,
                user_id=job.user_id,
                kind=job.modality,
                mime_type=output.mime_type,
                data=output.data,
                width=output.width,
                height=output.height,
                duration_ms=output.duration_ms,
                generation_id=job.generation_id,
            )

        if job.cost > 0:
            await credits.commit(
                session,
                user_id=job.user_id,
                amount=job.cost,
                reason=SUCCESS_CREDIT_REASON,
                ref_type="generation",
                ref_id=job.generation_id,
            )

        row.status = "succeeded"
        row.progress = 100
        row.locked_by = None
        row.locked_at = None
        row.error_code = None
        row.finished_at = utcnow()

    log.info(
        "generation.succeeded",
        generation_id=str(job.generation_id),
        assets=len(outputs),
        attempt=job.attempts,
        cost=job.cost,
    )
    return True


async def _settle_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: ClaimedJob,
    error_code: str,
    retryable: bool,
) -> bool:
    """Retry a retryable failure, or fail the job and refund it once.

    Returns `True` when the job stays queued for another attempt and `False` when
    it reached a terminal state. A discarded job (cancelled mid-run) also returns
    `False` and moves no Credits.
    """
    async with session_scope(session_factory) as session:
        row = await session.scalar(
            select(Generation)
            .where(
                Generation.id == job.generation_id,
                Generation.locked_by == job.worker_id,
            )
            .with_for_update()
        )
        if row is None:
            return False

        if row.status == "cancelled":
            # The cancel route already refunded. Release the lock and move on.
            row.locked_by = None
            row.locked_at = None
            log.info(
                "generation.discarded",
                generation_id=str(job.generation_id),
                reason="cancelled",
            )
            return False

        if row.status != "running":
            return False

        can_retry = retryable and row.attempts < row.max_attempts
        row.locked_by = None
        row.locked_at = None
        row.error_code = error_code
        if can_retry:
            row.status = "queued"
            log.warning(
                "generation.retrying",
                generation_id=str(job.generation_id),
                attempt=row.attempts,
                max_attempts=row.max_attempts,
                error_code=error_code,
            )
            return True

        await _mark_failed(session, row, error_code=error_code, reason="failed")
        return False


async def _mark_failed(
    session: AsyncSession,
    row: Generation,
    *,
    error_code: str,
    reason: str,
) -> None:
    """Mark a job failed and refund its reservation, in the caller's transaction.

    The refund is the compensating entry for the reservation made at create, so a
    failed job can never leave Credits held.
    """
    row.status = "failed"
    row.error_code = error_code
    row.finished_at = utcnow()
    row.locked_by = None
    row.locked_at = None
    if row.cost > 0:
        await credits.refund(
            session,
            user_id=row.user_id,
            amount=row.cost,
            reason=FAILED_CREDIT_REASON,
            ref_type="generation",
            ref_id=row.id,
        )
    log.warning(
        "generation.failed",
        generation_id=str(row.id),
        error_code=error_code,
        attempts=row.attempts,
        refunded=row.cost,
        reason=reason,
    )


def _error_code(exc: ProviderError) -> str:
    # The taxonomy's codes are the documented problem codes; they are stable
    # enough to store on the row and show to the owner.
    return exc.code.value


def _job_for(row: ClaimedJob) -> GenerationJob:
    return GenerationJob(
        generation_id=row.generation_id,
        modality=row.modality,  # type: ignore[arg-type]
        model=ProviderModel(slug=row.model_id, provider_model_id=row.model_id),
        prompt=row.prompt,
        params=dict(row.params),
    )


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    provider: GenerationProvider,
    *,
    job: ClaimedJob,
) -> bool:
    """Render one claimed job and settle it. Returns whether it succeeded.

    Progress is throttled to whole-percent changes, so a provider that reports
    often cannot flood the database with updates. Each outcome is counted once,
    by modality and a fixed outcome label; no prompt or output reaches a metric.
    """
    last_reported = -1
    started = time.perf_counter()

    def record(outcome: str) -> None:
        METRICS.increment("generation_jobs_total", modality=job.modality, outcome=outcome)
        METRICS.observe(
            "generation_job_duration_seconds",
            time.perf_counter() - started,
            modality=job.modality,
            outcome=outcome,
        )

    async def on_progress(percent: int) -> None:
        nonlocal last_reported
        if percent <= last_reported:
            return
        last_reported = percent
        owned = await report_progress(session_factory, job=job, percent=percent)
        if not owned:
            raise ProviderCancelledError("the generation is no longer owned")

    try:
        outputs = await provider.generate(_job_for(job), on_progress=on_progress)
        if not outputs:
            raise ProviderError("the provider returned no output", retryable=False)
    except ProviderCancelledError as exc:
        # The row is either cancelled (the cancel route already refunded) or no
        # longer ours. `_settle_failure` clears the lock only in the first case.
        await _settle_failure(
            session_factory,
            job=job,
            error_code=_error_code(exc),
            retryable=False,
        )
        record("cancelled")
        return False
    except ProviderError as exc:
        retrying = await _settle_failure(
            session_factory,
            job=job,
            error_code=_error_code(exc),
            retryable=exc.retryable,
        )
        record("retried" if retrying else "failed")
        return False
    except Exception:
        # An adapter bug must not strand a reservation: treat it as a final
        # failure and refund, and log the type without the payload.
        log.exception(
            "generation.provider_crashed",
            generation_id=str(job.generation_id),
        )
        await _settle_failure(
            session_factory,
            job=job,
            error_code=ERROR_INTERNAL,
            retryable=False,
        )
        record("crashed")
        return False

    settled = await _settle_success(
        session_factory,
        storage,
        job=job,
        outputs=outputs,
    )
    record("succeeded" if settled else "discarded")
    return settled


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    provider: GenerationProvider,
    *,
    worker_id: str,
    stale_after: timedelta,
) -> bool:
    """Claim and run at most one job. Returns whether a job was found."""
    job = await claim_next(
        session_factory,
        worker_id=worker_id,
        stale_after=stale_after,
    )
    if job is None:
        return False
    await run_job(session_factory, storage, provider, job=job)
    return True


async def recover_stale(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stale_after: timedelta,
) -> int:
    """Return stale `running` rows to `queued` so a live worker can claim them.

    This is a safety net for a lock that belongs to a worker that died before its
    job could be retried. It never touches Credits: the reservation stays held
    until a worker succeeds, or the job exhausts its attempts and fails.
    """
    cutoff = utcnow() - stale_after
    async with session_scope(session_factory) as session:
        statement = (
            update(Generation)
            .where(
                Generation.status == "running",
                Generation.locked_at < cutoff,
            )
            .values(status="queued", locked_by=None, locked_at=None)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await session.execute(statement))
    recovered = result.rowcount or 0
    if recovered:
        log.warning("generation.recovered", recovered=recovered)
    return recovered
