# Nero AI backend: roadmap

The order is deliberate. Do not start a phase until its gate passes. Each ticket is
issue-sized and ends with a PR. The spec in `docs/SPEC.md` is the source of truth for the
contract; this file is the execution order.

Legend: `[ ]` open, `[x]` done. Update this file in the PR that closes a ticket.

---

## Phase 0: Foundation

**Goal:** a running, tested FastAPI skeleton with PostgreSQL, migrations, and CI.

**Gate:** `/health/live` and `/health/ready` pass against a clean database; ruff, mypy,
pytest, Alembic, and Docker build pass in CI.

- [x] **B0.1 Scaffold the Python project**
  - `pyproject.toml` with Python 3.12, uv, FastAPI, Pydantic, SQLAlchemy, asyncpg,
    Alembic, httpx, structlog, pytest, pytest-asyncio, respx, ruff, mypy.
  - Package: `app/main.py`, `app/core/config.py`, `app/core/logging.py`.
  - `.env.example`, `.gitignore` (`.env`, `.venv`, caches, storage).
  - Acceptance: `uv run uvicorn app.main:app` serves `/health/live`; checks pass.

- [x] **B0.2 Error envelope, request IDs, and structured logs**
  - RFC 9457 `problem+json` handler for validation, HTTP, and unhandled errors.
  - `X-Request-ID` in/out; bind `requestId` to logs.
  - Redact authorization, cookies, and provider keys.
  - Acceptance: tests assert shape and request id propagation; no payload logging.

- [x] **B0.3 PostgreSQL, SQLAlchemy, and Alembic**
  - Async engine/session, `Base`, naming conventions, UUIDv7 helper.
  - `docker-compose.yml` with PostgreSQL 16 and a healthcheck.
  - Initial empty migration; `alembic upgrade head` from a clean DB.
  - Acceptance: CI service Postgres runs migrations; test session rolls back.

- [x] **B0.4 CI and task commands**
  - GitHub Actions: `uv sync --frozen`, ruff format check, ruff check, mypy, pytest,
    `alembic upgrade head`, Docker build.
  - Document commands in `README.md`.
  - Acceptance: workflow green on the scaffold PR.

---

## Phase 1: Accounts, sessions, and Credit provisioning

**Goal:** real multi-user auth with revocable server-side sessions and an atomic account
provisioning path.

**Gate:** two accounts can sign up and log in; unauthorized access fails; session
revocation and origin checks are tested.

- [ ] **B1.1 Users, sessions, plans, and account schema**
  - `users`, `sessions`, `plans`, `credit_accounts`, `credit_ledger_entries` tables and
    migration; seed Free, Pro, and Studio.
  - Argon2id password hashing (`pwdlib[argon2]` or `argon2-cffi`).
  - Opaque 32-byte session tokens; store SHA-256 only.
  - The Credit tables are provisioned here so signup is atomic; the ledger service is
    completed in Phase 2.
  - Acceptance: unit tests for hashing and token generation; migration runs clean; seed
    is idempotent.

- [ ] **B1.2 Auth endpoints**
  - `POST /v1/auth/signup`, `login`, `logout`; `GET /v1/auth/me`.
  - Cookie flags per spec; rotate session on login; generic login errors.
  - Signup creates user, session, Free account, and the initial 50-Credit grant in one
    transaction.
  - Acceptance: integration tests for success, duplicate email, wrong password,
    disabled user, cookie flags, and rollback when provisioning fails.

- [ ] **B1.3 Session hardening**
  - `PATCH /v1/auth/me`, `POST /v1/auth/password`, list/revoke sessions.
  - Origin check on unsafe methods; login and signup rate limits.
  - Password change revokes other sessions.
  - Acceptance: CSRF/origin tests, rate-limit tests, revocation tests.

- [ ] **B1.4 Account test matrix**
  - Parametrized authorization tests for authenticated vs anonymous routes.
  - Ensure no route accidentally leaks another user's row.
  - Acceptance: tests fail if a route is added without an auth dependency.

---

## Phase 2: Catalog and Credits

**Goal:** server-authoritative prices and a ledger that cannot overspend.

**Gate:** reserve/commit/refund tests pass, including concurrent reserves; the ledger is
append-only and no operation can make available Credits negative.

- [ ] **B2.1 Model catalog**
  - `models` table, migration, and seed script; plans are already seeded in Phase 1.
  - `GET /v1/plans`, `GET /v1/models`, `GET /v1/models/{id}`.
  - Seed Free/Pro/Studio and the available chat model; mock image/video models only when
    `GENERATION_MODE=mock`.
  - Acceptance: only available models are returned; prices match the seed; public read.

