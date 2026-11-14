"""UUIDv7 generation and timestamp extraction."""

from __future__ import annotations

import time
import uuid

import pytest

from app.core.ids import uuid7, uuid7_timestamp_ms


def test_version_and_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_embeds_current_time_in_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= uuid7_timestamp_ms(value) <= after


def test_timestamp_extraction_reads_first_six_bytes() -> None:
    value = uuid.UUID("0193a1b2-c3d4-7000-8000-000000000000")

    assert uuid7_timestamp_ms(value) == int("0193a1b2c3d4", 16)


def test_ids_are_unique() -> None:
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_ids_sort_by_time(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_700_000_000_000

    def tick() -> int:
        return now * 1_000_000

    monkeypatch.setattr("app.core.ids.time.time_ns", tick)
    first = uuid7()
    monkeypatch.setattr("app.core.ids.time.time_ns", lambda: (now + 1) * 1_000_000)
    second = uuid7()

    assert first < second
    assert uuid7_timestamp_ms(second) - uuid7_timestamp_ms(first) == 1
