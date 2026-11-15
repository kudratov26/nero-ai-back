"""Auth endpoint integration tests against PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import hash_password, hash_session_token
from app.db.models import CreditAccount, CreditLedgerEntry, User, UserSession
from app.db.session import create_session_factory
from app.domain import auth as auth_service
from app.main import create_app
from tests.support import PASSWORD, asgi_client, assert_problem, make_settings, post_signup

EXPECTED_COOKIE_MAX_AGE = 30 * 24 * 60 * 60


@pytest.fixture
async def client(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(create_app(make_settings())) as http:
        yield http


async def session_rows(db_session: AsyncSession, user_id: UUID) -> list[UserSession]:
    db_session.expire_all()
    rows = await db_session.scalars(
        select(UserSession).where(UserSession.user_id == user_id).order_by(UserSession.created_at)
    )
    return list(rows)


async def test_signup_provisions_account_and_session(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await post_signup(client, "Signup@Example.com")

    assert response.status_code == 201
    body = response.json()
    user_id = UUID(body["user"]["id"])
    assert body["user"] == {
        "id": body["user"]["id"],
        "email": "signup@example.com",
        "displayName": "Creator",
        "role": "user",
        "createdAt": body["user"]["createdAt"],
    }
    assert body["user"]["createdAt"].endswith("Z")
    assert body["plan"] == {"id": "free", "name": "Free", "monthlyCredits": 50}
    assert set(body["credits"]) == {
        "balance",
        "held",
        "available",
        "allowance",
        "periodStart",
        "periodEnd",
    }
    assert body["credits"]["balance"] == 50
    assert body["credits"]["held"] == 0
    assert body["credits"]["available"] == 50
    assert body["credits"]["allowance"] == 50
    period_start = datetime.fromisoformat(body["credits"]["periodStart"])
    period_end = datetime.fromisoformat(body["credits"]["periodEnd"])
    assert period_end - period_start == timedelta(days=30)

    cookie = response.headers["set-cookie"]
    lowered = cookie.lower()
    assert cookie.startswith("nero_session=")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert f"max-age={EXPECTED_COOKIE_MAX_AGE}" in lowered
    assert "secure" not in lowered

    token = client.cookies.get("nero_session")
    assert token
    assert body["user"]["id"] not in token

    stored = await db_session.get(UserSession, (await session_rows(db_session, user_id))[0].id)
    assert stored.token_hash == hash_session_token(str(token))
    account = await db_session.get(CreditAccount, user_id)
    assert account is not None
    assert (account.balance, account.held, account.allowance) == (50, 0, 50)
    ledger = await db_session.scalar(
        select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user_id)
    )
    assert ledger is not None
    assert (ledger.kind, ledger.delta_balance, ledger.balance_after) == ("grant", 50, 50)
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert user.last_login_at is None

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == body["user"]["id"]


async def test_signup_rejects_duplicate_email_case_insensitively(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    assert (await post_signup(client, "duplicate@example.com")).status_code == 201

    response = await post_signup(client, "DUPLICATE@example.com")

    body = assert_problem(response, 409, "email_taken")
    assert body["detail"] == "That email is already registered."
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "duplicate@example.com")
    )
    assert count == 1


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("weak-short@example.com", "short"),
        ("weak-long@example.com", "x" * 129),
        ("weak-email@example.com", "weak-email@example.com"),
        ("weak-common@example.com", "password123"),
    ],
)
async def test_signup_rejects_weak_password(
    client: httpx.AsyncClient, email: str, password: str
) -> None:
    response = await post_signup(client, email, password=password)

    body = assert_problem(response, 422, "weak_password")
    assert body["detail"]
    assert password not in response.text


async def test_signup_defaults_display_name_to_email_local_part(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={"email": "local.part@example.com", "password": PASSWORD},
    )

    assert response.status_code == 201
    assert response.json()["user"]["displayName"] == "local.part"


async def test_signup_validation_does_not_echo_input(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={"email": "not-an-email", "password": PASSWORD},
    )

    assert_problem(response, 400, "invalid_request")
    assert "not-an-email" not in response.text


async def test_login_rotates_the_current_session(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    signup = await post_signup(client, "rotate@example.com")
    old_token = str(client.cookies.get("nero_session"))
    user_id = UUID(signup.json()["user"]["id"])

    response = await client.post(
        "/v1/auth/login",
        json={"email": "rotate@example.com", "password": PASSWORD},
    )

    assert response.status_code == 200
    new_token = str(client.cookies.get("nero_session"))
    assert new_token != old_token
    rows = await session_rows(db_session, user_id)
    assert len(rows) == 2
    old_row = next(row for row in rows if row.token_hash == hash_session_token(old_token))
    new_row = next(row for row in rows if row.token_hash == hash_session_token(new_token))
    assert old_row.revoked_at is not None
    assert new_row.revoked_at is None
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.last_login_at is not None

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == str(user_id)


async def test_login_errors_are_generic(
    client: httpx.AsyncClient,
) -> None:
    await post_signup(client, "generic@example.com")
    client.cookies.clear()

    wrong_password = await client.post(
        "/v1/auth/login",
        json={"email": "generic@example.com", "password": "definitely wrong"},
    )
    unknown_email = await client.post(
        "/v1/auth/login",
        json={"email": "missing@example.com", "password": PASSWORD},
    )

    wrong_body = assert_problem(wrong_password, 401, "invalid_credentials")
    unknown_body = assert_problem(unknown_email, 401, "invalid_credentials")
    assert wrong_body["detail"] == unknown_body["detail"]
    assert wrong_body["detail"] == "Email or password is incorrect."
    assert "generic@example.com" not in wrong_password.text
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_email.headers


async def test_login_without_a_current_session(
    client: httpx.AsyncClient,
) -> None:
    await post_signup(client, "fresh-login@example.com")
    client.cookies.clear()

    response = await client.post(
        "/v1/auth/login",
        json={"email": "fresh-login@example.com", "password": PASSWORD},
    )

    assert response.status_code == 200
    assert client.cookies.get("nero_session")


async def test_login_ignores_an_unknown_current_session(
    client: httpx.AsyncClient,
) -> None:
    await post_signup(client, "stale-cookie@example.com")
    client.cookies.clear()
    client.cookies.set("nero_session", "not-a-real-token")

    response = await client.post(
        "/v1/auth/login",
        json={"email": "stale-cookie@example.com", "password": PASSWORD},
    )

    assert response.status_code == 200
    issued = response.headers["set-cookie"]
    assert issued.startswith("nero_session=")
    assert "not-a-real-token" not in issued


async def test_login_fails_closed_when_account_is_missing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    email = "no-account@example.com"
    async with create_session_factory(engine)() as session:
        session.add(
            User(
                email=email,
                display_name="No Account",
                password_hash=hash_password(PASSWORD),
            )
        )
        await session.commit()

    response = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})

    assert_problem(response, 500, "internal_error")
    assert email not in response.text


async def test_login_rejects_disabled_account(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    signup = await post_signup(client, "disabled@example.com")
    user_id = UUID(signup.json()["user"]["id"])
    async with engine.begin() as connection:
        await connection.execute(update(User).where(User.id == user_id).values(status="disabled"))

    response = await client.post(
        "/v1/auth/login",
        json={"email": "disabled@example.com", "password": PASSWORD},
    )

    assert_problem(response, 401, "invalid_credentials")
    me = await client.get("/v1/auth/me")
    body = assert_problem(me, 401, "unauthorized")
    assert body["detail"] == "Authentication required."


async def test_logout_revokes_session_and_clears_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    signup = await post_signup(client, "logout@example.com")
    user_id = UUID(signup.json()["user"]["id"])

    response = await client.post("/v1/auth/logout")

    assert response.status_code == 204
    assert response.content == b""
    lowered = response.headers["set-cookie"].lower()
    assert 'nero_session=""' in lowered
    assert "max-age=0" in lowered
    assert "httponly" in lowered
    assert client.cookies.get("nero_session") in (None, "")

    me = await client.get("/v1/auth/me")
    assert_problem(me, 401, "unauthorized")
    rows = await session_rows(db_session, user_id)
    assert len(rows) == 1
    assert rows[0].revoked_at is not None

    again = await client.post("/v1/auth/logout")
    assert again.status_code == 204


async def test_me_requires_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/auth/me")

    body = assert_problem(response, 401, "unauthorized")
    assert body["detail"] == "Authentication required."


async def test_logout_with_an_unknown_token_is_a_noop(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set("nero_session", "not-a-real-token")

    response = await client.post("/v1/auth/logout")

    assert response.status_code == 204
    lowered = response.headers["set-cookie"].lower()
    assert 'nero_session=""' in lowered
    assert "max-age=0" in lowered
    assert "httponly" in lowered


async def test_me_rejects_expired_session(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    signup = await post_signup(client, "expired@example.com")
    user_id = UUID(signup.json()["user"]["id"])
    now = datetime.now(tz=UTC)
    async with engine.begin() as connection:
        await connection.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(
                created_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )

    response = await client.get("/v1/auth/me")

    assert_problem(response, 401, "unauthorized")


async def test_me_refreshes_last_used_at_at_most_once_per_five_minutes(
    client: httpx.AsyncClient, engine: AsyncEngine, db_session: AsyncSession
) -> None:
    signup = await post_signup(client, "lastused@example.com")
    user_id = UUID(signup.json()["user"]["id"])
    stale = datetime.now(tz=UTC) - timedelta(minutes=10)
    async with engine.begin() as connection:
        await connection.execute(
            update(UserSession).where(UserSession.user_id == user_id).values(last_used_at=stale)
        )

    assert (await client.get("/v1/auth/me")).status_code == 200
    refreshed = (await session_rows(db_session, user_id))[0].last_used_at
    assert refreshed > stale

    assert (await client.get("/v1/auth/me")).status_code == 200
    unchanged = (await session_rows(db_session, user_id))[0].last_used_at
    assert unchanged == refreshed


async def test_signup_rolls_back_when_provisioning_fails(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_after_flush(session: AsyncSession, **_: Any) -> None:
        await session.flush()
        raise RuntimeError("provisioning failed")

    async def counts() -> tuple[int, int, int]:
        return (
            await db_session.scalar(select(func.count()).select_from(User)) or 0,
            await db_session.scalar(select(func.count()).select_from(CreditAccount)) or 0,
            await db_session.scalar(select(func.count()).select_from(CreditLedgerEntry)) or 0,
        )

    before = await counts()
    monkeypatch.setattr(auth_service, "_issue_session", fail_after_flush)

    response = await post_signup(client, "rollback@example.com")

    body = assert_problem(response, 500, "internal_error")
    assert body["detail"] == "An unexpected error occurred."
    assert "rollback@example.com" not in response.text
    assert await counts() == before
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "rollback@example.com")
    )
    assert count == 0
