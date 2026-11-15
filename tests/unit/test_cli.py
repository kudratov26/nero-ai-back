"""Tests for the operational entry point."""

from __future__ import annotations

import pytest

from app.cli import USAGE, main


def test_an_unknown_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["definitely-not-a-command"])

    assert exit_code == 2
    assert capsys.readouterr().err.strip() == USAGE


def test_no_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    assert exit_code == 2
    assert USAGE in capsys.readouterr().err
