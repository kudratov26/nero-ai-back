"""Pure asset-permission rules.

The database-backed cases live in `tests/integration/test_assets.py`; these tests
pin the small decision table so a refactor of the delivery path cannot quietly
change who may read bytes.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.db.models import Asset
from app.domain.assets import can_read


def asset_owned_by(user_id: UUID) -> Asset:
    asset_id = uuid4()
    return Asset(
        id=asset_id,
        user_id=user_id,
        kind="image",
        storage_key=f"assets/{user_id}/{asset_id}.png",
        mime_type="image/png",
        size_bytes=10,
    )


def test_the_owner_can_always_read() -> None:
    owner = uuid4()

    assert can_read(asset_owned_by(owner), user_id=owner) is True


def test_a_published_post_makes_an_asset_public() -> None:
    asset = asset_owned_by(uuid4())

    assert can_read(asset, user_id=None, published_post=True) is True
    assert can_read(asset, user_id=uuid4(), published_post=True) is True


def test_a_private_asset_is_not_public() -> None:
    asset = asset_owned_by(uuid4())

    assert can_read(asset, user_id=None) is False
    assert can_read(asset, user_id=uuid4()) is False
