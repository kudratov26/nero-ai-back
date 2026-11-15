"""A deterministic in-process `ChatProvider` for tests.

Never wired into the app: it exists so tests (and the chat streaming integration
in B3.3) run without a provider key. Anything it surfaces is labelled as a demo,
never as a real model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from app.providers.base import (
    ChatDelta,
    ChatParams,
    ChatUsage,
    ProviderError,
    ProviderMessage,
    ProviderModel,
)

DEFAULT_REPLY = "Hello from the fake provider."


@dataclass(frozen=True)
class FakeRequest:
    """One recorded call, so tests can assert what the provider was sent."""

    messages: tuple[ProviderMessage, ...]
    model: ProviderModel
    params: ChatParams


def _chunks(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


@dataclass
class FakeChatProvider:
    """Streams a fixed reply in fixed-size chunks.

    `fail_with` (with `fail_after` text chunks emitted first) simulates a
    mid-stream failure; `fail_after=None` fails after the whole reply but before
    the final delta. `usage` is reported on the final delta when set.
    """

    reply: str = DEFAULT_REPLY
    chunk_size: int = 4
    usage: ChatUsage | None = None
    finish_reason: str = "stop"
    fail_with: ProviderError | None = None
    fail_after: int | None = None
    requests: list[FakeRequest] = field(default_factory=list)
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True

    async def stream_chat(
        self,
        *,
        messages: Sequence[ProviderMessage],
        model: ProviderModel,
        params: ChatParams,
    ) -> AsyncIterator[ChatDelta]:
        self.requests.append(FakeRequest(messages=tuple(messages), model=model, params=params))
        chunks = _chunks(self.reply, self.chunk_size)
        for index, chunk in enumerate(chunks):
            if (
                self.fail_with is not None
                and self.fail_after is not None
                and index >= self.fail_after
            ):
                raise self.fail_with
            yield ChatDelta(text=chunk)
        if self.fail_with is not None and (
            self.fail_after is None or self.fail_after >= len(chunks)
        ):
            raise self.fail_with
        yield ChatDelta(finish_reason=self.finish_reason, usage=self.usage)
