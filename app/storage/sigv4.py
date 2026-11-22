"""AWS Signature Version 4 for the small S3 subset the app needs.

Only header-signed requests are produced: PUT, HEAD, GET, and DELETE with no
query parameters. That is enough for an object store and avoids pulling in the
AWS SDK and its synchronous credential chain.

The implementation follows the published SigV4 algorithm. A known AWS example
is asserted in `tests/unit/test_sigv4.py`, so a change to canonicalization
cannot silently break every storage call.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _normalize_header(value: str) -> str:
    """Collapse sequential whitespace, which SigV4 requires for signed values."""
    return " ".join(value.split())


def _canonical_query(raw_query: str) -> str:
    if not raw_query:
        return ""
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    encoded = sorted((quote(key, safe="~"), quote(value, safe="~")) for key, value in pairs)
    return "&".join(f"{key}={value}" for key, value in encoded)


def sign_headers(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime,
) -> dict[str, str]:
    """Return `headers` plus an `Authorization` header for one request.

    `url` must already have a percent-encoded path. The caller owns the URL
    builder, so the canonical URI and the transmitted path cannot diverge.
    """
    parts = urlsplit(url)
    canonical_uri = parts.path or "/"
    canonical_query = _canonical_query(parts.query)
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    signed_headers = {name.lower(): _normalize_header(value) for name, value in headers.items()}
    signed_headers.setdefault("host", parts.netloc)
    signed_headers.setdefault("x-amz-date", amz_date)
    signed_headers.setdefault("x-amz-content-sha256", payload_hash)

    names = sorted(signed_headers)
    canonical_headers = "".join(f"{name}:{signed_headers[name]}\n" for name in names)
    signed_names = ";".join(names)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_names,
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode()),
        ]
    )

    date_key = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, service)
    signing_key = _hmac(service_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    result = dict(headers)
    result.update(signed_headers)
    result["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return result
