# Nero AI backend: agent guide

This repository is the Python backend for Nero AI. The frontend lives in a separate
repository (`nero-ai-front`, Next.js). The backend owns accounts, sessions, Credits,
Chats, Generations, Assets, and provider access.

**Read before coding, in this order:**

1. `docs/SPEC.md` (contract and invariants)
2. `docs/ROADMAP.md` (current phase and ticket order)
3. `GLOSSARY.md` (domain vocabulary)
4. `docs/adr/` (decisions already made)

Status: **specification phase**. Do not start Phase 0 until the roadmap gate is accepted.

## Stack

Fixed unless a new ADR changes it:

- Python 3.12, FastAPI, Pydantic v2, pydantic-settings
- PostgreSQL 16, SQLAlchemy 2 (async), asyncpg, Alembic
- httpx for provider calls and streaming
- `uv` for dependencies and commands
- ruff (lint + format), mypy (strict on `app/`), pytest + pytest-asyncio
- Docker Compose for local PostgreSQL only

Do not introduce Django, Flask, SQLite for the multi-user path, Redis/Celery, or Node
code. A queue or cache is added only when the roadmap calls for it.

## Commands

```bash
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000

uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
uv run pytest --cov=app --cov-report=term-missing
```

Every change must pass all four checks (format, lint, typecheck, tests) before a PR.

## Architecture rules

```text
app/api/          HTTP routers, auth dependencies, request/response schemas
app/domain/       use-case services; owns invariants and transactions
app/db/           SQLAlchemy models, session factory, repositories for complex queries
app/providers/    provider ports and adapters (Coarena/OpenAI-compatible, mock)
app/workers/      generation worker entry point
app/storage/      asset persistence and retrieval
app/core/         config, security, logging, rate limiting
alembic/          migrations only
tests/            unit, integration, contract
```

- Routers never contain SQL or provider calls. They validate, call a service, and map
  the result to a response.
- Services own transactions. A use case is atomic: Credits, domain rows, and ledger
  entries commit or roll back together.
- Provider calls happen only in `app/providers/`, called by services or workers.
- Pydantic schemas are separate from SQLAlchemy models. Never return ORM objects.
- Only async I/O. No `requests`, no blocking SDK calls, no `time.sleep`.
- No repository layer ceremony for simple reads; use the session directly in a service
  and extract a repository only when a query is reused or complex.
- Every table change ships with an Alembic migration. No `create_all()` outside tests.

## Domain non-negotiables

- Credits are integers. Never floats. Never trust a client-supplied price.
- The ledger is append-only. Correct mistakes with a compensating entry, never an update.
- Reserve Credits before provider work; commit on success; refund on failure, cancel, or
  a stale hold. `available = balance - held`, and it can never go negative.
- Every resource query is scoped by the authenticated `user_id`. Assume IDOR tests exist
  for every route.
- The model catalog is server-authoritative. If a model has no working provider, do not
  advertise it as available.
- DeepSeek V4.1 Flash is a chat model. Image and Video stay on `GENERATION_MODE=mock`
  until a real image/video provider key and adapter exist. Never claim mock output is real.
- Timestamps are timezone-aware UTC, serialized as ISO 8601 with `Z`.
- JSON at the HTTP boundary is camelCase; Python stays snake_case. Use Pydantic aliases.

## Security non-negotiables

- Secrets only from environment variables. `.env` is gitignored; `.env.example` has no
  real values. Never log, echo, or return `AI_API_KEY` or session cookies.
- Never log prompts, message content, model output, or Authorization headers.
- Passwords: Argon2id via `argon2-cffi`/`pwdlib`. Never MD5/SHA/Web Crypto.
- Sessions: opaque 32-byte token in an `HttpOnly`, `SameSite=Lax`, `Secure` (prod)
  cookie; store only the SHA-256 hash. Rotate on login and password change.
- Unsafe methods require a matching `Origin` header. No wildcard CORS with credentials.
- Provider base URLs are fixed config. Never fetch arbitrary user-supplied URLs.
- Cap request bodies at 1 MiB and validate all pagination and enum inputs.
- Provider keys get a dashboard spend cap. Rotate any key that has been pasted into a
  commit, an issue, or a chat.
- Rate-limit login, signup, chat, and generation endpoints. In-memory is acceptable for
  one instance; move to Redis before scaling out.

## Testing rules

- Every behavior change adds a test. Bug fixes start with a failing test.
- Use the fake provider adapters in tests; never call a paid provider from CI.
- Credit invariants, auth/session behavior, authorization (IDOR), streaming events, and
  provider error mapping are mandatory integration tests.
- Migrations are tested by running `alembic upgrade head` against a clean database.
- Snapshot `openapi.json`; a contract change must be visible in the diff.
- Target: 85% coverage for `app/domain` and `app/providers`; 75% overall.

## Workflow

- One issue per milestone or meaningful feature. Use the tracker conventions in
  `docs/agents/issue-tracker.md`.
- Branch from `main`: `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, `chore/<slug>`.
- Small scoped conventional commits.
- Run all checks, push, open a PR that links the issue with `Closes #<n>`.
- Never commit directly to `main`.
- Update `docs/SPEC.md` when the contract changes; add an ADR when a decision changes.
- If `gh` is unavailable, use the GitHub REST API with a token from
  `git credential fill`; never print the token.
- The backend is not "done" until its PR is open and the roadmap gate is met.

## Definition of done

```text
[ ] behavior implemented behind the documented contract
[ ] migration included and tested from a clean DB
[ ] unit + integration tests added
[ ] ruff format, ruff check, mypy, pytest pass
[ ] OpenAPI snapshot updated when routes or schemas changed
[ ] no secret, prompt, or cookie in logs
[ ] docs updated (SPEC, ROADMAP status, GLOSSARY if a term changed)
[ ] PR links the issue and states the phase gate it satisfies
```

## Session bootstrap

Paste this when starting a new backend session:

```text
Repo: /home/culpe/Programmin/nero-ai-back
Read: AGENTS.md, docs/SPEC.md, docs/ROADMAP.md, GLOSSARY.md.
Stack: FastAPI, PostgreSQL, SQLAlchemy async, Alembic, httpx, uv, ruff, mypy, pytest.
Current task: <ticket from ROADMAP.md>.
Scope: <one ticket>. Non-goals: everything after this ticket.
Done: checks in AGENTS.md pass, tests added, OpenAPI updated, PR opened.
Do not commit to main. Do not log secrets or prompts. Do not advertise unavailable models.
```

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI. See
`docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, using the default strings. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root plus `docs/adr/`. See
`docs/agents/domain.md`.
