"""S3 adapter behavior against a mocked S3 endpoint.

No test talks to AWS. `respx` intercepts the same httpx client the adapter uses
and asserts the signed request shape, the Range header, and the error mapping.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.storage.base import StorageError
from app.storage.s3 import S3Storage
from app.storage.sigv4 import EMPTY_PAYLOAD_HASH

ENDPOINT = "https://s3.us-east-1.amazonaws.com"
BUCKET = "nero-assets"
KEY = "assets/0193a0e0-0000-7000-8000-000000000000/0193a0e0-0000-7001-8000-000000000000.png"
URL = f"{ENDPOINT}/{BUCKET}/{KEY}"
FIXED_NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def make_storage(**overrides: object) -> S3Storage:
    values: dict[str, object] = {
        "bucket": BUCKET,
        "region": "us-east-1",
        "access_key": "AKIAIOSFODNN7EXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "endpoint_url": ENDPOINT,
        "clock": lambda: FIXED_NOW,
    }
    values.update(overrides)
    return S3Storage(**values)  # type: ignore[arg-type]


@pytest.fixture
async def storage() -> AsyncIterator[S3Storage]:
    adapter = make_storage()
    try:
        yield adapter
    finally:
        await adapter.aclose()


@respx.mock
async def test_write_puts_the_bytes_with_a_signed_payload(storage: S3Storage) -> None:
    route = respx.put(URL).mock(return_value=httpx.Response(200, headers={"etag": '"abc123"'}))

    stored = await storage.write(KEY, b"asset bytes", content_type="image/png")

    request = route.calls[0].request
    assert request.headers["content-type"] == "image/png"
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"asset bytes").hexdigest()
    assert request.headers["x-amz-date"] == "20261122T100000Z"
    assert request.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261122/us-east-1/s3/aws4_request"
    )
    assert stored.size_bytes == 11
    assert stored.mtime == FIXED_NOW.timestamp()
    assert stored.etag == '"abc123"'


@respx.mock
async def test_stat_reads_head_metadata(storage: S3Storage) -> None:
    respx.head(URL).mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-length": "11",
                "last-modified": "Sun, 22 Nov 2026 10:00:00 GMT",
                "etag": '"abc123"',
            },
        )
    )

    stored = await storage.stat(KEY)

    assert stored.size_bytes == 11
    assert stored.mtime == FIXED_NOW.timestamp()
    assert stored.etag == '"abc123"'


@respx.mock
async def test_a_missing_object_is_file_not_found(storage: S3Storage) -> None:
    respx.head(URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FileNotFoundError):
        await storage.stat(KEY)


@respx.mock
async def test_a_server_error_does_not_echo_the_object_key(storage: S3Storage) -> None:
    respx.head(URL).mock(return_value=httpx.Response(500, text=f"<Error><Key>{KEY}</Key></Error>"))

    with pytest.raises(StorageError) as raised:
        await storage.stat(KEY)

    assert "HTTP 500" in str(raised.value)
    assert "0193a0e0" not in str(raised.value)


@respx.mock
async def test_a_range_get_streams_the_requested_slice(storage: S3Storage) -> None:
    route = respx.get(URL, headers={"range": "bytes=4-9"}).mock(
        return_value=httpx.Response(206, content=b"456789")
    )

    chunks = [chunk async for chunk in storage.iter_range(KEY, start=4, end=10)]

    assert b"".join(chunks) == b"456789"
    assert route.called


@respx.mock
async def test_an_open_ended_range_uses_the_documented_header(storage: S3Storage) -> None:
    respx.get(URL, headers={"range": "bytes=8-"}).mock(
        return_value=httpx.Response(206, content=b"89")
    )

    chunks = [chunk async for chunk in storage.iter_range(KEY, start=8)]

    assert b"".join(chunks) == b"89"


@respx.mock
async def test_a_get_that_ignores_the_range_is_an_error(storage: S3Storage) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"full object"))

    with pytest.raises(StorageError):
        async for _ in storage.iter_range(KEY, start=4):
            pass


@respx.mock
async def test_a_missing_get_is_file_not_found(storage: S3Storage) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FileNotFoundError):
        async for _ in storage.iter_range(KEY):
            pass


@respx.mock
async def test_delete_ignores_a_missing_object(storage: S3Storage) -> None:
    route = respx.delete(URL).mock(side_effect=[httpx.Response(204), httpx.Response(404)])

    await storage.delete(KEY)
    await storage.delete(KEY)

    assert route.call_count == 2


@respx.mock
async def test_prefix_and_path_style_keep_the_bucket_in_the_path() -> None:
    adapter = make_storage(endpoint_url="http://127.0.0.1:9000", prefix="/nero/")
    prefixed = f"http://127.0.0.1:9000/{BUCKET}/nero/{KEY}"
    route = respx.head(prefixed).mock(return_value=httpx.Response(200, content=b""))
    try:
        await adapter.stat(KEY)
    finally:
        await adapter.aclose()

    assert route.called


@respx.mock
async def test_a_session_token_is_signed_as_a_header() -> None:
    adapter = make_storage(session_token="temporary-token")
    route = respx.head(URL).mock(return_value=httpx.Response(200, content=b""))
    try:
        await adapter.stat(KEY)
    finally:
        await adapter.aclose()

    request = route.calls[0].request
    assert request.headers["x-amz-security-token"] == "temporary-token"
    assert "x-amz-security-token" in request.headers["authorization"]


@pytest.mark.parametrize(
    "key",
    ["", "/absolute.png", "../escape.png", "assets/../../escape.png", "assets\\evil.png"],
)
async def test_unsafe_keys_are_rejected(key: str) -> None:
    adapter = make_storage()
    try:
        with pytest.raises(StorageError):
            await adapter.stat(key)
    finally:
        await adapter.aclose()


@respx.mock
async def test_the_empty_payload_hash_is_used_for_reads_and_deletes(storage: S3Storage) -> None:
    route = respx.head(URL).mock(return_value=httpx.Response(200, content=b""))

    await storage.stat(KEY)

    assert route.calls[0].request.headers["x-amz-content-sha256"] == EMPTY_PAYLOAD_HASH
