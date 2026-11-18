"""The committed OpenAPI snapshot is the frontend contract.

B6.1 fails when the rendered schema and the committed `openapi.json` disagree,
so a route or schema change cannot ship without the snapshot being refreshed in
the same pull request.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.json"


def test_the_committed_openapi_snapshot_matches_the_app() -> None:
    assert json.loads(SNAPSHOT.read_text()) == app.openapi()
