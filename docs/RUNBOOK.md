# Operations runbook

This runbook covers the single-instance v1 deployment: one API process, one worker
process, PostgreSQL 16, and the `STORAGE_DIR` asset volume.

- **Readiness**: database reachable and Alembic at head.
- **Metrics**: `GET /metrics`, Prometheus text format.
- **Alerts**: `python -m app.cli ops-check`, exit `0` healthy, `1` alert, `2` check failed.
- **Backups**: PostgreSQL custom-format dump plus the asset directory, restored together.
- **Restore drill**: dated record at the end of this file.

Nothing here prints or stores a provider key, a session cookie, a prompt, or message
content. The alert webhook URL can carry a token, so treat it as a secret and never log
it.

## 1. Processes

```bash
uv run uvicorn app.main:app --port 8000       # API
uv run python -m app.worker                    # worker
uv run alembic upgrade head                    # release step, not container start
uv run python -m app.cli seed                  # idempotent catalog refresh
uv run python -m app.cli provider-check        # optional chat-gateway probe
```

The API and worker must share `DATABASE_URL` and `STORAGE_DIR`. A Generation becomes
`queued` before the worker renders it; if the queue never drains, first confirm the
worker points at the same database and storage root.

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

Back up the database and the asset directory as one set. A database row without its
asset bytes, or bytes without the row, is not a restorable Generation.

```bash
# PostgreSQL: custom format, compressed, no owner/ACL assumptions.
pg_dump --format=custom --no-owner --file="/backups/nero-$(date -u +%Y%m%dT%H%M%SZ).dump" "$DATABASE_URL"

# Assets: the whole storage root, preserving relative paths.
tar --create --gzip --file="/backups/storage-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
  -C /srv/nero storage
```

- Run daily and keep 7 days, per the deployment contract.
- Store both files off the host and encrypt them at rest; the database contains password
  hashes and session-token hashes, and assets are user content.
- Record the dump's `alembic_version` with the backup so a restore knows which release it
  belongs to.
- Verify a dump without restoring over production: `pg_restore --list <dump>` must list the
  tables, and the restore drill below must pass on a scratch database.
- A provider key or `.env` is never part of a backup. Restore configuration separately
  from the secret manager.

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

# 4. Migrate forward if the backup predates the current release.
DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://... uv run python -m app.cli seed

# 5. Start the API, then the worker, and verify.
curl -fsS http://localhost:8000/health/ready
uv run python -m app.cli ops-check
```

After restore, spot-check one owner's `GET /v1/generations` and open one asset URL. A
`404` for an asset that exists in the database usually means the asset archive was
restored to the wrong root.

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

## 8. Release and rollback

1. Run `alembic upgrade head` as a release step, then `python -m app.cli seed`.
2. Deploy the API, wait for `/health/ready`, then deploy or restart the worker.
3. Run `python -m app.cli ops-check`; expect exit `0`.

If readiness never passes, roll back the application image. Migrations are
backward-compatible for one release (expand/contract), so the previous image can run on
the new schema. If the schema itself must be restored, follow the restore procedure from
the latest verified dump pair rather than running a downgrade in production.
