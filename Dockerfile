FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

WORKDIR /app

FROM base AS builder

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./

FROM base AS runtime

RUN groupadd --system --gid 1000 nero \
    && useradd --system --uid 1000 --gid nero --home-dir /app nero

COPY --from=builder --chown=nero:nero /app /app

# Create the asset root in the image so a fresh named volume inherits this
# ownership instead of being created root-owned on first mount.
RUN mkdir -p /app/storage && chown nero:nero /app/storage

ENV PATH="/app/.venv/bin:$PATH"

USER nero
EXPOSE 8000

# `/health/live` does not touch PostgreSQL, so the container is healthy while the
# release step or the database is still starting. Readiness is a load-balancer
# decision, not a restart decision.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

# Proxy headers are trusted because the API port is never published; only the
# reverse proxy on the private network can reach it. The worker overrides CMD.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header"]
