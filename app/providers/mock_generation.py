"""A deterministic in-process `GenerationProvider` for the mock mode.

`GENERATION_MODE=mock` selects this adapter, so Image and Video jobs run
end-to-end through the real port without a paid provider key. It produces real,
valid files — a PNG for images, a bundled MP4 for video — that satisfy the same
port as a live adapter, so switching later does not change the API. Nothing here
makes external calls and nothing here reports a real model.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.providers.base import (
    GenerationJob,
    GenerationOutput,
    GenerationProvider,
)

#: Bundled demo clips shipped next to the catalog seed, so the Docker image (which
#: copies `app/`) has everything the mock video provider needs.
SEED_MEDIA_DIR = Path(__file__).resolve().parent.parent / "db" / "seed_media"

MOCK_VIDEO_CLIPS: tuple[str, ...] = (
    "clip-01.mp4",
    "clip-02.mp4",
    "clip-03.mp4",
    "clip-04.mp4",
)

DEFAULT_IMAGE_DIMENSIONS: tuple[int, int] = (512, 512)
MOCK_VIDEO_WIDTH = 640
MOCK_VIDEO_HEIGHT = 360
MOCK_VIDEO_DURATION_MS = 5_000

_ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "1:1": (512, 512),
    "4:5": (512, 640),
    "16:9": (640, 360),
    "9:16": (360, 640),
}


async def _no_progress(_percent: int) -> None:  # pragma: no cover - a default
    return None


def _chunk(tag: bytes, data: bytes) -> bytes:
    """One PNG chunk, with its CRC, as the PNG signature format requires."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, hue: float) -> bytes:
    """Encode a deterministic vertical gradient PNG with a true colour type."""
    top = (int(70 + 60 * hue), int(100 + 80 * hue), int(170 - 60 * hue))
    bottom = (int(30 + 40 * hue), int(50 + 60 * hue), int(110 + 70 * (1 - hue)))
    rows = bytearray()
    for y in range(height):
        t = y / (height - 1) if height > 1 else 0.0
        scanline = bytearray((0,))
        for x in range(width):
            u = x / (width - 1) if width > 1 else 0.0
            scanline += bytes(
                (
                    int(top[0] + (bottom[0] - top[0]) * u * t),
                    int(top[1] + (bottom[1] - top[1]) * u * t),
                    int(top[2] + (bottom[2] - top[2]) * u * t),
                )
            )
        rows += scanline
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def _image_outputs(job: GenerationJob, index: int) -> list[GenerationOutput]:
    aspect_ratio = str(job.params.get("aspectRatio") or "1:1")
    width, height = _ASPECT_DIMENSIONS.get(aspect_ratio, DEFAULT_IMAGE_DIMENSIONS)
    count = job.params.get("count") or 1
    variants = count if isinstance(count, int) and not isinstance(count, bool) else 1
    hue = (index * 0.37) % 1.0
    return [
        GenerationOutput(
            data=_png(width, height, hue + offset * 0.05),
            mime_type="image/png",
            width=width,
            height=height,
        )
        for offset in range(variants)
    ]


def _video_output(job: GenerationJob, index: int) -> GenerationOutput:
    duration_ms = MOCK_VIDEO_DURATION_MS
    raw_duration = job.params.get("duration")
    if isinstance(raw_duration, int) and not isinstance(raw_duration, bool) and raw_duration > 0:
        duration_ms = raw_duration * 1_000
    file_name = MOCK_VIDEO_CLIPS[index % len(MOCK_VIDEO_CLIPS)]
    return GenerationOutput(
        data=(SEED_MEDIA_DIR / file_name).read_bytes(),
        mime_type="video/mp4",
        width=MOCK_VIDEO_WIDTH,
        height=MOCK_VIDEO_HEIGHT,
        duration_ms=duration_ms,
    )


@dataclass(frozen=True)
class MockGenerationRequest:
    """One recorded call, so tests can assert what the worker handed over."""

    job: GenerationJob
    index: int


@dataclass
class MockGenerationProvider:
    """Renders deterministic mock outputs and records the calls it made."""

    progress: tuple[int, ...] = (25, 60, 90)
    requests: list[MockGenerationRequest] = field(default_factory=list)
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True

    async def generate(
        self,
        job: GenerationJob,
        *,
        on_progress: Callable[[int], Awaitable[None]] = _no_progress,
    ) -> Sequence[GenerationOutput]:
        index = len(self.requests)
        self.requests.append(MockGenerationRequest(job=job, index=index))
        for percent in self.progress:
            await on_progress(percent)
        outputs = (
            [_video_output(job, index)] if job.modality == "video" else _image_outputs(job, index)
        )
        return outputs


def build_mock_generation_provider(settings: Settings) -> GenerationProvider | None:
    """Return the deterministic mock adapter while `GENERATION_MODE=mock`."""
    if settings.generation_mode != "mock":
        # A real image/video adapter arrives with its own provider key; until it
        # does, a worker in live mode refuses to render instead of mocking.
        return None
    return MockGenerationProvider()
