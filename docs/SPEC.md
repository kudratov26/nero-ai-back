# Nero AI backend: product and technical specification

Version: 1.0 (v1)
Status: proposed; implementation starts at Phase 0 in `docs/ROADMAP.md`.
Frontend: `nero-ai-front` (Next.js). This document is the contract between the two repos.

## 1. Purpose

Turn the local-first Nero AI demo into a multi-user application with:

- real accounts and server-side sessions
- a server-authoritative model catalog and Credit ledger
- real chat streaming through a model provider
- persistent Chats, Generations, and Assets across devices
- a shared Explore gallery with likes
- a clean path to add real image and video providers later

The frontend keeps its local mode as an offline demo. API mode is selected explicitly and
uses this backend as the source of truth.

## 2. Scope

### In scope for v1

- Email + password signup, login, logout, current-user endpoint
- Opaque session cookies, session revocation, password change
- Plans and a simulated plan-change flow
- Credit accounts, append-only ledger, holds, commits, refunds
- Server-driven model catalog
- Chat CRUD, message persistence, SSE streaming from one real chat provider
- Generation jobs for Image and Video using deterministic mock providers
- Asset storage and authenticated delivery
- Explore posts and likes, seeded from bundled assets
- Per-user demo reset
- OpenAPI contract and generated TypeScript types
- Docker deployment with PostgreSQL and a worker

### Explicit non-goals for v1

- Real payments or invoices (plan changes are simulated and dev-only)
- Email verification, password reset by email, MFA
- User uploads, public publishing from a Generation, comments, follows
- Real image or video generation (requires separate provider credentials)
- Team accounts, roles beyond `user`/`admin`, sharing
- Mobile apps and desktop shell
- Horizontal scaling beyond one API instance and one worker

## 3. Actors

| Actor | Description |
| --- | --- |
| Anonymous | Can read Explore, plans, and the available model catalog |
| User | Authenticated account; owns Chats, Generations, Assets, liked posts |
| Admin | Internal only in v1; seeds catalog and gallery via CLI, no admin UI |
| Provider | Coarena gateway for DeepSeek V4.1 Flash; mocked for Image/Video |
| Worker | Background process that runs Generation jobs |

## 4. Runtime architecture

```text
Browser (Next.js)
   │  same-origin /api/* rewrite
   ▼
FastAPI  ──► PostgreSQL
   │
   ├──► Coarena / OpenAI-compatible chat provider (streaming)
   ├──► mock image provider
   ├──► mock video provider
   └──► asset storage (local disk in dev, S3-compatible later)
   ▲
   └── Generation worker (polling Postgres queue)
```

The browser never talks to the provider directly. The API key never leaves the server.
In production the frontend proxies `/api/*` so cookies are first-party and CORS is not
needed. Direct cross-origin access is supported only for local development.

## 5. Conventions

- Base path: `/v1`
- JSON field names: camelCase. Python names: snake_case, mapped with Pydantic aliases.
- IDs: UUIDv7 strings, sortable by creation time.
- Timestamps: ISO 8601 UTC with `Z`.
- Credits: signed integers.
- Pagination: opaque cursor, `limit` default 20, max 100.
- Response envelope for lists: `{ "items": [...], "nextCursor": "..." | null }`
- Errors: `application/problem+json` (RFC 9457 shape), see section 9.
- Idempotency: `Idempotency-Key` on generation creation; a chat message carries the
  client's `clientId`, and a retry is rejected as `duplicate_message`.
- Every response includes `X-Request-ID`; clients may send one, otherwise it is generated.
- API version changes only for breaking contract changes. Additive fields are allowed.

## 6. Authentication and accounts

### 6.1 User model

```text
users
  id                 uuid pk
  email              text unique, normalized lowercase
  display_name       text, 1..80
  password_hash      text (Argon2id)
  role               text: user | admin, default user
  status             text: active | disabled
  email_verified_at  timestamptz null
  last_login_at      timestamptz null
  created_at         timestamptz
  updated_at         timestamptz
```

Email is unique, case-insensitive. Display name is required at signup, defaults to the
email local part. Passwords are 10 to 128 characters; no composition rules. Reject
passwords equal to the email and common-password entries if a small denylist is present.

### 6.2 Sessions

```text
sessions
  id            uuid pk
  user_id       uuid fk users
  token_hash    bytea unique  (SHA-256 of the opaque token)
  user_agent    text null
  ip            inet null
  created_at    timestamptz
  last_used_at  timestamptz
  expires_at    timestamptz
  revoked_at    timestamptz null
```

- Token: 32 random bytes, URL-safe encoded. Returned once in `Set-Cookie`.
- Cookie: `nero_session=<token>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`;
  `Secure` when `APP_ENV=production`.
- TTL 30 days. Rotate the token on login and on password change.
- Logout revokes the current session and clears the cookie.
- Password change revokes all other sessions.
- `last_used_at` updates at most once per 5 minutes to avoid a write per request.

### 6.3 CSRF

Cookies are first-party through the Next rewrite. For `POST`, `PUT`, `PATCH`, and
`DELETE`, require an `Origin` header equal to `FRONTEND_ORIGIN` (or the API origin).
Missing or mismatched origin returns `403 origin_not_allowed`. No double-submit token in
v1 because the app is same-origin. If a cross-site deployment is introduced, add one and
record an ADR.

### 6.4 Auth endpoints

