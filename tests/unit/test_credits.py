"""Unit tests for the pure parts of the Credit ledger."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.db.models import CreditAccount
from app.db.models.common import utcnow
from app.domain.credits import available


def make_account(balance: int, held: int) -> CreditAccount:
    now = utcnow()
    return CreditAccount(
        user_id=uuid4(),
        plan_id="free",
        balance=balance,
        held=held,
        allowance=balance,
        period_start=now,
        period_end=now + timedelta(days=30),
    )


@pytest.mark.parametrize(
    ("balance", "held", "expected"),
    [(50, 0, 50), (50, 20, 30), (0, 0, 0), (7, 7, 0), (1, 0, 1)],
)
def test_available_is_balance_minus_held(balance: int, held: int, expected: int) -> None:
    assert available(make_account(balance, held)) == expected
