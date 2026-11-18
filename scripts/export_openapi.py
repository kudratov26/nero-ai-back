"""Export and check the committed OpenAPI snapshot.

The schema in `openapi.json` is the contract the frontend generates TypeScript
types from. It is committed, so a route or schema change must update it in the
same pull request; CI fails when the rendered schema no longer matches.

    uv run python scripts/export_openapi.py          # refresh `openapi.json`
    uv run python scripts/export_openapi.py --check # exit 1 when out of date

The check runs without a database, provider, or network access: the app factory
only builds the routes and schemas.

"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: The committed snapshot lives at the repository root, next to the package it
#: describes.
SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"

# The schema does not depend on deployment settings, but importing `app.main`
# builds the app at import time and requires a usable configuration.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET", "openapi-snapshot")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost:5432/nero")

from app.main import create_app  # noqa: E402  # import after the defaults above


def render() -> str:
    """Serialize the app schema as deterministic JSON text."""
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        rendered = render()
        committed = SNAPSHOT.read_text() if SNAPSHOT.exists() else ""
        if committed != rendered:
            print("openapi.json is out of date.", file=sys.stderr)
            print(
                "Run `uv run python scripts/export_openapi.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("openapi.json is up to date")
        return 0
    SNAPSHOT.write_text(render())
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
