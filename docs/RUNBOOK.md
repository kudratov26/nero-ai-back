# Operations runbook

This runbook covers the single-instance v1 deployment: one API process, one worker
process, PostgreSQL 16, and either a shared `STORAGE_DIR` volume or an S3-compatible
bucket.

- **Deployment**: `deploy/compose.yml`, one-shot release job, Caddy TLS.
- **Readiness**: database reachable and Alembic at head.
- **Metrics**: `GET /metrics`, Prometheus text format.
- **Alerts**: `python -m app.cli ops-check`, exit `0` healthy, `1` alert, `2` check failed.
- **Backups**: PostgreSQL custom-format dump plus the asset volume or bucket, restored
  together.
- **Restore drill**: dated record at the end of this file.
- **Staging smoke**: `uv run python -m scripts.smoke`, run after every release.

Nothing here prints or stores a provider key, a session cookie, a prompt, or message
content. The alert webhook URL and S3 credentials can carry secrets, so treat them as
sensitive and never log them.

## 1. Processes

Development:

```bash
uv run uvicorn app.main:app --port 8000       # API
uv run python -m app.worker                    # worker
uv run alembic upgrade head                    # release step, not container start
uv run python -m app.cli seed                  # idempotent catalog refresh
uv run python -m app.cli provider-check        # optional chat-gateway probe
```

Container deployment:

```bash
docker compose -f deploy/compose.yml up -d --build
docker compose -f deploy/compose.yml ps
docker compose -f deploy/compose.yml logs -f release
```

The stack starts `db`, runs one `release` container (`alembic upgrade head` then
`python -m app.cli seed`), and only then starts `api` and `worker`. Caddy is the only
published service. Migrations never run in the API or worker entry point.

The API, worker, and release job must share `DATABASE_URL` and, when
`STORAGE_BACKEND=local`, the `storage` volume. With `STORAGE_BACKEND=s3` they share the
bucket instead. A Generation becomes `queued` before the worker renders it; if the queue
never drains, first confirm the worker points at the same database and storage backend.

## 2. Health probes

| Probe | Healthy | Meaning |
| --- | --- | --- |
| `GET /health/live` | `200 {"status":"ok"}` | The process is alive. It does not touch dependencies. |
| `GET /health/ready` | `200 {"status":"ready"}` | PostgreSQL answers and `alembic_version` matches the Alembic head. |
| `GET /health/ready` | `503 internal_error` | Database unreachable or a migration is pending. The body never includes a driver message or a connection string. |

Point the load balancer at `/health/ready` and the process supervisor at `/health/live`.
A readiness alert is `critical`; do not send traffic to a not-ready instance.

## 3. Logs

Structured JSON on stdout. Access logs carry `requestId`, `userId` (after
authentication), `method`, `path`, `status`, `durationMs`, and `outcome`. Worker events
carry `generation_id`, `attempt`, and counts. Exceptions are formatted before redaction,
so a traceback cannot quote a provider key; the key, the app secret, cookies, and
authorization headers are masked.

Never log request bodies, prompts, completions, provider response bodies, or cookies.
When adding a log line, log ids, counts, durations, and fixed enums only.

Useful events:

| Event | Meaning |
| --- | --- |
| `request.completed` | One HTTP request. `outcome` is `success`, `client_error`, or `error`. |
| `generation.claimed` | A worker took a job. |
| `generation.succeeded` | Assets and the Credit commit are written. |
| `generation.retrying` | A retryable provider failure left the job queued. |
| `generation.failed` | The job is terminal and its reservation was refunded. |
| `generation.recovered` | A stale worker lock was returned to the queue. |
| `ops.alert` | An alert fired. |
| `ops.alert_sink_failed` | One sink failed; other sinks still received the alert. |
| `worker.not_configured` | The worker has no Image/Video provider and refuses to claim. |

## 4. Metrics

`GET /metrics` renders the in-process registry. It is public so a collector on the same
private network can scrape it. Restrict the path at the reverse proxy when the API is
internet-facing.

```bash
curl -sS http://localhost:8000/metrics
```

