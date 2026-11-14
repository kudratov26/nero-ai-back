"""Database fixtures: one clean migrated test database per session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import create_session_factory
from tests.support import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[2]


def _connection_kwargs(url: str, database: str) -> dict[str, Any]:
    parsed = make_url(url)
    return {
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.host or "localhost",
        "port": parsed.port or 5432,
        "database": database,
        "timeout": 10,
    }


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """Recreate the test database and migrate it from empty to head."""
    name = make_url(TEST_DATABASE_URL).database
    assert name, "TEST_DATABASE_URL must name a database"

    async def recreate() -> None:
        connection = await asyncpg.connect(**_connection_kwargs(TEST_DATABASE_URL, "postgres"))
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await connection.execute(f'CREATE DATABASE "{name}"')
        finally:
            await connection.close()

    asyncio.run(recreate())
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    yield TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """Per-test engine: NullPool avoids sharing connections across event loops."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session whose transaction is rolled back after the test.

    Tests can flush freely (including expected constraint violations) without
    leaving rows behind. Nothing is committed.
    """
    async with create_session_factory(engine)() as session:
        try:
            yield session
        finally:
            await session.rollback()
