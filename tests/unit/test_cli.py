"""Tests for the operational entry point."""

from __future__ import annotations

import asyncio

import pytest

from app.cli import (
    PROVIDER_NOT_CONFIGURED,
    USAGE,
    main,
    provider_check,
    run_provider_check,
)
from app.providers.base import ChatUsage, ProviderTimeoutError
from app.providers.fake import FakeChatProvider
from tests.support import make_settings

CHAT_MODEL = "deepseek-v4.1-flash"


def test_an_unknown_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["definitely-not-a-command"])

    assert exit_code == 2
    assert capsys.readouterr().err.strip() == USAGE


def test_no_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    assert exit_code == 2
    assert USAGE in capsys.readouterr().err


def test_usage_mentions_provider_check() -> None:
    assert "provider-check" in USAGE


async def test_provider_check_prints_model_status_and_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeChatProvider(reply="Hello there, how can I help you today?")

    exit_code = await run_provider_check(provider, model_id=CHAT_MODEL)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"model={CHAT_MODEL}" in out
    assert "status=ok" in out
    assert "prompt_tokens=" in out
    assert "completion_tokens=" in out
    assert "latency_ms=" in out


async def test_provider_check_reports_unknown_usage_when_the_provider_omits_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeChatProvider(reply="hi")

    await run_provider_check(provider, model_id=CHAT_MODEL)

    assert "prompt_tokens=unknown completion_tokens=unknown" in capsys.readouterr().out


async def test_provider_check_reports_reported_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeChatProvider(reply="hi", usage=ChatUsage(prompt_tokens=5, completion_tokens=9))

    await run_provider_check(provider, model_id=CHAT_MODEL)

    assert "prompt_tokens=5 completion_tokens=9" in capsys.readouterr().out


async def test_provider_check_never_prints_the_completion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeChatProvider(reply="a-secret-completion")

    await run_provider_check(provider, model_id=CHAT_MODEL)

    captured = capsys.readouterr()
    assert "a-secret-completion" not in captured.out
    assert "a-secret-completion" not in captured.err


async def test_provider_check_prints_an_error_code_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeChatProvider(
        reply="partial answer that must not leak",
        fail_with=ProviderTimeoutError("the upstream took too long"),
    )

    exit_code = await run_provider_check(provider, model_id=CHAT_MODEL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status=error" in captured.out
    assert "code=provider_timeout" in captured.out
    assert "partial answer that must not leak" not in captured.out
    assert "the upstream took too long" not in captured.out


def test_provider_check_without_configuration_explains_and_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.cli.get_settings", make_settings)

    exit_code = asyncio.run(provider_check())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert PROVIDER_NOT_CONFIGURED in captured.err


def test_provider_check_with_configured_provider_closes_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeChatProvider(reply="hi")
    settings = make_settings(
        ai_base_url="https://gateway.example.test/v1",
        ai_api_key="a-key",
        ai_chat_model=CHAT_MODEL,
    )
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.build_chat_provider", lambda _settings: provider)

    exit_code = asyncio.run(provider_check())

    assert exit_code == 0
    assert provider.closed is True
    assert "a-key" not in capsys.readouterr().out


def test_main_dispatches_provider_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_provider_check() -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr("app.cli.provider_check", fake_provider_check)

    assert main(["provider-check"]) == 0
    assert calls == 1
