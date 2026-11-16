"""Authorization matrix across every documented route.

The unit suite in `tests/unit/test_route_matrix.py` proves every protected route
carries the `get_current_auth` dependency. This suite proves the dependency
actually rejects anonymous callers and that an authenticated caller cannot reach
or mutate another account's row.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.problems import ApiError, ErrorCode
from app.db.models import Chat, UserSession
from app.db.seed import upsert_models
from app.db.session import create_session_factory, session_scope
from app.domain import auth as auth_service
from app.main import create_app
from tests.support import (
    PASSWORD,
    PROTECTED_ROUTES,
    ROUTES,
    RouteCase,
    asgi_client,
    assert_problem,
    is_unauthorized,
    make_settings,
    post_signup,
    route_path,
)

PATH_PARAM = re.compile(r"\{([^{}]+)\}")
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
PROTECTED_PARAM_NAMES = frozenset(
    name for case in PROTECTED_ROUTES for name in PATH_PARAM.findall(case.path)
)
#: Path parameters that name a public resource rather than an account-owned row.
#: A `post_id` is deliberately reachable by every signed-in account, so an
#: attacker cannot be refused with 404 when liking it; the like relation itself
#: stays account-scoped and is covered in `test_explore.py`.
PUBLIC_RESOURCE_PARAMS = frozenset({"post_id"})
OWNED_PARAM_NAMES = PROTECTED_PARAM_NAMES - PUBLIC_RESOURCE_PARAMS
CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}


@dataclass
class Account:
    """A signed-in browser: its cookie jar, client, and identity."""

    client: httpx.AsyncClient
    user_id: UUID
    email: str


def request_body(case: RouteCase) -> dict[str, object] | None:
    """A minimal body for methods that carry one.

    Dependencies run before body validation, so `{}` is enough to reach the auth
    layer; these probes never need a valid payload. A route whose handler
    validates before checking ownership declares `probe_body` instead, so the
    probe still reaches the ownership check rather than stopping at a `400`.
    """
    if case.probe_body is not None:
        return dict(case.probe_body)
    return {} if case.method in BODY_METHODS else None


@asynccontextmanager
async def _account(label: str) -> AsyncIterator[Account]:
    async with asgi_client(create_app(make_settings()), headers={"user-agent": label}) as http:
        email = f"{label}-{uuid4().hex}@example.com"
        signup = await post_signup(http, email)
        assert signup.status_code == 201, signup.text
        yield Account(
            client=http,
            user_id=UUID(signup.json()["user"]["id"]),
            email=email,
        )


@pytest.fixture
async def anonymous(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(create_app(make_settings())) as http:
        yield http


@pytest.fixture
async def attacker(migrated_database: str) -> AsyncIterator[Account]:
    async with _account("attacker") as account:
        yield account


@pytest.fixture
async def victim(migrated_database: str) -> AsyncIterator[Account]:
    async with _account("victim") as account:
        yield account


# --- Anonymous matrix --------------------------------------------------------


@pytest.mark.parametrize("case", ROUTES, ids=lambda case: f"{case.method} {case.path}")
async def test_anonymous_requests_meet_the_declared_expectation(
    anonymous: httpx.AsyncClient, case: RouteCase
) -> None:
    response = await anonymous.request(case.method, route_path(case), json=request_body(case))

    if case.anonymous_status is None:
        # 404 (not seeded) or 200 (seeded); either way it must not be an auth rejection.
        assert not is_unauthorized(response), response.text
    elif case.anonymous_code is None:
        assert response.status_code == case.anonymous_status, response.text
    else:
        assert_problem(response, case.anonymous_status, case.anonymous_code)

    if case in PROTECTED_ROUTES:
        # A rejected request must not hand out a session or mutate anything.
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("case", PROTECTED_ROUTES, ids=lambda case: f"{case.method} {case.path}")
async def test_protected_routes_reject_a_revoked_session(
    anonymous: httpx.AsyncClient, case: RouteCase
) -> None:
    email = f"revoked-{uuid4().hex}@example.com"
    await post_signup(anonymous, email)
    cookie = anonymous.cookies.get("nero_session")
    assert cookie
    assert (await anonymous.post("/v1/auth/logout")).status_code == 204

    response = await anonymous.request(
        case.method,
        route_path(case),
        json=request_body(case),
        headers={"cookie": f"nero_session={cookie}"},
    )

    assert_problem(response, 401, "unauthorized")


# --- Cross-user (IDOR) matrix ------------------------------------------------


async def _victim_session_id(victim: Account, _engine: AsyncEngine) -> str:
    listed = await victim.client.get("/v1/auth/sessions")
    assert listed.status_code == 200, listed.text
    items = listed.json()
    assert items, "the victim should have at least one live session"
    return str(items[0]["id"])


async def _victim_chat_id(victim: Account, engine: AsyncEngine) -> str:
    """Return the victim's chat, creating it once.

    Written directly rather than through `POST /v1/chats`: a chat needs the
    catalog seeded, which is a different suite's concern, and these probes are
    about authorization. The write commits, because the API reads through its own
    connection and would not see an uncommitted row. The find-or-create makes the
    id stable across the before/after check, so a rejected `PATCH` or `DELETE`
    that leaked through would show up as a changed row.
    """
    settings = make_settings(**CONFIGURED_CHAT)
    async with session_scope(create_session_factory(engine)) as session:
        await upsert_models(session, settings)
        existing = await session.scalar(
            select(Chat).where(Chat.user_id == victim.user_id).order_by(Chat.id).limit(1)
        )
        if existing is not None:
            return str(existing.id)
        chat = Chat(user_id=victim.user_id, model_id=settings.ai_chat_model, title="Victim chat")
        session.add(chat)
        await session.flush()
        return str(chat.id)


#: Path parameter -> the victim's resource the attacker must not reach. Every
#: account-owned parameter on a protected route needs an entry; the guard test
#: below enforces it.
IDOR_RESOLVERS: dict[str, Callable[[Account, AsyncEngine], Awaitable[str]]] = {
    "session_id": _victim_session_id,
    "chat_id": _victim_chat_id,
}


def test_every_owned_resource_parameter_has_an_idor_resolver() -> None:
    assert set(IDOR_RESOLVERS) == OWNED_PARAM_NAMES


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in PROTECTED_ROUTES
        if PATH_PARAM.findall(case.path) and set(PATH_PARAM.findall(case.path)) <= OWNED_PARAM_NAMES
    ],
    ids=lambda case: f"{case.method} {case.path}",
)
async def test_cross_user_requests_are_not_found(
    case: RouteCase,
    attacker: Account,
    victim: Account,
    engine: AsyncEngine,
) -> None:
    names = PATH_PARAM.findall(case.path)
    before = {name: await IDOR_RESOLVERS[name](victim, engine) for name in names}
    path = case.path
    for name, value in before.items():
        path = path.replace(f"{{{name}}}", value)

    response = await attacker.client.request(case.method, path, json=request_body(case))

    assert_problem(response, 404, "not_found")
    # The rejected request neither mutated nor hid another account's row.
    assert (await victim.client.get("/v1/auth/me")).status_code == 200
    after = {name: await IDOR_RESOLVERS[name](victim, engine) for name in names}
    assert after == before


async def test_the_victim_can_still_reach_a_chat_the_attacker_was_denied(
    victim: Account, engine: AsyncEngine
) -> None:
    """The `404` hides the row without destroying it."""
    chat_id = await _victim_chat_id(victim, engine)

    response = await victim.client.get(f"/v1/chats/{chat_id}")

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Victim chat"
    assert response.json()["id"] == chat_id


async def test_cross_user_session_ids_stay_separate(
    attacker: Account, victim: Account, db_session: AsyncSession
) -> None:
    """Listing is scoped, so neither account can see the other's sessions."""
    attacker_ids = {item["id"] for item in (await attacker.client.get("/v1/auth/sessions")).json()}
    victim_ids = {item["id"] for item in (await victim.client.get("/v1/auth/sessions")).json()}

    assert attacker_ids and victim_ids
    assert attacker_ids.isdisjoint(victim_ids)

    db_session.expire_all()
    rows = await db_session.scalars(
        select(UserSession).where(UserSession.id.in_({UUID(i) for i in attacker_ids}))
    )
    assert {row.user_id for row in rows} == {attacker.user_id}


async def test_password_change_cannot_target_another_users_session(
    attacker: Account, victim: Account, engine: AsyncEngine
) -> None:
    """The domain re-checks ownership, so a mismatched pair is rejected outright."""
    victim_session = (await victim.client.get("/v1/auth/sessions")).json()[0]["id"]

    with pytest.raises(ApiError) as caught:
        await auth_service.change_password(
            create_session_factory(engine),
            user_id=attacker.user_id,
            session_id=UUID(victim_session),
            current_password=PASSWORD,
            new_password="a whole new battery horse",
        )

    assert caught.value.code is ErrorCode.UNAUTHORIZED
