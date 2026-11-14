"""Database, session, and migration integration tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.health import expected_revision
from app.db.session import create_session_factory, session_scope


async def test_engine_connects(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1


async def test_database_is_migrated_to_head(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        revision = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()

    assert revision == expected_revision()


async def test_session_scope_commits(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE session_commit_probe (id integer)"))
    try:
        async with session_scope(create_session_factory(engine)) as session:
            await session.execute(text("INSERT INTO session_commit_probe (id) VALUES (1)"))

        async with engine.connect() as connection:
            count = (
                await connection.execute(text("SELECT count(*) FROM session_commit_probe"))
            ).scalar_one()
        assert count == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS session_commit_probe"))


async def test_session_scope_rolls_back_on_error(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE session_rollback_probe (id integer)"))
    try:
        with pytest.raises(RuntimeError):
            async with session_scope(create_session_factory(engine)) as session:
                await session.execute(text("INSERT INTO session_rollback_probe (id) VALUES (1)"))
                raise RuntimeError("use case failed")

        async with engine.connect() as connection:
            count = (
                await connection.execute(text("SELECT count(*) FROM session_rollback_probe"))
            ).scalar_one()
        assert count == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS session_rollback_probe"))
