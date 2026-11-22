"""S3-compatible object storage.

The adapter speaks the small S3 REST subset the app needs: PUT, HEAD, GET with a
byte range, and DELETE. Requests are signed with SigV4 (`app.storage.sigv4`),
so the runtime has no AWS SDK dependency and no blocking credential chain.

Path-style URLs are used for every endpoint. AWS still accepts them, and MinIO,
Cloudflare R2, and other compatible stores expect or prefer them. A configured
prefix keeps Nero objects under one key namespace in a shared bucket.

S3 error bodies are never read or logged: they can echo the object key, and the
key names a user. Failures surface as `StorageError` with the HTTP status only.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import httpx

from app.storage.base import StorageError, StoredFile
from app.storage.sigv4 import EMPTY_PAYLOAD_HASH, sign_headers

CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def _quoted_etag(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith('"') else f'"{value}"'


def _mtime(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


class S3Storage:
    """An S3 bucket behind the same async port as local disk."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str,
        prefix: str = "",
        session_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._endpoint = endpoint_url.rstrip("/")
        self._prefix = prefix.strip("/")
        self._session_token = session_token
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )

    def _object_key(self, key: str) -> str:
        if not key or key.startswith("/") or "\x00" in key:
            raise StorageError("storage key must be relative")
        if "\\" in key or ".." in key.split("/"):
            raise StorageError("storage key must use safe forward-slash segments")
        return f"{self._prefix}/{key}" if self._prefix else key

    def _url(self, key: str) -> str:
        object_key = self._object_key(key)
        encoded = quote(object_key, safe="/-_.~")
        bucket = quote(self._bucket, safe="-_.~")
        return f"{self._endpoint}/{bucket}/{encoded}"

    def _signed(
        self,
        method: str,
        url: str,
        payload_hash: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "host": urlsplit(url).netloc,
            "x-amz-content-sha256": payload_hash,
        }
        if self._session_token:
            headers["x-amz-security-token"] = self._session_token
        headers.update(extra or {})
        return sign_headers(
            method=method,
            url=url,
            headers=headers,
            payload_hash=payload_hash,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self._region,
            now=self._clock(),
        )

    @staticmethod
    def _raise_for(response: httpx.Response, operation: str) -> None:
        if response.status_code >= 400:
            raise StorageError(f"S3 {operation} failed with HTTP {response.status_code}")

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredFile:
        url = self._url(key)
        headers = self._signed(
            "PUT",
            url,
            hashlib.sha256(data).hexdigest(),
            {"content-type": content_type or "application/octet-stream"},
        )
        response = await self._client.put(url, content=data, headers=headers)
        self._raise_for(response, "PUT")
        return StoredFile(
            size_bytes=len(data),
            mtime=self._clock().timestamp(),
            etag=_quoted_etag(response.headers.get("etag")),
        )

    async def stat(self, key: str) -> StoredFile:
        url = self._url(key)
        response = await self._client.head(
            url,
            headers=self._signed("HEAD", url, EMPTY_PAYLOAD_HASH),
        )
        if response.status_code == 404:
            raise FileNotFoundError(key)
        self._raise_for(response, "HEAD")
        return StoredFile(
            size_bytes=int(response.headers.get("content-length", "0")),
            mtime=_mtime(response.headers.get("last-modified")),
            etag=_quoted_etag(response.headers.get("etag")),
        )

    async def iter_range(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> AsyncIterator[bytes]:
        if start < 0 or (end is not None and end <= start):
            raise StorageError("invalid storage range")
        if start == 0 and end is None:
            range_value = "bytes=0-"
        elif end is None:
            range_value = f"bytes={start}-"
        else:
            range_value = f"bytes={start}-{end - 1}"

        url = self._url(key)
        headers = self._signed("GET", url, EMPTY_PAYLOAD_HASH, {"range": range_value})
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 404:
                raise FileNotFoundError(key)
            if response.status_code not in (200, 206):
                raise StorageError(f"S3 GET failed with HTTP {response.status_code}")
            if response.status_code == 200 and start != 0:
                raise StorageError("S3 ignored the requested byte range")
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                yield chunk

    async def delete(self, key: str) -> None:
        try:
            url = self._url(key)
        except StorageError:
            return
        response = await self._client.delete(
            url,
            headers=self._signed("DELETE", url, EMPTY_PAYLOAD_HASH),
        )
        if response.status_code == 404:
            return
        self._raise_for(response, "DELETE")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