```text
POST   /v1/auth/signup      { email, password, displayName }  -> 201 MeResponse + cookie
POST   /v1/auth/login       { email, password }               -> 200 MeResponse + cookie
POST   /v1/auth/logout                                         -> 204
GET    /v1/auth/me                                             -> 200 MeResponse
PATCH  /v1/auth/me          { displayName }                    -> 200 User
POST   /v1/auth/password    { currentPassword, newPassword }   -> 204, revokes other sessions
GET    /v1/auth/sessions                                       -> 200 list of own sessions
DELETE /v1/auth/sessions/{id}                                  -> 204
```

`MeResponse`:

```json
{
  "user": {
    "id": "0193...",
    "email": "creator@example.com",
    "displayName": "Creator",
    "role": "user",
    "createdAt": "2026-11-14T12:00:00Z"
  },
  "plan": { "id": "free", "name": "Free", "monthlyCredits": 50 },
  "credits": {
    "balance": 50,
    "held": 0,
    "available": 50,
    "allowance": 50,
    "periodStart": "2026-11-14T12:00:00Z",
    "periodEnd": "2026-12-14T12:00:00Z"
  }
}
```

Signup grants the Free plan allowance in the same transaction. Login failures return a
generic `401 invalid_credentials` regardless of whether the email exists.

`GET /v1/auth/sessions` returns the caller's live sessions, newest first:

```json
[
  {
    "id": "0193...",
    "userAgent": "Mozilla/5.0 ...",
    "ip": "203.0.113.7",
    "createdAt": "2026-11-14T12:00:00Z",
    "lastUsedAt": "2026-11-14T12:05:00Z",
    "expiresAt": "2026-12-14T12:00:00Z",
    "current": true
  }
]
```

`DELETE /v1/auth/sessions/{id}` revokes one session and is idempotent; an id that
belongs to another user is `404 not_found`. Revoking the current session also clears
the cookie.

`PATCH /v1/auth/me` takes `{ displayName }` and returns the `User` object. `POST
/v1/auth/password` takes `{ currentPassword, newPassword }`, returns `204` with a
replacement cookie, and revokes every other session. A wrong current password is
`403 forbidden`.

Rate-limited responses carry `Retry-After` in seconds.

## 7. Plans and Credits

### 7.1 Plans

Seeded to match the frontend:

| id | name | price | monthly Credits |
| --- | --- | --- | --- |
| free | Free | $0 | 50 |
| pro | Pro | $19 | 1,000 |
| studio | Studio | $49 | 3,000 |

Fields: `id`, `name`, `priceCents`, `monthlyCredits`, `tagline`, `features[]`,
`highlighted`, `sortOrder`.

Plan changes are simulated; there is no payment provider in v1. The endpoint is enabled
by default everywhere except production, and `ALLOW_SIMULATED_PLAN_CHANGES` forces it on
or off. Applying a plan:

1. locks the Credit account
2. rejects the change with `409 conflict` while `held > 0`
3. records a `plan_change` ledger entry
4. replaces the allowance and refills available Credits to the new allowance
5. starts a new 30-day period

This is intentionally not a billing system. A production deployment must replace it with
a payment provider before enabling paid plans.

### 7.2 Credit account and ledger

```text
credit_accounts
  user_id          uuid pk fk users
  plan_id          text fk plans
  balance          integer >= 0   -- total owned Credits
  held             integer >= 0   -- reserved for in-flight work
  allowance        integer >= 0   -- plan Credits for the current period
  period_start     timestamptz
  period_end       timestamptz
  updated_at       timestamptz

credit_ledger_entries
  id               uuid pk
  user_id          uuid fk users
  kind             text: grant | reserve | commit | refund | plan_change | adjustment | reset
  delta_balance    integer
  delta_held       integer
  balance_after    integer
  held_after       integer
  reason           text
  ref_type         text null   -- chat_message | generation | plan | demo_reset
  ref_id           uuid null
  created_at       timestamptz
```

Invariants:

- `available = balance - held`, never negative.
- Ledger rows are immutable. No updates or deletes through the application.
- Every reserve is eventually committed or refunded, including timeouts.
- Holds expire: 15 minutes for chat, job duration + 10 minutes for generations. A sweeper
  refunds expired holds and marks the related work failed.
- Operations lock the account row (`SELECT ... FOR UPDATE`) and update the ledger in the
  same transaction.

`app/domain/credits.py` is the only writer. It exposes `reserve`, `commit`, `refund`,
`grant`, and `reset`; each locks the account row, applies the change, and appends one
`credit_ledger_entries` row in the caller's transaction, so Credits and the related domain
rows commit or roll back together. The opening signup grant goes through the same path.

Lifecycle (`delta_balance`/`delta_held` are the changes to the account columns):

| Step | balance | held | available | delta_balance | delta_held | kind |
| --- | --- | --- | --- | --- | --- | --- |
| Grant 50 | 50 | 0 | 50 | +50 | 0 | grant |
| Reserve 2 | 50 | 2 | 48 | 0 | +2 | reserve |
| Commit 2 | 48 | 0 | 48 | -2 | -2 | commit |
| Refund 2 instead | 50 | 0 | 50 | 0 | -2 | refund |

A reserve larger than `available` is `409 insufficient_credits` with the required and
remaining amounts, e.g. `This run needs 15 Credits; 6 are available.` A non-positive
amount or settling more than is held is a programming error (the client never sends an
amount), and `reset` restores `balance` to the current `allowance` and refunds any hold
with a single `reset` entry; when nothing changes it writes nothing.

### 7.3 Credit endpoints

```text
GET  /v1/credits                       -> CreditsSummary
GET  /v1/credits/ledger?cursor=&limit= -> LedgerPage, own entries only
POST /v1/account/plan   { planId }     -> MeResponse (simulated only)
POST /v1/demo/reset                    -> 204, resets own workspace
```

