"""Explore helpers that do not need a database."""

from __future__ import annotations

import pytest

from app.domain.explore import initials_for


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Mara Ellison", "ME"),
        ("Mara A. Ellison", "ME"),
        ("Nero", "NE"),
        ("  ", "?"),
    ],
)
def test_initials_for(display_name: str, expected: str) -> None:
    assert initials_for(display_name) == expected
