# Glossary

Use these terms in issues, code, tests, and API names. If a new term is needed, add it
here in the same PR.

| Term | Meaning |
| --- | --- |
| **Account** | The product-facing identity. Maps to a row in `users`; use "Account" in UI copy and "user" in code. |
| **Workspace** | The signed-in product area: Chat, Image, Video, Explore, and account controls. |
| **Session** | A revocable server-side login. The browser holds an opaque token in an HttpOnly cookie; the database stores only its hash. |
| **Plan** | Free, Pro, or Studio. Defines the monthly Credit allowance and price. |
| **Credits** | Integer units spent on model work. Never fractional. |
| **Allowance** | The Credit amount granted by the current Plan for the current period. |
| **Available** | `balance - held`. The amount a user can spend right now. |
| **Held** | Credits reserved for in-flight work and not yet committed or refunded. |
| **Reservation** | The act of moving Credits from available to held before provider work. |
| **Commit** | Finalizing a reservation after successful work. Held Credits are consumed. |
| **Refund** | Returning held Credits to available after failure, cancel, or timeout. |
| **Ledger** | The append-only record of every Credit movement. Rows are never updated or deleted. |
| **Grant** | Credits added by signup, a plan change, or a reset. |
| **Reset** | Restoring the account to the current plan Allowance and refunding any Held Credits in one `reset` ledger entry. |
| **Model** | A selectable chat, image, or video model in the server catalog. Has a stable public `id` and a provider-specific `provider_model_id`. |
| **Modality** | One of `chat`, `image`, or `video`. |
| **Provider** | The external service that runs a model, such as the Coarena gateway. |
| **Catalog** | The server-owned list of available Models, their capabilities, and their Credit prices. |
| **Chat** | A persisted conversation owned by one Account. |
| **Message** | One user or assistant turn inside a Chat. |
| **Stream** | Server-Sent Events delivered while an assistant Message is produced. |
| **Generation** | A persisted Image or Video job with status, progress, cost, and Assets. |
| **Worker** | The separate process (`python -m app.worker`) that claims queued Generations, renders them with the configured provider, and settles their Credits. |
| **Asset** | A stored image or video file owned by an Account or attached to an Explore Post. |
| **Storage port** | The async `write`/`stat`/`iter_range`/`delete` interface shared by the local and S3 adapters. Domain code never builds a filesystem path from a client value. |
| **Storage backend** | `local` (a shared `STORAGE_DIR`) or `s3` (a bucket under `S3_PREFIX`, signed with SigV4). Chosen once at startup. |
| **Release job** | The one-shot deploy step that runs `alembic upgrade head` and `python -m app.cli seed` before the API and worker start. |
| **Explore** | The public gallery of curated Posts. |
| **Post** | A published gallery item with a prompt, Model, params, and Asset. |
| **System author** | The internal Account that owns seeded Explore Posts and their Assets. It has no usable password and cannot sign in. |
| **Like** | A unique Account-to-Post relation. Likes are idempotent. |
| **Remix** | Prefilling a workspace from a Post's prompt, Model, and params. Frontend-only; no backend endpoint. |
| **Demo reset** | An authenticated action that deletes the caller's Chats and Messages, Generations, Assets and their bytes, likes, and create-idempotency records, and restores the plan Allowance. |
| **Demo mode** | The frontend-only local-first mode that uses localStorage and fixtures. No backend. |
| **API mode** | The frontend mode that treats this backend as the source of truth. |
| **Mock provider** | A deterministic in-process provider used for Image/Video in development and CI. Clearly labelled; never presented as a real model run. |
| **Idempotency key** | A client-supplied key that makes retrying a create operation safe. Same key + same body returns the original resource. |
| **Problem details** | The RFC 9457 error shape returned as `application/problem+json`. |
| **Cursor** | An opaque token that points at the next page of a list. Clients pass it back unchanged and never parse it. |
| **OpenAPI snapshot** | The committed `openapi.json` rendered from the app. It is the contract the frontend generates its types from; CI fails when it is stale. |
| **Rate limit window** | A fixed interval during which one throttle key may make a bounded number of attempts. Counter rows live in `rate_limit_windows` and are shared by every API process. |
| **Metric** | A process-local counter or duration histogram rendered by `GET /metrics` in the Prometheus text format. Labels are fixed in code and never carry user data. |
| **Alert** | A fixed condition an operator must act on: `readiness_failed`, `queue_backlog`, `provider_error_rate`, or `credit_invariant_violation`. Delivered by `ops-check` to logs and the optional webhook. |
| **ops-check** | The read-only command (`python -m app.cli ops-check`) that evaluates readiness, backlog, provider errors, and Credit invariants, then exits `0`, `1`, or `2`. |
| **Restore drill** | A dated rehearsal that restores a database dump and the asset directory into a scratch environment and verifies they agree. Documented in `docs/RUNBOOK.md`. |
| **Staging smoke** | The post-release check (`python -m scripts.smoke`) for liveness, readiness, metrics, catalog, authentication, and a ranged seeded-asset download. |
| **Gate** | The acceptance checklist that must pass before the next roadmap phase starts. |
| **Hold expiry** | The deadline after which an uncommitted reservation is refunded and its work marked failed. |
| **IDOR** | Insecure direct object reference. Prevented by scoping every query to the authenticated user. |

## Naming rules

- API paths use plural nouns: `/v1/chats`, `/v1/generations`.
- JSON uses camelCase; Python uses snake_case.
- Credit fields are `balance`, `held`, `available`, `allowance`.
- Statuses are lowercase enums: `queued`, `running`, `succeeded`, `failed`, `cancelled`.
- Use "Account" in user-facing strings and "user" in database/code identifiers.
