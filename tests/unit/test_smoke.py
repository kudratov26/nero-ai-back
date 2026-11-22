"""Smoke script checks against a mocked staging API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from scripts.smoke import (
    SMOKE_PASSWORD,
    SmokeConfig,
    SmokeFailure,
    main,
    run_smoke,
)


def config(**overrides: Any) -> SmokeConfig:
    values: dict[str, Any] = {
        "base_url": "https://staging.example.test",
        "origin": "https://staging.example.test",
    }
    values.update(overrides)
    return SmokeConfig(**values)


def staging_handler(
    *,
    fail_path: str | None = None,
    asset: bool = True,
    calls: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == fail_path:
            return httpx.Response(503, json={"code": "not_ready"})
        if path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if path == "/metrics":
            return httpx.Response(
                200,
                text="# TYPE http_requests_total counter\nhttp_requests_total 1\n",
                headers={"content-type": "text/plain; version=0.0.4"},
            )
        if path == "/v1/plans":
            return httpx.Response(200, json=[{"id": "free"}])
        if path == "/v1/models":
            return httpx.Response(200, json=[{"id": "deepseek-v4.1-flash"}])
        if path in {"/v1/auth/signup", "/v1/auth/login"}:
            return httpx.Response(201 if path.endswith("signup") else 200, json={})
        if path == "/v1/auth/me":
            return httpx.Response(200, json={"user": {"id": "user-1"}})
        if path == "/v1/credits":
            return httpx.Response(200, json={"balance": 50, "held": 0})
        if path == "/v1/explore/posts":
            item = {"id": "post-1", "asset": None}
            if asset:
                item["asset"] = {"id": "asset-1", "url": "/v1/assets/asset-1"}
            return httpx.Response(200, json={"items": [item], "nextCursor": None})
        if path == "/v1/assets/asset-1":
            return httpx.Response(
                206,
                content=b"1234",
                headers={"content-range": "bytes 0-3/100"},
            )
        if path in {"/v1/demo/reset"}:
            return httpx.Response(204)
        if path == "/v1/auth/logout":
            return httpx.Response(204)
        return httpx.Response(404, json={"code": "not_found"})

    return httpx.MockTransport(handler)


async def test_every_check_passes_against_a_healthy_staging_api() -> None:
    checks = await run_smoke(config(), transport=staging_handler())

    names = [check.name for check in checks]
    assert names == [
        "health/live",
        "health/ready",
        "metrics",
        "plans",
        "models",
        "auth/signup",
        "auth/me",
        "credits",
        "asset",
        "demo/reset",
        "auth/logout",
    ]
    assert all(check.ok for check in checks)


async def test_a_failed_readiness_check_stops_the_run() -> None:
    with pytest.raises(SmokeFailure) as raised:
        await run_smoke(config(), transport=staging_handler(fail_path="/health/ready"))

    assert "health/ready" in str(raised.value)
    assert "503" in str(raised.value)


async def test_a_missing_seed_asset_fails() -> None:
    with pytest.raises(SmokeFailure) as raised:
        await run_smoke(config(), transport=staging_handler(asset=False))

    assert "asset" in str(raised.value)


async def test_the_signup_sends_the_origin_header() -> None:
    calls: list[httpx.Request] = []
    await run_smoke(config(), transport=staging_handler(calls=calls))

    request = next(call for call in calls if call.url.path == "/v1/auth/signup")
    assert request.headers["origin"] == "https://staging.example.test"
    body = json.loads(request.content)
    assert body["password"] == SMOKE_PASSWORD
    assert body["email"].startswith("smoke-")


async def test_a_dedicated_account_logs_in_instead_of_signing_up() -> None:
    calls: list[httpx.Request] = []
    dedicated = config(email="smoke@example.com", password="a-dedicated-password")

    checks = await run_smoke(dedicated, transport=staging_handler(calls=calls))

    paths = [call.url.path for call in calls]
    assert "/v1/auth/login" in paths
    assert "/v1/auth/signup" not in paths
    assert "/v1/demo/reset" not in paths
    assert "auth/login" in [check.name for check in checks]


def test_main_reports_success(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(_config: SmokeConfig) -> list[Any]:
        return []

    monkeypatch.setattr("scripts.smoke.run_smoke", fake)

    assert main(["--base-url", "https://staging.example.test"]) == 0
    assert "smoke: ok" in capsys.readouterr().out


def test_main_reports_a_smoke_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(_config: SmokeConfig) -> list[Any]:
        raise SmokeFailure("health/ready: expected 200, got 503")

    monkeypatch.setattr("scripts.smoke.run_smoke", fake)

    assert main(["--base-url", "https://staging.example.test"]) == 1
    assert "health/ready" in capsys.readouterr().err


def test_main_reports_a_transport_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(_config: SmokeConfig) -> list[Any]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("scripts.smoke.run_smoke", fake)

    assert main(["--base-url", "https://staging.example.test"]) == 1
    assert "ConnectError" in capsys.readouterr().err


def test_main_requires_a_password_when_email_is_set(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMOKE_EMAIL", "smoke@example.com")
    monkeypatch.delenv("SMOKE_PASSWORD", raising=False)

    assert main(["--base-url", "https://staging.example.test"]) == 2
    assert "SMOKE_PASSWORD" in capsys.readouterr().err


def test_main_uses_the_base_url_as_the_default_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[SmokeConfig] = []

    async def fake(config: SmokeConfig) -> list[Any]:
        seen.append(config)
        return []

    monkeypatch.setattr("scripts.smoke.run_smoke", fake)

    assert main(["--base-url", "https://staging.example.test/"]) == 0
    assert seen[0].base_url == "https://staging.example.test"
    assert seen[0].origin == "https://staging.example.test"
