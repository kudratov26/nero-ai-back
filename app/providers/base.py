"""Provider ports, DTOs, and the error taxonomy.

Domain code depends on these protocols, never on httpx or a vendor SDK. An
adapter translates a vendor payload into these DTOs and raises a `ProviderError`
subclass, whose `code` maps to the documented API problem code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.api.problems import ErrorCode

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ProviderMessage:
    """One turn handed to the provider."""

    role: Role
    content: str


@dataclass(frozen=True)
class ProviderModel:
    """The catalog model halves an adapter needs.

    `slug` is the public catalog id; `provider_model_id` is what the gateway
    expects. They can differ, so both travel together.
    """

    slug: str
    provider_model_id: str


@dataclass(frozen=True)
class ChatParams:
    """Optional generation knobs. Unset fields are omitted from the request."""

    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class ChatUsage:
    """Token counts reported by the provider. Either side may be unknown."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ChatDelta:
    """One streamed chunk.

    `text` carries content to append. The final chunk of a stream may instead
    carry `finish_reason` and/or `usage`, with `text` empty.
    """

    text: str = ""
    finish_reason: str | None = None
    usage: ChatUsage | None = None


class ChatProvider(Protocol):
    """A streaming chat backend.

    Implementations are async generators: an adapter yields `ChatDelta`s as they
    arrive and raises a `ProviderError` subclass on failure.
    """

    def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]: ...


class ProviderError(Exception):
    """A provider failure, mapped to a documented problem code.

    `retryable` marks failures an adapter may retry while no token has been
    delivered yet (network errors, timeouts, 5xx). It is never set for a 4xx.
    """

    code: ErrorCode = ErrorCode.PROVIDER_ERROR
    retryable: bool = False

    def __init__(self, detail: str, *, retryable: bool | None = None) -> None:
        self.detail = detail
        if retryable is not None:
            self.retryable = retryable
        super().__init__(detail)


class ProviderTimeoutError(ProviderError):
    """The provider did not answer within the read/connect timeout. Retryable
    before the first token."""

    code = ErrorCode.PROVIDER_TIMEOUT
    retryable = True


class ProviderRateLimitError(ProviderError):
    """The provider returned 429. Never retried."""

    code = ErrorCode.RATE_LIMITED


class ProviderUnavailableError(ProviderError):
    """A 5xx or a dropped connection. Retryable before the first token."""

    code = ErrorCode.PROVIDER_ERROR
    retryable = True


class ProviderStreamError(ProviderError):
    """The stream was malformed or the response was not an event stream."""

    code = ErrorCode.PROVIDER_ERROR
