"""FastAPI application entry point.

Phase 0 scope only: `/healthz` and `/readyz`. Everything else in PLAN.md Appendix A.12
(`/v1/runs`, `/v1/replay/{scenario}`, `/v1/approvals/{approval_id}`, `/webhooks/github`, ...)
is wired in later phases once the Orchestrator / MemoryStore / PolicyEngine exist to back
them — `src/api/deps.py` (the composition root) is explicitly Phase 1.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, Response, status

from src.settings import get_settings

APP_VERSION = "0.1.0"

logger = logging.getLogger("harness.api")

app = FastAPI(title="Agent Harness", version=APP_VERSION)

# Fail fast, fail loud: validate Settings at import time (i.e. at container/process
# start) rather than lazily on first request. A misconfigured env then surfaces as a
# readable pydantic ValidationError naming the missing field(s) in the boot log, not
# a KeyError buried inside the first request's stack trace (DoD requirement).
get_settings()


async def _db_reachable(db_path: Path) -> str:
    """Phase 0 has no schema yet, so "healthy" means: the directory exists (or can be
    created), the file is openable by aiosqlite, and a trivial query round-trips.

    Returns one of three values, per PLAN.md Appendix B.3:
    - "ok": the trivial query round-tripped.
    - "degraded": an `OperationalError` whose message names the B.3 "disk full"
      condition (`disk I/O error`) — the component is impaired, not dead; `healthz`
      still returns 200 for this case (only `readyz` fails closed on it).
    - "error": anything else (missing/corrupt file, permission denied, ...) — Phase 3
      owns turning this into `config_error` escalations; Phase 0 just reports it.
    """
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            await db.execute("SELECT 1")
        return "ok"
    except sqlite3.OperationalError as exc:
        logger.exception("healthz: db check failed (operational)")
        return "degraded" if "disk i/o error" in str(exc).lower() else "error"
    except Exception:  # noqa: BLE001 - health checks must never raise
        logger.exception("healthz: db check failed")
        return "error"


async def _db_writable(db_path: Path) -> bool:
    """A real write round-trip: create a throwaway probe table, insert, delete."""
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS _healthcheck_probe (id INTEGER PRIMARY KEY)"
            )
            await db.execute("INSERT INTO _healthcheck_probe DEFAULT VALUES")
            await db.commit()
            await db.execute("DELETE FROM _healthcheck_probe")
            await db.commit()
        return True
    except Exception:  # noqa: BLE001 - health checks must never raise
        logger.exception("readyz: db writability check failed")
        return False


@app.get("/healthz")
async def healthz(response: Response) -> dict[str, str]:
    settings = get_settings()
    db_status = await _db_reachable(settings.database_path)
    # A healthy db keeps "status" == "ok" byte-for-byte (DoD's exact-body requirement);
    # any other db value is surfaced in "status" too rather than a hardcoded "ok" papering
    # over it (Wave-3 audit finding 10).
    #
    # Re-audit finding 1: HTTP status must not be a blanket 200. `fly.toml`'s
    # `[[http_service.checks]]` and the Dockerfile `HEALTHCHECK` both point at this route
    # and both key rotation-out-of-service purely on the HTTP status code, not the JSON
    # body — repointing them at `/readyz` isn't viable yet because `readyz` hardcodes
    # `policy_loaded = False` until Phase 2, which would fail it permanently. So: "ok" and
    # "degraded" both stay 200 (PLAN.md Appendix B.3 — a degraded-but-serving machine
    # belongs in rotation), and only "error" (db unreachable in a way that isn't the
    # known disk-full case) returns 503, pulling an unopenable-database machine out of
    # rotation instead of 500ing every request that reaches it.
    response.status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if db_status == "error"
        else status.HTTP_200_OK
    )
    return {
        "status": "ok" if db_status == "ok" else db_status,
        "db": db_status,
        "version": APP_VERSION,
    }


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    settings = get_settings()

    db_writable = await _db_writable(settings.database_path)
    gemini_key_present = bool(settings.gemini_api_key.get_secret_value())
    # PLAN.md Appendix E / policy loading is Phase 2 work (owned by the cicd-integration
    # agent's policy.yaml + the harness-core PolicyEngine loader). Reporting False here
    # rather than faking readiness.
    policy_loaded = False

    body = {
        "db_writable": db_writable,
        "gemini_key_present": gemini_key_present,
        "policy_loaded": policy_loaded,
    }
    response.status_code = (
        status.HTTP_200_OK
        if all(body.values())
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return body
