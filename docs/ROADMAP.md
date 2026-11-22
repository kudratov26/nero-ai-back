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

- [x] **B1.1 Users, sessions, plans, and account schema**
  - `users`, `sessions`, `plans`, `credit_accounts`, `credit_ledger_entries` tables and
    migration; seed Free, Pro, and Studio.
  - Argon2id password hashing (`pwdlib[argon2]` or `argon2-cffi`).
  - Opaque 32-byte session tokens; store SHA-256 only.
  - The Credit tables are provisioned here so signup is atomic; the ledger service is
    completed in Phase 2.
  - Acceptance: unit tests for hashing and token generation; migration runs clean; seed
    is idempotent.

- [x] **B1.2 Auth endpoints**
  - `POST /v1/auth/signup`, `login`, `logout`; `GET /v1/auth/me`.
  - Cookie flags per spec; rotate session on login; generic login errors.
  - Signup creates user, session, Free account, and the initial 50-Credit grant in one
    transaction.
  - Acceptance: integration tests for success, duplicate email, wrong password,
    disabled user, cookie flags, and rollback when provisioning fails.

- [x] **B1.3 Session hardening**
  - `PATCH /v1/auth/me`, `POST /v1/auth/password`, list/revoke sessions.
  - Origin check on unsafe methods; login and signup rate limits.
  - Password change revokes other sessions.
  - Acceptance: CSRF/origin tests, rate-limit tests, revocation tests.

- [x] **B1.4 Account test matrix**
  - Parametrized authorization tests for authenticated vs anonymous routes.
  - Ensure no route accidentally leaks another user's row.
  - Acceptance: tests fail if a route is added without an auth dependency.

---

## Phase 2: Catalog and Credits

**Goal:** server-authoritative prices and a ledger that cannot overspend.

**Gate:** reserve/commit/refund tests pass, including concurrent reserves; the ledger is
append-only and no operation can make available Credits negative.

- [x] **B2.1 Model catalog**
  - `models` table, migration (`0003`), and `python -m app.cli seed`; plans were already
    seeded in Phase 1.
  - `GET /v1/plans`, `GET /v1/models`, `GET /v1/models/{id}` (public).
  - Seeds the chat model from `AI_CHAT_MODEL`, advertised only when the gateway is
    configured; the `Demo` image/video models only when `GENERATION_MODE=mock`.
  - Acceptance: only available models are returned (unknown or unavailable → `404`);
    prices match the seed; all three reads are public. Covered by
    `tests/integration/test_catalog.py` and the route matrix.

- [x] **B2.2 Credit ledger service**
  - `app/domain/credits.py` implements `reserve`, `commit`, `refund`, `grant`, and
    `reset` over the Phase 1 schema; the signup opening grant now goes through `grant`.
  - Each operation locks the account row (`SELECT ... FOR UPDATE`), applies the change,
    and appends one immutable ledger entry in the caller's transaction.
  - Acceptance: property-style random-walk test, a concurrency test where two reserves
    cannot overspend, and ledger-entry assertions for every transition
    (`tests/integration/test_credits.py`).

- [x] **B2.3 Credit endpoints and simulated plan change**
  - `GET /v1/credits` (summary), `GET /v1/credits/ledger` (opaque-cursor pagination,
    newest first, own entries only), and `POST /v1/account/plan`.
  - Plan changes are gated behind `ALLOW_SIMULATED_PLAN_CHANGES` and rejected with
    `409 conflict` while Credits are held.
  - Acceptance: ledger pagination by cursor, plan refill to the new allowance, the
    no-negative invariant under a hold, and the route/auth matrix. Covered by
    `tests/integration/test_billing.py`, `tests/unit/test_cursor.py`, and
    `tests/unit/test_config.py`.

- [x] **B2.4 Idempotency foundation**
  - `idempotency_keys` table (migration `0004`), keyed on `(user_id, key)`, plus
    `app/domain/idempotency.py` as the helper service (`hash_request`, `find`, `execute`).
  - Same key + same request returns the original resource; a different body or operation
    returns `409 idempotency_conflict`.
  - Acceptance: concurrent duplicates create one resource, replayed work runs `create`
    once, and a non-race integrity error propagates without a key row. Covered by
    `tests/integration/test_idempotency.py`, `tests/unit/test_idempotency.py`, and the
    schema tests.

---

