"""Session hardening: origin checks, rate limits, profile, password, sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.auth import LOGIN_ATTEMPT_LIMIT, SIGNUP_ATTEMPT_LIMIT
from app.core.security import verify_password
from app.db.models import User, UserSession
from app.main import create_app
from tests.support import (
    PASSWORD,
    TEST_ORIGIN,
    asgi_client,
    assert_problem,
    make_settings,
    post_signup,
)

NEW_PASSWORD = "a whole new battery horse"
SESSION_FIELDS = {
    "id",
    "userAgent",
    "ip",
    "createdAt",
    "lastUsedAt",
    "expiresAt",
    "current",
}


@pytest.fixture
async def client(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(create_app(make_settings())) as http:
        yield http


@pytest.fixture
async def other_device(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    """A second app with its own cookie jar, standing in for another browser."""
    async with asgi_client(
        create_app(make_settings()),
        headers={"user-agent": "other-device"},
    ) as http:
        yield http


def session_ids(items: list[dict[str, object]]) -> list[str]:
    return [str(item["id"]) for item in items]


# --- Origin checks -----------------------------------------------------------


async def test_signup_without_an_origin_is_rejected(
    migrated_database: str, db_session: AsyncSession
) -> None:
    async with asgi_client(create_app(make_settings()), origin=None) as bare:
        response = await post_signup(bare, "no-origin@example.com")

    body = assert_problem(response, 403, "origin_not_allowed")
    assert body["detail"]
    assert "set-cookie" not in response.headers
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "no-origin@example.com")
    )
    assert count == 0


async def test_signup_with_a_foreign_origin_is_rejected(
    migrated_database: str, db_session: AsyncSession
) -> None:
    async with asgi_client(create_app(make_settings()), origin="https://evil.example") as hostile:
        response = await post_signup(hostile, "hostile@example.com")

    assert_problem(response, 403, "origin_not_allowed")
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "hostile@example.com")
    )
    assert count == 0


@pytest.mark.parametrize(
    "origin",
    [
        TEST_ORIGIN,
        "HTTP://LocalHost:3000/",
        "http://test",
    ],
)
async def test_signup_accepts_frontend_and_api_origins(migrated_database: str, origin: str) -> None:
    async with asgi_client(create_app(make_settings()), origin=origin) as http:
        response = await post_signup(http, f"origin-{uuid4().hex}@example.com")

    assert response.status_code == 201, origin


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/auth/signup"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/password"),
        ("PATCH", "/v1/auth/me"),
        ("DELETE", "/v1/auth/sessions/0193a0e0-0000-7000-8000-000000000000"),
    ],
)
async def test_unsafe_routes_are_guarded_before_authentication(
    migrated_database: str, method: str, path: str
) -> None:
    async with asgi_client(create_app(make_settings()), origin=None) as bare:
        response = await bare.request(method, path, json={})

    # 403, not 401: the origin guard runs before the session lookup.
    assert_problem(response, 403, "origin_not_allowed")


async def test_safe_methods_do_not_require_an_origin(migrated_database: str) -> None:
    async with asgi_client(create_app(make_settings()), origin=None) as bare:
        response = await bare.get("/v1/auth/me")

    assert_problem(response, 401, "unauthorized")


async def test_origin_check_is_reported_as_a_problem(migrated_database: str) -> None:
    async with asgi_client(create_app(make_settings()), origin="https://evil.example") as hostile:
        response = await hostile.post("/v1/auth/login", json={"email": "a@example.com"})

    body = assert_problem(response, 403, "origin_not_allowed")
    assert body["type"] == "https://nero.ai/problems/origin-not-allowed"
    assert response.headers["vary"] == "Origin"


# --- Rate limits -------------------------------------------------------------


async def test_login_is_rate_limited_per_ip_and_email(client: httpx.AsyncClient) -> None:
    email = "limited@example.com"
    await post_signup(client, email)
    client.cookies.clear()

    attempts = [
        await client.post("/v1/auth/login", json={"email": email, "password": "definitely wrong"})
        for _ in range(LOGIN_ATTEMPT_LIMIT)
    ]
    assert [attempt.status_code for attempt in attempts] == [401] * LOGIN_ATTEMPT_LIMIT

    blocked = await client.post(
        "/v1/auth/login", json={"email": email, "password": "definitely wrong"}
    )

    body = assert_problem(blocked, 429, "rate_limited")
    retry_after = int(blocked.headers["retry-after"])
    assert 1 <= retry_after <= 15 * 60
    assert email not in blocked.text
    assert body["detail"] == "Too many attempts. Try again later."


async def test_login_rate_limit_is_per_email(client: httpx.AsyncClient) -> None:
    await post_signup(client, "throttled@example.com")
    await post_signup(client, "unaffected@example.com")
    client.cookies.clear()

    for _ in range(LOGIN_ATTEMPT_LIMIT):
        await client.post(
            "/v1/auth/login",
            json={"email": "throttled@example.com", "password": "definitely wrong"},
        )

    throttled = await client.post(
        "/v1/auth/login",
        json={"email": "throttled@example.com", "password": "definitely wrong"},
    )
    unaffected = await client.post(
        "/v1/auth/login",
        json={"email": "unaffected@example.com", "password": PASSWORD},
    )

    assert throttled.status_code == 429
    assert unaffected.status_code == 200


async def test_unknown_emails_are_rate_limited_too(client: httpx.AsyncClient) -> None:
    email = "nobody@example.com"
    for _ in range(LOGIN_ATTEMPT_LIMIT):
        response = await client.post(
            "/v1/auth/login", json={"email": email, "password": "definitely wrong"}
        )
        assert response.status_code == 401

    blocked = await client.post(
        "/v1/auth/login", json={"email": email, "password": "definitely wrong"}
    )

    assert_problem(blocked, 429, "rate_limited")


async def test_successful_logins_count_towards_the_limit(client: httpx.AsyncClient) -> None:
    email = "counting@example.com"
    await post_signup(client, email)
    client.cookies.clear()

    statuses = []
    for _ in range(LOGIN_ATTEMPT_LIMIT + 1):
        response = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        statuses.append(response.status_code)
        client.cookies.clear()

    assert statuses == [200] * LOGIN_ATTEMPT_LIMIT + [429]


async def test_signup_is_rate_limited_per_ip(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    for index in range(SIGNUP_ATTEMPT_LIMIT):
        response = await post_signup(client, f"bulk-{index}@example.com")
        assert response.status_code == 201
        client.cookies.clear()

    blocked = await post_signup(client, "bulk-over@example.com")

    assert_problem(blocked, 429, "rate_limited")
    assert blocked.headers["retry-after"]
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "bulk-over@example.com")
    )
    assert count == 0


# --- Request body cap --------------------------------------------------------


async def test_oversized_bodies_are_rejected(
    migrated_database: str, db_session: AsyncSession
) -> None:
    settings = make_settings(max_request_body_bytes=512)
    async with asgi_client(create_app(settings)) as small:
        response = await small.post(
            "/v1/auth/signup",
            json={
                "email": "oversized@example.com",
                "password": PASSWORD,
                "displayName": "x" * 2_000,
            },
        )

    body = assert_problem(response, 413, "payload_too_large")
    assert body["detail"] == "The request body is too large."
    assert "x" * 2_000 not in response.text
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "oversized@example.com")
    )
    assert count == 0


# --- Profile -----------------------------------------------------------------


async def test_profile_update_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.patch("/v1/auth/me", json={"displayName": "Nope"})

    assert_problem(response, 401, "unauthorized")


async def test_profile_update_renames_the_account(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    signup = await post_signup(client, "rename@example.com", display_name="Before")
    user_id = UUID(signup.json()["user"]["id"])

    response = await client.patch("/v1/auth/me", json={"displayName": "  After  "})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "email", "displayName", "role", "createdAt"}
    assert body["id"] == str(user_id)
    assert body["email"] == "rename@example.com"
    assert body["displayName"] == "After"
    assert body["role"] == "user"
    assert body["createdAt"].endswith("Z")

    me = await client.get("/v1/auth/me")
    assert me.json()["user"]["displayName"] == "After"
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.display_name == "After"


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 81])
async def test_profile_update_rejects_invalid_names(
    client: httpx.AsyncClient, display_name: str
) -> None:
    await post_signup(client, f"invalid-{uuid4().hex}@example.com")

    response = await client.patch("/v1/auth/me", json={"displayName": display_name})

    body = assert_problem(response, 400, "invalid_request")
    assert body["detail"]
    if len(display_name) > 80:
        assert display_name not in response.text


async def test_profile_update_rejects_unknown_fields(client: httpx.AsyncClient) -> None:
    await post_signup(client, "extra-field@example.com")

    response = await client.patch(
        "/v1/auth/me",
        json={"displayName": "Fine", "email": "attacker@example.com"},
    )

    assert_problem(response, 400, "invalid_request")
    assert "attacker@example.com" not in response.text


# --- Password change ---------------------------------------------------------


async def test_password_change_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )

    assert_problem(response, 401, "unauthorized")


async def test_password_change_with_an_unknown_session_is_unauthorized(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        headers={"cookie": "nero_session=not-a-real-token"},
    )

    assert_problem(response, 401, "unauthorized")
    assert "set-cookie" not in response.headers


async def test_password_change_rejects_a_wrong_current_password(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    signup = await post_signup(client, "wrong-current@example.com")
    user_id = UUID(signup.json()["user"]["id"])

    response = await client.post(
        "/v1/auth/password",
        json={"currentPassword": "not the current one", "newPassword": NEW_PASSWORD},
    )

    body = assert_problem(response, 403, "forbidden")
    assert body["detail"] == "Current password is incorrect."
    assert "set-cookie" not in response.headers
    user = await db_session.get(User, user_id)
    assert user is not None
    assert verify_password(PASSWORD, user.password_hash)
    assert (await client.get("/v1/auth/me")).status_code == 200


async def test_password_change_rejects_a_weak_new_password(
    client: httpx.AsyncClient,
) -> None:
    await post_signup(client, "weak-new@example.com")

    response = await client.post(
        "/v1/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": "short"},
    )

    body = assert_problem(response, 422, "weak_password")
    assert body["detail"]
    assert "short" not in response.text
    assert "set-cookie" not in response.headers


async def test_password_change_rotates_the_session_and_revokes_the_rest(
    client: httpx.AsyncClient,
    other_device: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    email = "rotate-password@example.com"
    signup = await post_signup(client, email)
    user_id = UUID(signup.json()["user"]["id"])
    original_token = str(client.cookies.get("nero_session"))
    login = await other_device.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200

    response = await client.post(
        "/v1/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )

    assert response.status_code == 204
    assert response.content == b""
    rotated = response.headers["set-cookie"]
    assert rotated.startswith("nero_session=")
    assert original_token not in rotated
    new_token = str(client.cookies.get("nero_session"))
    assert new_token != original_token

    stale = await client.get("/v1/auth/me", headers={"cookie": f"nero_session={original_token}"})
    assert_problem(stale, 401, "unauthorized")
    assert_problem(await other_device.get("/v1/auth/me"), 401, "unauthorized")
    assert (await client.get("/v1/auth/me")).status_code == 200
    live = await client.get("/v1/auth/sessions")
    assert len(live.json()) == 1

    user = await db_session.get(User, user_id)
    assert user is not None
    assert verify_password(NEW_PASSWORD, user.password_hash)

    client.cookies.clear()
    assert (
        await client.post("/v1/auth/login", json={"email": email, "password": NEW_PASSWORD})
    ).status_code == 200
    client.cookies.clear()
    assert_problem(
        await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}),
        401,
        "invalid_credentials",
    )


# --- Session listing and revocation ------------------------------------------


async def test_sessions_require_authentication(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/v1/auth/sessions"), 401, "unauthorized")
    assert_problem(await client.delete(f"/v1/auth/sessions/{uuid4()}"), 401, "unauthorized")


async def test_session_list_shows_the_callers_live_sessions(
    client: httpx.AsyncClient, other_device: httpx.AsyncClient
) -> None:
    email = "listed@example.com"
    await post_signup(client, email)
    login = await other_device.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200

    response = await client.get("/v1/auth/sessions")

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2
    assert [item["current"] for item in items] == [False, True]
    assert items[0]["userAgent"] == "other-device"
    for item in items:
        assert set(item) == SESSION_FIELDS
        assert item["ip"] == "127.0.0.1"
        assert item["createdAt"].endswith("Z")
        assert item["lastUsedAt"].endswith("Z")
        assert item["expiresAt"].endswith("Z")


async def test_revoking_a_session_signs_the_other_device_out(
    client: httpx.AsyncClient, other_device: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    email = "revoke-other@example.com"
    await post_signup(client, email)
    await other_device.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    items = (await client.get("/v1/auth/sessions")).json()
    target = next(item["id"] for item in items if not item["current"])

    response = await client.delete(f"/v1/auth/sessions/{target}")

    assert response.status_code == 204
    assert "set-cookie" not in response.headers
    assert_problem(await other_device.get("/v1/auth/me"), 401, "unauthorized")
    remaining = (await client.get("/v1/auth/sessions")).json()
    assert session_ids(remaining) == [item["id"] for item in items if item["current"]]
    rows = await db_session.scalars(select(UserSession).where(UserSession.id == UUID(target)))
    assert rows.one().revoked_at is not None


async def test_revoking_a_session_twice_is_idempotent(
    client: httpx.AsyncClient, other_device: httpx.AsyncClient
) -> None:
    email = "revoke-twice@example.com"
    await post_signup(client, email)
    await other_device.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    target = next(
        item["id"] for item in (await client.get("/v1/auth/sessions")).json() if not item["current"]
    )

    first = await client.delete(f"/v1/auth/sessions/{target}")
    second = await client.delete(f"/v1/auth/sessions/{target}")

    assert (first.status_code, second.status_code) == (204, 204)


async def test_revoking_the_current_session_clears_the_cookie(
    client: httpx.AsyncClient,
) -> None:
    await post_signup(client, "revoke-self@example.com")
    items = (await client.get("/v1/auth/sessions")).json()
    current_id = next(item["id"] for item in items if item["current"])

    response = await client.delete(f"/v1/auth/sessions/{current_id}")

    assert response.status_code == 204
    lowered = response.headers["set-cookie"].lower()
    assert 'nero_session=""' in lowered
    assert "max-age=0" in lowered
    assert_problem(await client.get("/v1/auth/me"), 401, "unauthorized")


async def test_revoking_another_users_session_is_not_found(
    client: httpx.AsyncClient, other_device: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await post_signup(client, "owner@example.com")
    await post_signup(other_device, "intruder@example.com")
    victim = (await other_device.get("/v1/auth/sessions")).json()[0]["id"]
    assert victim not in session_ids((await client.get("/v1/auth/sessions")).json())

    response = await client.delete(f"/v1/auth/sessions/{victim}")

    assert_problem(response, 404, "not_found")
    assert (await other_device.get("/v1/auth/me")).status_code == 200
    row = await db_session.get(UserSession, UUID(victim))
    assert row is not None
    assert row.revoked_at is None


async def test_revoking_an_unknown_session_is_not_found(client: httpx.AsyncClient) -> None:
    await post_signup(client, "unknown-session@example.com")

    response = await client.delete(f"/v1/auth/sessions/{uuid4()}")

    assert_problem(response, 404, "not_found")


async def test_expired_sessions_are_not_listed(
    client: httpx.AsyncClient, other_device: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    email = "expired-listing@example.com"
    await post_signup(client, email)
    await other_device.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    items = (await client.get("/v1/auth/sessions")).json()
    assert len(items) == 2
    stale_id = UUID(next(item["id"] for item in items if not item["current"]))
    now = datetime.now(tz=UTC)
    async with engine.begin() as connection:
        await connection.execute(
            update(UserSession)
            .where(UserSession.id == stale_id)
            .values(created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
        )

    listed = (await client.get("/v1/auth/sessions")).json()

    assert [item["id"] for item in listed] == [item["id"] for item in items if item["current"]]
    assert_problem(await other_device.get("/v1/auth/me"), 401, "unauthorized")