| Metric | Type | Labels | Use |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `outcome` | Traffic volume and error share. |
| `http_request_duration_seconds` | histogram | `method` | Latency, including streaming requests. |
| `provider_requests_total` | counter | `provider`, `outcome` | Chat-gateway attempts, including retries. |
| `provider_request_duration_seconds` | histogram | `provider`, `outcome` | Provider latency. |
| `generation_jobs_total` | counter | `modality`, `outcome` | Worker completions, retries, cancellations, crashes. |
| `generation_job_duration_seconds` | histogram | `modality`, `outcome` | End-to-end render time. |

Labels are fixed in code. There is no user id, prompt, token, asset id, or provider body
in any metric.

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: nero-api
    metrics_path: /metrics
    static_configs:
      - targets: ["api.internal:8000"]
  - job_name: nero-worker
    metrics_path: /metrics
    static_configs:
      - targets: ["worker.internal:9100"]
```

The worker has no HTTP server, so expose its registry through your process exporter or a
sidecar if you need worker samples in Prometheus. `ops-check` covers worker health from
the database side and is the required alert path for v1.

## 5. Alerts

`ops-check` is read-only. It checks readiness, queue backlog, provider failures, and the
Credit ledger invariant, then delivers every alert to logs and, when configured, to
`OPS_ALERT_WEBHOOK_URL`.

```bash
uv run python -m app.cli ops-check
```

| Exit | Meaning |
| --- | --- |
| `0` | No alert fired. |
| `1` | At least one alert fired; the CLI prints one `severity code=... summary` line per alert. |
| `2` | The check itself failed (for example, the database is unreachable). |

Delivery is best-effort per sink: a failing webhook never suppresses the log sink, and a
failed sink logs `ops.alert_sink_failed` with the alert code and exception type only.
The webhook payload is:

```json
{
  "service": "nero-ai-back",
  "code": "queue_backlog",
  "severity": "warning",
  "summary": "12 Generation(s) queued for more than 300s.",
  "detail": "Check that the worker process is running and the provider is healthy.",
  "detectedAt": "2026-11-21T09:30:00Z"
}
```

| Code | Severity | Condition | Action |
| --- | --- | --- | --- |
| `readiness_failed` | critical | Database unreachable or migrations not at head. | Check PostgreSQL, run `alembic upgrade head`, then `/health/ready`. |
| `queue_backlog` | warning | At least `OPS_QUEUE_BACKLOG_THRESHOLD` jobs queued longer than `OPS_QUEUE_BACKLOG_AGE_SECONDS`. | Confirm the worker is running, points at this database, and has a provider. Check `generation.*` logs. |
| `provider_error_rate` | warning | At least `OPS_PROVIDER_ERROR_THRESHOLD` failed chat Messages or Generations with a provider code inside `OPS_PROVIDER_ERROR_WINDOW_SECONDS`. | Check the gateway status, the key, and the spend cap. Failed work refunds automatically. |
| `credit_invariant_violation` | critical | A `credit_accounts` row disagrees with the sum of its ledger entries. | Stop spending. Inspect the account and ledger, then correct with a compensating ledger entry; never update or delete ledger rows. |

Thresholds and the window come from `OPS_QUEUE_BACKLOG_THRESHOLD`,
`OPS_QUEUE_BACKLOG_AGE_SECONDS`, `OPS_PROVIDER_ERROR_THRESHOLD`, and
`OPS_PROVIDER_ERROR_WINDOW_SECONDS`. All default to the values above.

Run every five minutes from cron. Redirect output to the platform log collector and keep
the exit code visible to the scheduler:

```cron
*/5 * * * * cd /srv/nero && /usr/local/bin/uv run python -m app.cli ops-check
```

For systemd, use a oneshot service with `Restart=no` and a `.timer` with
`OnUnitActiveSec=5min`; treat exit `1` and `2` as failed units so the alerting layer sees
them.

The four alert routes are covered by tests: readiness, backlog, provider errors, and the
Credit invariant, plus sink isolation and webhook payload delivery.

## 6. Backups

Back up the database and the asset store as one set. A database row without its asset
bytes, or bytes without the row, is not a restorable Generation. Use the application
release tag as the backup label so a restore knows which schema and code belong together.

```bash
# PostgreSQL: strip the SQLAlchemy driver suffix first.
PG_URL="${DATABASE_URL/postgresql+asyncpg/postgresql}"
pg_dump --format=custom --no-owner \
  --file="/backups/nero-$(date -u +%Y%m%dT%H%M%SZ).dump" "$PG_URL"

