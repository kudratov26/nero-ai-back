# ADR 0003: Python FastAPI backend with server-side sessions

Status: accepted
Date: 2026-11-14
Supersedes: the v1-only assumptions in ADR-0001

## Context

ADR-0001 kept v1 entirely in the browser: localStorage/IndexedDB repositories and
fixture providers. ADR-0002 (in the frontend repository, `nero-ai-front`) said any real
auth, generation, or payments would arrive with a backend rather than a Next.js route
handler. We now need multi-user accounts, a shared Credit ledger, persistent Chats and
Generations, and real model access. The model key must never reach the browser.

## Decision

Build a separate Python backend with:

- FastAPI + Pydantic v2 for the HTTP contract and OpenAPI.
- PostgreSQL 16 + SQLAlchemy 2 async + Alembic for multi-user data and migrations.
- Opaque, server-side sessions in HttpOnly cookies. No JWT in browser storage.
- A server-authoritative Credit ledger with reserves, commits, refunds, and holds.
- Provider ports in `app/providers`; an OpenAI-compatible Coarena adapter for DeepSeek
  V4.1 Flash. Image and Video use deterministic mock providers until a real provider key
  and adapter exist.
- Same-origin Next.js `/api` rewrite so cookies are first-party and CORS is unnecessary.
- A Postgres-backed generation queue and a separate worker process. No Redis or Celery
  in v1.

The frontend keeps its local mode for offline demos and adds an HTTP adapter selected by
an explicit data-mode flag. There is never more than one source of truth at runtime.

## Consequences

- The frontend gains a real service adapter, auth state hydration, streaming via fetch,
  and generated types from OpenAPI.
- The backend becomes responsible for authorization, Credit invariants, provider secrets,
  rate limits, and data deletion.
- PostgreSQL is required for development and tests; SQLite is not supported.
- Image and Video remain simulated until a second provider is chosen. The UI must label
  them as demo output.
- Real billing, email verification, and password reset are explicitly post-v1; the
  simulated plan-change endpoint is disabled in production.
- Deployment needs an API process, a worker, PostgreSQL, persistent asset storage, and a
  reverse proxy with TLS.
