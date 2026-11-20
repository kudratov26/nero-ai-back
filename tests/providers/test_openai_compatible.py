"""Provider-layer tests for the OpenAI-compatible adapter.

These use `respx` to intercept httpx. No test calls a real provider.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.api.problems import ErrorCode
from app.core.logging import configure_logging
from app.providers.base import (
    ChatParams,
    ChatProvider,
    ProviderError,
    ProviderMessage,
    ProviderModel,
    ProviderRateLimitError,
    ProviderStreamError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.openai_compatible import OpenAIChatProvider, build_chat_provider
from tests.support import make_settings

BASE_URL = "https://gateway.example.test/v1"
ENDPOINT = f"{BASE_URL}/chat/completions"
API_KEY = "test-key-not-printed"
MESSAGES = [ProviderMessage(role="user", content="hi")]
MODEL = ProviderModel(slug="deepseek-v4.1-flash", provider_model_id="deepseek-v4.1-flash")

SSE = (
    b'data: {"choices":[{"delta":{"content":"Shoot"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" the"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":80}}\n\n'
    b"data: [DONE]\n\n"
)


def make_provider(**overrides: object) -> OpenAIChatProvider:
    kwargs: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": API_KEY,
        "read_timeout": 5.0,
        "retry_backoff_seconds": 0.0,
    }
    kwargs.update(overrides)
    return OpenAIChatProvider(**kwargs)  # type: ignore[arg-type]


def sse_response() -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE)


async def collect(provider: ChatProvider, params: ChatParams | None = None) -> list:
    return [
        delta
        async for delta in provider.stream_chat(
            messages=MESSAGES, model=MODEL, params=params or ChatParams()
        )
    ]


@respx.mock
async def test_streams_sse_deltas_and_usage() -> None:
    respx.post(ENDPOINT).mock(return_value=sse_response())
    provider = make_provider()
    try:
        deltas = await collect(provider)
    finally:
        await provider.aclose()

    assert "".join(delta.text for delta in deltas) == "Shoot the"
    assert deltas[-1].usage is not None
    assert deltas[-1].usage.prompt_tokens == 12
    assert deltas[-1].usage.completion_tokens == 80


@respx.mock
async def test_sends_the_expected_request() -> None:
    route = respx.post(ENDPOINT).mock(return_value=sse_response())
    provider = make_provider()
    try:
        await collect(provider, ChatParams(temperature=0.2, max_tokens=1))
    finally:
        await provider.aclose()

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["accept"] == "text/event-stream"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4.1-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 1


@respx.mock
async def test_unset_params_are_omitted() -> None:
    route = respx.post(ENDPOINT).mock(return_value=sse_response())
    provider = make_provider()
    try:
        await collect(provider)
    finally:
        await provider.aclose()

    body = json.loads(route.calls.last.request.content)
    assert "temperature" not in body
    assert "max_tokens" not in body


@respx.mock
async def test_rate_limit_is_not_retried() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(429, json={"error": "slow down"}))
    provider = make_provider()
    try:
        with pytest.raises(ProviderRateLimitError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert caught.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_a_server_error_maps_to_provider_error() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    provider = make_provider(max_retries=0)
    try:
        with pytest.raises(ProviderUnavailableError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert caught.value.code == ErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is True


@respx.mock
async def test_a_client_error_is_not_retried() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(401))
    provider = make_provider()
    try:
        with pytest.raises(ProviderError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert caught.value.code == ErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_a_provider_error_body_is_never_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    """The gateway may quote the key in its error body; the adapter must not."""
    configure_logging(make_settings(ai_api_key=API_KEY))
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": f"invalid key {API_KEY}"})
    )
    provider = make_provider()
    try:
        with pytest.raises(ProviderError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert API_KEY not in str(caught.value)
    assert API_KEY not in caught.value.detail
    assert API_KEY not in capsys.readouterr().out


@respx.mock
async def test_retries_before_the_first_token_then_succeeds() -> None:
    route = respx.post(ENDPOINT).mock(side_effect=[httpx.Response(503), sse_response()])
    provider = make_provider()
    try:
        deltas = await collect(provider)
    finally:
        await provider.aclose()

    assert "".join(delta.text for delta in deltas) == "Shoot the"
    assert route.call_count == 2


@respx.mock
async def test_retries_are_bounded() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    provider = make_provider()
    try:
        with pytest.raises(ProviderUnavailableError):
            await collect(provider)
    finally:
        await provider.aclose()

    # The first attempt plus `MAX_RETRIES` (2).
    assert route.call_count == 3


@respx.mock
async def test_a_timeout_maps_to_provider_timeout() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectTimeout("timed out"))
    provider = make_provider(max_retries=0)
    try:
        with pytest.raises(ProviderTimeoutError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert caught.value.code == ErrorCode.PROVIDER_TIMEOUT
    assert caught.value.retryable is True


class _DropAfterFirstEvent(httpx.AsyncByteStream):
    """A stream that delivers one SSE event, then drops the connection."""

    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        raise httpx.ReadError("connection dropped")


async def test_no_retry_after_the_first_token() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_DropAfterFirstEvent()
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = make_provider(client=client)
    received: list[str] = []
    try:
        with pytest.raises(ProviderUnavailableError):
            async for delta in provider.stream_chat(
                messages=MESSAGES, model=MODEL, params=ChatParams()
            ):
                received.append(delta.text)
    finally:
        await provider.aclose()
        # The adapter must not close a client it did not create.
        assert client.is_closed is False
        await client.aclose()

    assert received == ["Hi"]
    assert calls == 1


@pytest.mark.parametrize(
    "content",
    [b"data: {not-json}\n\n", b"data: [1, 2, 3]\n\n"],
)
@respx.mock
async def test_a_malformed_chunk_raises_a_stream_error(content: bytes) -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    provider = make_provider()
    try:
        with pytest.raises(ProviderStreamError) as caught:
            await collect(provider)
    finally:
        await provider.aclose()

    assert caught.value.code == ErrorCode.PROVIDER_ERROR


@respx.mock
async def test_comments_and_unknown_fields_are_ignored() -> None:
    content = (
        b": keep-alive\n\n"
        b"event: message\n"
        b"data:\n\n"
        b'data: {"choices":[]}\n\n'
        b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    provider = make_provider()
    try:
        deltas = await collect(provider)
    finally:
        await provider.aclose()

    assert "".join(delta.text for delta in deltas) == "Hi"


@respx.mock
async def test_a_missing_usage_is_none() -> None:
    content = (
        b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )
    )
    provider = make_provider()
    try:
        deltas = await collect(provider)
    finally:
        await provider.aclose()

    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage is None


@respx.mock
async def test_a_non_event_stream_response_is_rejected() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"error":"nope"}'
        )
    )
    provider = make_provider()
    try:
        with pytest.raises(ProviderStreamError):
            await collect(provider)
    finally:
        await provider.aclose()


def test_build_chat_provider_returns_none_when_unconfigured() -> None:
    assert build_chat_provider(make_settings()) is None


async def test_build_chat_provider_builds_when_configured() -> None:
    settings = make_settings(
        ai_base_url=BASE_URL,
        ai_api_key=API_KEY,
        ai_request_timeout_seconds=30,
    )

    provider = build_chat_provider(settings)

    assert isinstance(provider, OpenAIChatProvider)
    assert provider is not None
    await provider.aclose()