`CreditsSummary` is the `credits` object from `MeResponse`. Model costs are always read
from the server catalog; the client never sends a cost.

`GET /v1/credits/ledger` returns the caller's entries newest first as an opaque-cursor
page. `limit` defaults to 20 and is capped at 100.

```json
{
  "items": [
    {
      "id": "0193...",
      "kind": "plan_change",
      "deltaBalance": 950,
      "deltaHeld": 0,
      "balanceAfter": 1000,
      "heldAfter": 0,
      "reason": "Changed plan to Pro",
      "refType": "plan",
      "refId": null,
      "createdAt": "2026-11-14T12:00:00Z"
    }
  ],
  "nextCursor": "eyJjcmVhdGVkQXQiOiI..."
}
```

`kind` is one of `grant`, `reserve`, `commit`, `refund`, `plan_change`, `adjustment`,
`reset`; `refType` is `chat_message`, `generation`, `plan`, `demo_reset`, or `null`.
`nextCursor` is `null` on the last page. The cursor is opaque; a malformed one is
`400 invalid_request` with the detail `The cursor is not valid.` Entries are immutable and
never deleted, so pages stay stable while new entries are appended.

`POST /v1/account/plan` switches the plan and refills the account, then returns the new
`MeResponse`. An unknown `planId` is `404 not_found`; a change while Credits are held is
`409 conflict`; when plan changes are disabled the route is `404 not_found`. Plan changes
are only a simulated refill and never touch a payment provider.

Demo reset (`POST /v1/demo/reset`, `204`) refunds any held Credits, deletes the user's
Chats (and their Messages), Generations, Assets and their stored bytes, likes, and create
idempotency records, then restores the current plan allowance with a single `reset` ledger
entry (`refType: "demo_reset"`). It writes no entry when the account already sits at the
allowance with no hold, so it is idempotent. It never touches the user row, password, or
published Explore posts, and it only affects the caller.

## 8. Models

```text
models
  id                 text pk        -- public slug, stable across providers
  name               text
  provider           text           -- display name, e.g. Coarena
  provider_model_id  text           -- sent to the provider
  modality           text: chat | image | video
  description        text
  badge              text null
  credit_cost        integer        -- per message, image, or 5s of video
  credit_cost_10s    integer null   -- video only
  capabilities       jsonb          -- streaming, vision, maxOutputTokens...
  context_window     integer null
  available          boolean
  sort_order         integer
```

`provider_model_id` is internal plumbing and is never returned to a client.

Seed rules (written by `python -m app.cli seed`, not by a migration, because
availability depends on configuration):

- The chat model is `AI_CHAT_MODEL` and is available only when `AI_BASE_URL` and
  `AI_API_KEY` are both set. Run `python -m app.cli provider-check` (11.2) with the real
  key before trusting the catalog row; B3.5 performs the first real round trip.
- Do not list GPT, Claude, Gemini, or other providers as available without an adapter.
- The Image and Video models are the deterministic `Demo` providers. They are available
  only when `GENERATION_MODE=mock`; the response carries `provider: "Demo"` so no caller
  can mistake them for real output.
- Re-seeding refreshes rows and retires models that are no longer configured: the row is
  marked `available: false` rather than deleted, because Chats and Generations reference a
  model with `ON DELETE RESTRICT`. A changed `AI_CHAT_MODEL` therefore never leaves the old
  slug advertised, and history keeps naming the model that produced it.
- Changing a price is a seed change plus an OpenAPI-visible catalog update.

Seeded credits: chat message `1`, image `5`, video `15` for 5s and `30` for 10s.

### 8.1 Catalog reads

```text
GET /v1/models?modality=chat|image|video   -> [Model]
GET /v1/models/{id}                        -> Model | 404 not_found
GET /v1/plans                              -> [Plan]
```

All three are public (no session). Only models with `available = true` are ever listed,
and `GET /v1/models/{id}` returns `404 not_found` for an unknown *or* unavailable model —
an unavailable model is indistinguishable from a missing one so the catalog can never
advertise work the server cannot do. An unknown `modality` is `400 invalid_request`.

`Model` JSON (camelCase; `available` and `providerModelId` are omitted):

```json
{
  "id": "deepseek-v4.1-flash",
  "name": "DeepSeek V4.1 Flash",
  "provider": "Coarena",
  "modality": "chat",
  "description": "Flagship assistant for reasoning, writing, and code.",
  "badge": "New",
  "creditCost": 1,
  "creditCost10s": null,
  "capabilities": { "streaming": true, "vision": false, "maxOutputTokens": 8192 },
  "contextWindow": 128000,
  "sortOrder": 0
}
```

## 9. Errors

Shape:

```json
{
  "type": "https://nero.ai/problems/insufficient-credits",
  "title": "Insufficient Credits",
  "status": 409,
  "code": "insufficient_credits",
  "detail": "This run needs 15 Credits; 6 are available.",
  "requestId": "0193..."
}
```

`code` values:

`invalid_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`,
`duplicate_message`, `email_taken`, `invalid_credentials`, `weak_password`, `rate_limited`,
`insufficient_credits`, `model_unavailable`, `provider_error`, `provider_timeout`,
`generation_not_cancellable`, `hold_expired`, `idempotency_conflict`,
`origin_not_allowed`, `payload_too_large`, `internal_error`.

Mapping: 400 validation, 401 unauthenticated, 403 authorization/CSRF, 404 missing or not
owned, 409 state conflicts, 422 semantic validation, 429 rate limit, 502/504 provider,
500 unexpected. Never leak provider response bodies, SQL, or stack traces.

