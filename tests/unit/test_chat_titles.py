"""Unit tests for chat title derivation and request validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas.chat import CreateChatRequest, UpdateChatRequest
from app.db.models.chat import DEFAULT_TITLE, MAX_TITLE_SOURCE_LENGTH
from app.domain.chat import title_from_message


def test_a_short_message_becomes_the_title_verbatim() -> None:
    assert title_from_message("Give me three campaign angles.") == "Give me three campaign angles."


def test_whitespace_is_collapsed_and_trimmed() -> None:
    assert title_from_message("  Plan\n\n a\tlaunch  ") == "Plan a launch"


def test_an_empty_message_falls_back_to_the_default_title() -> None:
    assert title_from_message("   \n\t ") == DEFAULT_TITLE


def test_a_long_message_is_cut_to_the_documented_length() -> None:
    title = title_from_message("word " * 40)

    assert len(title) <= MAX_TITLE_SOURCE_LENGTH
    assert title == title.strip()


def test_a_long_word_is_hard_cut_rather_than_left_blank() -> None:
    title = title_from_message("x" * 200)

    assert title == "x" * MAX_TITLE_SOURCE_LENGTH


def test_a_multi_line_paste_never_becomes_a_wall_of_text() -> None:
    title = title_from_message("\n".join(["paragraph"] * 50))

    assert "\n" not in title
    assert len(title) <= MAX_TITLE_SOURCE_LENGTH


def test_create_accepts_a_missing_title() -> None:
    body = CreateChatRequest(model_id="deepseek-v4.1-flash")

    assert body.title is None
    assert body.model_id == "deepseek-v4.1-flash"


def test_create_trims_a_supplied_title() -> None:
    assert CreateChatRequest(model_id="m", title="  Launch plan  ").title == "Launch plan"


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_create_rejects_a_blank_title(title: str) -> None:
    with pytest.raises(ValidationError):
        CreateChatRequest(model_id="m", title=title)


def test_create_rejects_an_unknown_field() -> None:
    """A client cannot smuggle a server-owned value such as a price."""
    with pytest.raises(ValidationError):
        CreateChatRequest(model_id="m", credit_cost=0)  # type: ignore[call-arg]


def test_create_requires_a_model_id() -> None:
    with pytest.raises(ValidationError):
        CreateChatRequest()


def test_update_requires_a_non_blank_title() -> None:
    assert UpdateChatRequest(title="  Renamed ").title == "Renamed"
    with pytest.raises(ValidationError):
        UpdateChatRequest(title="  ")
