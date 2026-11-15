"""Auth endpoints.

Routers stay thin: validate input, call a domain use case, and map the result.
Cookies are set here because they are transport concerns.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_app_settings, get_current_auth, get_session_factory
from app.api.schemas.auth import (
    CreditsSummary,
    LoginRequest,
    MeResponse,
    PlanSummary,
    SignupRequest,
    UserResponse,
)
from app.core.config import Settings
from app.domain.auth import (
    AuthContext,
    IssuedSession,
)
from app.domain.auth import (
    login as login_user,
)
from app.domain.auth import (
    logout as logout_user,
)
from app.domain.auth import (
    signup as signup_user,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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


def _me_response(context: AuthContext) -> MeResponse:
    account = context.account
    return MeResponse(
        user=UserResponse(
            id=context.user.id,
            email=context.user.email,
            display_name=context.user.display_name,
            role=context.user.role,
            created_at=context.user.created_at,
        ),
        plan=PlanSummary(
            id=context.plan.id,
            name=context.plan.name,
            monthly_credits=context.plan.monthly_credits,
        ),
        credits=CreditsSummary(
            balance=account.balance,
            held=account.held,
            available=account.balance - account.held,
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
) -> MeResponse:
    issued = await signup_user(
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
) -> MeResponse:
    issued = await login_user(
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
    await logout_user(
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
