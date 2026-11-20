# Nero AI backend

Python backend for the Nero AI frontend. It owns real Accounts, sessions, a
server-authoritative Credit ledger, Chats, Generations, Assets, and provider access.
The browser never receives a provider key.

**Status:** v1 is feature-complete through Phase 6. Accounts, sessions, Credits, Chats,
Generations with a worker queue, Explore, asset delivery, and demo reset run against
PostgreSQL; the Next.js app consumes this service in API mode; the committed
`openapi.json` is the contract both sides generate from. Phase 7 (hardening and
deployment) is next.

Image and Video are deterministic mock providers until a real provider key and adapter
exist. They are labelled `Demo`, and the catalog never advertises a model whose provider
is not configured. Chat runs on DeepSeek V4.1 Flash through the Coarena gateway when
`AI_BASE_URL` and `AI_API_KEY` are set.

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

## Quick start

Prerequisites: Python 3.12 with [`uv`](https://docs.astral.sh/uv/), and Docker for the
local PostgreSQL container.

```bash
# 1. Configure and install. `.env` is gitignored; placeholders are enough for a
#    local run with mock Image/Video and no chat provider.
cp .env.example .env
uv sync

# 2. PostgreSQL 16. `--wait` blocks until the healthcheck passes.
#    Data persists in the `postgres-data` volume.
docker compose up -d --wait db

# 3. Schema, plans, model catalog, and the Explore gallery. Idempotent.
uv run alembic upgrade head
uv run python -m app.cli seed

# 4. API on http://localhost:8000
uv run uvicorn app.main:app --reload --port 8000
```

Start the Generation worker in a second terminal. It reads the same `.env`,
`DATABASE_URL`, and `STORAGE_DIR` as the API:

```bash
# 5. Worker: claims queued Image/Video jobs and settles their Credits.
uv run python -m app.worker
```

Check the processes:

```bash
curl -sS http://localhost:8000/health/live    # {"status":"ok"}
curl -sS http://localhost:8000/health/ready   # {"status":"ready"} once migrated
```

`/health/ready` answers `503` until the database is reachable and Alembic is at head.
It never echoes driver messages or connection strings. Interactive API docs are at
`http://localhost:8000/docs` (disabled in production); the raw schema is
`/openapi.json`.

Which models appear depends on configuration:

```bash
curl -sS http://localhost:8000/v1/models
```

- The chat model appears only when `AI_BASE_URL` and `AI_API_KEY` are both set.
- `image-demo` and `video-demo` appear only while `GENERATION_MODE=mock`.
- Re-run `uv run python -m app.cli seed` after changing either setting; a model that is
  no longer configured is marked unavailable, never silently advertised.

## First requests

Unsafe methods (`POST`, `PUT`, `PATCH`, `DELETE`) require an `Origin` header that matches
`FRONTEND_ORIGIN` (or the API's own origin). The session lives in an HttpOnly cookie, so
curl keeps it in a jar.

```bash
# Sign up; 201 returns the user, plan, and Credits and writes the session cookie.
curl -sS -c cookies.txt \
  -H 'Origin: http://localhost:3000' -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"correct horse battery","displayName":"Demo"}' \
  http://localhost:8000/v1/auth/signup

# Who am I, and how many Credits are available?
curl -sS -b cookies.txt http://localhost:8000/v1/auth/me
curl -sS -b cookies.txt http://localhost:8000/v1/credits
```

A queued Image job costs 5 Credits per image and needs the worker:

```bash
curl -sS -b cookies.txt \
  -H 'Origin: http://localhost:3000' -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"modality":"image","modelId":"image-demo","prompt":"A sunlit reading room","params":{"aspectRatio":"4:5","count":1,"style":"Cinematic"}}' \
  http://localhost:8000/v1/generations

# Poll the id from the 202 response until status is "succeeded" or "failed".
curl -sS -b cookies.txt http://localhost:8000/v1/generations/$GENERATION_ID
```

The response's `assets[0].url` is a permission-checked `GET /v1/assets/{id}`; send the
cookie. A video job uses `"modality":"video"`, `"modelId":"video-demo"`, and
`{"aspectRatio":"16:9","duration":5}`.

Chat streaming needs the chat provider. Create a chat and then post a message with
`Accept: text/event-stream`; the events are documented in `docs/SPEC.md` §10.3:

```bash
CHAT_ID=$(curl -sS -b cookies.txt \
  -H 'Origin: http://localhost:3000' -H 'Content-Type: application/json' \
  -d '{"title":"Launch plan","modelId":"deepseek-v4.1-flash"}' \
  http://localhost:8000/v1/chats | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -sS -N -b cookies.txt \
  -H 'Origin: http://localhost:3000' -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"content":"Give me three campaign angles."}' \
  "http://localhost:8000/v1/chats/$CHAT_ID/messages"
```

Demo reset clears the caller's workspace and restores the current plan allowance. It
returns `204`, is idempotent, and never touches the user row or published Explore posts:

```bash
curl -sS -b cookies.txt -X POST -H 'Origin: http://localhost:3000' \
  -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/demo/reset
```

Errors are RFC 9457 `application/problem+json` with a stable `code` such as
`insufficient_credits`, `model_unavailable`, or `hold_expired`. The full list is in
`docs/SPEC.md` §9.

## Environment variables

`app/core/config.py` is the validator; missing required values fail at startup. `.env` is
gitignored, and `.env.example` contains placeholders only.

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`, `test`, or `production`. |
| `APP_SECRET` | dev placeholder | Must be a real value in production; never reuse the placeholder. |
| `LOG_LEVEL` | `info` | Structured JSON logs. |
| `DATABASE_URL` | required | `postgresql+asyncpg://...`; migrations and runtime use it. |
| `FRONTEND_ORIGIN` | `http://localhost:3000` | Allowed `Origin` for unsafe methods and the proxy origin. |
| `SESSION_COOKIE_NAME` | `nero_session` | |
| `SESSION_TTL_DAYS` | `30` | Sessions are revocable server-side. |
| `COOKIE_SECURE` | derived | True in production; `false` is rejected when `APP_ENV=production`. |
| `RATE_LIMIT_BACKEND` | `postgres` | `postgres` shares windows across processes and survives restarts; `memory` is for tests or one process. `redis` is not implemented and fails at startup. |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | 1 MiB cap; larger bodies are `413 payload_too_large`. |
| `AI_BASE_URL` | unset | Coarena gateway base URL. Blank disables the chat model. |
| `AI_API_KEY` | unset | Server-only; never log, commit, or return it. |
| `AI_CHAT_MODEL` | `deepseek-v4.1-flash` | Must exist on the gateway. |
| `AI_REQUEST_TIMEOUT_SECONDS` | `120` | Read timeout for one provider call. |
| `GENERATION_MODE` | `mock` | `live` has no Image/Video adapter yet, so those models stay unadvertised. |
| `STORAGE_BACKEND` | `local` | Only `local` is implemented; S3 is Phase 7. |
| `STORAGE_DIR` | `./storage` | Asset root. The API and worker must share it. |
| `WORKER_CONCURRENCY` | `2` | Number of claim loops in the worker process. |
| `WORKER_POLL_INTERVAL_SECONDS` | `1` | Sleep between empty claims. |
| `WORKER_STALE_AFTER_SECONDS` | `300` | A lock older than this is recovered. |
| `HOLD_TTL_SECONDS` | `3600` | Expired holds are failed and refunded exactly once. |
| `ALLOW_SIMULATED_PLAN_CHANGES` | true outside production | `POST /v1/account/plan` is a simulated refill, never a payment; set `false` in production. |
| `SENTRY_DSN` | unset | Reserved; error reporting is not wired yet. |

Production additionally requires a real `APP_SECRET`, secure cookies (`COOKIE_SECURE`
unset or true; `false` is rejected), HTTPS at the reverse proxy, and a provider spend cap.

## Frontend in API mode

Run the backend on `8000` and the frontend on `3000`. In the frontend repository
(`nero-ai-front`), `.env.local` selects the backend:

```dotenv
NEXT_PUBLIC_DATA_MODE=api
API_URL=http://127.0.0.1:8000
NEXT_PUBLIC_FRONTEND_ORIGIN=http://localhost:3000
```

- `API_URL` is server-only. The Next.js `/api/*` proxy forwards to `${API_URL}/v1/*`,
  which keeps cookies first-party and avoids CORS.
- The proxy forwards the browser's `Origin`; the backend's `FRONTEND_ORIGIN` must match
  the origin used to open the frontend.
- Restart `npm run dev` after changing `API_URL` or `NEXT_PUBLIC_DATA_MODE`.
- The worker must be running for Image/Video jobs to leave `queued`.
- `NEXT_PUBLIC_DATA_MODE=local` keeps the browser-only demo mode; never run both sources
  of truth at once.

The committed `openapi.json` at the repository root is the snapshot the frontend
generates types from. Regenerate it in the same pull request that changes routes or
schemas:

```bash
uv run python -m scripts.export_openapi          # refresh openapi.json
uv run python -m scripts.export_openapi --check  # what CI runs; fails when stale
```

Policy: additive changes are allowed (a new optional field, path, enum value, or relaxed
validation). Removing or renaming a field or path, changing a type, making an optional
field required, or tightening validation is breaking; it needs an ADR and coordinated
frontend regeneration before it can merge.

## Database and seeding

Migrations live in `alembic/versions/`. The URL comes from `DATABASE_URL`; never put a
real URL in `alembic.ini`.

```bash
uv run alembic upgrade head             # apply migrations
uv run alembic current                  # show the applied revision
uv run alembic revision -m "add table"  # create a migration
```

Migration `0002` seeds the Free, Pro, and Studio plans. The `models` catalog and the
Explore gallery are seeded at runtime because availability depends on configuration:

```bash
uv run python -m app.cli seed  # idempotent; run after `alembic upgrade head`
```

`seed` refreshes plans and models, retires models that are no longer configured, and
copies 16 posts plus 14 bundled JPEG/MP4 assets from `app/db/seed_media/` into
`STORAGE_DIR`. Seeded ids are deterministic, so rerunning restores fixture content
without touching likes or other accounts' posts. `uv run python -m scripts.seed` is an
alias for deploys that prefer a script path.

Tests use a separate database (`nero_test` by default, overridable with
`TEST_DATABASE_URL`). The suite drops and recreates it once per session, so never point
it at a database you care about.

## Tests and checks

CI runs these on every pull request; run them locally before opening one:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run python -m scripts.export_openapi --check
uv run --with pip-audit pip-audit
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=75
docker build -t nero-ai-back:dev .
```

The suite needs PostgreSQL running (`docker compose up -d --wait db`); it recreates
`nero_test` itself. Target coverage is 85% for `app/domain` and `app/providers`, and 75%
overall.

Provider probe, when the chat gateway is configured:

```bash
uv run python -m app.cli provider-check  # one round trip; never prints the key, prompt, or reply
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `403 origin_not_allowed` on a write | Missing or mismatched `Origin` header | Send the frontend origin (default `http://localhost:3000`) or call through the Next proxy. |
| `/health/ready` returns `503` | Database down or migrations pending | `docker compose up -d --wait db`, then `uv run alembic upgrade head`. |
| Startup rejects settings | `APP_ENV=production` with the placeholder secret or `COOKIE_SECURE=false`, or `RATE_LIMIT_BACKEND=redis` | Set real production values; Redis is not implemented, keep `postgres` (or `memory` for one process). |
| `/v1/models` has no chat model | `AI_BASE_URL` or `AI_API_KEY` unset | Set both, then re-run `uv run python -m app.cli seed`. |
| `/v1/models` has no Image/Video models | `GENERATION_MODE` is not `mock` | Set `GENERATION_MODE=mock` and reseed. |
| A Generation stays `queued` | Worker is not running or points at another database | Start `uv run python -m app.worker` with the same `.env`. |
| A Generation fails with `hold_expired` | Its reservation outlived `HOLD_TTL_SECONDS` (worker stalled) | Restart the worker; the sweeper refunds the hold exactly once. |
| An asset URL returns `404` | API and worker use different `STORAGE_DIR`, or the file was removed | Point both processes at the same directory; re-run `seed` for fixture media. |
| `401 unauthorized` from the frontend proxy | No valid session cookie (expired or signed out) | Sign in again; confirm `API_URL` and restart the dev server after env changes. |
| `429 rate_limited` while testing | Signup is 3/hour per IP; login 5/15 min; chat 30/min; generations 10/min | Wait for `Retry-After` or reuse an existing session. |
| `409 duplicate_message` on a retried chat turn | The same `clientId` was already used in that chat | Refetch history instead of retrying the same id. |
| Tests cannot connect | PostgreSQL not running or `TEST_DATABASE_URL` wrong | Start the database; the suite creates and migrates its own test database. |

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
tests/          unit, integration, providers, contract
docs/           spec, roadmap, ADRs, agent docs
scripts/        seed and maintenance commands
```

## Security

- Never commit `.env` or a provider key. Rotate any key that has been pasted into a
  commit, an issue, or a chat.
- Never log prompts, message content, model output, cookies, or Authorization headers.
- Plan changes are simulated in v1. Do not enable paid plans in production without a
  real payment integration.
- Image and Video are deterministic mocks until real provider credentials exist. Never
  present mock output as a real model run.
- Use the same-origin frontend proxy. If direct cross-origin access is ever added, allow
  only the exact `FRONTEND_ORIGIN`; wildcard origins with credentials are never allowed.
- `RATE_LIMIT_BACKEND=postgres` is the default: windows are shared by every process and
  survive a restart. `memory` is single-process only; add the Redis backend before
  high-write multi-instance scaling.
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`, and `X-Frame-Options: DENY`; HSTS is production-only. Errors and stream
  responses are covered too.
- Dependencies are scanned with `uv run --with pip-audit pip-audit` in CI. Address a
  high-severity finding before merge; do not silence it.
