# Nero AI backend

Python backend for the Nero AI frontend. It owns real accounts, sessions, a
server-authoritative Credit ledger, Chats, Generations, Assets, and provider access.

**Status:** Phase 0 complete. Phase 1 (accounts and sessions) is in progress:
B1.1 provides the user, session, plan, and Credit schema plus Argon2id and opaque
session-token primitives; B1.2 provides `signup`, `login`, `logout`, and `/v1/auth/me`
with atomic Free-plan provisioning; B1.3 adds profile and password updates, session
listing and revocation, the Origin check on unsafe methods, login/signup rate limits,
and the 1 MiB request body cap.

## Read first

- [`docs/SPEC.md`](docs/SPEC.md): product and technical specification, API contract,
  data model, security, deployment.
- [`docs/ROADMAP.md`](docs/ROADMAP.md): phased tickets and gates.
- [`AGENTS.md`](AGENTS.md): rules for the AI agent building this repository.
- [`GLOSSARY.md`](GLOSSARY.md): domain vocabulary.
- [`docs/adr/`](docs/adr/): decisions.

## Stack

- Python 3.12, FastAPI, Pydantic v2
- PostgreSQL 16, SQLAlchemy 2 async, asyncpg, Alembic
- httpx, uv, ruff, mypy, pytest
- Docker Compose for local PostgreSQL

## Local development

```bash
cp .env.example .env          # fill in real values locally; never commit .env
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000

curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

## Database

Migrations live in `alembic/versions/`. The URL comes from `DATABASE_URL`; never put a
real URL in `alembic.ini`.

```bash
uv run alembic upgrade head             # apply migrations
uv run alembic current                  # show the applied revision
uv run alembic revision -m "add table"  # create a migration
```

Migration `0002` seeds the Free, Pro, and Studio plan catalog. `app.db.seed.upsert_plans`
refreshes the same rows idempotently, so it is safe to run on every deploy.

Tests use a separate database: `nero_test` on the same server by default, overridable
with `TEST_DATABASE_URL`. The suite recreates and migrates it once per session, so never
point it at a database you care about.

## Checks

CI runs these on every pull request; run them locally before opening one.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=75
docker build -t nero-ai-back:dev .
```

Worker (from Phase 5):

```bash
uv run python -m app.worker
```

Provider probe (after Phase 3):

```bash
uv run python -m app.cli provider-check
```

## Repository layout

```text
app/
  api/          routers, dependencies, problem responses
  core/         config, logging, security, rate limiting
  db/           SQLAlchemy models and session
  domain/       use-case services and invariants
  providers/    provider ports and adapters
  storage/      asset persistence
  workers/      generation worker
alembic/        migrations
tests/          unit, integration, contract
docs/           spec, roadmap, ADRs, agent docs
scripts/        seed and maintenance commands
```

## Frontend relationship

The frontend remains local-first in demo mode. In API mode it proxies `/api/*` to this
service and treats the backend as the source of truth. The contract is defined in
`docs/SPEC.md`; the backend exports `openapi.json` and the frontend generates its types
from it.

## Security

- Never commit `.env` or a provider key.
- Never log prompts, message content, cookies, or authorization headers.
- Plan changes are simulated in v1. Do not enable paid plans in production without a
  real payment integration.
- Image and Video are deterministic mocks until real provider credentials exist.
