"""The bundled gallery fixture is complete and self-consistent.

These checks run before any database: a post that names a missing file or a
duplicate id would otherwise fail halfway through a deploy seed.
"""

from __future__ import annotations

import pytest

from app.db.explore_seed import (
    ASSETS_BY_ID,
    MEDIA_BY_SLUG,
    MEDIA_SEEDS,
    POST_SEEDS,
    POSTS_BY_ID,
    SEED_MEDIA_DIR,
    MediaSeed,
    media_bytes,
    model_id_for,
    seed_uuid,
)
from app.db.seed import IMAGE_MODEL_ID, VIDEO_MODEL_ID


def test_the_gallery_has_sixteen_posts_across_three_modalities() -> None:
    assert len(POST_SEEDS) == 16
    assert len(MEDIA_SEEDS) == 14

    modalities = [post.modality for post in POST_SEEDS]
    assert modalities.count("image") == 10
    assert modalities.count("video") == 4
    assert modalities.count("chat") == 2


def test_slugs_and_ids_are_unique() -> None:
    post_slugs = [post.slug for post in POST_SEEDS]
    media_slugs = [media.slug for media in MEDIA_SEEDS]

    assert len(set(post_slugs)) == len(post_slugs)
    assert len(set(media_slugs)) == len(media_slugs)
    assert len(POSTS_BY_ID) == len(POST_SEEDS)
    assert len(ASSETS_BY_ID) == len(MEDIA_SEEDS)


def test_every_image_and_video_post_has_a_bundled_asset() -> None:
    for post in POST_SEEDS:
        if post.modality == "chat":
            assert post.asset_slug is None
            continue
        assert post.asset_slug in MEDIA_BY_SLUG
        assert MEDIA_BY_SLUG[post.asset_slug].kind == post.modality


@pytest.mark.parametrize("media", MEDIA_SEEDS, ids=lambda media: media.slug)
def test_every_media_file_is_bundled_and_matches_its_metadata(media: MediaSeed) -> None:
    path = SEED_MEDIA_DIR / media.file_name

    assert path.is_file()
    assert path.stat().st_size > 0
    assert len(media_bytes(media)) == path.stat().st_size
    assert path.suffix == {"image/jpeg": ".jpg", "video/mp4": ".mp4"}[media.mime_type]
    assert media.width > 0
    assert media.height > 0
    if media.kind == "video":
        assert media.duration_ms is not None and media.duration_ms > 0
    else:
        assert media.duration_ms is None


def test_seed_ids_are_stable_and_kind_scoped() -> None:
    assert seed_uuid("post", "blue-hour") == seed_uuid("post", "blue-hour")
    assert seed_uuid("post", "blue-hour") != seed_uuid("asset", "blue-hour")
    assert seed_uuid("post", "blue-hour") != seed_uuid("post", "last-train")


def test_model_id_for_points_at_advertised_catalog_rows() -> None:
    image = next(post for post in POST_SEEDS if post.modality == "image")
    video = next(post for post in POST_SEEDS if post.modality == "video")
    chat = next(post for post in POST_SEEDS if post.modality == "chat")

    assert model_id_for(image, chat_model_id="chat-model") == IMAGE_MODEL_ID
    assert model_id_for(video, chat_model_id="chat-model") == VIDEO_MODEL_ID
    assert model_id_for(chat, chat_model_id="chat-model") == "chat-model"


def test_created_at_is_timezone_aware() -> None:
    for post in POST_SEEDS:
        assert post.created_at.tzinfo is not None
        assert post.created_at.utcoffset() is not None


def test_a_missing_media_file_is_reported() -> None:
    missing = MediaSeed("missing", "not-there.jpg", "image", "image/jpeg", 10, 10)

    with pytest.raises(FileNotFoundError, match=r"not-there\.jpg"):
        media_bytes(missing)
