"""Test environment defaults.

Environment variables are set before any `app` import so the cached `Settings` can
be constructed without a local `.env`. Tests that need different values use
`tests.support.make_settings`.
"""

from __future__ import annotations

import os

from tests.support import TEST_DATABASE_URL, TEST_SECRET

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET", TEST_SECRET)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:3000")
os.environ.setdefault("LOG_LEVEL", "info")
