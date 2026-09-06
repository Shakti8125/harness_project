# syntax=docker/dockerfile:1
# Multi-stage build: resolve deps with uv in a builder stage, ship a slim runtime
# image with a non-root user and no build tooling.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Dependency layer first so it caches across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# This is an application (tool.uv.package = false), not an installable library —
# there is no separate "install the project" step; only src/ needs to be shipped.
COPY src ./src


FROM python:3.12-slim AS runtime

RUN groupadd --system harness && useradd --system --gid harness --create-home harness

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# PLAN.md's Phase 1 Verify step (`docker compose up -d --build` then
# `POST /v1/replay/real_regression`) runs against the *container*, not the host repo, so
# the demo fixtures must ship in the image. `.dockerignore` excludes only
# `fixtures/recorded/` (large recorded HTTP cassettes, not needed to run scenarios),
# which is the signal that the rest of `fixtures/` was always meant to ship — Wave-3
# audit finding 9.
COPY fixtures ./fixtures

RUN mkdir -p /app/data && chown -R harness:harness /app

USER harness

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
