"""Seed entry point for deploys.

This is a thin alias for `python -m app.cli seed` so the documented
`scripts/seed.py` exists. It refreshes the plan, model, and Explore catalogs, and
is idempotent: run it after `alembic upgrade head` on every deploy.
"""

from __future__ import annotations

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["seed"]))