## Phase 3: Chat and streaming

**Goal:** a real DeepSeek V4.1 Flash conversation through Coarena, persisted and billed.

**Gate:** a real key produces a streamed reply through `curl` and the automated fake
provider tests pass; disconnect and provider failure refund Credits; no duplicate message
on retry.

- [x] **B3.1 Provider port and OpenAI-compatible adapter**
  - `ChatProvider` protocol, DTOs, and error taxonomy in `app/providers/base.py`.
  - Coarena/OpenAI-compatible adapter (`app/providers/openai_compatible.py`) with httpx
    streaming, connect/write/read timeouts, and a jittered retry that runs only before the
    first delta; `FakeChatProvider` (`app/providers/fake.py`) for tests;
    `python -m app.cli provider-check`.
  - Acceptance: respx tests for SSE parsing, 429, 5xx, timeout, and malformed chunks; the
    CLI prints model/status/usage only. Covered by `tests/providers/`,
    `tests/unit/test_providers.py`, `tests/unit/test_cli.py`, and
    `tests/unit/test_config.py`.

- [x] **B3.2 Chat CRUD**
  - `chats` table, migration, service, routes.
  - Cursor pagination, soft delete, ownership 404 behavior, title generation.
  - Acceptance: CRUD tests, cross-user 404 tests, pagination tests. Covered by
    `tests/integration/test_chats.py`, `tests/integration/test_schema.py`,
    `tests/integration/test_auth_matrix.py`, and `tests/unit/test_chat_titles.py`.

- [x] **B3.3 Messages and SSE**
  - `messages` table and persistence.
  - `POST /v1/chats/{id}/messages` with `message.started`, `message.delta`,
    `message.completed`, `message.failed`, `done`; `GET /v1/chats/{id}/messages` for
    cursor-paginated history.
  - Reserve/commit/refund integration; heartbeat comments; persist partial content on
    failure.
  - Acceptance: streaming test collects ordered events; credits commit exactly once;
    provider error refunds; no buffering. Covered by
    `tests/integration/test_messages.py` and `tests/integration/test_schema.py`.

- [x] **B3.4 Disconnect, cancel, and idempotency**
  - Detect client disconnect, cancel provider task, mark message `cancelled`, refund.
  - `clientId` uniqueness per chat; duplicate returns `409 duplicate_message`.
  - Handle provider timeout and rate limit with mapped codes.
  - Acceptance: abort test with a slow fake provider; duplicate request test; refund test.
    Covered by `tests/integration/test_messages.py`; the duplicate code is in
    `app/api/problems.py`.

- [x] **B3.5 Real-provider smoke (human-assisted)**
  - Run `provider-check` and one streamed chat with the real key in a local environment.
  - Record the model slug and latency in the PR; never record the key or prompt content.
  - Acceptance: chat completes and usage is stored; key remains only in `.env`.
    Verified against `deepseek-v4-1-flash-1017`: `provider-check` returned `status=ok`,
    and a `curl` stream emitted `message.started`, deltas, `message.completed`, and
    `done`; history stored usage and the ledger recorded `reserve` then `commit`.

---

## Phase 4: Explore and assets

**Goal:** a shared gallery that works for anonymous readers and signed-in likers.

**Gate:** anonymous browse works; likes are idempotent and authenticated; assets are
permission-checked and traversal-safe.

- [x] **B4.1 Assets schema and delivery**
  - `assets` table, local storage adapter, `GET /v1/assets/{id}`.
  - Owner or published-post permission; ETag, cache headers, range support.
  - Acceptance: permission tests, path traversal test, range request test.
    Owner delivery is implemented in `tests/integration/test_assets.py`;
    traversal safety is in `tests/unit/test_storage.py` plus the symlink test;
    published-post access joins the same permission helper in B4.2, when `posts`
    exists.

- [x] **B4.2 Posts and likes**
  - `posts`, `post_likes` tables, routes, cursor pagination, `likedByMe`.
  - Idempotent like/unlike.
  - Acceptance: public read, auth write, duplicate like, cross-user isolation tests.
    Public browsing, cursor paging, and the like matrix live in
    `tests/integration/test_explore.py`; published-post asset delivery extends
    `tests/integration/test_assets.py`. The system author and seeded posts are B4.3.