# Local storage volume: preserve relative paths.
tar --create --gzip --file="/backups/storage-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
  -C /srv/nero storage

# S3: copy one consistent version of the prefix. Enable bucket versioning so an
# overwrite during the copy is recoverable; provider replication is not a substitute
# for an off-account backup.
aws s3 sync "s3://${S3_BUCKET}/${S3_PREFIX}" "/backups/s3/$(date -u +%Y%m%dT%H%M%SZ)"
```

- Run daily and keep 7 days, per the deployment contract.
- Store both files off the host and encrypt them at rest; the database contains password
  hashes and session-token hashes, and assets are user content.
- Record the dump's `alembic_version` and the image tag with the backup.
- Verify a dump without restoring over production: `pg_restore --list <dump>` must list the
  tables, and the restore drill below must pass on a scratch database.
- A provider key, S3 credential, or `.env` is never part of a backup. Restore
  configuration separately from the secret manager.
- Enable S3 versioning and lifecycle rules before the first production write; a lifecycle
  rule that expires noncurrent versions is itself part of the retention policy.

### Restore procedure

Stop the API and worker first. Do not restore into a live database; a `pg_restore` over
active connections can leave a half-restored schema and the release migration step may
race the workers.

```bash
# 1. Stop API and worker, then create an empty target database.
createdb --owner=nero nero_restored

# 2. Restore the dump. --clean is safe only on the empty target.
pg_restore --no-owner --clean --if-exists --dbname=nero_restored /backups/<dump>

# 3. Restore the assets into the configured STORAGE_DIR.
tar --extract --gzip --file=/backups/<storage>.tar.gz -C /srv/nero
# S3 instead: aws s3 sync /backups/<s3-snapshot> "s3://${S3_BUCKET}/${S3_PREFIX}"

# 4. Migrate forward if the backup predates the current release.
DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://... uv run python -m app.cli seed

# 5. Start the API, then the worker, and verify.
curl -fsS http://localhost:8000/health/ready
uv run python -m app.cli ops-check
uv run python -m scripts.smoke --base-url https://app.example.com
```

After restore, spot-check one owner's `GET /v1/generations` and open one asset URL. A
`404` for an asset that exists in the database usually means the asset archive or bucket
was restored to the wrong root or prefix. The smoke script's ranged asset check catches
that before traffic arrives.

## 7. Restore drill record

**Date:** 2026-11-21 (UTC)
**Environment:** local PostgreSQL 16 container (`nero-pg16-test`, test database
`nero_test`), source checkout at `feat/58-observability-ops`.
**Purpose:** prove a dump and the asset directory restore together and agree.

Commands executed:

```bash
# Database dump and scratch restore.
docker exec nero-pg16-test pg_dump -U nero -Fc -f /tmp/nero_test.dump nero_test
docker exec nero-pg16-test psql -U nero -d postgres -c 'CREATE DATABASE nero_restore_drill'
docker exec nero-pg16-test pg_restore -U nero -d nero_restore_drill /tmp/nero_test.dump

# Schema and content checks.
docker exec nero-pg16-test psql -U nero -d nero_restore_drill -tAc \
  'SELECT version_num FROM alembic_version'
docker exec nero-pg16-test psql -U nero -d nero_restore_drill -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
docker exec nero-pg16-test psql -U nero -d nero_restore_drill -tAc 'SELECT count(*) FROM users'

# Asset archive and file-level comparison.
tar -czf /tmp/storage-drill.tar.gz storage
tar -xzf /tmp/storage-drill.tar.gz -C /tmp/restore-drill
diff <(cd storage && find . -type f | sort) \
     <(cd /tmp/restore-drill/storage && find . -type f | sort)