## 10. Chats and messages

### 10.1 Chat model

```text
chats
  id          uuid pk
  user_id     uuid fk
  title       text, default "New chat"
  title_is_custom  boolean, default false
  model_id    text fk models
  created_at  timestamptz
  updated_at  timestamptz
  deleted_at  timestamptz null

messages
  id           uuid pk
  chat_id      uuid fk chats
  user_id      uuid fk users
  role         text: user | assistant | system
  content      text
  model_id     text null
  status       text: complete | streaming | failed | cancelled
  prompt_tokens      integer null
  completion_tokens  integer null
  error_code   text null
  client_id    text null   -- client-generated message id
  created_at   timestamptz
  updated_at   timestamptz
```

Indexes: `(user_id, updated_at, id)` on chats where `deleted_at is null`;
`(chat_id, created_at)` on messages; unique `(chat_id, client_id)` where not null.

Titles: first user message, trimmed to 60 characters, unless the user set one.
`title_is_custom` records whether the caller chose the title; B3.3 derives a title from
the first message only while it is false, so a rename is never overwritten.

Deleting a chat is soft: `deleted_at` is set, the row and its messages stay, and every
read and listing excludes it. A second `DELETE` is a no-op (`204`), so a retried delete
succeeds. `chats.model_id` is `ON DELETE RESTRICT`, so retiring a model from the catalog
marks it unavailable rather than deleting the row that history points at.

### 10.2 Endpoints

```text
GET    /v1/chats?cursor=                 -> paginated ChatSummary
POST   /v1/chats          { title?, modelId } -> 201 Chat
GET    /v1/chats/{id}                    -> Chat
PATCH  /v1/chats/{id}     { title }      -> Chat
DELETE /v1/chats/{id}                    -> 204 soft delete
GET    /v1/chats/{id}/messages?before=   -> paginated Message list
POST   /v1/chats/{id}/messages           -> text/event-stream, see 10.3
```

Ownership is checked for every route. A missing chat and someone else's chat both return
404 to avoid resource enumeration.

Shapes (implemented in B3.2 and B3.3):

```json
{ "id": "0193...", "title": "Launch plan", "modelId": "deepseek-v4.1-flash",
  "createdAt": "2026-11-15T12:00:00Z", "updatedAt": "2026-11-15T12:00:00Z" }
```

A `Message` carries the persisted turn, including a partial failed reply and its mapped
error code:

```json
{ "id": "0193...", "role": "assistant", "content": "Shoot the campaign",
  "modelId": "deepseek-v4.1-flash", "status": "complete", "promptTokens": 12,
  "completionTokens": 80, "errorCode": null, "clientId": null,
  "createdAt": "2026-11-15T12:00:00Z", "updatedAt": "2026-11-15T12:00:01Z" }
```

`role` is `system`, `user`, or `assistant`; `status` is `complete`, `streaming`, `failed`,
or `cancelled`. `modelId` is null on user turns and names the model that produced an
assistant turn. Usage fields are null when the provider does not report them. A user turn
may carry the client's `clientId`; an assistant turn never does.

`POST /v1/chats` answers `422 model_unavailable` when `modelId` is unknown, not a chat
model, or not currently advertised. `GET /v1/chats` is ordered by `updatedAt` descending
with `id` as the tiebreaker, returns at most `limit` (1–100, default 20) items, and sets
`nextCursor` only when another page exists. The cursor is opaque and escapes to the
client, so a malformed one is `400 invalid_request`.

`GET /v1/chats/{id}/messages` is ordered by `createdAt` descending with `id` as the
tiebreaker, so a page reads newest first. `before` is the opaque `nextCursor` of the
previous page. `limit` is 1–100 with a default of 50, and `nextCursor` is null when there
is nothing older. Failed and partial assistant turns are included so a client can render
what the model produced before the failure. A malformed cursor is `400 invalid_request`;
a missing or foreign chat is `404 not_found`.

`POST /v1/chats/{id}/messages` accepts `content` (1–4000 characters, not blank),
`clientId` (up to 120 characters, optional), and `modelId` (optional). When `modelId` is
present it must match the chat's model, otherwise the response is `400 invalid_request`.
The server checks the chat, the model's current availability, the provider configuration,
and Credits before it returns `200`; each failure is a normal JSON problem response
(`404 not_found`, `422 model_unavailable`, or `409 insufficient_credits`).

### 10.3 Streaming protocol

Request:

```json
{ "content": "Give me three campaign angles.", "modelId": "deepseek-v4.1-flash", "clientId": "c-1" }
```

`content` is required and limited to 4000 characters. `clientId` is optional; it is
stored on the user turn and is unique per chat. `modelId` is optional and must equal the
chat's model when sent.

Headers: `Accept: text/event-stream`.

The server checks auth, chat ownership, model availability, and Credits **before**
returning 200. Insufficient Credits is a normal 409 JSON response.

Events, in order:

```text
event: message.started
data: {"userMessageId":"...","assistantMessageId":"...","modelId":"..."}

event: message.delta
data: {"messageId":"...","delta":"Shoot"}

event: message.completed
data: {"messageId":"...","content":"...","usage":{"promptTokens":12,"completionTokens":80},
       "credits":{"balance":48,"held":0,"available":48,"charged":2}}

event: done
data: {}
```

Failure:

```text
event: message.failed
data: {"messageId":"...","code":"provider_timeout","detail":"The model did not respond.","refunded":true}
```

A failure is terminal: `message.failed` is the last event and no `done` follows.

Rules:

