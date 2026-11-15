"""Unit tests for the idempotency helper's pure parts."""

from __future__ import annotations

from uuid import uuid4

from app.db.models import IdempotencyKey
from app.domain import idempotency


def test_hashing_is_stable_across_key_order() -> None:
    assert idempotency.hash_request({"a": 1, "b": [1, 2]}) == idempotency.hash_request(
        {"b": [1, 2], "a": 1}
    )


def test_hashing_changes_with_the_body() -> None:
    assert idempotency.hash_request({"prompt": "x"}) != idempotency.hash_request({"prompt": "y"})


def test_hashing_is_a_lowercase_sha256_hex_digest() -> None:
    digest = idempotency.hash_request({"modality": "image"})

    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_hashing_handles_nested_and_non_json_values() -> None:
    payload = {"id": uuid4(), "params": {"count": 2, "tags": ["a", "b"]}}

    assert len(idempotency.hash_request(payload)) == 64


def test_operation_constants_are_distinct_and_non_blank() -> None:
    operations = {idempotency.GENERATION_CREATE, idempotency.CHAT_MESSAGE}

    assert len(operations) == 2
    assert all(operation.strip() for operation in operations)


def test_the_race_constraint_matches_the_model_primary_key() -> None:
    # `execute` recognizes a lost race by the constraint name in the error.
    assert IdempotencyKey.__table__.primary_key.name == idempotency._RACE_CONSTRAINT
