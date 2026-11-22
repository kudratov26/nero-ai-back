"""Integration tests for the Generation worker.

These run against PostgreSQL because the claim query depends on `FOR UPDATE SKIP
LOCKED` and on real row locks. Each test seeds a queued Generation with a
reservation, drives the worker directly, then asserts the row, the Assets, and the
Credit ledger agree.

Nothing here starts `python -m app.worker`: the loop is covered separately, and a
test that owns the loop would be a sleep-and-hope test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import METRICS
from app.db.models import Asset, CreditAccount, CreditLedgerEntry, Generation, User
from app.db.models.common import utcnow
from app.db.seed import IMAGE_CREDITS, IMAGE_MODEL_ID, VIDEO_MODEL_ID, upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import credits, generation_worker
from app.providers.base import (
    GenerationJob,
    GenerationOutput,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.fake_generation import TINY_PNG, FakeGenerationProvider
from app.providers.mock_generation import MockGenerationProvider
from app.storage.local import LocalStorage
from app.workers.generation import run_worker
from tests.support import make_settings

STALE_AFTER = timedelta(seconds=300)


@dataclass
class WorkerEnv:
    factory: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    settings: Settings
    user_id: UUID


@pytest.fixture(autouse=True)
def clean_metrics() -> None:
    """The registry is process-wide; keep outcome assertions exact."""
    METRICS.reset()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


@pytest.fixture
async def worker_env(
    migrated_database: str,
    engine: AsyncEngine,
    storage: LocalStorage,
) -> AsyncIterator[WorkerEnv]:
    """An account with the catalog seeded, 50 Credits, and no Generations."""
    settings = make_settings()
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await upsert_models(session, settings)
        user = User(
            email=f"worker-{uuid4().hex}@example.com",
            display_name="Worker",
            password_hash="$argon2id$test-placeholder",
        )
        session.add(user)
        await session.flush()
        now = utcnow()
        session.add(
            CreditAccount(
                user_id=user.id,
                plan_id="free",
                balance=0,
                held=0,
                allowance=50,
                period_start=now,
                period_end=now + timedelta(days=30),
            )
        )
        await session.flush()
        await credits.grant(session, user_id=user.id, amount=50, reason="test allowance")
        user_id = user.id

    # The claim query is global by design, so leftovers from an earlier test (a
    # retryable failure deliberately leaves a `queued` row) would be claimed by
    # this test. Start from an empty queue.
    async with session_scope(factory) as session:
        await session.execute(delete(Generation))
        await session.execute(delete(Asset))

    yield WorkerEnv(factory=factory, storage=storage, settings=settings, user_id=user_id)


async def seed_generation(
    env: WorkerEnv,
    *,
    modality: str = "image",
    cost: int = IMAGE_CREDITS,
    model_id: str = IMAGE_MODEL_ID,
    params: dict[str, Any] | None = None,
    status: str = "queued",
    attempts: int = 0,
    max_attempts: int = 3,
    locked_at: Any = None,
    locked_by: str | None = None,
) -> UUID:
    """Insert a Generation and reserve its cost, mirroring the create route."""
    resolved_params = params or (
        {"aspectRatio": "1:1", "count": 2, "style": "None"}
        if modality == "image"
        else {"aspectRatio": "16:9", "duration": 5}
    )
    async with session_scope(env.factory) as session:
        generation = Generation(
            user_id=env.user_id,
            modality=modality,
            model_id=model_id,
            prompt="A sunlit reading room",
            params=resolved_params,
            status=status,
            progress=0,
            cost=cost,
            provider="Demo",
            attempts=attempts,
            max_attempts=max_attempts,
            locked_at=locked_at,
            locked_by=locked_by,
        )
        session.add(generation)
        await session.flush()
        if cost > 0 and status == "queued":
            await credits.reserve(
                session,
                user_id=env.user_id,
                amount=cost,
                reason="Image generation",
                ref_type="generation",
                ref_id=generation.id,
            )
        return generation.id


async def generation_row(env: WorkerEnv, generation_id: UUID) -> Generation:
    async with session_scope(env.factory) as session:
        row = await session.get(Generation, generation_id)
        assert row is not None
        return row


async def assets_for(env: WorkerEnv, generation_id: UUID) -> list[Asset]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(Asset).where(Asset.generation_id == generation_id).order_by(Asset.id)
        )
        return list(rows)


async def ledger_kinds(env: WorkerEnv, generation_id: UUID) -> list[str]:
    async with session_scope(env.factory) as session:
        rows = await session.scalars(
            select(CreditLedgerEntry.kind)
            .where(
                CreditLedgerEntry.user_id == env.user_id,
                CreditLedgerEntry.ref_type == "generation",
                CreditLedgerEntry.ref_id == generation_id,
            )
            .order_by(CreditLedgerEntry.created_at, CreditLedgerEntry.id)
        )
        return list(rows)


async def held(env: WorkerEnv) -> int:
    async with session_scope(env.factory) as session:
        from app.db.models import CreditAccount

        account = await session.scalar(
            select(CreditAccount).where(CreditAccount.user_id == env.user_id)
        )
        assert account is not None
        return account.held


# --- Success ----------------------------------------------------------------


async def test_a_queued_image_generation_completes_with_assets(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)
    provider = FakeGenerationProvider()

    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert worked is True
    row = await generation_row(worker_env, generation_id)
    assert row.status == "succeeded"
    assert row.progress == 100
    assert row.error_code is None
    assert row.locked_by is None
    assert row.locked_at is None
    assert row.finished_at is not None
    assert row.started_at is not None
    assert 'generation_jobs_total{modality="image",outcome="succeeded"} 1' in METRICS.render()

    assets = await assets_for(worker_env, generation_id)
    assert len(assets) == 2
    for asset in assets:
        assert asset.user_id == worker_env.user_id
        assert asset.kind == "image"
        assert asset.mime_type == "image/png"
        assert asset.size_bytes == len(TINY_PNG)
        assert worker_env.storage.path_for(asset.storage_key).read_bytes() == TINY_PNG

    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "commit"]
    assert await held(worker_env) == 0


async def test_a_video_generation_persists_one_mp4_asset(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env, modality="video", model_id=VIDEO_MODEL_ID)

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        FakeGenerationProvider(),
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assets = await assets_for(worker_env, generation_id)
    assert len(assets) == 1
    assert assets[0].kind == "video"
    assert assets[0].mime_type == "video/mp4"
    assert assets[0].duration_ms == 5_000
    assert (assets[0].width, assets[0].height) == (640, 360)


async def test_progress_is_reported_and_never_moves_backwards(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)
    provider = FakeGenerationProvider(progress=(10, 40, 40, 20, 90))

    seen: list[int] = []
    original = generation_worker.report_progress

    async def spy(factory: Any, *, job: Any, percent: int) -> bool:
        owned = await original(factory, job=job, percent=percent)
        if owned:
            seen.append(percent)
        return owned

    generation_worker.report_progress = spy  # type: ignore[assignment]
    try:
        await generation_worker.process_one(
            worker_env.factory,
            worker_env.storage,
            provider,
            worker_id="worker-test",
            stale_after=STALE_AFTER,
        )
    finally:
        generation_worker.report_progress = original  # type: ignore[assignment]

    assert seen == [10, 40, 90]
    assert (await generation_row(worker_env, generation_id)).progress == 100


# --- Claiming ---------------------------------------------------------------


async def test_the_queue_is_empty_when_nothing_is_queued(worker_env: WorkerEnv) -> None:
    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        FakeGenerationProvider(),
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert worked is False


async def test_two_workers_never_claim_the_same_generation(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)

    first = await generation_worker.claim_next(
        worker_env.factory, worker_id="worker-a", stale_after=STALE_AFTER
    )
    second = await generation_worker.claim_next(
        worker_env.factory, worker_id="worker-b", stale_after=STALE_AFTER
    )

    assert first is not None
    assert first.generation_id == generation_id
    assert first.worker_id == "worker-a"
    assert second is None


async def test_concurrent_claims_do_not_double_claim(worker_env: WorkerEnv) -> None:
    await seed_generation(worker_env)

    claims = await asyncio.gather(
        *(
            generation_worker.claim_next(
                worker_env.factory, worker_id=f"worker-{index}", stale_after=STALE_AFTER
            )
            for index in range(4)
        )
    )

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1


async def test_a_claim_increments_attempts_and_marks_running(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env)

    job = await generation_worker.claim_next(
        worker_env.factory, worker_id="worker-test", stale_after=STALE_AFTER
    )

    assert job is not None
    assert job.attempts == 1
    row = await generation_row(worker_env, generation_id)
    assert row.status == "running"
    assert row.attempts == 1
    assert row.locked_by == "worker-test"
    assert row.started_at is not None


# --- Stale locks ------------------------------------------------------------


async def test_an_abandoned_lock_is_recovered(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(
        worker_env,
        status="running",
        attempts=1,
        locked_by="worker-dead",
        locked_at=utcnow() - timedelta(seconds=600),
    )

    recovered = await generation_worker.recover_stale(worker_env.factory, stale_after=STALE_AFTER)

    assert recovered == 1
    row = await generation_row(worker_env, generation_id)
    assert row.status == "queued"
    assert row.locked_by is None
    assert row.locked_at is None


async def test_a_live_lock_is_not_recovered(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(
        worker_env,
        status="running",
        attempts=1,
        locked_by="worker-live",
        locked_at=utcnow(),
    )

    recovered = await generation_worker.recover_stale(worker_env.factory, stale_after=STALE_AFTER)

    assert recovered == 0
    assert (await generation_row(worker_env, generation_id)).status == "running"


async def test_a_stale_running_job_is_claimable_without_a_sweep(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(
        worker_env,
        status="running",
        attempts=1,
        locked_by="worker-dead",
        locked_at=utcnow() - timedelta(seconds=600),
    )

    job = await generation_worker.claim_next(
        worker_env.factory, worker_id="worker-new", stale_after=STALE_AFTER
    )

    assert job is not None
    assert job.generation_id == generation_id
    assert job.attempts == 2


# --- Failure and retry ------------------------------------------------------


async def test_a_retryable_failure_requeues_without_refunding(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env, max_attempts=3)
    provider = FakeGenerationProvider(fail_with=ProviderUnavailableError("upstream 503"))

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "queued"
    assert row.attempts == 1
    assert row.error_code == "provider_error"
    assert row.locked_by is None
    assert await ledger_kinds(worker_env, generation_id) == ["reserve"]
    assert await held(worker_env) == IMAGE_CREDITS
    assert 'generation_jobs_total{modality="image",outcome="retried"} 1' in METRICS.render()


async def test_the_last_attempt_fails_and_refunds_once(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env, max_attempts=1)
    provider = FakeGenerationProvider(fail_with=ProviderTimeoutError("timed out"))

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "provider_timeout"
    assert row.finished_at is not None
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]
    assert await held(worker_env) == 0
    assert 'generation_jobs_total{modality="image",outcome="failed"} 1' in METRICS.render()


async def test_a_non_retryable_failure_fails_immediately(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env, max_attempts=3)
    provider = FakeGenerationProvider(fail_with=ProviderError("bad request", retryable=False))

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]


async def test_retries_stop_at_max_attempts(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env, max_attempts=2)
    provider = FakeGenerationProvider(fail_with=ProviderUnavailableError("upstream 503"))

    for _ in range(3):
        await generation_worker.process_one(
            worker_env.factory,
            worker_env.storage,
            provider,
            worker_id="worker-test",
            stale_after=STALE_AFTER,
        )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert row.attempts == 2
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]


async def test_an_unexpected_provider_crash_fails_and_refunds(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)
    provider = FakeGenerationProvider()
    provider.generate = _raising(ValueError("adapter bug"))  # type: ignore[method-assign]

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert row.error_code == "internal_error"
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]


def _raising(exc: Exception) -> Any:
    async def generate(job: GenerationJob, *, on_progress: Any) -> Any:
        raise exc

    return generate


async def test_a_provider_with_no_output_is_a_failure(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env, max_attempts=1)
    provider = FakeGenerationProvider(outputs=[])

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]


async def test_an_exhausted_queued_job_fails_without_being_rendered(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env, attempts=3, max_attempts=3)
    provider = FakeGenerationProvider()

    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    # The claim itself fails the job: there is nothing left to render.
    assert worked is False
    assert provider.requests == []
    row = await generation_row(worker_env, generation_id)
    assert row.status == "failed"
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]


# --- Cancel racing the worker -----------------------------------------------


async def test_a_job_cancelled_mid_run_is_discarded_without_a_second_refund(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)
    provider = FakeGenerationProvider(progress=(10, 50))

    async def cancel_on_progress(percent: int) -> None:
        if percent == 10:
            async with session_scope(worker_env.factory) as session:
                row = await session.get(Generation, generation_id)
                assert row is not None
                row.status = "cancelled"
                row.finished_at = utcnow()
                await credits.refund(
                    session,
                    user_id=worker_env.user_id,
                    amount=row.cost,
                    reason="Cancelled generation",
                    ref_type="generation",
                    ref_id=row.id,
                )

    provider.progress = (10,)
    provider.generate = _progress_then_cancel(cancel_on_progress)  # type: ignore[method-assign]

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    row = await generation_row(worker_env, generation_id)
    assert row.status == "cancelled"
    assert row.locked_by is None
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "refund"]
    assert await held(worker_env) == 0
    assert await assets_for(worker_env, generation_id) == []


def _progress_then_cancel(side_effect: Any) -> Any:
    async def generate(job: GenerationJob, *, on_progress: Any) -> Any:
        await on_progress(10)
        await side_effect(10)
        # The next report finds the row no longer owned and raises.
        await on_progress(50)
        return []

    return generate


async def test_a_cancelled_job_that_lost_its_lock_does_not_move_credits(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env)
    job = await generation_worker.claim_next(
        worker_env.factory, worker_id="worker-old", stale_after=STALE_AFTER
    )
    assert job is not None
    # Another worker took the row after the lock went stale.
    async with session_scope(worker_env.factory) as session:
        row = await session.get(Generation, generation_id)
        assert row is not None
        row.locked_by = "worker-new"

    settled = await generation_worker._settle_success(
        worker_env.factory,
        worker_env.storage,
        job=job,
        outputs=[GenerationOutput(data=TINY_PNG, mime_type="image/png", width=1, height=1)],
    )

    assert settled is False
    assert await ledger_kinds(worker_env, generation_id) == ["reserve"]
    assert await assets_for(worker_env, generation_id) == []


# --- Success does not double-settle -----------------------------------------


async def test_credits_are_committed_exactly_once(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env)
    provider = FakeGenerationProvider()

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )
    # Nothing is claimable now, so a second pass must not touch the row.
    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert worked is False
    assert await ledger_kinds(worker_env, generation_id) == ["reserve", "commit"]
    async with session_scope(worker_env.factory) as session:
        count = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.generation_id == generation_id)
        )
    assert count == 2


async def test_a_zero_cost_generation_succeeds_without_a_commit(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(worker_env, cost=0)

    await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        FakeGenerationProvider(),
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert (await generation_row(worker_env, generation_id)).status == "succeeded"
    assert await ledger_kinds(worker_env, generation_id) == []


# --- The mock provider end to end --------------------------------------------


async def test_a_mock_image_generation_persists_png_assets(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(
        worker_env,
        params={"aspectRatio": "16:9", "count": 2, "style": "None"},
    )
    provider = MockGenerationProvider()

    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert worked is True
    row = await generation_row(worker_env, generation_id)
    assert row.status == "succeeded"
    assert row.provider == "Demo"

    assets = await assets_for(worker_env, generation_id)
    assert len(assets) == 2
    for asset in assets:
        assert asset.kind == "image"
        assert asset.mime_type == "image/png"
        assert asset.width == 640
        assert asset.height == 360
        assert asset.size_bytes > 8_000
        assert worker_env.storage.path_for(asset.storage_key).read_bytes()[:8] == TINY_PNG[:8]


async def test_a_mock_video_generation_persists_an_mp4_asset(
    worker_env: WorkerEnv,
) -> None:
    generation_id = await seed_generation(
        worker_env,
        modality="video",
        model_id=VIDEO_MODEL_ID,
        params={"aspectRatio": "16:9", "duration": 5},
    )
    provider = MockGenerationProvider()

    worked = await generation_worker.process_one(
        worker_env.factory,
        worker_env.storage,
        provider,
        worker_id="worker-test",
        stale_after=STALE_AFTER,
    )

    assert worked is True
    row = await generation_row(worker_env, generation_id)
    assert row.status == "succeeded"
    assert row.provider == "Demo"

    assets = await assets_for(worker_env, generation_id)
    assert len(assets) == 1
    assert assets[0].kind == "video"
    assert assets[0].mime_type == "video/mp4"
    assert assets[0].duration_ms == 5_000
    assert (assets[0].width, assets[0].height) == (640, 360)
    assert assets[0].size_bytes > 8_000


# --- The worker process loop -----------------------------------------------


async def test_run_worker_drains_the_queue_then_stops(worker_env: WorkerEnv) -> None:
    generation_id = await seed_generation(worker_env)
    settings = make_settings(worker_concurrency=1, worker_poll_interval_seconds=0.01)
    provider = FakeGenerationProvider()
    stop = asyncio.Event()

    async def on_idle() -> None:
        stop.set()

    await run_worker(
        settings,
        provider,
        storage=worker_env.storage,
        stop=stop,
        on_idle=on_idle,
    )

    assert provider.closed is True
    assert (await generation_row(worker_env, generation_id)).status == "succeeded"


async def test_run_worker_processes_jobs_from_several_loops(worker_env: WorkerEnv) -> None:
    first = await seed_generation(worker_env)
    second = await seed_generation(worker_env)
    settings = make_settings(worker_concurrency=2, worker_poll_interval_seconds=0.01)
    provider = FakeGenerationProvider()
    stop = asyncio.Event()

    async def on_idle() -> None:
        stop.set()

    await run_worker(
        settings,
        provider,
        storage=worker_env.storage,
        stop=stop,
        on_idle=on_idle,
    )

    assert len(provider.requests) == 2
    assert (await generation_row(worker_env, first)).status == "succeeded"
    assert (await generation_row(worker_env, second)).status == "succeeded"


async def test_run_worker_without_a_provider_exits_immediately() -> None:
    settings = make_settings(worker_concurrency=1)

    # No provider is wired until B5.3; the worker must refuse rather than fail
    # every job with an unconfigured adapter.
    await run_worker(settings, None, stop=asyncio.Event())


async def test_run_worker_exits_when_stopped_before_claiming(
    worker_env: WorkerEnv,
) -> None:
    await seed_generation(worker_env)
    settings = make_settings(worker_concurrency=1)
    provider = FakeGenerationProvider()
    stop = asyncio.Event()
    stop.set()

    await run_worker(settings, provider, storage=worker_env.storage, stop=stop)

    assert provider.requests == []
    assert (await generation_row(worker_env, await _only_generation(worker_env))).status == "queued"


async def _only_generation(worker_env: WorkerEnv) -> UUID:
    async with session_scope(worker_env.factory) as session:
        row = await session.scalar(
            select(Generation).where(Generation.user_id == worker_env.user_id)
        )
        assert row is not None
        return row.id
