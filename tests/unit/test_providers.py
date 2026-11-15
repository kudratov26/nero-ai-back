"""Unit tests for the provider port, its DTOs, the error taxonomy, and the fake.

Nothing here touches the network: the adapter's wire behavior lives in
`tests/providers/test_openai_compatible.py`.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.api.problems import ErrorCode
from app.providers import base
from app.providers.base import (
    ChatDelta,
    ChatParams,
    ChatUsage,
    ProviderError,
    ProviderMessage,
    ProviderModel,
    ProviderRateLimitError,
    ProviderStreamError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.fake import DEFAULT_REPLY, FakeChatProvider, FakeRequest

MESSAGES = [ProviderMessage(role="user", content="hi")]
MODEL = ProviderModel(slug="deepseek-v4.1-flash", provider_model_id="deepseek-v4.1-flash")


async def collect(provider: FakeChatProvider, params: ChatParams | None = None) -> list[ChatDelta]:
    return [
        delta
        async for delta in provider.stream_chat(
            messages=MESSAGES, model=MODEL, params=params or ChatParams()
        )
    ]


# --- DTOs --------------------------------------------------------------------


def test_provider_message_carries_a_role_and_content() -> None:
    message = ProviderMessage(role="assistant", content="hi")

    assert message.role == "assistant"
    assert message.content == "hi"


def test_provider_model_keeps_slug_and_provider_id_apart() -> None:
    model = ProviderModel(slug="public-slug", provider_model_id="vendor-id")

    assert model.slug == "public-slug"
    assert model.provider_model_id == "vendor-id"


def test_chat_params_default_to_unset() -> None:
    params = ChatParams()

    assert params.temperature is None
    assert params.max_tokens is None


def test_chat_usage_sides_default_to_unknown() -> None:
    usage = ChatUsage()

    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None


def test_chat_delta_defaults_to_empty_text() -> None:
    delta = ChatDelta()

    assert delta.text == ""
    assert delta.finish_reason is None
    assert delta.usage is None


def test_chat_delta_can_carry_a_finish_and_usage() -> None:
    delta = ChatDelta(finish_reason="stop", usage=ChatUsage(1, 2))

    assert delta.text == ""
    assert delta.finish_reason == "stop"
    assert delta.usage == ChatUsage(1, 2)


@pytest.mark.parametrize(
    "dto",
    [
        ProviderMessage("user", "hi"),
        ProviderModel("slug", "id"),
        ChatParams(),
        ChatUsage(),
        ChatDelta(),
    ],
)
def test_dtos_are_frozen(dto: Any) -> None:
    assert dataclasses.is_dataclass(dto)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dto.probe = 1


# --- Error taxonomy ----------------------------------------------------------

PROBLEM_CODES = {code.value for code in ErrorCode}


def test_every_provider_error_code_is_a_documented_problem_code() -> None:
    for error in (
        ProviderError,
        ProviderTimeoutError,
        ProviderRateLimitError,
        ProviderUnavailableError,
        ProviderStreamError,
    ):
        assert error.code.value in PROBLEM_CODES


def test_a_base_error_defaults_to_provider_error_and_is_not_retryable() -> None:
    error = ProviderError("nope")

    assert error.code == ErrorCode.PROVIDER_ERROR
    assert error.retryable is False
    assert error.detail == "nope"
    assert str(error) == "nope"


def test_rate_limit_maps_to_rate_limited_and_is_never_retryable() -> None:
    error = ProviderRateLimitError("slow down")

    assert error.code == ErrorCode.RATE_LIMITED
    assert error.retryable is False


def test_timeout_maps_to_provider_timeout() -> None:
    error = ProviderTimeoutError("late")

    assert error.code == ErrorCode.PROVIDER_TIMEOUT
    assert error.retryable is True


def test_unavailable_is_retryable_by_default() -> None:
    error = ProviderUnavailableError("down")

    assert error.code == ErrorCode.PROVIDER_ERROR
    assert error.retryable is True


def test_stream_error_is_not_retryable_by_default() -> None:
    assert ProviderStreamError("bad stream").retryable is False


def test_retryable_can_be_overridden_per_instance() -> None:
    assert ProviderError("x", retryable=True).retryable is True
    assert ProviderUnavailableError("x", retryable=False).retryable is False


def test_an_error_is_a_provider_error_subclass_for_catching() -> None:
    with pytest.raises(ProviderError):
        raise ProviderTimeoutError("late")


def test_the_port_declares_stream_chat() -> None:
    assert hasattr(base.ChatProvider, "stream_chat")


# --- FakeChatProvider --------------------------------------------------------


async def test_fake_streams_the_reply_in_chunks() -> None:
    provider = FakeChatProvider(reply="abcdefgh", chunk_size=3)

    deltas = await collect(provider)

    assert [delta.text for delta in deltas] == ["abc", "def", "gh", ""]
    assert "".join(delta.text for delta in deltas) == "abcdefgh"


async def test_fake_streams_a_default_reply_and_finishes() -> None:
    provider = FakeChatProvider()

    deltas = await collect(provider)

    assert "".join(delta.text for delta in deltas) == DEFAULT_REPLY
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage is None


async def test_fake_reports_usage_when_configured() -> None:
    usage = ChatUsage(prompt_tokens=3, completion_tokens=7)
    provider = FakeChatProvider(reply="hi", usage=usage)

    deltas = await collect(provider)

    assert deltas[-1].usage == usage


async def test_fake_finish_reason_is_configurable() -> None:
    provider = FakeChatProvider(reply="hi", finish_reason="length")

    deltas = await collect(provider)

    assert deltas[-1].finish_reason == "length"


async def test_fake_records_each_request() -> None:
    provider = FakeChatProvider(reply="hi")
    params = ChatParams(temperature=0.1)

    await collect(provider, params)
    await collect(provider)

    assert len(provider.requests) == 2
    assert provider.requests[0] == FakeRequest(messages=tuple(MESSAGES), model=MODEL, params=params)
    assert provider.requests[1].params == ChatParams()


async def test_fake_emits_nothing_for_an_empty_reply() -> None:
    provider = FakeChatProvider(reply="")

    deltas = await collect(provider)

    assert [delta.text for delta in deltas] == [""]
    assert deltas[-1].finish_reason == "stop"


async def test_fake_fails_after_n_text_chunks() -> None:
    failure = ProviderUnavailableError("dropped")
    provider = FakeChatProvider(reply="abcdefgh", chunk_size=3, fail_with=failure, fail_after=1)

    received: list[str] = []
    with pytest.raises(ProviderUnavailableError):
        async for delta in provider.stream_chat(
            messages=MESSAGES, model=MODEL, params=ChatParams()
        ):
            received.append(delta.text)

    assert received == ["abc"]


async def test_fake_fails_after_the_whole_reply_when_fail_after_is_unset() -> None:
    failure = ProviderTimeoutError("late")
    provider = FakeChatProvider(reply="hi", fail_with=failure)

    received: list[str] = []
    with pytest.raises(ProviderTimeoutError):
        async for delta in provider.stream_chat(
            messages=MESSAGES, model=MODEL, params=ChatParams()
        ):
            received.append(delta.text)

    assert "".join(received) == "hi"


async def test_fake_does_not_fail_when_no_failure_is_set() -> None:
    provider = FakeChatProvider(reply="hi")

    deltas = await collect(provider)

    assert deltas[-1].finish_reason == "stop"


async def test_fake_close_is_recorded() -> None:
    provider = FakeChatProvider()

    assert provider.closed is False
    await provider.aclose()
    assert provider.closed is True
