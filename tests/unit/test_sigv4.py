"""SigV4 against a published AWS example.

The example is from the AWS S3 SigV4 documentation: a GET for `/test.txt` with
a `Range` header. Matching the documented signature proves canonicalization,
header ordering, and the signing-key derivation all agree with S3.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.storage.sigv4 import EMPTY_PAYLOAD_HASH, sign_headers

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_the_aws_documented_get_signature() -> None:
    headers = sign_headers(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        headers={
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_PAYLOAD_HASH,
            "x-amz-date": "20130524T000000Z",
        },
        payload_hash=EMPTY_PAYLOAD_HASH,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region="us-east-1",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )

    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        f"Credential={ACCESS_KEY}/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_header_whitespace_is_collapsed() -> None:
    common = {
        "method": "GET",
        "url": "https://examplebucket.s3.amazonaws.com/key",
        "payload_hash": EMPTY_PAYLOAD_HASH,
        "access_key": ACCESS_KEY,
        "secret_key": SECRET_KEY,
        "region": "us-east-1",
        "now": datetime(2026, 11, 22, 9, 30, tzinfo=UTC),
    }
    plain = sign_headers(headers={"x-amz-meta-note": "a b"}, **common)
    padded = sign_headers(headers={"x-amz-meta-note": "  a   b  "}, **common)

    assert plain["Authorization"] == padded["Authorization"]
