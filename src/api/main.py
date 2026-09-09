"""FastAPI application entry point.

Phase 1 scope: `/healthz`, `/readyz`, and the run surface of PLAN.md Appendix A.12 that
the Investigator + Diagnostician slice can actually back — `POST /v1/runs`,
`GET /v1/runs/{run_id}`, `GET /v1/runs`, `GET /v1/runs/{run_id}/trace`, and the demo path
`POST /v1/replay/{scenario}`. The rest of A.12 (`/v1/approvals/{id}`,
`/v1/escalations`, `/webhooks/github`, `/runs/{id}/view`) is wired in later phases once
the Guardrails, Evaluator and Memory exist to back them.

Errors use RFC 9457 `application/problem+json`, per A.12.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from src.api.deps import AppContext, get_app_context, mint_run_id
from src.api.run_registry import RunRegistry
from src.harness.contracts import RunId, RunOutcome, RunRequest
from src.integrations.cicd.agents.investigator import parse_subject
from src.integrations.cicd.wiring import INTEGRATION
from src.settings import get_settings

APP_VERSION = "0.1.0"

PROBLEM_JSON = "application/problem+json"

logger = logging.getLogger("harness.api")

registry = RunRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the span table before the first request can try to write to it."""
    context = get_app_context()
    await context.recorder.initialize()
    yield


app = FastAPI(title="Agent Harness", version=APP_VERSION, lifespan=lifespan)

# Fail fast, fail loud: validate Settings at import time (i.e. at container/process
# start) rather than lazily on first request. A misconfigured env then surfaces as a
# readable pydantic ValidationError naming the missing field(s) in the boot log, not
# a KeyError buried inside the first request's stack trace (DoD requirement).
get_settings()


def _digest_str_field(container: dict[str, Any], field: str) -> None:
    """Replace `container[field]` (a `str`) with `{field}_length` + `{field}_sha256`.

    A no-op if the field is absent or not a `str` — in particular, `FileChange.patch`
    is `str | None` and a `None` patch (GitHub omitted it: binary or too large) must
    stay `None`, not become a digest of the empty string.
    """
    value = container.get(field)
    if not isinstance(value, str):
        return
    del container[field]
    container[f"{field}_length"] = len(value)
    container[f"{field}_sha256"] = hashlib.sha256(value.encode()).hexdigest()


def _serialize_run_outcome(outcome: RunOutcome) -> dict[str, Any]:
    """`RunOutcome.model_dump(mode="json")`, with two exceptions applied at this HTTP
    boundary only: every `final.<artifact>.logs[].excerpt` and every
    `final.<artifact>.diff.files[].patch` is replaced by its length and sha256 digest
    rather than served verbatim.

    Wave-3 audit finding 3: `final.bundle.logs[].excerpt` is the entire budgeted CI job
    log the Investigator collected — tens of thousands of characters on the current
    fixture — served unauthenticated and unredacted (the `Redactor` covers span
    attributes only; nothing scrubs this path). PLAN.md:862-866 plans a Phase 5 fixture
    whose log fixture itself contains a pasted credential; the day that fixture lands,
    this route would serve that credential verbatim to anyone on the internet, and the
    leak test as specified would not catch it (it inspects spans, escalations and
    `harness.db`, not HTTP responses).

    `final.bundle.diff.files[].patch` (`FileChange.patch`, `src/integrations/cicd/
    schemas.py`) is the same exposure class on the same route: raw repository diff
    content, unbounded in principle, and a credential committed into a file (`.env`, a
    key, a config) is at least as likely as one pasted into a job log. It measured small
    on the current single-file fixture (252 chars) next to the 40k-character log excerpt,
    which is why the first pass of this fix missed it — not a difference in risk.

    Length + digest, not truncation: a truncated prefix still leaks whatever a credential
    fixture happens to place in the kept portion, so it isn't actually a security fix,
    only a smaller one. And a bare removal throws away a real capability for free: the
    digest lets a caller who has independently fetched the real content (from the CI
    provider / the repository itself) verify byte-for-byte that this is the excerpt or
    patch the harness actually reasoned over, without this process ever re-serving the
    content — the same content-addressed shape `Evidence.sha256` already uses elsewhere
    in this codebase. Nothing a legitimate consumer needs is lost: `total_lines`,
    `included_lines`, `anchor_line_numbers` and the `truncation` report on every
    `LogExcerpt`, and `path`/`status`/`additions`/`deletions` on every `FileChange`, are
    untouched; the harness's own designed evidence surface for a human or downstream
    system to read is `final.diagnosis.citations[].quote` (bounded, `max_length=500`,
    produced and cited deliberately by the Diagnostician) — left byte-for-byte intact.

    This is a deliberate, narrow divergence from A.12's literal "200 RunOutcome": the
    served JSON is no longer a lossless `model_dump` of the internal object for these two
    nested fields. Flagged for the record rather than decided silently — see
    docs/progress/phase-1/api-surface.md.

    Deliberately a field-name walk, not a structural/type-based scrub: matches on the
    literal keys `excerpt` and `patch` inside `logs[]` / `diff.files[]`. That is a known,
    accepted limitation, not an oversight — see the same file's "Notes for the reviewer"
    for why it is not being generalised in this round.
    """
    body = outcome.model_dump(mode="json")
    for artifact in body.get("final", {}).values():
        if not isinstance(artifact, dict):
            continue
        logs = artifact.get("logs")
        if isinstance(logs, list):
            for log_entry in logs:
                if isinstance(log_entry, dict):
                    _digest_str_field(log_entry, "excerpt")
        diff = artifact.get("diff")
        files = diff.get("files") if isinstance(diff, dict) else None
        if isinstance(files, list):
            for file_entry in files:
                if isinstance(file_entry, dict):
                    _digest_str_field(file_entry, "patch")
    return body