- [ ] **B2.2 Credit ledger service**
  - Implement `reserve`, `commit`, `refund`, `grant`, and `reset` over the Phase 1
    schema; enforce invariants in one place.
  - Row locking; `available = balance - held`; append-only entries.
  - Acceptance: property-style tests, concurrency test with two reserves, ledger entries
    written for every transition.

- [ ] **B2.3 Credit endpoints and simulated plan change**
  - `GET /v1/credits`, `GET /v1/credits/ledger` (cursor pagination),
    `POST /v1/account/plan`.
  - Gate plan changes behind `ALLOW_SIMULATED_PLAN_CHANGES`.
  - Acceptance: ledger pagination, plan refill, no-negative tests.

- [ ] **B2.4 Idempotency foundation**
  - `idempotency_keys` table and helper service.
  - Same key + same request returns the original resource; different request body returns
    `409 idempotency_conflict`.
  - Acceptance: concurrent duplicate requests create one resource.

---

## Phase 3: Chat and streaming

**Goal:** a real DeepSeek V4.1 Flash conversation through Coarena, persisted and billed.

**Gate:** a real key produces a streamed reply through `curl` and the automated fake
provider tests pass; disconnect and provider failure refund Credits; no duplicate message
on retry.

- [ ] **B3.1 Provider port and OpenAI-compatible adapter**
  - `ChatProvider` protocol, DTOs, error taxonomy.
  - Coarena/OpenAI-compatible adapter with httpx streaming, timeouts, first-token retry.
  - `FakeChatProvider` for tests; `python -m app.cli provider-check`.
  - Acceptance: respx tests for SSE parsing, 429, 5xx, timeout, malformed chunks; CLI
    prints model/status/usage only.

- [ ] **B3.2 Chat CRUD**
  - `chats` table, migration, service, routes.
  - Cursor pagination, soft delete, ownership 404 behavior, title generation.
  - Acceptance: CRUD tests, cross-user 404 tests, pagination tests.

- [ ] **B3.3 Messages and SSE**
  - `messages` table and persistence.
  - `POST /v1/chats/{id}/messages` with `message.started`, `message.delta`,
    `message.completed`, `message.failed`, `done`.
  - Reserve/commit/refund integration; heartbeat comments; persist partial content on
    failure.
  - Acceptance: streaming test collects ordered events; credits commit exactly once;
    provider error refunds; no buffering.

- [ ] **B3.4 Disconnect, cancel, and idempotency**
  - Detect client disconnect, cancel provider task, mark message `cancelled`, refund.
  - `clientId` uniqueness per chat; duplicate returns 409.
  - Handle provider timeout and rate limit with mapped codes.
  - Acceptance: abort test with a slow fake provider; duplicate request test; refund test.

- [ ] **B3.5 Real-provider smoke (human-assisted)**
  - Run `provider-check` and one streamed chat with the real key in a local environment.
  - Record the model slug and latency in the PR; never record the key or prompt content.
  - Acceptance: chat completes and usage is stored; key remains only in `.env`.

---

## Phase 4: Explore and assets

**Goal:** a shared gallery that works for anonymous readers and signed-in likers.

**Gate:** anonymous browse works; likes are idempotent and authenticated; assets are
permission-checked and traversal-safe.

- [ ] **B4.1 Assets schema and delivery**
  - `assets` table, local storage adapter, `GET /v1/assets/{id}`.
  - Owner or published-post permission; ETag, cache headers, range support.
  - Acceptance: permission tests, path traversal test, range request test.

- [ ] **B4.2 Posts and likes**
  - `posts`, `post_likes` tables, routes, cursor pagination, `likedByMe`.
  - Idempotent like/unlike.
  - Acceptance: public read, auth write, duplicate like, cross-user isolation tests.

- [ ] **B4.3 Seed Explore**
  - `scripts/seed.py` creates the system author, 16 posts, and bundled sample assets.
  - Idempotent reseed command.
  - Acceptance: fresh DB returns 16 posts with models, prompts, and assets.

---

## Phase 5: Generations and worker

**Goal:** Image and Video flows run end-to-end on deterministic mock providers.

**Gate:** a queued generation completes, assets are served, Credits are correct, cancel
and failure paths refund, and a stale worker lock recovers.

- [ ] **B5.1 Generation API**
  - `generations` table, cost calculation, validation per modality.
  - `POST /v1/generations` (202), list, detail, cancel.
  - Reserve Credits on create; idempotent by header.
  - Acceptance: cost tests, insufficient Credits, invalid params, idempotency tests.

- [ ] **B5.2 Worker and queue**
  - `python -m app.worker`, `FOR UPDATE SKIP LOCKED`, stale lock recovery, retries.
  - Progress updates and structured logs without prompts.
  - Acceptance: worker integration test, two workers do not double-claim, stale recovery.

