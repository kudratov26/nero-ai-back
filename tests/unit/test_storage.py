"""Local storage adapter tests.

These are the adversarial cases: a clean key is boring, so every test here asks
the adapter to escape the root one way or another and expects a refusal.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.storage.local import LocalStorage, StorageError, storage_key_for


def test_storage_key_matches_the_documented_shape() -> None:
    user_id = uuid4()
    asset_id = uuid4()

    key = storage_key_for(user_id=user_id, asset_id=asset_id, extension="png")

    assert key == f"assets/{user_id}/{asset_id}.png"


def test_write_then_stat_round_trips(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = storage_key_for(user_id=uuid4(), asset_id=uuid4(), extension="png")

    written = storage.write(key, b"asset bytes")

    assert written.path == (tmp_path / key).resolve()
    assert written.size_bytes == 11
    assert storage.stat(key) == written
    assert written.path.read_bytes() == b"asset bytes"
    assert not list(written.path.parent.glob("*.tmp"))


def test_stat_reports_a_missing_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(FileNotFoundError):
        storage.stat("assets/missing.png")


def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = storage_key_for(user_id=uuid4(), asset_id=uuid4(), extension="png")
    storage.write(key, b"bytes")

    storage.delete(key)
    storage.delete(key)

    with pytest.raises(FileNotFoundError):
        storage.stat(key)


def test_a_root_itself_is_not_a_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    (tmp_path / "assets").mkdir()

    with pytest.raises(StorageError):
        storage.stat(".")
    with pytest.raises(FileNotFoundError):
        storage.stat("assets")


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


def test_a_symlink_out_of_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"not yours")
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "link.png").symlink_to(outside / "secret.png")

    storage = LocalStorage(root)

    with pytest.raises(StorageError):
        storage.stat("assets/link.png")


def test_a_symlink_inside_the_root_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.png").write_bytes(b"fine")
    (root / "link.png").symlink_to(root / "real.png")

    stored = LocalStorage(root).stat("link.png")

    assert stored.size_bytes == 4


def test_delete_ignores_an_unsafe_key(tmp_path: Path) -> None:
    """Cleanup must not raise on a key the adapter already refuses to resolve."""
    LocalStorage(tmp_path).delete("../outside.png")