- Persist the user message and an assistant placeholder before calling the provider.
- Reserve Credits before the provider call; commit on `message.completed`; refund on
  failure or client disconnect before completion. A failed stream stores the partial
  content, `status=failed`, and the mapped `errorCode` before the refund commits.
- On disconnect, cancel the provider request, persist the partial content with
  `status=cancelled`, and refund the reservation. Credits move exactly once: a reply
  interrupted during its completion write still ends either committed or refunded.
- Send a heartbeat comment (`: ping`) every 15 seconds.
- No resume/replay in v1. If a client retries with the same `(chatId, clientId)`, return
  `409 duplicate_message`; the client should refetch history.
- Do not buffer the whole response; flush each delta.
- Map provider errors to `provider_error`, `provider_timeout`, or `rate_limited`.

## 11. Providers

### 11.1 Port

```python
class ChatProvider(Protocol):
    async def stream_chat(
        self, *, messages: list[ProviderMessage], model: ProviderModel, params: ChatParams
    ) -> AsyncIterator[ChatDelta]: ...

class ImageProvider(Protocol):
    async def generate(self, *, prompt: str, model: ProviderModel, params: dict) -> ProviderAssetBatch: ...

class VideoProvider(Protocol):
    async def submit(self, ...) -> ProviderJob: ...
    async def poll(self, job_id: str) -> ProviderJob: ...
```

Ports live in `app/providers/base.py`. Domain code depends on ports, never on httpx or a
vendor SDK.

### 11.2 Coarena / DeepSeek V4.1 Flash

Ports, DTOs (`ProviderMessage`, `ProviderModel`, `ChatParams`, `ChatUsage`, `ChatDelta`),
and the error taxonomy live in `app/providers/base.py`. The adapter is
`app/providers/openai_compatible.py`; `app/providers/fake.py` is a deterministic provider
for tests and is never wired into the app.

- Treat the gateway as OpenAI-compatible until its documentation says otherwise:
  `POST {AI_BASE_URL}/chat/completions` with `Authorization: Bearer <key>`, `stream: true`,
  `stream_options.include_usage: true`, and one JSON object per `data:` line, terminated by
  `data: [DONE]`. `AI_BASE_URL` has no trailing slash (the settings validator strips it).
- Configuration: `AI_BASE_URL`, `AI_API_KEY`, `AI_CHAT_MODEL`. The public slug can differ
  from the provider model id; store both on the model row. `build_chat_provider(settings)`
  returns `None` when the base URL or key is missing, so wiring can fail closed.
- Timeouts: connect 10s, write 30s, read from `AI_REQUEST_TIMEOUT_SECONDS` (default 120).
- Retry only before the first delta has been delivered, at most twice, with a jittered
  exponential backoff. Never retry a 4xx, and never retry after content reached the client.
- Error mapping: 429 → `rate_limited`; a timeout → `provider_timeout`; a 5xx or a dropped
  connection → `provider_error`; a malformed `data:` line, a non-object chunk, or a
  response that is not `text/event-stream` → `provider_error`. Every error carries a
  `retryable` flag; it is true only for timeouts and 5xx/transport failures.
- An unsent optional parameter (`temperature`, `max_tokens`) is omitted from the payload
  rather than sent as `null`. Blank lines, SSE comments (`: ping`), and other `event:` or
  `id:` fields are ignored; usage may be absent, in which case token counts are unknown.
- A CLI validates the key without printing it:
  `uv run python -m app.cli provider-check` sends a one-token request and prints exactly
  `model=... status=ok finish=... prompt_tokens=... completion_tokens=... latency_ms=...`,
  or `status=error code=<problem code>` and exits non-zero. It prints neither the prompt,
  the completion, nor the key. When the gateway is unconfigured it explains that on
  stderr and exits 1.
- If the gateway is not OpenAI-compatible, add `app/providers/coarena.py` behind the same
  port. Do not leak gateway details into services.
- Prompts and completions are never logged. Log only model id, token counts, latency, and
  outcome.

### 11.3 Mock image and video

`GENERATION_MODE=mock` is the default in development and CI.

- Mock image: deterministic gradient PNG with the prompt excerpt and model name, sized by
  aspect ratio; `count` variants differ by hue.
- Mock video: 3-5 second bundled MP4 clip with metadata sidecar; no external calls.
- Responses and assets clearly mark `provider: "mock"`.
- Mock providers must satisfy the same ports as real providers so switching later does not
  change the API.

## 12. Generations and assets

### 12.1 Models

```text
generations
  id                uuid pk
  user_id           uuid fk
  modality          text: image | video
  model_id          text fk models
  prompt            text, 1..4000
  params            jsonb       -- aspectRatio, count, style, duration...
  status            text: queued | running | succeeded | failed | cancelled
  progress          integer 0..100
  cost              integer
  provider          text
  provider_job_id   text null
  error_code        text null
  attempts          integer default 0
  max_attempts      integer default 3
  locked_by         text null
  locked_at         timestamptz null
  created_at        timestamptz
  started_at        timestamptz null
  finished_at       timestamptz null

assets
  id            uuid pk
  user_id       uuid fk
  generation_id uuid fk generations null
  post_id       uuid fk posts null
  kind          text: image | video
  storage_key   text unique
  mime_type     text
  size_bytes    bigint
  width         integer null
  height        integer null
  duration_ms   integer null
  created_at    timestamptz

idempotency_keys
  user_id     uuid
  key         text
  operation   text
  request_hash text
  resource_id uuid           -- no fk: whatever the operation created
  created_at  timestamptz
  primary key (user_id, key)
```

