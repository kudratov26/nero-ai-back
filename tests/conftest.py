"""Test environment defaults.

Set before any `app` import so the cached `Settings` can be constructed without a
local `.env`. Tests that need different values build `Settings` explicitly with
`_env_file=None`.
"""

from __future__ import annotations

import os

TEST_DATABASE_URL = "postgresql+asyncpg://nero:nero@localhost:5432/nero_test"
TEST_SECRET = "test-secret-not-for-production"

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET", TEST_SECRET)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("LOG_LEVEL", "info")
