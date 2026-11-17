"""Tests for the deterministic mock generation provider."""

from __future__ import annotations

import struct
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from app.providers.base import GenerationJob, ProviderModel
from app.providers.mock_generation import (
    DEFAULT_IMAGE_DIMENSIONS,
    SEED_MEDIA_DIR,
    MockGenerationProvider,
    build_mock_generation_provider,
)
from tests.support import make_settings

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _progress() -> Callable[[int], Awaitable[None]]:
    async def report(_percent: int) -> None:
        return None

    return report


def _job(
    *,
    modality: Literal["image", "video"] = "image",
    params: dict[str, object] | None = None,
) -> GenerationJob:
    return GenerationJob(
        generation_id=UUID("0193a0e0-0000-7000-8000-000000000001"),
        modality=modality,
        model=ProviderModel(slug="image-demo", provider_model_id="image-demo"),
        prompt="A sunlit reading room",
        params=dict(params or {"aspectRatio": "1:1", "count": 1, "style": "None"}),
    )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    assert data.startswith(_SIGNATURE)
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def test_the_mock_image_provider_is_deterministic() -> None:
    first_provider = MockGenerationProvider()
    second_provider = MockGenerationProvider()

    first = await first_provider.generate(_job(), on_progress=_progress())
    second = await second_provider.generate(_job(), on_progress=_progress())

    assert [output.data for output in first] == [output.data for output in second]
    assert len(first) == 1


async def test_the_mock_image_provider_sizes_by_aspect_ratio() -> None:
    provider = MockGenerationProvider()

    square = await provider.generate(
        _job(params={"aspectRatio": "1:1", "count": 1}), on_progress=_progress()
    )
    landscape = await provider.generate(
        _job(params={"aspectRatio": "16:9", "count": 1}), on_progress=_progress()
    )

    assert (square[0].width, square[0].height) == DEFAULT_IMAGE_DIMENSIONS
    assert (landscape[0].width, landscape[0].height) == (640, 360)


async def test_the_mock_image_provider_renders_one_png_per_variant() -> None:
    provider = MockGenerationProvider()

    outputs = await provider.generate(
        _job(params={"aspectRatio": "1:1", "count": 3}), on_progress=_progress()
    )

    assert len(outputs) == 3
    assert len({output.data for output in outputs}) == 3
    for output in outputs:
        assert output.mime_type == "image/png"
        assert (output.width, output.height) == (512, 512)
        assert _png_dimensions(output.data) == (512, 512)


async def test_the_mock_video_provider_returns_a_bundled_mp4() -> None:
    provider = MockGenerationProvider()
    outputs = await provider.generate(
        _job(modality="video", params={"aspectRatio": "16:9", "duration": 5}),
        on_progress=_progress(),
    )

    assert len(outputs) == 1
    assert outputs[0].mime_type == "video/mp4"
    assert outputs[0].duration_ms == 5_000
    assert (outputs[0].width, outputs[0].height) == (640, 360)
    assert outputs[0].data == (SEED_MEDIA_DIR / "clip-01.mp4").read_bytes()


def test_build_mock_generation_provider_returns_none_outside_mock_mode() -> None:
    assert build_mock_generation_provider(make_settings(generation_mode="live")) is None
    assert build_mock_generation_provider(make_settings(generation_mode="mock")) is not None
