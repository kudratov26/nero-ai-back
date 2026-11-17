"""Unit tests for Generation validation and server-owned cost calculation.

These are pure functions, so the matrix runs without a database. The integration
suite proves the same rules through the HTTP contract and the Credit ledger.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.problems import ApiError, ErrorCode
from app.db.models import Model
from app.domain import generations


def make_model(**overrides: Any) -> Model:
    values: dict[str, Any] = {
        "id": "image-demo",
        "name": "Image Demo",
        "provider": "Demo",
        "provider_model_id": "image-demo",
        "modality": "image",
        "description": "A deterministic mock image model.",
        "badge": None,
        "credit_cost": 5,
        "credit_cost_10s": None,
        "capabilities": {},
        "context_window": None,
        "available": True,
        "sort_order": 10,
    }
    values.update(overrides)
    return Model(**values)


def assert_invalid(call: Any) -> None:
    with pytest.raises(ApiError) as caught:
        call()
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert caught.value.status == 422


# --- Param defaults and capabilities ----------------------------------------


def test_image_defaults_are_canonicalized() -> None:
    assert generations.validate_params("image", {}, {}) == {
        "aspectRatio": "1:1",
        "count": 1,
        "style": "None",
    }


def test_video_defaults_are_canonicalized() -> None:
    assert generations.validate_params("video", {}, {}) == {
        "aspectRatio": "16:9",
        "duration": 5,
    }


def test_model_capabilities_override_the_documented_fallbacks() -> None:
    capabilities = {
        "aspectRatios": ["9:16"],
        "maxCount": 2,
        "styles": ["Anime"],
        "durations": [10],
    }

    assert generations.validate_params("image", {}, capabilities) == {
        "aspectRatio": "9:16",
        "count": 1,
        "style": "Anime",
    }
    assert generations.validate_params("video", {}, capabilities) == {
        "aspectRatio": "9:16",
        "duration": 10,
    }


def test_unknown_keys_are_rejected_consistently() -> None:
    assert_invalid(lambda: generations.validate_params("image", {"cost": 1}, {}))
    assert_invalid(lambda: generations.validate_params("video", {"durationSeconds": 5}, {}))
    assert_invalid(lambda: generations.validate_params("image", {"aspectRatio": "1:1", "x": 1}, {}))


# --- Image validation -------------------------------------------------------


@pytest.mark.parametrize("aspect_ratio", generations.IMAGE_ASPECT_RATIOS)
def test_every_advertised_image_aspect_ratio_is_accepted(aspect_ratio: str) -> None:
    params = generations.validate_params("image", {"aspectRatio": aspect_ratio}, {})

    assert params["aspectRatio"] == aspect_ratio


@pytest.mark.parametrize("count", [1, 2, 4])
def test_every_image_count_within_the_advertised_maximum_is_accepted(count: int) -> None:
    params = generations.validate_params("image", {"count": count}, {})

    assert params["count"] == count


@pytest.mark.parametrize(
    "params",
    [
        {"aspectRatio": "3:2"},
        {"aspectRatio": 4},
        {"count": 0},
        {"count": 5},
        {"count": True},
        {"count": "2"},
        {"style": "Vaporwave"},
        {"style": 3},
    ],
)
def test_invalid_image_params_are_rejected(params: dict[str, Any]) -> None:
    assert_invalid(lambda: generations.validate_params("image", params, {}))


def test_a_models_max_count_is_the_ceiling() -> None:
    capabilities = {"maxCount": 1}

    assert_invalid(lambda: generations.validate_params("image", {"count": 2}, capabilities))
    assert generations.validate_params("image", {"count": 1}, capabilities)["count"] == 1


# --- Video validation -------------------------------------------------------


@pytest.mark.parametrize("duration", [5, 10])
def test_every_advertised_video_duration_is_accepted(duration: int) -> None:
    params = generations.validate_params("video", {"duration": duration}, {})

    assert params["duration"] == duration


@pytest.mark.parametrize(
    "params",
    [
        {"duration": 7},
        {"duration": 0},
        {"duration": True},
        {"duration": "5"},
        {"aspectRatio": "4:5"},
        {"aspectRatio": None},
    ],
)
def test_invalid_video_params_are_rejected(params: dict[str, Any]) -> None:
    assert_invalid(lambda: generations.validate_params("video", params, {}))


def test_chat_is_not_a_generation_modality() -> None:
    assert_invalid(lambda: generations.validate_params("chat", {}, {}))


# --- Cost -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, 5), (2, 10), (4, 20)],
)
def test_image_cost_multiplies_the_catalog_price_by_count(count: int, expected: int) -> None:
    model = make_model(credit_cost=5)
    params = generations.validate_params("image", {"count": count}, {})

    assert generations.cost_for(model, params=params) == expected


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(5, 15), (10, 30)],
)
def test_video_cost_uses_the_duration_price(duration: int, expected: int) -> None:
    model = make_model(
        id="video-demo",
        modality="video",
        credit_cost=15,
        credit_cost_10s=30,
    )
    params = generations.validate_params("video", {"duration": duration}, {})

    assert generations.cost_for(model, params=params) == expected


def test_a_video_without_a_long_price_falls_back_to_the_base_price() -> None:
    model = make_model(modality="video", credit_cost=15, credit_cost_10s=None)
    params = generations.validate_params("video", {"duration": 10}, {})

    assert generations.cost_for(model, params=params) == 15


def test_a_chat_model_has_no_generation_cost() -> None:
    with pytest.raises(ValueError, match="cannot make a Generation"):
        generations.cost_for(make_model(modality="chat"), params={})


def test_unvalidated_params_are_a_programming_error() -> None:
    with pytest.raises(ValueError, match="not validated"):
        generations.cost_for(make_model(), params={})


# --- Prompt and reasons -----------------------------------------------------


def test_prompt_is_trimmed_before_storage() -> None:
    assert generations.normalize_prompt("  A sunlit reading room  ") == ("A sunlit reading room")


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 4001])
def test_invalid_prompts_are_rejected(prompt: str) -> None:
    assert_invalid(lambda: generations.normalize_prompt(prompt))


def test_credit_reasons_name_the_use_case_and_nothing_else() -> None:
    assert generations.credit_reason("image") == "Image generation"
    assert generations.credit_reason("video") == "Video generation"