def problem(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    run_id: str | None = None,
) -> JSONResponse:
    """An RFC 9457 `application/problem+json` body.

    `detail` is authored at each call site rather than interpolated from an exception, so
    nothing an upstream system said reaches the client unread. The `Redactor` covers the
    trace; this covers the response.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if run_id is not None:
        body["run_id"] = run_id
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_JSON)


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


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def idempotency_key_for(subject: dict[str, Any]) -> str:
    """PLAN.md Appendix C: keyed off the *domain object*, not the delivery.

    Two redeliveries of the same webhook, or a manual re-post of the same body, produce
    the same key; a genuine re-run of the workflow produces a different one, because
    `run_attempt` is part of it.
    """
    parsed = parse_subject(subject)
    digest = hashlib.sha256(
        f"{parsed['repo']}|{parsed['run_id']}|{parsed['run_attempt']}".encode()
    ).hexdigest()[:32]
    return f"{INTEGRATION}:{digest}"


async def _execute(
    context: AppContext,
    request_model: RunRequest,
    scenario_dir: Path,
    repo: str,
    run_id: RunId,
) -> RunOutcome:
    """Run one request to completion under the concurrency limit."""
    async with context.run_semaphore:
        orchestrator = context.build_replay_orchestrator(scenario_dir, repo, run_id=run_id)
        outcome = await orchestrator.run(request_model)
    await registry.save(outcome)
    return outcome


def _load_scenario(context: AppContext, scenario: str) -> tuple[Path, dict[str, Any]]:
    directory = context.scenario_dir(scenario)
    webhook = directory / "webhook.json"
    if not webhook.is_file():
        raise FileNotFoundError(scenario)
    return directory, json.loads(webhook.read_text(encoding="utf-8"))


@app.post("/v1/replay/{scenario}")
async def replay(
    request: Request,
    scenario: str,
    fresh: bool = Query(True, description="Ignored in this phase; replays are always fresh."),
    sync: bool = Query(True, description="Run synchronously and return the outcome."),
) -> Response:
    """Replay a recorded scenario. Synchronous by default — this is the demo path.

    `fresh` is accepted for A.12 compatibility and documented as a no-op: deduplicating a
    replay needs the idempotency claim in the memory store, which arrives in a later
    phase. Until then every replay genuinely re-runs, which is also what makes it usable
    as a demo.
    """
    context = get_app_context()
    try:
        scenario_dir, webhook = _load_scenario(context, scenario)
    except ValueError:
        return problem(
            request, status_code=400, title="Invalid scenario",
            detail="The scenario name is not a valid fixture directory name.",
        )
    except FileNotFoundError:
        return problem(
            request, status_code=404, title="Scenario not found",
            detail=f"No recorded scenario named {scenario!r}.",
        )
    except json.JSONDecodeError:
        return problem(
            request, status_code=500, title="Malformed fixture",
            detail=f"The recorded webhook for {scenario!r} is not valid JSON.",
        )

    parsed = parse_subject(webhook)
    run_request = RunRequest(
        integration=INTEGRATION,
        subject=webhook,
        idempotency_key=idempotency_key_for(webhook),
        mode="replay",
        replay_fixture=scenario,
        requested_by="replay",
    )
    run_id = mint_run_id()

    if not sync:
        await registry.mark_in_progress(
            run_id, INTEGRATION, f"/v1/runs/{run_id}/trace"
        )
        asyncio.create_task(  # noqa: RUF006 - fire and forget; the registry is the handle
            _execute(context, run_request, scenario_dir, parsed["repo"], run_id)
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"run_id": run_id, "status": "in_progress"},
        )

    outcome = await _execute(context, run_request, scenario_dir, parsed["repo"], run_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK, content=_serialize_run_outcome(outcome)
    )


@app.post("/v1/runs")
async def create_run(request: Request, run_request: RunRequest) -> Response:
    """Accept a run and execute it in the background. `202 {run_id, status}` per A.12.

    The run id is minted here rather than inside the orchestrator so that the 202 can
    name the run it just accepted; the orchestrator is then wired to use that same id, so
    the id in this response and the id in the trace are the same string by construction.
    """
    context = get_app_context()
    if run_request.integration != INTEGRATION:
        return problem(
            request, status_code=400, title="Unknown integration",
            detail=f"No integration named {run_request.integration!r} is registered.",
        )

    # Live gateways arrive with the Remediator phase; until then the only backing a run
    # can have is a recorded scenario, and saying so plainly beats a confusing failure
    # deep inside collection.
    scenario = run_request.replay_fixture
    if run_request.mode != "replay" or not scenario:
        return problem(
            request, status_code=501, title="Live mode not available",
            detail=(
                "This build serves replay runs only: set mode='replay' and name a "
                "replay_fixture. The live gateway is wired in a later phase."
            ),
        )
    try:
        scenario_dir, _ = _load_scenario(context, scenario)
    except (ValueError, FileNotFoundError):
        return problem(
            request, status_code=404, title="Scenario not found",
            detail=f"No recorded scenario named {scenario!r}.",
        )

    parsed = parse_subject(dict(run_request.subject))
    run_id = mint_run_id()
    await registry.mark_in_progress(run_id, run_request.integration, f"/v1/runs/{run_id}/trace")
    asyncio.create_task(  # noqa: RUF006 - fire and forget; the registry is the handle
        _execute(context, run_request, scenario_dir, parsed["repo"], run_id)
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": run_id, "status": "in_progress"},
    )


@app.get("/v1/runs")
async def list_runs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    runs = await registry.list(limit=limit, status=status_filter)
    return {
        "items": [
            {
                "run_id": run.run_id,
                "integration": run.integration,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
                "duration_ms": run.duration_ms,
            }
            for run in runs
        ],
        # Cursor pagination needs a durable, ordered store; it arrives with the memory
        # phase. Reported as null rather than omitted so the response shape is already
        # the one A.12 specifies.
        "next_cursor": None,
    }


@app.get("/v1/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> Response:
    outcome = await registry.get(run_id)
    if outcome is None:
        return problem(
            request, status_code=404, title="Run not found",
            detail="No run with that id is known to this process.", run_id=run_id,
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK, content=_serialize_run_outcome(outcome)
    )


@app.get("/v1/runs/{run_id}/trace")
async def get_run_trace(request: Request, run_id: str) -> Response:
    """The span trace, read back out of SQLite by the recorder that wrote it."""
    context = get_app_context()
    trace = await context.recorder.read_trace(run_id)
    if trace is None:
        return problem(
            request, status_code=404, title="Trace not found",
            detail="No spans are recorded for that run id.", run_id=run_id,
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK, content=trace.model_dump(mode="json")
    )
