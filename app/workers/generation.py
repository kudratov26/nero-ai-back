"""The Generation worker: a claim loop behind `python -m app.worker`.

A job is claimed in one short transaction with `FOR UPDATE SKIP LOCKED`, so two
workers never take the same row and neither blocks the other. The claim stamps
`locked_by`/`locked_at`, increments `attempts`, and moves the row to `running`.
Rendering then happens outside any transaction, because a slow provider must not
hold a connection, and progress is reported in its own short transaction each time.

Success is atomic: the output Assets, the Credit commit, and the `succeeded` status
commit together. A terminal failure marks the row `failed` and refunds in one
transaction, so a reservation is either consumed or returned and never both. A job
cancelled while it was running is discarded without touching Credits, because the
cancel route already refunded it.

Retryable provider failures leave the row `queued` with its reservation intact, so
the next claim tries again. When `attempts` reaches `max_attempts`, the job fails
instead. A lock older than `stale_after` belongs to a worker that died, so the next
claim takes it over rather than leaving the job stuck forever.

Logs carry ids, attempts, and counts. They never carry the prompt or the output.
"""

from __future__ import annotations

import asyncio
import signal
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from types import FrameType
from uuid import uuid4

import structlog

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.domain import generation_worker
from app.providers.base import GenerationProvider
from app.storage.local import LocalStorage

log = structlog.get_logger(__name__)

WORKER_NOT_CONFIGURED = (
    "The generation worker has no provider. Image and Video renderers arrive in B5.3."
)


def worker_id(prefix: str = "worker") -> str:
    """A stable-per-process identity, so a lock names the worker that took it."""
    return f"{prefix}-{socket.gethostname()}-{uuid4().hex[:8]}"


async def run_worker(
    settings: Settings,
    provider: GenerationProvider | None,
    *,
    storage: LocalStorage | None = None,
    stop: asyncio.Event | None = None,
    on_idle: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run the claim loop until `stop` is set.

    `provider` is injected so tests drive the loop without a real adapter. A
    worker with no provider logs once and exits instead of failing every job.
    """
    if provider is None:
        log.error("worker.not_configured", detail=WORKER_NOT_CONFIGURED)
        return

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    resolved_storage = storage or LocalStorage(settings.storage_dir)
    stale_after = timedelta(seconds=settings.worker_stale_after_seconds)
    poll_interval = settings.worker_poll_interval_seconds
    stop = stop or asyncio.Event()

    async def loop(index: int) -> None:
        name = worker_id(f"worker-{index}")
        log.info("worker.started", worker_id=name, stale_after_seconds=stale_after.total_seconds())
        while not stop.is_set():
            worked = await generation_worker.process_one(
                factory,
                resolved_storage,
                provider,
                worker_id=name,
                stale_after=stale_after,
            )
            if worked:
                continue
            await generation_worker.recover_stale(factory, stale_after=stale_after)
            if on_idle is not None:
                await on_idle()
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)

    total = max(1, settings.worker_concurrency)
    try:
        await asyncio.gather(*(loop(index) for index in range(total)))
    finally:
        await provider.aclose()
        await engine.dispose()
        log.info("worker.stopped")


async def main() -> int:
    """Entry point: configure logging, install signal handlers, and run."""
    settings = get_settings()
    configure_logging(settings)
    stop = asyncio.Event()

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        log.info("worker.shutdown_requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop, sig, None)

    await run_worker(settings, None, stop=stop)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(asyncio.run(main()))
