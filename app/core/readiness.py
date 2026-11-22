"""Readiness checks shared by the probe and the operational checks.

Readiness means the database answers and the schema is at the Alembic head. It
never calls a provider and never surfaces a driver message or a connection
string.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def expected_revision() -> str | None:
    """Return the Alembic head, or None when this is not a source checkout."""
    config_path = _REPO_ROOT / "alembic.ini"
    if not config_path.exists():
        return None
    script = ScriptDirectory.from_config(Config(str(config_path)))
    return script.get_current_head()


async def database_is_ready(engine: AsyncEngine) -> bool:
    """Cheap readiness check: connectivity plus migrations at head."""
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()
    except (SQLAlchemyError, OSError):
        return False
    expected = expected_revision()
    if expected is None:
        return True
    return current == expected
