"""Auth endpoints.

Routers stay thin: validate input, call a domain use case, and map the result.
Cookies and rate-limit budgets are transport concerns, so they live here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import (
    enforce_rate_limit,
    get_app_settings,
    get_current_auth,
    get_rate_limiter,
    get_session_factory,
)
from app.api.schemas.auth import (
    CreditsSummary,
    LoginRequest,
    MeResponse,
    PasswordChangeRequest,
    PlanSummary,
    SessionSummary,
    SignupRequest,
    UpdateProfileRequest,
    UserResponse,
)
from app.core.config import Settings
from app.core.rate_limit import MemoryRateLimiter
from app.db.models import User, UserSession
from app.domain import auth as auth_service
from app.domain import credits as credits_service
from app.domain.auth import AuthContext, IssuedSession, normalize_email

# `docs/SPEC.md` section 15: login 5 per 15 minutes per IP + email, signup 3 per
# hour per IP. Both count every attempt, successful or not.
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
SIGNUP_ATTEMPT_LIMIT = 3
SIGNUP_ATTEMPT_WINDOW_SECONDS = 60 * 60

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _rate_limit_key(request: Request, *, scope: str, email: str | None = None) -> str:
    ip = _client_ip(request) or "unknown"
    if email is None:
        return f"{scope}:{ip}"
    return f"{scope}:{ip}:{normalize_email(email)}"


def _user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:512] if value else None


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    """Set the opaque session cookie with the flags from the spec."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
    )


def _session_response(row: UserSession, *, current_id: UUID) -> SessionSummary:
    return SessionSummary(
        id=row.id,
        user_agent=row.user_agent,
        ip=row.ip,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        current=row.id == current_id,
    )


def _me_response(context: AuthContext) -> MeResponse:
    account = context.account
    return MeResponse(
        user=_user_response(context.user),
        plan=PlanSummary(
            id=context.plan.id,
            name=context.plan.name,
            monthly_credits=context.plan.monthly_credits,
        ),
        credits=CreditsSummary(
            balance=account.balance,
            held=account.held,
            available=credits_service.available(account),
            allowance=account.allowance,
            period_start=account.period_start,
            period_end=account.period_end,
        ),
    )


def _issued_response(response: Response, settings: Settings, issued: IssuedSession) -> MeResponse:
    set_session_cookie(response, settings, issued.token)
    return _me_response(issued)


@router.post(
    "/signup",
    response_model=MeResponse,
    status_code=201,
    summary="Create an account",
    description=(
        "Creates the user, a Free Credit account with its opening grant, and a "
        "session in one transaction."
    ),
)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limiter: Annotated[MemoryRateLimiter, Depends(get_rate_limiter)],
) -> MeResponse:
    enforce_rate_limit(
        limiter,
        _rate_limit_key(request, scope="signup"),
        limit=SIGNUP_ATTEMPT_LIMIT,
        window_seconds=SIGNUP_ATTEMPT_WINDOW_SECONDS,
    )
    issued = await auth_service.signup(
        session_factory,
        email=str(body.email),
        password=body.password,
        display_name=body.display_name,
        user_agent=_user_agent(request),
        ip=_client_ip(request),
        ttl_days=settings.session_ttl_days,
    )
    return _issued_response(response, settings, issued)


@router.post(
    "/login",
    response_model=MeResponse,
    summary="Log in",
    description=(
        "Verifies credentials and issues a fresh session. The current session, if any, is revoked."
    ),
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limiter: Annotated[MemoryRateLimiter, Depends(get_rate_limiter)],
) -> MeResponse:
    enforce_rate_limit(
        limiter,
        _rate_limit_key(request, scope="login", email=str(body.email)),
        limit=LOGIN_ATTEMPT_LIMIT,
        window_seconds=LOGIN_ATTEMPT_WINDOW_SECONDS,
    )
    issued = await auth_service.login(
        session_factory,
        email=str(body.email),
        password=body.password,
        existing_token=request.cookies.get(settings.session_cookie_name),
        user_agent=_user_agent(request),
        ip=_client_ip(request),
        ttl_days=settings.session_ttl_days,
    )
    return _issued_response(response, settings, issued)


@router.post(
    "/logout",
    status_code=204,
    summary="Log out",
    description="Revokes the current session and clears the cookie. Idempotent.",
)
async def logout(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await auth_service.logout(
        session_factory,
        token=request.cookies.get(settings.session_cookie_name),
    )
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Get the current account",
    description="Returns the authenticated user, plan, and Credit summary.",
)
async def me(
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> MeResponse:
    return _me_response(context)


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Update the current account",
    description="Updates the display name of the authenticated user.",
)
async def update_me(
    body: UpdateProfileRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> UserResponse:
    user = await auth_service.update_display_name(
        session_factory,
        user_id=context.user.id,
        display_name=body.display_name,
    )
    return _user_response(user)


@router.post(
    "/password",
    status_code=204,
    summary="Change the password",
    description=(
        "Verifies the current password, rotates this session's token, and revokes "
        "every other session. The response carries the replacement cookie."
    ),
)
async def change_password(
    body: PasswordChangeRequest,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    token = await auth_service.change_password(
        session_factory,
        user_id=context.user.id,
        session_id=context.session.id,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    response = Response(status_code=204)
    set_session_cookie(response, settings, token)
    return response


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="List sessions",
    description="Returns the caller's live sessions, newest first.",
)
async def sessions(
    context: Annotated[AuthContext, Depends(get_current_auth)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[SessionSummary]:
    rows = await auth_service.list_sessions(session_factory, user_id=context.user.id)
    current_id = context.session.id
    return [_session_response(row, current_id=current_id) for row in rows]


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    summary="Revoke a session",
    description=(
        "Revokes one of the caller's sessions. Revoking the current session also "
        "clears the cookie. Idempotent."
    ),
)
async def revoke_session(
    session_id: UUID,
    context: Annotated[AuthContext, Depends(get_current_auth)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await auth_service.revoke_session(
        session_factory,
        user_id=context.user.id,
        session_id=session_id,
    )
    response = Response(status_code=204)
    if session_id == context.session.id:
        clear_session_cookie(response, settings)
    return response
