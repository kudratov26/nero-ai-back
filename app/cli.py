"""Operational entry points.

```bash
uv run python -m app.cli seed
uv run python -m app.cli provider-check
```

`seed` is idempotent and safe to run on every deploy. It runs after
`alembic upgrade head`, because the plan and model catalogs live in the database
so prices have one authoritative home.

`provider-check` sends one short request to the configured chat gateway and
prints the model, status, and usage. It never prints the key, the prompt, or the
completion.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Sequence

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.seed import upsert_models, upsert_plans
from app.db.session import create_engine, create_session_factory, session_scope
from app.providers.base import (
    ChatParams,
    ChatProvider,
    ChatUsage,
    ProviderError,
    ProviderMessage,
    ProviderModel,
)
from app.providers.openai_compatible import build_chat_provider

log = structlog.get_logger(__name__)

USAGE = "usage: python -m app.cli seed | provider-check"

#: A one-token probe. The prompt content is never printed.
PROVIDER_CHECK_PROMPT = "Reply with a single word."
PROVIDER_CHECK_MAX_TOKENS = 1
PROVIDER_NOT_CONFIGURED = (
    "provider-check: the chat provider is not configured; set AI_BASE_URL and AI_API_KEY."
)


async def seed_catalog() -> None:
    """Refresh the plan and model catalogs in one transaction."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        factory = create_session_factory(engine)
        async with session_scope(factory) as session:
            await upsert_plans(session)
            await upsert_models(session, settings)
    finally:
        await engine.dispose()
    log.info(
        "catalog.seeded",
        generation_mode=settings.generation_mode,
        chat_provider_configured=settings.chat_provider_configured,
    )


def _token_count(value: int | None) -> str:
    return "unknown" if value is None else str(value)


async def run_provider_check(provider: ChatProvider, *, model_id: str) -> int:
    """Send one probe and print the model, status, and usage. Returns the exit code."""
    model = ProviderModel(slug=model_id, provider_model_id=model_id)
    messages = [ProviderMessage(role="user", content=PROVIDER_CHECK_PROMPT)]
    params = ChatParams(max_tokens=PROVIDER_CHECK_MAX_TOKENS)
    started = time.perf_counter()
    usage = ChatUsage()
    finish_reason: str | None = None
    try:
        async for delta in provider.stream_chat(messages=messages, model=model, params=params):
            if delta.usage is not None:
                usage = delta.usage
            if delta.finish_reason is not None:
                finish_reason = delta.finish_reason
    except ProviderError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(
            f"provider-check: model={model_id} status=error code={exc.code.value} "
            f"latency_ms={elapsed_ms}"
        )
        return 1
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(
        f"provider-check: model={model_id} status=ok finish={finish_reason or 'none'} "
        f"prompt_tokens={_token_count(usage.prompt_tokens)} "
        f"completion_tokens={_token_count(usage.completion_tokens)} latency_ms={elapsed_ms}"
    )
    return 0


async def provider_check() -> int:
    """Build the chat provider from settings and probe it."""
    settings = get_settings()
    provider = build_chat_provider(settings)
    if provider is None:
        print(PROVIDER_NOT_CONFIGURED, file=sys.stderr)
        return 1
    try:
        return await run_provider_check(provider, model_id=settings.ai_chat_model)
    finally:
        await provider.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    configure_logging(get_settings())
    if args == ["seed"]:
        asyncio.run(seed_catalog())
        return 0
    if args == ["provider-check"]:
        return asyncio.run(provider_check())
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
