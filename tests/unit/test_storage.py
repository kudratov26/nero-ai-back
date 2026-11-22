"""Local storage adapter tests.

These are the adversarial cases: a clean key is boring, so every test here asks
the adapter to escape the root one way or another and expects a refusal.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.storage.base import StorageError, storage_key_for
from app.storage.local import LocalStorage


def test_storage_key_matches_the_documented_shape() -> None:
    user_id = uuid4()
    asset_id = uuid4()

    key = storage_key_for(user_id=user_id, asset_id=asset_id, extension="png")

    assert key == f"assets/{user_id}/{asset_id}.png"


async def test_write_then_stat_round_trips(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = storage_key_for(user_id=uuid4(), asset_id=uuid4(), extension="png")

    written = await storage.write(key, b"asset bytes")

    assert written.size_bytes == 11
    assert written.etag is not None and written.etag.startswith('"')
    assert await storage.stat(key) == written
    assert storage.path_for(key).read_bytes() == b"asset bytes"
    assert not list(storage.path_for(key).parent.glob("*.tmp"))
    await storage.aclose()


async def test_the_etag_changes_when_the_bytes_change(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = "assets/etag.png"

    first = await storage.write(key, b"first")
    second = await storage.write(key, b"second")

    assert first.etag != second.etag


async def test_stat_reports_a_missing_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(FileNotFoundError):
        await storage.stat("assets/missing.png")


async def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = storage_key_for(user_id=uuid4(), asset_id=uuid4(), extension="png")
    await storage.write(key, b"bytes")

    await storage.delete(key)
    await storage.delete(key)

    with pytest.raises(FileNotFoundError):
        await storage.stat(key)


async def test_a_range_streams_only_the_requested_bytes(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    await storage.write("assets/range.bin", b"0123456789")

    chunks = [chunk async for chunk in storage.iter_range("assets/range.bin", start=3, end=7)]

    assert b"".join(chunks) == b"3456"
    assert all(len(chunk) <= 64 * 1024 for chunk in chunks)


async def test_an_open_ended_range_reads_to_the_end(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    await storage.write("assets/range.bin", b"0123456789")

    chunks = [chunk async for chunk in storage.iter_range("assets/range.bin", start=8)]

    assert b"".join(chunks) == b"89"


async def test_a_root_itself_is_not_a_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    (tmp_path / "assets").mkdir()

    with pytest.raises(StorageError):
        await storage.stat(".")
    with pytest.raises(FileNotFoundError):
        await storage.stat("assets")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/etc/passwd",
        "\\windows\\system32\\config",
        "../escape.txt",
        "assets/../../escape.txt",
        "assets/../assets/escape.txt",
        "assets\\evil.png",
        "assets/\x00evil.png",
    ],
)
def test_unsafe_keys_are_rejected(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(StorageError):
        storage.path_for(key)


async def test_a_symlink_out_of_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"not yours")
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "link.png").symlink_to(outside / "secret.png")

    storage = LocalStorage(root)

    with pytest.raises(StorageError):
        await storage.stat("assets/link.png")


async def test_a_symlink_inside_the_root_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.png").write_bytes(b"fine")
    (root / "link.png").symlink_to(root / "real.png")

    stored = await LocalStorage(root).stat("link.png")

    assert stored.size_bytes == 4


async def test_delete_ignores_an_unsafe_key(tmp_path: Path) -> None:
    """Cleanup must not raise on a key the adapter already refuses to resolve."""
    await LocalStorage(tmp_path).delete("../outside.png")


def test_the_factory_builds_local_storage() -> None:
    from app.core.config import Settings
    from app.storage import build_storage

    settings = Settings(
        _env_file=None,
        app_env="test",
        app_secret="test-secret",
        database_url="postgresql+asyncpg://localhost/nero_test",
        storage_dir="/tmp/nero-storage-test",
    )

    storage = build_storage(settings)

    assert isinstance(storage, LocalStorage)
    assert storage.path_for("assets/a.png") == Path("/tmp/nero-storage-test/assets/a.png")


async def test_the_factory_builds_s3_storage() -> None:
    from app.core.config import Settings
    from app.storage import build_storage
    from app.storage.s3 import S3Storage

    settings = Settings(
        _env_file=None,
        app_env="test",
        app_secret="test-secret",
        database_url="postgresql+asyncpg://localhost/nero_test",
        storage_backend="s3",
        s3_bucket="nero-assets",
        s3_access_key_id="key",
        s3_secret_access_key="secret",
        s3_prefix="nero",
    )

    storage = build_storage(settings)

    assert isinstance(storage, S3Storage)
    await storage.aclose()


def test_the_factory_refuses_incomplete_s3_settings() -> None:
    from app.core.config import Settings
    from app.storage import build_storage

    settings = Settings.model_construct(storage_backend="s3", storage_dir=Path("/tmp"))

    with pytest.raises(RuntimeError):
        build_storage(settings)