`idempotency_keys` is the foundation for every idempotent create. A create route
requires an `Idempotency-Key` header and keys its request on `(user_id, key)`. The record
is written in the same transaction as the resource it names, so a failed request leaves no
key behind. Replaying the same key and body returns the original `resource_id`; the same
key with a different body (or a different operation under the same key) is
`409 idempotency_conflict`. `app/domain/idempotency.py` owns this: `hash_request`
canonicalizes the body into `request_hash`, `find` reads a stored key, and `execute`
creates once and replays after that, including when two concurrent requests race on the
same key. An unkeyed or malformed key is rejected at the route, not the service.

### 12.2 Endpoints

```text
POST /v1/generations                 -> 202 Generation (reserves Credits)
GET  /v1/generations?status=&cursor= -> paginated own Generations
GET  /v1/generations/{id}            -> Generation
POST /v1/generations/{id}/cancel     -> 202 Generation
GET  /v1/assets/{id}                 -> binary, permission checked
```

`POST` body:

```json
{
  "modality": "image",
  "modelId": "image-demo",
  "prompt": "A sunlit reading room...",
  "params": { "aspectRatio": "4:5", "count": 2, "style": "Cinematic" }
}
```

Validation:

- Prompt 1..4000 characters after trimming; blank prompts are `422 invalid_request`.
- Params are validated per modality against the model's advertised capabilities; unknown
  keys are rejected with `422 invalid_request`, never silently ignored.
  - Image: `aspectRatio` must be one of the model's `aspectRatios` (default `1:1`),
    `count` is an integer from 1 to `maxCount` (default `1`), and `style` must be one of
    the model's `styles` (default `None`).
  - Video: `aspectRatio` must be one of the model's `aspectRatios` (default `16:9`) and
    `duration` must be one of the model's `durations` (default `5`).
- Cost is computed from the catalog and the validated params, never accepted from the
  client:
  - Image: `creditCost * count`.
  - Video: `creditCost` for a 5-second clip and `creditCost10s` for a 10-second clip when
    the catalog sets it; otherwise `creditCost`.
- An unknown, unavailable, or wrong-modality model is `422 model_unavailable`. When the
  model cannot be used, validation of its params is not attempted.
- The row is inserted and the cost reserved in one transaction. A reservation that cannot
  be covered is `409 insufficient_credits` and leaves no Generation and no idempotency key
  behind.
- `Idempotency-Key` is required; a missing, blank, or over-length key is `400
  invalid_request`. Replaying the same key and body returns the same Generation. Same key
  with a different body returns `409 idempotency_conflict`.
- Create answers `202` with the queued Generation. The response carries `id`, `modality`,
  `modelId`, `prompt`, `params`, `status`, `progress`, `cost`, `provider`, `errorCode`,
  `assets`, `createdAt`, `startedAt`, and `finishedAt`.
- `GET /v1/generations` is newest first, defaults to 20 items, accepts `limit` 1..100, an
  optional `status` filter applied before pagination, and an opaque `cursor`.
- `POST /v1/generations/{id}/cancel` answers `202`. A queued or running Generation becomes
  `cancelled` and its reservation is refunded exactly once; a repeated cancel is a no-op.
  A Generation that already `succeeded` or `failed` is `409 generation_not_cancellable`.
  Every read and write is scoped by `user_id`, so another account's Generation is `404`.
- Create is rate-limited to 10 requests per minute per user and returns `429 rate_limited`
  with `Retry-After` beyond that.

### 12.3 Worker

- Entry point: `python -m app.worker`.
- Claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED` where status is `queued` or a
  `running` job is stale (locked more than 5 minutes, `WORKER_STALE_AFTER_SECONDS`).
- The claim transaction increments `attempts` and stamps `locked_by`/`locked_at`. A row
  with no attempts left is failed and refunded in the same transaction rather than
  rendered again.
- Updates progress at meaningful boundaries; progress is monotonic and only the owning
  worker can advance it.
- On success: one Asset per output, the Credit reservation is committed, and the row is
  `succeeded`, all in one transaction.
- On failure: a retryable error requeues the row with its reservation intact; a
  non-retryable error, or the attempt after `max_attempts`, marks it `failed`, records
  `errorCode`, and refunds once.
- On cancel while queued: mark cancelled and refund immediately. While running: the
  worker observes it no longer owns the row, discards the output, and does not refund
  again because the cancel route already did.
- Before each claim the loop settles expired holds: a `queued` Generation whose hold is
  older than `HOLD_TTL_SECONDS`, a `running` Generation whose lock is older than the same
  cutoff, and an assistant message left `streaming` past it are marked `failed` with
  `errorCode: "hold_expired"`. Only the Credits still held for that reference in the
  ledger are refunded, so a committed or already-refunded reservation is never refunded
  twice. A `running` job with a fresh lock is left alone.
- `WORKER_CONCURRENCY` (default 2), `WORKER_POLL_INTERVAL_SECONDS`,
  `WORKER_STALE_AFTER_SECONDS`, and `HOLD_TTL_SECONDS` (default 3600) are configurable.
  An idle loop runs a stale-lock sweep.
- Structured logs carry ids, attempts, and counts only; never the prompt or the output.
- Image and Video rendering is injected behind `GenerationProvider`. With
  `GENERATION_MODE=mock` the worker builds the deterministic mock adapter; a live
  mode still has no provider, so the loop refuses to claim rather than fail every job.

### 12.4 Asset delivery

- Storage key: `assets/{userId}/{assetId}.{ext}`; local root is `STORAGE_DIR`.
- Serve only through `GET /v1/assets/{id}`; never mount the storage directory statically.
- Permission: owner, or the asset belongs to a published Explore post.
- Set `Content-Type`, `Content-Length`, `ETag`, `Cache-Control: private, max-age=3600`
  for owner assets; public gallery assets may be `public, max-age=86400`.
- Support HTTP Range for video through the framework's file response.
- Reject path traversal and unknown ids with 404.

## 13. Explore

```text
posts
  id            uuid pk
  author_id     uuid fk users null   -- system author for seeds
  asset_id      uuid fk assets
  modality      text: chat | image | video
  title         text
  prompt        text
  model_id      text fk models
  params        jsonb
  created_at    timestamptz
  published     boolean default true

