"""Provider ports, DTOs, and the error taxonomy.

Domain code depends on these protocols, never on httpx or a vendor SDK. An
adapter translates a vendor payload into these DTOs and raises a `ProviderError`
subclass, whose `code` maps to the documented API problem code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from app.api.problems import ErrorCode

Role = Literal["system", "user", "assistant"]
GenerationModality = Literal["image", "video"]


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
    arrive and raises a `ProviderError` subclass on failure. `aclose` releases
    any pooled transport and runs when the application shuts down.
    """

    def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]: ...

    async def aclose(self) -> None: ...


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


class ProviderCancelledError(ProviderError):
    """The provider aborted the job, so its output must be discarded.

    A Generation cancelled while a worker is running reaches this path: the cancel
    route already refunded the reservation, so the worker must not commit or refund
    again.
    """

    code = ErrorCode.CONFLICT


# --- Image and Video generation -----------------------------------------------


@dataclass(frozen=True)
class GenerationJob:
    """One queued Generation, exactly as the worker needs to render it.

    Carries only the fields a provider renders from. The prompt is required, so it
    travels here rather than through logs.
    """

    generation_id: UUID
    modality: GenerationModality
    model: ProviderModel
    prompt: str
    params: dict[str, Any]


@dataclass(frozen=True)
class GenerationOutput:
    """One rendered file, before it becomes an Asset.

    `data` is the encoded file. Dimensions and duration are what the catalog
    advertises; a provider that cannot know them (a mock clip) reports the values
    its fixture actually has.
    """

    data: bytes
    mime_type: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


async def _no_progress(_percent: int) -> None:  # pragma: no cover - a default
    return None


class GenerationProvider(Protocol):
    """A backend that renders an Image or Video Generation.

    `generate` reports progress through `on_progress` and returns one
    `GenerationOutput` per requested variant. It raises a `ProviderError`
    subclass on failure; a non-retryable error stops the job immediately, while a
    retryable one lets the worker try again until `max_attempts` is reached.
    `aclose` releases any pooled transport at shutdown.
    """

    async def generate(
        self,
        job: GenerationJob,
        *,
        on_progress: Callable[[int], Awaitable[None]] = _no_progress,
    ) -> Sequence[GenerationOutput]: ...

    async def aclose(self) -> None: ...
