"""Authentication use cases: signup, login, logout, and session lookup.

Transactions are owned here. Signup commits the user, session, Credit account,
and opening grant together, so a half-provisioned account cannot exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.problems import ApiError, ErrorCode
from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.db.models import CreditAccount, CreditLedgerEntry, Plan, User, UserSession
from app.db.session import session_scope

FREE_PLAN_ID = "free"
PLAN_PERIOD_DAYS = 30
LAST_USED_UPDATE_INTERVAL = timedelta(minutes=5)
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

INVALID_CREDENTIALS_DETAIL = "Email or password is incorrect."
UNAUTHORIZED_DETAIL = "Authentication required."

# Verified against when the email is unknown so login takes the same path.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$NMuhZ72HUx85YogWTOFgsQ$"
    "S8ZejTrvp5W4XflFKWJGgtCj7KdtYhZNvpNauEABdqc"
)

_COMMON_PASSWORDS = frozenset(
    {
        "1234567890",
        "admin12345",
        "iloveyou123",
        "letmein123",
        "password123",
        "qwertyuiop",
    }
)


@dataclass(frozen=True)
class AuthContext:
    """Everything a `MeResponse` needs for one authenticated request."""

    user: User
    session: UserSession
    plan: Plan
    account: CreditAccount


@dataclass(frozen=True)
class IssuedSession(AuthContext):
    """An authenticated context plus the raw token that goes into the cookie."""

    token: str


def normalize_email(email: str) -> str:
    """Store emails normalized so the unique constraint is case-insensitive."""
    return email.strip().lower()


def display_name_for(display_name: str | None, email: str) -> str:
    """Use the submitted name, falling back to the email local part."""
    candidate = (display_name or "").strip()
    if candidate:
        return candidate
    return email.split("@", 1)[0][:80] or "Creator"


def validate_password(password: str, email: str) -> None:
    """Enforce the documented password policy before hashing."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ApiError(
            ErrorCode.WEAK_PASSWORD,
            422,
            detail=(f"Password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters."),
        )
    if password.casefold() == email.casefold():
        raise ApiError(
            ErrorCode.WEAK_PASSWORD,
            422,
            detail="Password must not match your email.",
        )
    if password.casefold() in _COMMON_PASSWORDS:
        raise ApiError(
            ErrorCode.WEAK_PASSWORD,
            422,
            detail="Choose a less common password.",
        )


def _invalid_credentials() -> ApiError:
    return ApiError(
        ErrorCode.INVALID_CREDENTIALS,
        401,
        detail=INVALID_CREDENTIALS_DETAIL,
    )


def _unauthorized() -> ApiError:
    return ApiError(ErrorCode.UNAUTHORIZED, 401, detail=UNAUTHORIZED_DETAIL)


def _new_session(
    *,
    user_id: UUID,
    now: datetime,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[UserSession, str]:
    token = generate_session_token()
    row = UserSession(
        user_id=user_id,
        token_hash=hash_session_token(token),
        user_agent=user_agent,
        ip=ip,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=ttl_days),
    )
    return row, token