- [x] **B4.3 Seed Explore**
  - `scripts/seed.py` creates the system author, 16 posts, and bundled sample assets.
  - Idempotent reseed command.
  - Acceptance: fresh DB returns 16 posts with models, prompts, and assets.
    The fixtures and the upsert live in `app/db/explore_seed.py`; the CLI runs them
    with the catalog seed, and `tests/integration/test_seed.py` proves the fresh
    gallery, anonymized media delivery, stable ordering, and an idempotent rerun
    that preserves likes.

---

## Phase 5: Generations and worker

**Goal:** Image and Video flows run end-to-end on deterministic mock providers.

**Gate:** a queued generation completes, assets are served, Credits are correct, cancel
and failure paths refund, and a stale worker lock recovers.

- [x] **B5.1 Generation API**
  - `generations` table, cost calculation, validation per modality.
  - `POST /v1/generations` (202), list, detail, cancel.
  - Reserve Credits on create; idempotent by header.
  - Acceptance: cost tests, insufficient Credits, invalid params, idempotency tests.

- [x] **B5.2 Worker and queue**
  - `python -m app.worker`, `FOR UPDATE SKIP LOCKED`, stale lock recovery, retries.
  - Progress updates and structured logs without prompts.
  - Acceptance: worker integration test, two workers do not double-claim, stale recovery.

- [x] **B5.3 Mock providers and asset persistence**
  - Deterministic image PNG and bundled video MP4 behind real ports.
  - Persist one asset per output; mark `provider: "mock"`.
  - Acceptance: image and video jobs succeed; assets have correct mime/size/dimensions.

- [x] **B5.4 Cancel, refund, and failure**
  - Cancel queued vs running; refund exactly once; failed jobs retry then refund.
  - Expired hold sweeper.
  - Acceptance: cancel tests, failure tests, no negative balance, no double refund.

- [x] **B5.5 Demo reset across all resources**
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

- [x] **B6.1 OpenAPI and generated types**
  - Commit `openapi.json`; CI check that regeneration is clean.
  - Document the breakage policy and additive changes.
  - Acceptance: snapshot test; schema diff required for route changes.

- [x] **B6.2 Frontend proxy and HTTP adapter** _(frontend repo PR: `nero-ai-front` #27)_
  - Next rewrite `/api/*` to the backend using server-only `API_URL`.
  - `HttpServices` implementing the existing service interfaces; `NEXT_PUBLIC_DATA_MODE`.
  - Cookie auth, 401 handling, error-code mapping.
  - Acceptance: local mode and API mode both build; adapter tests against a fake server.

- [x] **B6.3 Golden path against the backend** _(frontend repo PR: `nero-ai-front` #28)_
  - Playwright: signup, chat stream, history reload, image/video mock generation, explore
    like, demo reset, plan change.
  - Backend runs with fake provider and fresh database.
  - Acceptance: test is deterministic and passes in CI.

- [x] **B6.4 Documentation and handoff**
  - README quick start, env table, API examples, troubleshooting.
  - Update `GLOSSARY.md` and any ADR touched.
  - Acceptance: a new developer can run the stack from the README alone.

---

## Phase 7: Hardening and deployment

**Goal:** a deployable single-instance service with operational safeguards.

**Gate:** staging deploy passes the golden path; backups restore; rate limits and spend
caps are configured; no secrets in logs.

- [x] **B7.1 Security hardening**
  - Security headers, body limits, persistent rate limits, dependency audit.
  - Authorization test matrix review; provider key redaction audit.
  - Acceptance: checklist in the PR; no high-severity audit findings unaddressed.

- [x] **B7.2 Observability and operations**
  - Structured logs, metrics hooks, readiness, backup/restore runbook.
  - Alerts for readiness, provider errors, queue depth, invariant violations.
  - Acceptance: restore drill documented; alert routes tested.

- [x] **B7.3 Production deployment**
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
| 1 Accounts | complete (B1.1–B1.4) |
| 2 Credits | complete (B2.1–B2.4) |
| 3 Chat | complete (B3.1–B3.5) |
| 4 Explore | complete (B4.1–B4.3) |
| 5 Generations | complete (B5.1–B5.5) |
| 6 Frontend | complete (B6.1–B6.4; B6.2–B6.3 shipped in `nero-ai-front`) |
| 7 Deploy | in progress (B7.1–B7.3 done; B7.4 blocked on provider key and budget) |
