"""Staging smoke test.

Run this after a release, from outside the private network:

```bash
python -m scripts.smoke --base-url https://staging.example.com \
    --origin https://staging.example.com
```

It checks the HTTP edges a deploy can break: liveness, readiness, Prometheus
metrics, the seeded catalog, authentication, and a ranged download of a seeded
asset. The asset check matters because it exercises whichever storage backend is
configured; a bad bucket or missing volume fails here rather than in the browser.

The default flow signs up a throwaway account. Signup is rate-limited per IP, so
repeated runs should use a dedicated account through `SMOKE_EMAIL` and
`SMOKE_PASSWORD`. Neither value is printed, and the password never appears in
process arguments.

Exit codes: `0` every check passed, `1` a check failed, `2` invalid arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

SMOKE_PASSWORD = "smoke-test-password-not-a-user-secret"
RANGE_BYTES = 4


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


class SmokeFailure(Exception):
    """One check failed; the message is safe to print."""


@dataclass
class SmokeConfig:
    base_url: str
    origin: str
    insecure: bool = False
    timeout: float = 15.0
    email: str | None = None
    password: str | None = None


def _expect(response: httpx.Response, status: int, *, check: str) -> httpx.Response:
    if response.status_code != status:
        code = ""
        with contextlib.suppress(ValueError):
            code = response.json().get("code", "")
        suffix = f" code={code}" if code else ""
        raise SmokeFailure(f"{check}: expected {status}, got {response.status_code}{suffix}")
    return response


def _json_object(response: httpx.Response, *, check: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise SmokeFailure(f"{check}: response is not JSON") from None
    if not isinstance(body, dict):
        raise SmokeFailure(f"{check}: expected a JSON object")
    return body


def _json_list(response: httpx.Response, *, check: str) -> list[Any]:
    try:
        body = response.json()
    except ValueError:
        raise SmokeFailure(f"{check}: response is not JSON") from None
    if not isinstance(body, list):
        raise SmokeFailure(f"{check}: expected a JSON array")
    return body


async def _check_health(client: httpx.AsyncClient) -> list[Check]:
    checks: list[Check] = []
    live = _expect(await client.get("/health/live"), 200, check="health/live")
    if _json_object(live, check="health/live").get("status") != "ok":
        raise SmokeFailure("health/live: status is not ok")
    checks.append(Check("health/live", True))

    ready = _expect(await client.get("/health/ready"), 200, check="health/ready")
    if _json_object(ready, check="health/ready").get("status") != "ready":
        raise SmokeFailure("health/ready: status is not ready")
    checks.append(Check("health/ready", True))
    return checks


async def _check_metrics(client: httpx.AsyncClient) -> Check:
    response = _expect(await client.get("/metrics"), 200, check="metrics")
    if "http_requests_total" not in response.text:
        raise SmokeFailure("metrics: http_requests_total is missing")
    if not response.headers.get("content-type", "").startswith("text/plain"):
        raise SmokeFailure("metrics: unexpected content type")
    return Check("metrics", True)


async def _check_catalog(client: httpx.AsyncClient) -> list[Check]:
    plans = _json_list(
        _expect(await client.get("/v1/plans"), 200, check="plans"),
        check="plans",
    )
    if not plans:
        raise SmokeFailure("plans: the catalog is empty; did the release seed run?")
    models = _json_list(
        _expect(await client.get("/v1/models"), 200, check="models"),
        check="models",
    )
    return [Check("plans", True), Check("models", True, f"{len(models)} available")]


async def _sign_in(client: httpx.AsyncClient, config: SmokeConfig) -> Check:
    if config.email is not None:
        payload = {"email": config.email, "password": config.password or ""}
        _expect(
            await client.post("/v1/auth/login", json=payload),
            200,
            check="auth/login",
        )
        return Check("auth/login", True)
    email = f"smoke-{uuid.uuid4().hex}@example.com"
    _expect(
        await client.post(
            "/v1/auth/signup",
            json={"email": email, "password": SMOKE_PASSWORD, "displayName": "Smoke"},
        ),
        201,
        check="auth/signup",
    )
    return Check("auth/signup", True)


async def _check_session(client: httpx.AsyncClient) -> list[Check]:
    me = _json_object(
        _expect(await client.get("/v1/auth/me"), 200, check="auth/me"),
        check="auth/me",
    )
    if not me.get("user", {}).get("id"):
        raise SmokeFailure("auth/me: no user id")
    credits = _json_object(
        _expect(await client.get("/v1/credits"), 200, check="credits"),
        check="credits",
    )
    if not isinstance(credits.get("balance"), int):
        raise SmokeFailure("credits: balance is not an integer")
    return [Check("auth/me", True), Check("credits", True)]


async def _check_asset(client: httpx.AsyncClient) -> Check:
    page = _json_object(
        _expect(
            await client.get("/v1/explore/posts", params={"limit": 20}),
            200,
            check="explore",
        ),
        check="explore",
    )
    items = page.get("items")
    if not isinstance(items, list) or not items:
        raise SmokeFailure("explore: no published posts; did the release seed run?")
    asset = next(
        (item.get("asset") for item in items if isinstance(item, dict) and item.get("asset")),
        None,
    )
    if not isinstance(asset, dict) or not asset.get("url"):
        raise SmokeFailure("explore: no post carries an asset")
    url = str(asset["url"])
    response = await client.get(url, headers={"range": f"bytes=0-{RANGE_BYTES - 1}"})
    _expect(response, 206, check="asset")
    if len(response.content) != RANGE_BYTES:
        raise SmokeFailure("asset: ranged response has the wrong length")
    if "content-range" not in response.headers:
        raise SmokeFailure("asset: Content-Range is missing")
    return Check("asset", True)


async def _sign_out(client: httpx.AsyncClient) -> Check:
    _expect(await client.post("/v1/auth/logout"), 204, check="auth/logout")
    return Check("auth/logout", True)


async def run_smoke(
    config: SmokeConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Check]:
    """Run every check in order. Raises `SmokeFailure` on the first failure."""
    checks: list[Check] = []
    async with httpx.AsyncClient(
        base_url=config.base_url,
        headers={"origin": config.origin},
        timeout=config.timeout,
        verify=not config.insecure,
        follow_redirects=True,
        transport=transport,
    ) as client:
        checks.extend(await _check_health(client))
        checks.append(await _check_metrics(client))
        checks.extend(await _check_catalog(client))
        checks.append(await _sign_in(client, config))
        checks.extend(await _check_session(client))
        checks.append(await _check_asset(client))

        if config.email is None:
            # A throwaway account's workspace is empty; reset is still a useful
            # check that the authenticated write path and its Origin guard work.
            reset = await client.post("/v1/demo/reset")
            _expect(reset, 204, check="demo/reset")
            checks.append(Check("demo/reset", True))

        checks.append(await _sign_out(client))
    return checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.smoke")
    parser.add_argument("--base-url", required=True, help="Public API URL to test.")
    parser.add_argument(
        "--origin",
        default=None,
        help="Allowed frontend Origin. Defaults to --base-url.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (local Caddy CA only).",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = SmokeConfig(
        base_url=args.base_url.rstrip("/"),
        origin=args.origin or args.base_url.rstrip("/"),
        insecure=args.insecure,
        timeout=args.timeout,
        email=os.environ.get("SMOKE_EMAIL") or None,
        password=os.environ.get("SMOKE_PASSWORD") or None,
    )
    if config.email is not None and config.password is None:
        print("smoke: SMOKE_EMAIL is set but SMOKE_PASSWORD is not", file=sys.stderr)
        return 2
    try:
        checks = asyncio.run(run_smoke(config))
    except SmokeFailure as exc:
        print(f"smoke: failed: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"smoke: transport failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    for check in checks:
        suffix = f" ({check.detail})" if check.detail else ""
        print(f"smoke: {check.name} ok{suffix}")
    print(f"smoke: ok ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
