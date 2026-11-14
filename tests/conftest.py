"""Test environment defaults.

Environment variables are set before any `app` import so the cached `Settings` can
be constructed without a local `.env`. The suite always points at the dedicated
test database, never at a development database.
"""

from __future__ import annotations

import os

from app.core.config import get_settings
from tests.support import TEST_DATABASE_URL, TEST_SECRET

os.environ["APP_ENV"] = "test"
os.environ["APP_SECRET"] = TEST_SECRET
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("LOG_LEVEL", "info")

# `app.main` is imported while `tests.support` loads, which happens before the
# assignments above. Drop the settings cached from `.env` so Alembic and the
# app in tests use the test database.
get_settings.cache_clear()
