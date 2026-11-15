"""Operational entry points.

```bash
uv run python -m app.cli seed
```

`seed` is idempotent and safe to run on every deploy. It runs after
`alembic upgrade head`, because the plan and model catalogs live in the database
so prices have one authoritative home.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.seed import upsert_models, upsert_plans
from app.db.session import create_engine, create_session_factory, session_scope

log = structlog.get_logger(__name__)

USAGE = "usage: python -m app.cli seed"


async def seed_catalog() -> None:
    """Refresh the plan and model catalogs in one transaction."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        factory = create_session_factory(engine)
        async with session_scope(factory) as session:
            await upsert_plans(session)
            await upsert_models(session, settings)
    finally:
        await engine.dispose()
    log.info(
        "catalog.seeded",
        generation_mode=settings.generation_mode,
        chat_provider_configured=settings.chat_provider_configured,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    configure_logging(get_settings())
    if args == ["seed"]:
        asyncio.run(seed_catalog())
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
