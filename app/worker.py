"""Operational entry point for the Generation worker.

```bash
uv run python -m app.worker
```

Configures logging, installs the shutdown signal handlers, and hands the stop
event to the claim loop in `app.workers.generation`. The loop runs
`WORKER_CONCURRENCY` of them against the queue.

Image and Video rendering is injected behind `GenerationProvider`. With
`GENERATION_MODE=mock` the worker builds the deterministic mock adapter
(`app/providers/mock_generation.py`); a live mode still has no provider, so the
loop refuses to claim rather than fail every job.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from types import FrameType

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.providers.mock_generation import build_mock_generation_provider
from app.workers.generation import run_worker

log = structlog.get_logger(__name__)


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

    await run_worker(settings, build_mock_generation_provider(settings), stop=stop)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(asyncio.run(main()))