sha256sum storage/<sample> /tmp/restore-drill/storage/<sample>
```

Observed result:

- `alembic_version` restored as `0011`, matching the source head.
- Restored schema had 15 public tables, the same count as the source.
- Restored `users` count equalled the source (`359` at drill time).
- The asset archive restored 20 files with an identical relative-path listing and a
  matching SHA-256 for the sampled file.
- Scratch database and temporary files were dropped after the checks.

The drill validates the dump/restore commands and the database-to-assets pairing. Repeat
it after a migration that changes storage layout, after a PostgreSQL major upgrade, and
before the first production release.

## 8. Production deployment

### 8.1 Prepare

1. Point DNS at the host and open ports 80/443 (or the staging ports you chose).
2. Create the Compose interpolation file and the application environment file:

   ```bash
   cp deploy/.env.example deploy/.env
   cp deploy/production.env.example deploy/production.env
   chmod 600 deploy/.env deploy/production.env
   ```

3. Fill in `deploy/.env`:
   - `POSTGRES_PASSWORD`: a random password.
   - `NERO_DOMAIN`: the public hostname, or `localhost` for a local staging run.
   - `NERO_IMAGE_TAG`: the release SHA you are deploying.
   - `NERO_HTTP_PORT` / `NERO_HTTPS_PORT`: defaults `80` / `443`.
4. Fill in `deploy/production.env`; `DATABASE_URL`'s password must match
   `POSTGRES_PASSWORD`.
   - `APP_ENV=production`, a real `APP_SECRET` (`openssl rand -hex 32`), and
     `COOKIE_SECURE=true`.
   - `FRONTEND_ORIGIN` is the exact browser origin that proxies `/api/*`.
   - `ALLOW_SIMULATED_PLAN_CHANGES=false`.
   - `AI_API_KEY` only after a spend cap is set in the provider dashboard. A missing key
     keeps the chat model out of the catalog.
   - `STORAGE_BACKEND=local` with the shared volume, or `s3` with `S3_BUCKET`,
     `S3_ACCESS_KEY_ID`, and `S3_SECRET_ACCESS_KEY`. The endpoint must be HTTPS in
     production.
   - `OPS_ALERT_WEBHOOK_URL` is optional and is treated as a secret.
5. S3 bucket policy: grant object `GET`, `HEAD`, `PUT`, and `DELETE` under `S3_PREFIX`
   only. The API does not list the bucket. Enable versioning and the retention policy.

### 8.2 Build and start

```bash
docker build -t nero-ai-back:"$(git rev-parse --short HEAD)" .
NERO_IMAGE_TAG="$(git rev-parse --short HEAD)" \
  docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml ps
docker compose -f deploy/compose.yml logs -f release
```

The release container runs `alembic upgrade head` and `python -m app.cli seed`; the API
and worker do not start until it exits `0`. If the release log shows a failed migration
or seed, the API is still on the previous release and no traffic reaches it.

### 8.3 Verify

```bash
curl -fsS https://app.example.com/health/ready
uv run python -m scripts.smoke --base-url https://app.example.com \
  --origin https://app.example.com
docker compose -f deploy/compose.yml exec api python -m app.cli ops-check
```

Expected: readiness `{"status":"ready"}`, every smoke check `ok`, and `ops-check: ok`.
The smoke script signs up a throwaway `smoke-*@example.com` account unless
`SMOKE_EMAIL`/`SMOKE_PASSWORD` name a dedicated account. Signup is rate-limited per IP;
use the dedicated account for repeated staging runs.

For a local staging run:

```bash
NERO_DOMAIN=localhost NERO_HTTPS_PORT=8443 \
  docker compose -f deploy/compose.yml up -d
uv run python -m scripts.smoke --base-url https://localhost:8443 --insecure
```

Caddy uses its local CA for `localhost`, so `--insecure` is correct there and nowhere
else.

### 8.4 Rollback

Migrations are backward-compatible for one release (expand/contract), so the previous
image can run on the new schema. Do not run `alembic downgrade` in production.

```bash
# Deploy the previous image without re-running the release job or touching the db.
NERO_IMAGE_TAG='<previous-sha>' \
  docker compose -f deploy/compose.yml up -d --no-deps api worker
docker compose -f deploy/compose.yml logs -f api
curl -fsS https://app.example.com/health/ready
```

`--no-deps` is deliberate: re-running the old release container against a newer schema
can fail because its Alembic script directory does not know the new revision. The API
and worker are the only processes that need to move.

If the schema itself is wrong, stop the API and worker and follow the restore procedure
in §6 from the latest verified database and asset pair. Restoring a database without its
assets leaves every asset URL `404`.