post_likes
  post_id     uuid fk posts
  user_id     uuid fk users
  created_at  timestamptz
  primary key (post_id, user_id)
```

```text
GET    /v1/explore/posts?modality=&modelId=&cursor=  -> public, likedByMe when signed in
GET    /v1/explore/posts/{id}                         -> public
POST   /v1/explore/posts/{id}/like                    -> 204, idempotent
DELETE /v1/explore/posts/{id}/like                    -> 204, idempotent
```

Seed 16 posts with bundled assets (`scripts/seed.py`), matching the frontend demo's
variety: ten images, four clips, and two text posts. Every seeded row is owned by one
system author, an internal Account that cannot sign in and whose `.invalid` email cannot
be registered. Image and video posts use the `image-demo` and `video-demo` models and
point at small JPEG/MP4 files shipped under `app/db/seed_media/`; the seed copies them
into `STORAGE_DIR` and writes `assets` rows. Text posts use the configured chat model and
carry no asset. Remix is frontend-only: the post payload contains the prompt, model, and
params needed to prefill a workspace. Users cannot publish Generations in v1.

Seeded ids are derived deterministically, so `python -m app.cli seed` is idempotent: a
rerun updates the same sixteen posts and fourteen assets, restores edited fixture
content, rewrites the bundled files, and leaves likes alone. Posts keep their fixture
`createdAt`, so Explore ordering is stable across reseeds.

`GET /v1/explore/posts` is public, ordered by `createdAt` descending with `id` as the
tiebreaker, and returns at most `limit` (1–100, default 20) items. `nextCursor` is set
only when another page exists. `modality` and `modelId` filter the gallery; an unknown
model id is an empty page, not an error. A malformed cursor is `400 invalid_request`,
and the cursor is a position in one ordering, not a snapshot: follow it with the same
filters.

A post carries the prompt, Model, and params needed to remix it, plus the fields a
gallery renders:

```json
{ "id": "0193...", "modality": "image", "title": "Coastal light",
  "prompt": "A lighthouse at dusk", "modelId": "image-demo",
  "params": {"aspectRatio": "4:5"},
  "author": {"id": "0193...", "displayName": "Mara Ellison", "initials": "ME"},
  "asset": {"id": "0193...", "url": "/v1/assets/0193...", "mimeType": "image/png",
            "width": 1200, "height": 1500, "durationMs": null},
  "likeCount": 12, "likedByMe": false, "createdAt": "2026-11-15T12:00:00Z" }
```

`author` is null for a post without an account author, and `asset` is null for a chat
post. `likedByMe` is false for a signed-out reader. `GET /v1/explore/posts/{id}`
returns the same shape; an unknown or unpublished id is `404 not_found`.

`POST` and `DELETE /v1/explore/posts/{id}/like` require a session and answer `204`
with no body. Both are idempotent: a repeated like leaves one row, and a repeated
unlike leaves none. An unknown or unpublished post is `404 not_found`, so a like
request cannot discover drafts.

An asset is readable by its owner, or by anyone when a published post covers it
(`posts.asset_id`) or attaches it (`assets.post_id`). A non-owner read of a published
asset uses `Cache-Control: public, max-age=86400`; the owner keeps
`private, max-age=3600`.

## 14. Configuration

Environment variables (validated at startup; fail fast on missing required values):

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `APP_ENV` | yes | `development` | development, test, production |
| `APP_SECRET` | prod | dev value | reserved for signing; do not reuse |
| `DATABASE_URL` | yes | — | `postgresql+asyncpg://...` |
| `FRONTEND_ORIGIN` | yes | `http://localhost:3000` | origin check and optional CORS |
| `SESSION_COOKIE_NAME` | no | `nero_session` | |
| `SESSION_TTL_DAYS` | no | `30` | |
| `COOKIE_SECURE` | no | derived from env | must be true in production |
| `AI_BASE_URL` | chat | — | Coarena gateway base URL, no trailing slash |
| `AI_API_KEY` | chat | — | server only |
| `AI_CHAT_MODEL` | chat | `deepseek-v4.1-flash` | confirm exact slug with the gateway |
| `AI_REQUEST_TIMEOUT_SECONDS` | no | `120` | read timeout |
| `GENERATION_MODE` | no | `mock` | mock or live |
| `STORAGE_DIR` | no | `./storage` | local asset root |
| `STORAGE_BACKEND` | no | `local` | local or s3 |
| `ALLOW_SIMULATED_PLAN_CHANGES` | no | `true` in dev | false in production |
| `RATE_LIMIT_BACKEND` | no | `memory` | memory or redis |
| `MAX_REQUEST_BODY_BYTES` | no | `1048576` | 1 MiB request body cap |
| `LOG_LEVEL` | no | `info` | |
| `SENTRY_DSN` | no | — | optional |

`.env` is gitignored. `.env.example` contains placeholders only.

## 15. Security and privacy

- TLS terminates at the reverse proxy; cookies are `Secure` in production.
- CORS: no wildcard. If direct cross-origin access is enabled, allow only
  `FRONTEND_ORIGIN` with credentials.