- [ ] **B5.3 Mock providers and asset persistence**
  - Deterministic image PNG and bundled video MP4 behind real ports.
  - Persist one asset per output; mark `provider: "mock"`.
  - Acceptance: image and video jobs succeed; assets have correct mime/size/dimensions.

- [ ] **B5.4 Cancel, refund, and failure**
  - Cancel queued vs running; refund exactly once; failed jobs retry then refund.
  - Expired hold sweeper.
  - Acceptance: cancel tests, failure tests, no negative balance, no double refund.

- [ ] **B5.5 Demo reset across all resources**
  - `POST /v1/demo/reset` deletes only the caller's Chats, Generations, Assets, and likes,
    then restores the current plan allowance with a `reset` ledger entry.
  - Acceptance: two-user isolation test; reset is idempotent; allowance restored exactly
    once.

---

## Phase 6: Frontend integration

**Goal:** the existing Next.js app uses the backend in API mode without regressing local
mode.

**Gate:** signup, login, chat streaming, history, credits, image, video, explore likes,
reset, and plan change work against the backend in a staging build.

- [ ] **B6.1 OpenAPI and generated types**
  - Commit `openapi.json`; CI check that regeneration is clean.
  - Document the breakage policy and additive changes.
  - Acceptance: snapshot test; schema diff required for route changes.

- [ ] **B6.2 Frontend proxy and HTTP adapter** _(frontend repo PR)_
  - Next rewrite `/api/*` to the backend using server-only `API_URL`.
  - `HttpServices` implementing the existing service interfaces; `NEXT_PUBLIC_DATA_MODE`.
  - Cookie auth, 401 handling, error-code mapping.
  - Acceptance: local mode and API mode both build; adapter tests against a fake server.

- [ ] **B6.3 Golden path against the backend**
  - Playwright: signup, chat stream, history reload, image/video mock generation, explore
    like, demo reset, plan change.
  - Backend runs with fake provider and fresh database.
  - Acceptance: test is deterministic and passes in CI.

- [ ] **B6.4 Documentation and handoff**
  - README quick start, env table, API examples, troubleshooting.
  - Update `GLOSSARY.md` and any ADR touched.
  - Acceptance: a new developer can run the stack from the README alone.

---

## Phase 7: Hardening and deployment

**Goal:** a deployable single-instance service with operational safeguards.

**Gate:** staging deploy passes the golden path; backups restore; rate limits and spend
caps are configured; no secrets in logs.

- [ ] **B7.1 Security hardening**
  - Security headers, body limits, persistent rate limits, dependency audit.
  - Authorization test matrix review; provider key redaction audit.
  - Acceptance: checklist in the PR; no high-severity audit findings unaddressed.

- [ ] **B7.2 Observability and operations**
  - Structured logs, metrics hooks, readiness, backup/restore runbook.
  - Alerts for readiness, provider errors, queue depth, invariant violations.
  - Acceptance: restore drill documented; alert routes tested.

- [ ] **B7.3 Production deployment**
  - Dockerfile, Compose/prod manifests, release migration step, S3 storage adapter.
  - HTTPS, `COOKIE_SECURE=true`, `FRONTEND_ORIGIN`, provider spend cap.
  - Acceptance: staging smoke passes; rollback procedure documented.

- [ ] **B7.4 Real image and video provider** _(blocked: key and budget)_
  - Implement one provider behind the existing ports; keep mock mode for CI.
  - Cost mapping, polling/webhooks, cancel semantics.
  - Acceptance: one real image and one real video job complete; failures refund.

---

## Post-v1 backlog

- Email verification and password reset
- Real billing (Stripe or similar) and invoices
- Publish own Generations to Explore; comments and follows
- Team workspaces, roles, sharing
- Multi-instance queue (Redis + worker), rate limiting in Redis
- Usage dashboards and per-user spend caps
- Desktop shell integration
- Account deletion and data export

---

## Suggested issue sequence

Create issues in this order; each depends on the previous phase gate.

```text
B0.1 -> B0.2 -> B0.3 -> B0.4
B1.1 -> B1.2 -> B1.3 -> B1.4
B2.1 -> B2.2 -> B2.3 -> B2.4
B3.1 -> B3.2 -> B3.3 -> B3.4 -> B3.5 (human-assisted)
B4.1 -> B4.2 -> B4.3
B5.1 -> B5.2 -> B5.3 -> B5.4 -> B5.5
B6.1 -> B6.2 -> B6.3 -> B6.4
B7.1 -> B7.2 -> B7.3 -> B7.4 (blocked on provider key)
```

## Phase status

| Phase | Status |
| --- | --- |
| 0 Foundation | done |
| 1 Accounts | not started |
| 2 Credits | not started |
| 3 Chat | not started |
| 4 Explore | not started |
| 5 Generations | not started |
| 6 Frontend | not started |
| 7 Deploy | not started |
