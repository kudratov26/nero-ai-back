# Nero AI backend

Python backend for the Nero AI frontend. It owns real accounts, sessions, a
server-authoritative Credit ledger, Chats, Generations, Assets, and provider access.

**Status:** Phase 0 in progress. B0.1–B0.3 are in place: settings, structured
logging, RFC 9457 errors with request IDs, PostgreSQL/SQLAlchemy/Alembic, and
`/health/live` + `/health/ready`. CI lands in B0.4.

## Read first

- [`docs/SPEC.md`](docs/SPEC.md): product and technical specification, API contract,
  data model, security, deployment.
- [`docs/ROADMAP.md`](docs/ROADMAP.md): phased tickets and gates.
- [`AGENTS.md`](AGENTS.md): rules for the AI agent building this repository.
- [`GLOSSARY.md`](GLOSSARY.md): domain vocabulary.
- [`docs/adr/`](docs/adr/): decisions.

## Planned stack

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

Worker (separate terminal):

```bash
uv run python -m app.worker
```

Checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
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
