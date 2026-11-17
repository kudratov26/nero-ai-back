"""A deterministic in-process `GenerationProvider` for tests.

Never wired into the app: B5.3 adds the renderable mock adapters that produce a
real PNG and MP4. This provider exists so the worker can be exercised without a
media encoder, and it reports a tiny, clearly synthetic payload instead of
pretending to be image or video bytes.

Tests set `outputs`, `fail_with`, and `progress` to drive each worker path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.providers.base import (
    GenerationJob,
    GenerationOutput,
    ProviderError,
)

#: A 1x1 PNG, so the payload is a real image even though it is not artwork.
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class FakeGenerationRequest:
    """One recorded call, so tests can assert what the worker handed over."""

    job: GenerationJob
    progress: tuple[int, ...]


def default_outputs(job: GenerationJob) -> list[GenerationOutput]:
    """One tiny PNG per requested variant, sized from the aspect ratio."""
    if job.modality == "video":
        return [
            GenerationOutput(
                data=TINY_PNG,
                mime_type="video/mp4",
                width=640,
                height=360,
                duration_ms=5_000,
            )
        ]
    count = job.params.get("count", 1)
    variants = count if isinstance(count, int) and not isinstance(count, bool) else 1
    return [
        GenerationOutput(data=TINY_PNG, mime_type="image/png", width=512, height=512)
        for _ in range(variants)
    ]


@dataclass
class FakeGenerationProvider:
    """Renders fixed outputs and records the progress it was asked to report.

    `progress` is the sequence reported through the callback before the outputs
    are returned. `fail_with` raises instead, optionally after reporting
    `fail_after_progress` steps, so retry and cancel paths are easy to drive.
    """

    outputs: list[GenerationOutput] | None = None
    progress: tuple[int, ...] = (10, 50, 90)
    fail_with: ProviderError | None = None
    fail_after_progress: int | None = None
    requests: list[FakeGenerationRequest] = field(default_factory=list)
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True

    async def generate(
        self,
        job: GenerationJob,
        *,
        on_progress: Callable[[int], Awaitable[None]],
    ) -> Sequence[GenerationOutput]:
        reported: list[int] = []
        for index, percent in enumerate(self.progress):
            if (
                self.fail_with is not None
                and self.fail_after_progress is not None
                and index >= self.fail_after_progress
            ):
                self.requests.append(FakeGenerationRequest(job=job, progress=tuple(reported)))
                raise self.fail_with
            await on_progress(percent)
            reported.append(percent)
        if self.fail_with is not None and (
            self.fail_after_progress is None or self.fail_after_progress >= len(self.progress)
        ):
            self.requests.append(FakeGenerationRequest(job=job, progress=tuple(reported)))
            raise self.fail_with
        self.requests.append(FakeGenerationRequest(job=job, progress=tuple(reported)))
        if self.outputs is not None:
            return list(self.outputs)
        return default_outputs(job)