- Headers: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`; HSTS in production.
- Body limit 1 MiB; prompt limit 4000 characters.
- Login rate limit: 5 attempts per 15 minutes per IP + email. Signup: 3 per hour per IP.
  Chat: 30 messages per minute per user. Generations: 10 per minute per user.
  Limits are enforced in-process on a single instance; `RATE_LIMIT_BACKEND=redis` must
  be implemented before running more than one.
- Authorization: every query is scoped by user id. Public routes are explicitly listed;
  a test asserts that the route table matches the app and that every protected route
  depends on `get_current_auth`, so a route cannot ship without an auth decision.
- Provider URLs come from configuration only (no SSRF surface).
- Secrets are redacted from logs and error responses. Authorization headers are never
  logged.
- Audit events (login, logout, plan change, demo reset) are logged without payloads.
- Data deletion: demo reset covers user data. Account deletion is post-v1 but the schema
  supports soft-disable now.
- Provider spend cap and per-user daily Credit cap are mandatory before production.

## 16. Observability

- Structured JSON logs with `requestId`, `userId`, `method`, `path`, `status`,
  `durationMs`, `modelId`, `provider`, `outcome`.
- Never log request bodies or model content.
- `/health/live` returns 200 if the process is alive.
- `/health/ready` checks database connectivity and migrations; it does not call the
  provider.
- Metrics (post-v1): request latency, stream duration, provider errors, Credit spend,
  queue depth, job failure rate.
- Alerts (post-v1): readiness failures, provider error rate, job backlog, negative-balance
  invariant violation.

## 17. Frontend integration

- Add a server-only `API_URL` to the frontend and rewrite `/api/:path*` to
  `${API_URL}/v1/:path*`. The browser uses relative `/api` URLs.
- Cookies are set on the frontend origin by the proxy: no CORS, no token storage in JS.
- Add `HttpServices` implementing the existing `lib/services` interfaces. Keep
  `LocalServices` for `NEXT_PUBLIC_DATA_MODE=local`; API mode is `api`.
- Streaming: use `fetch` with `ReadableStream`; `EventSource` cannot POST.
- On `401`, clear client auth state and redirect to `/login`.
- On `409 insufficient_credits`, link to `/pricing`; do not retry automatically.
- Map backend `problem+json` codes to the existing toast copy.
- Generate TypeScript types from `openapi.json` and fail CI when the committed schema
  differs.
- Delete or disable the local password/auth path in API mode; never run both sources of
  truth at once.
- Required flows: signup, login, logout, chat streaming, chat history, credits, image,
  video, explore likes, demo reset, plan change.

## 18. Testing strategy

| Layer | Scope | Tools |
| --- | --- | --- |
| Unit | Credit math, password hashing, cursor encoding, SSE parsing | pytest |
| Integration | Routers + services + PostgreSQL, fake providers | httpx ASGITransport, pytest-asyncio |
| Provider | OpenAI-compatible adapter, retries, timeouts | respx |
| Contract | OpenAPI snapshot, frontend generated types | schemathesis optional |
| Concurrency | Two reserves cannot overspend; job locking | asyncio + Postgres |
| Authorization | Every user resource across two accounts | parametrized tests |
| Frontend e2e | Golden path against real backend + fake provider | Playwright (frontend repo) |

CI requires PostgreSQL 16 as a service. No test may call a real provider. Tests use a
clean migrated database and roll back per test.

## 19. Deployment

- Multi-stage `Dockerfile` (Python 3.12 slim), non-root user, `uv sync --frozen --no-dev`.
- Compose services: `api`, `worker`, `db`; `db` volume; `storage` volume.
- Run `alembic upgrade head` as a release step, not on every container start.
- Run `python -m app.cli seed` after migrations. It is idempotent and refreshes the plan
  and model catalogs; `available` is derived from the environment, so this is where a
  missing provider key keeps a model out of the catalog.
- One API instance and one worker for v1. SQLite is not supported.
- Production: managed PostgreSQL, S3-compatible storage, HTTPS, provider spend cap,
  backups daily with 7-day retention, and log retention.
- Migrations are backward-compatible for one release (expand/contract) before scaling to
  multiple instances.

## 20. v1 acceptance criteria

```text
[ ] signup, login, logout, me; cookie is HttpOnly and sessions are revocable
[ ] every user resource is authorization-tested with two accounts
[ ] Credit reserve/commit/refund and no-negative invariant tested under concurrency
[ ] one real chat model streams end-to-end through Coarena with a key
[ ] chat history persists across sessions and devices
[ ] model catalog exposes only available models with server-side prices
[ ] explore posts and likes work; public read, authenticated write
[ ] image and video mock generations complete with stored assets and correct Credits
[ ] demo reset restores the plan allowance and removes the user's work
[ ] OpenAPI snapshot and generated frontend types are in sync
[ ] frontend golden path runs against the backend in CI
[ ] no secret, prompt, or cookie appears in logs
[ ] ruff, mypy, pytest, and Docker build pass in CI
```

## 21. Open questions

1. Confirm Coarena's exact base URL, auth scheme, and model slug. The OpenAI-compatible
   adapter is the default assumption; adjust before Phase 3.
2. Confirm whether Coarena supports usage/token counts in streamed responses. If not,
   store null usage and rely on message counts for cost.
3. Decide the production domain layout (`app.example.com` + `/api` proxy vs.
   `api.example.com`). Same-origin proxy is strongly preferred.
4. Image/video provider choice and budget. Until then, mock mode only.
5. Whether email verification and password reset are required before any public launch.
   They are post-v1 in this spec.
