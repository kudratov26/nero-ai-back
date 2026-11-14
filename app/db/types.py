"""Custom column types."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Dialect
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.types import TypeDecorator


class InetAddress(TypeDecorator[str]):
    """PostgreSQL `inet` stored as a plain string in Python.

    asyncpg returns `ipaddress` objects for `inet`; the API and logs want the
    textual form, so normalize at the type boundary.
    """

    impl = INET
    cache_ok = True

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(value)
