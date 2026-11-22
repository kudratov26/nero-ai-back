"""OpenAI-compatible chat adapter, used for the Coarena gateway.

Speaks `POST {base_url}/chat/completions` with SSE, which is what the gateway is
assumed to implement until its documentation says otherwise. Nothing here leaks
into services: domain code depends on `app.providers.base` only.

Prompts and completions are never logged by this module.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.metrics import METRICS, Metrics
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

#: Connect and write timeouts are fixed; only the read timeout is configurable
#: (`AI_REQUEST_TIMEOUT_SECONDS`).
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 30.0

CHAT_COMPLETIONS_PATH = "/chat/completions"
SSE_MEDIA_TYPE = "text/event-stream"
DONE_SENTINEL = "[DONE]"
PROVIDER_NAME = "openai-compatible"

#: Retry a failed attempt at most this many times, only before the first token.
MAX_RETRIES = 2
#: Base delay between retries; each retry doubles it and adds jitter.
RETRY_BACKOFF_SECONDS = 0.25


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _decode_sse_data(line: str) -> dict[str, Any] | None:
    """Parse one SSE line into a JSON object, or `None` to skip it.

    Blank lines, comments (`: ping`), and non-`data:` fields are ignored. A
    `data:` line that is not a JSON object is a malformed chunk.
    """
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    raw = line[len("data:") :].strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderStreamError("The provider returned an unreadable stream.") from exc
    if not isinstance(payload, dict):
        raise ProviderStreamError("The provider returned an unreadable stream.")
    return payload


def _is_done(line: str) -> bool:
    return line.startswith("data:") and line[len("data:") :].strip() == DONE_SENTINEL


def _to_delta(payload: Mapping[str, Any]) -> ChatDelta | None:
    """Translate one decoded chunk, or `None` when it carries nothing useful."""
    text = ""
    finish_reason: str | None = None
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            text = delta["content"]
        reason = choice.get("finish_reason")
        if isinstance(reason, str):
            finish_reason = reason

    usage: ChatUsage | None = None
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        usage = ChatUsage(
            prompt_tokens=_int_or_none(raw_usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(raw_usage.get("completion_tokens")),
        )

    if not text and finish_reason is None and usage is None:
        return None
    return ChatDelta(text=text, finish_reason=finish_reason, usage=usage)


class OpenAIChatProvider:
    """A `ChatProvider` over an OpenAI-compatible `/chat/completions` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        read_timeout: float,
        client: httpx.AsyncClient | None = None,
        max_retries: int = MAX_RETRIES,
        retry_backoff_seconds: float = RETRY_BACKOFF_SECONDS,
        metrics: Metrics | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
        self._max_retries = max(0, max_retries)
        self._retry_backoff = retry_backoff_seconds
        self._metrics = metrics or METRICS
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=read_timeout,
                connect=CONNECT_TIMEOUT_SECONDS,
                write=WRITE_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            )
        )

    async def aclose(self) -> None:
        """Close the underlying client when this adapter created it."""
        if self._owns_client:
            await self._client.aclose()

    def _record_attempt(self, outcome: str, started: float) -> None:
        """Record one provider attempt by outcome, never by prompt or body."""
        self._metrics.increment(
            "provider_requests_total",
            provider=PROVIDER_NAME,
            outcome=outcome,
        )
        self._metrics.observe(
            "provider_request_duration_seconds",
            time.perf_counter() - started,
            provider=PROVIDER_NAME,
            outcome=outcome,
        )

    def _payload(
        self,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model.provider_model_id,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        if params.max_tokens is not None:
            payload["max_tokens"] = params.max_tokens
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": SSE_MEDIA_TYPE,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _error_for_status(status: int) -> ProviderError:
        if status == 429:
            return ProviderRateLimitError("The provider is rate limiting requests.")
        if 500 <= status < 600:
            return ProviderUnavailableError("The provider is temporarily unavailable.")
        return ProviderError(f"The provider rejected the request ({status}).")

    async def _stream_once(
        self,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]:
        payload = self._payload(messages, model, params)
        try:
            async with self._client.stream(
                "POST", self._endpoint, json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_for_status(response.status_code)
                content_type = response.headers.get("content-type", "")
                if content_type and not content_type.startswith(SSE_MEDIA_TYPE):
                    await response.aread()
                    raise ProviderStreamError("The provider returned an unexpected response.")
                async for line in response.aiter_lines():
                    if _is_done(line):
                        break
                    chunk = _decode_sse_data(line)
                    if chunk is None:
                        continue
                    delta = _to_delta(chunk)
                    if delta is not None:
                        yield delta
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("The provider did not respond in time.") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError("The connection to the provider was lost.") from exc

    async def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]:
        """Yield deltas, retrying only until the first delta has been delivered."""
        attempt = 0
        while True:
            delivered = False
            started = time.perf_counter()
            try:
                async for delta in self._stream_once(messages, model, params):
                    delivered = True
                    yield delta
                self._record_attempt("ok", started)
                return
            except ProviderError as exc:
                self._record_attempt(exc.code.value, started)
                if delivered or not exc.retryable or attempt >= self._max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(self._retry_backoff * (2 ** (attempt - 1)) * random.random())
            except Exception:
                # A transport or adapter bug still counts as a failed attempt,
                # but the exception type is all that is recorded; no payload.
                self._record_attempt("error", started)
                raise


def build_chat_provider(
    settings: Settings,
    *,
    metrics: Metrics | None = None,
) -> OpenAIChatProvider | None:
    """Build the chat provider from settings, or `None` when it is not configured."""
    base_url = settings.ai_base_url
    api_key = settings.ai_api_key
    if not base_url or not api_key:
        return None
    return OpenAIChatProvider(
        base_url=base_url,
        api_key=api_key,
        read_timeout=settings.ai_request_timeout_seconds,
        metrics=metrics,
    )