async def _issue_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[UserSession, str]:
    row, token = _new_session(
        user_id=user_id,
        now=now,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    session.add(row)
    await session.flush()
    return row, token


async def _provision_free_account(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime,
) -> tuple[Plan, CreditAccount]:
    """Create the account and its opening grant from the seeded Free plan."""
    plan = await session.get(Plan, FREE_PLAN_ID)
    if plan is None:
        raise RuntimeError("the free plan is not seeded")
    account = CreditAccount(
        user_id=user_id,
        plan_id=plan.id,
        balance=plan.monthly_credits,
        held=0,
        allowance=plan.monthly_credits,
        period_start=now,
        period_end=now + timedelta(days=PLAN_PERIOD_DAYS),
    )
    grant = CreditLedgerEntry(
        user_id=user_id,
        kind="grant",
        delta_balance=plan.monthly_credits,
        delta_held=0,
        balance_after=plan.monthly_credits,
        held_after=0,
        reason=f"{plan.name} plan allowance",
    )
    session.add_all([account, grant])
    return plan, account


async def _load_billing(session: AsyncSession, user_id: UUID) -> tuple[Plan, CreditAccount]:
    account = await session.get(CreditAccount, user_id)
    if account is None:
        raise RuntimeError("user has no credit account")
    plan = await session.get(Plan, account.plan_id)
    if plan is None:
        raise RuntimeError("the account plan is not seeded")
    return plan, account


async def signup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    display_name: str | None,
    user_agent: str | None,
    ip: str | None,
    ttl_days: int,
) -> IssuedSession:
    """Create a user, Free Credit account, opening grant, and session atomically."""
    normalized = normalize_email(email)
    validate_password(password, normalized)
    name = display_name_for(display_name, normalized)
    now = datetime.now(tz=UTC)

    try:
        async with session_scope(session_factory) as session:
            existing = await session.scalar(select(User.id).where(User.email == normalized))
            if existing is not None:
                raise ApiError(
                    ErrorCode.EMAIL_TAKEN,
                    409,
                    detail="That email is already registered.",
                )
            user = User(
                email=normalized,
                display_name=name,
                password_hash=hash_password(password),
            )
            session.add(user)
            await session.flush()

            plan, account = await _provision_free_account(session, user_id=user.id, now=now)
            session_row, token = await _issue_session(
                session,
                user_id=user.id,
                now=now,
                ttl_days=ttl_days,
                user_agent=user_agent,
                ip=ip,
            )
            return IssuedSession(
                user=user,
                session=session_row,
                plan=plan,
                account=account,
                token=token,
            )
    except IntegrityError as exc:
        # A concurrent signup can win the unique-email race after the check.
        raise ApiError(
            ErrorCode.EMAIL_TAKEN,
            409,
            detail="That email is already registered.",
        ) from exc


async def login(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    existing_token: str | None,
    user_agent: str | None,
    ip: str | None,
    ttl_days: int,
) -> IssuedSession:
    """Verify credentials and issue a fresh session, revoking the current one."""
    normalized = normalize_email(email)
    now = datetime.now(tz=UTC)

    async with session_scope(session_factory) as session:
        user = await session.scalar(select(User).where(User.email == normalized))
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(password, password_hash)
        if user is None or not password_ok or user.status != "active":
            raise _invalid_credentials()

        if existing_token:
            previous = await session.scalar(
                select(UserSession).where(
                    UserSession.token_hash == hash_session_token(existing_token),
                    UserSession.user_id == user.id,
                    UserSession.revoked_at.is_(None),
                )
            )
            if previous is not None:
                previous.revoked_at = now

        plan, account = await _load_billing(session, user.id)
        user.last_login_at = now
        session_row, token = await _issue_session(
            session,
            user_id=user.id,
            now=now,
            ttl_days=ttl_days,
            user_agent=user_agent,
            ip=ip,
        )
        return IssuedSession(
            user=user,
            session=session_row,
            plan=plan,
            account=account,
            token=token,
        )


async def authenticate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    token: str | None,
) -> AuthContext:
    """Resolve a session cookie to its active user, plan, and Credit account."""
    if not token:
        raise _unauthorized()
    now = datetime.now(tz=UTC)

    async with session_scope(session_factory) as session:
        row = (
            await session.execute(
                select(UserSession, User, Plan, CreditAccount)
                .join(User, User.id == UserSession.user_id)
                .join(CreditAccount, CreditAccount.user_id == User.id)
                .join(Plan, Plan.id == CreditAccount.plan_id)
                .where(
                    UserSession.token_hash == hash_session_token(token),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                    User.status == "active",
                )
            )
        ).first()
        if row is None:
            raise _unauthorized()

        session_row, user, plan, account = row
        if session_row.last_used_at <= now - LAST_USED_UPDATE_INTERVAL:
            session_row.last_used_at = now
        return AuthContext(user=user, session=session_row, plan=plan, account=account)


async def logout(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    token: str | None,
) -> None:
    """Revoke the current session. Missing or unknown tokens are a no-op."""
    if not token:
        return
    now = datetime.now(tz=UTC)
    async with session_scope(session_factory) as session:
        row = await session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
            )
        )
        if row is not None:
            row.revoked_at = now
