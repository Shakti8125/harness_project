"""FastAPI application entry point.

The run surface of PLAN.md Appendix A.12 that the built slices can back: `/healthz`,
`/readyz`, `POST /v1/runs` (replay, and live behind two opt-ins), `GET /v1/runs/{run_id}`,
`GET /v1/runs`, `GET /v1/runs/{run_id}/trace`, the demo path `POST /v1/replay/{scenario}`,
and -- since the Guardrails phase -- `POST /v1/approvals/{approval_id}` and
`GET /v1/escalations`. `/webhooks/github` and `/runs/{id}/view` arrive with the
observability phase.

Errors use RFC 9457 `application/problem+json`, per A.12.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Coroutine, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final, Literal

import aiosqlite
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.approval_registry import ApprovalEntry, ApprovalRegistry, RunContext
from src.api.deps import AppContext, get_app_context, mint_run_id
from src.api.run_registry import RunRegistry
from src.harness.contracts import EscalationRecord, RunId, RunOutcome, RunRequest
from src.harness.gateway import ToolGateway
from src.harness.observability import REDACTION_PLACEHOLDER
from src.integrations.cicd.agents.investigator import parse_subject
from src.integrations.cicd.remediation import (
    EVALUATION_SKIPPED,
    build_facts,
    decide_plan,
    denial_summary,
    execute_plan,
    failed_execution,
    failure_summary,
    plan_verdict,
)
from src.integrations.cicd.rendering import validate_prompt_templates
from src.integrations.cicd.schemas import Diagnosis, FailureBundle, RemediationResult
from src.integrations.cicd.wiring import INTEGRATION, REMEDIATION_KEY
from src.settings import get_settings

APP_VERSION = "0.1.0"

PROBLEM_JSON = "application/problem+json"

# Mirrors `RunId` (`src.harness.contracts`) — kept as a plain module-level regex rather
# than imported from there so this file doesn't reach into pydantic internals
# (`RunId.__metadata__`) just to get the pattern back out. Used by `problem()` to decide
# whether a caller-controlled path segment is shaped like a real run id before echoing it
# (re-audit finding 5).
_RUN_ID_PATTERN = re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$")

logger = logging.getLogger("harness.api")

registry = RunRegistry()
approvals = ApprovalRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Fail loudly at startup rather than on the first request.

    Two checks, both deliberately left to raise: a missing or malformed prompt template
    (`prompts/*.md`) under the pre-round Python-constant design was an `ImportError` at
    process start; the `.md` port made `load_prompt_template` a lazy, unguarded file read
    inside `build_prompt`, so the same defect became a `500 text/plain` on the first
    `/v1/replay` call instead — no RFC 9457 body, no `run_id`, nothing in the trace
    (review-2.md finding 3). `validate_prompt_templates()` restores the original property
    by loading every template this integration renders before the app accepts traffic.
    Ordered first so a template failure is reported before anything else runs.
    """
    validate_prompt_templates()
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


def _digest_content_b64(node: Any) -> None:
    """Replace every `content_b64` string anywhere under `node` with its length + digest."""
    if isinstance(node, dict):
        _digest_str_field(node, "content_b64")
        for value in node.values():
            _digest_content_b64(value)
    elif isinstance(node, list):
        for value in node:
            _digest_content_b64(value)


def _serialize_run_outcome(outcome: RunOutcome) -> dict[str, Any]:
    """`RunOutcome.model_dump(mode="json")`, with two exceptions applied at this HTTP
    boundary and a full-body pass through the `Redactor` on top of them: every
    `final.<artifact>.logs[].excerpt` and every `final.<artifact>.diff.files[].patch` is
    replaced by its length and sha256 digest rather than served verbatim, and the
    resulting body is then scrubbed for registered secrets and credential-shaped strings
    wherever they occur.

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
    produced and cited deliberately by the Diagnostician) — left in place, but see below.

    This is a deliberate, narrow divergence from A.12's literal "200 RunOutcome": the
    served JSON is no longer a lossless `model_dump` of the internal object for these two
    nested fields. Flagged for the record rather than decided silently — see
    docs/progress/phase-1/api-surface.md.

    Deliberately a field-name walk, not a structural/type-based scrub: matches on the
    literal keys `excerpt` and `patch` inside `logs[]` / `diff.files[]`. That is a known,
    accepted limitation, not an oversight — see the same file's "Notes for the reviewer"
    for why it is not being generalised in this round. It is also, on its own, an
    incomplete fix: `final.diagnosis.citations[].quote` and
    `final.bundle.notes.observations[]` are model-authored fields that
    `prompts/diagnostician.md` explicitly instructs the model to fill with a **verbatim**
    quote of the evidence it was given, so the same credential this digest substitution
    withholds from `logs[].excerpt` can still reach the client through a citation or an
    observation that happens to quote the line it appears on — `Citation.quote` bounds
    length (`max_length=500`) but not content, and `InvestigationNotes.observations`
    bounds item *count* (`max_length=8`), not item length. Rather than special-case those
    two fields too — which only narrows the same class of bug to whatever raw-content
    field the next integration adds — the whole serialized body is passed through the
    `Redactor` below. That covers every field, named here or not, present today or added
    later, and reuses the same registered-secret and pattern list (`gh[pousr]_`,
    `github_pat_`, `AIza`, `xox[baprs]-`, bearer tokens — `src/api/deps.py`) that already
    scrubs the trace. It does a different job than the field-name walk above — credential
    removal by content, not bulk removal by field name — so both stay, additively.
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
    # Phase 2: a `create_or_update_file` call carries the whole drafted file as
    # `args.content_b64`, wherever the plan appears (`final.remediation.plan`, and again
    # inside `pending_approval.plan`). Base64 is exactly the encoding the `Redactor`'s
    # patterns cannot see through -- a credential the model reproduced from the diff into
    # the draft would ride out in a form no regex matches -- so it is digested here like
    # `patch`, by key name, anywhere in the body. The plaintext twin
    # (`pr_draft.files[].new_content`) is model-authored like a citation and is left to
    # the whole-body scrub below, which does see it.
    _digest_content_b64(body)
    scrubbed = get_app_context().recorder.redactor.scrub(body)
    assert isinstance(scrubbed, dict)  # body was a dict; Redactor preserves the JSON shape
    return scrubbed


def problem(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    detail_suffix: str = "",
    run_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, JsonValue] | None = None,
) -> JSONResponse:
    """An RFC 9457 `application/problem+json` body.

    `extensions` are RFC 9457 §3.2 extension members (the approval route answers a `409`
    with the approval's current `state`, which A.12 specifies). They are merged *before*
    the `Redactor` pass, may not shadow a standard member, and are harness-authored --
    a fixed key around an enum value -- never caller-derived.

    Every call site still authors `detail` itself rather than interpolating an upstream
    message verbatim — that discipline is worth keeping — but it is no longer the only
    thing standing between a leaked secret and the client. `detail` (and the rest of the
    body) is passed through `get_app_context().recorder.redactor` before being returned,
    the same `Redactor` instance that scrubs the trace, built from the same registered
    secrets and credential-shaped patterns (`src/api/deps.py`). Wave-3 finding 6: the
    prior docstring described a *discipline* guarantee — "nothing an upstream system
    said reaches the client unread" holds only as long as every call site keeps
    authoring constants. The catch-all handler below is exactly the call site where an
    exception's string first enters `detail`'s scope, which is why this round makes the
    guarantee structural instead: whatever ends up in `detail`, by convention or by
    accident, is scrubbed before it leaves the process.

    `headers`, if given, are attached to the response as-is — deliberately **not** passed
    through the `Redactor`. They are always framework- or route-authored (RFC 9110
    §15.5.6's `Allow` on a 405, a future route's `Retry-After` on a 429 or
    `WWW-Authenticate` on a 401 — `StarletteHTTPException.headers`, never something lifted
    from the request), the same trust boundary this function already extends to `title`
    and to `exc.detail` in `handle_http_exception` below. A generic byte-content scrub
    here would risk mangling a spec-shaped value (`Allow: HEAD, POST, GET`,
    `Retry-After: 30`) for a threat model — this process's own code choosing to leak a
    secret through a header it authored itself — that scrubbing the body already doesn't
    defend against either. If a later phase ever threads caller-influenced data into a
    response header, that call site should scrub it before it reaches `problem()`, the
    same discipline `detail`'s authors already keep; this function will not silently do
    it for them, because doing so for every header would break the ones above (re-audit
    finding 1).

    One header is not passed through: `Content-Type`. Starlette's `Response.init_headers`
    lets an explicit `Content-Type` in `headers` *displace* `media_type`, so a future
    route raising `HTTPException(409, headers={"Content-Type": ...})` would silently
    serve something other than `application/problem+json` and break A.12 with no test
    noticing. The media type of a problem document is not a call site's to choose, so it
    is dropped here rather than honoured (final-audit finding 7).

    `run_id` is echoed into the body only when it is shaped like a real `RunId`
    (`_RUN_ID_PATTERN`) — a caller-supplied path segment that failed to route (`GET
    /v1/runs/x`, `_run_id_in_scope` reading `request.path_params["run_id"]`) is silently
    omitted rather than reflected back unvalidated (re-audit finding 5).
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if run_id is not None and _RUN_ID_PATTERN.match(run_id):
        body["run_id"] = run_id
    for key, value in (extensions or {}).items():
        if key not in body:
            body[key] = value
    scrubbed = get_app_context().recorder.redactor.scrub(body)
    assert isinstance(scrubbed, dict)  # body was a dict; Redactor preserves the JSON shape
    scrubbed["detail"] = _bound_detail(str(scrubbed["detail"]), detail_suffix)
    return JSONResponse(
        status_code=status_code,
        content=scrubbed,
        media_type=PROBLEM_JSON,
        headers=_response_headers(headers),
    )


def _response_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """`headers` minus any `Content-Type` -- see `problem`'s docstring."""
    if not headers:
        return None
    kept = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    return kept or None


def _run_id_in_scope(request: Request) -> str | None:
    """Best-effort `run_id` for an error response.

    Exception handlers run outside the route function and cannot see its locals, so this
    reads whichever of two places the route left one: the `{run_id}` path parameter, for
    routes that take an existing run's id (`GET /v1/runs/{run_id}`, `.../trace`); or
    `request.state.run_id`, which `POST /v1/runs` and `POST /v1/replay/{scenario}` set
    immediately after minting a fresh id and before doing anything that could fail, for
    exactly this reason. `None` when neither is set — e.g. a validation error on the
    request body, raised before any run id exists.
    """
    path_value = request.path_params.get("run_id")
    if isinstance(path_value, str):
        return path_value
    state_value = getattr(request.state, "run_id", None)
    return state_value if isinstance(state_value, str) else None


_MAX_VALIDATION_ERRORS = 20   # errors beyond this are counted, not rendered
_MAX_DETAIL_LENGTH = 2000     # characters, not bytes: `len()` and slicing count code points,
                              # and Starlette renders with `ensure_ascii=False`, so the served
                              # body's byte length can exceed this by the UTF-8 expansion
                              # factor. Characters are what this cap is for -- bounding a
                              # caller-chosen key reflected back -- and a character costs the
                              # same bytes in the request as in the response, so there is no
                              # amplification either way (final-audit finding 5).


def _bound_detail(detail: str, suffix: str) -> str:
    """Cap `detail` at `_MAX_DETAIL_LENGTH`, then append the harness-authored `suffix`.

    Called from `problem()` *after* the `Redactor` pass, which is the whole point: cutting
    a caller-influenced string before scrubbing it leaves a credential that no longer
    matches `SECRET_PATTERNS`, and its surviving prefix is then served (final-audit
    finding 3).

    Cutting *after* the scrub buys that at the cost of a hazard the old order did not
    have: the cut can now land inside a `***REDACTED***` marker, and a bare `***RED` in
    the served body reads as content rather than as an elision. So a trailing partial
    marker is dropped. A *complete* marker is never trimmed — no proper prefix of
    `***REDACTED***` is also one of its suffixes — and over-trimming a run of literal
    asterisks that merely looks like a marker prefix costs at most 13 characters of an
    already-truncated string.

    `suffix` is exempt from the cap by design. It is the "... and N more error(s)" note,
    authored here around an `int`, and it exists precisely to say that something was
    elided — letting the cap eat it would hide the elision it announces (finding 6).
    Nothing caller-derived may be passed here, since it does not see the `Redactor`.
    """
    if len(detail) > _MAX_DETAIL_LENGTH:
        cut = detail[: _MAX_DETAIL_LENGTH - 1]
        for size in range(len(REDACTION_PLACEHOLDER) - 1, 0, -1):
            if cut.endswith(REDACTION_PLACEHOLDER[:size]):
                cut = cut[:-size]
                break
        detail = cut.rstrip() + "…"
    return detail + suffix


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """RFC 9457 for a malformed request body or query, per A.12 (Wave-3 finding 5).

    FastAPI's default 422 handler serves `{"detail": [...]}` with each error's `"input"`
    key — the caller's submitted *value* — echoed back verbatim; for `POST /v1/runs`,
    `input` is `RunRequest.subject`, arbitrary caller-supplied JSON. This handler builds
    `detail` from only `loc` (where) and `msg` (why) and never touches `err["input"]`, so
    the submitted value never reaches the response.

    That is narrower than "nothing the client sent is reproduced" — the previous version
    of this docstring overclaimed it (re-audit finding 3). `RunRequest` is
    `extra="forbid"`, so an unrecognised JSON *key* is itself part of `loc` (Pydantic
    reports `"Extra inputs are not permitted"` at `body.<that key>`), and an attacker who
    controls key names controls a slice of this response. `problem()`'s `Redactor` pass
    still runs over the result and catches a registered secret sitting in key position,
    but an arbitrary non-secret-shaped key passes through unchanged; `loc`/`msg`-only
    construction doesn't close that, because here the key name *is* the location.

    Two bounds keep that reflection from becoming an amplification vector on this
    unauthenticated route: at most `_MAX_VALIDATION_ERRORS` errors are rendered (the rest
    are counted into a trailing "N more" note), and the assembled `detail` is hard-capped
    at `_MAX_DETAIL_LENGTH` characters regardless of how many errors contributed to it or
    how long any single `loc` is — so one caller-chosen giant key bounds the same way many
    small ones do.

    Neither bound is applied here. Both live in `problem()`, *after* the `Redactor` pass,
    because truncating first defeats scrubbing: a credential straddling the cut is no
    longer shaped like one, so no `SECRET_PATTERNS` entry matches it and the surviving
    prefix is served (final-audit finding 3). The "N more" note travels as
    `detail_suffix` rather than as part of `detail`, so the cap cannot eat the very count
    that says something was elided (final-audit finding 6); it is harness-authored — a
    fixed string around an `int` — so nothing caller-derived skips the scrub by riding it.
    """
    errors = exc.errors()
    reasons: list[str] = []
    for error in errors[:_MAX_VALIDATION_ERRORS]:
        loc = ".".join(str(part) for part in error["loc"])
        reasons.append(f"{loc}: {error['msg']}")
    detail = "; ".join(reasons) or "The request could not be validated."
    remaining = len(errors) - len(reasons)
    return problem(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        title="Validation error",
        detail=detail,
        detail_suffix=f"; ... and {remaining} more error(s)" if remaining > 0 else "",
        run_id=_run_id_in_scope(request),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """RFC 9457 for every framework-raised or route-raised `HTTPException`.

    No route in this module raises `HTTPException` today — each one builds its own
    `problem()` response directly — so in this phase this only reaches the cases FastAPI
    generates itself: 404 on a path with no matching route, 405 on a matched path with
    the wrong method. Routing those through `problem()` too, rather than leaving them on
    FastAPI's default `{"detail": ...}` JSON, is the judgement call the finding asks for:
    A.12 specifies RFC 9457 for "errors" without carving these out, nothing in this repo
    reads FastAPI's default shape (`app.py`'s Space UI calls the JSON routes below, not a
    404 page), and a later phase's route that chooses `raise HTTPException(409, ...)` for
    A.12's already-decided-approval case gets the right body shape for free rather than
    needing to remember `problem()` instead. `exc.detail` is framework/route-authored,
    never client input, so it is safe to surface as `detail` — and still passes through
    `problem()`'s `Redactor` scrub regardless.

    `exc.headers` is forwarded to `problem()` and attached to the response unchanged.
    Starlette's own default handler sets it (its 405 for a matched path with the wrong
    method carries `Allow`, RFC 9110 §15.5.6's MUST), and this handler is what stands
    between that default and the client now that routing goes through `problem()`
    instead — dropping `exc.headers` here would silently strip `Allow` off every 405, and
    `Retry-After`/`WWW-Authenticate` off any future route-raised `HTTPException(429, ...)`
    or `HTTPException(401, ...)`, which is exactly the regression re-audit finding 1
    measured (`DELETE /v1/runs` losing `Allow`). See `problem()`'s docstring for why those
    headers are forwarded as-is rather than run through the `Redactor`.
    """
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "Error"
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else title
    return problem(
        request,
        status_code=exc.status_code,
        title=title,
        detail=detail,
        run_id=_run_id_in_scope(request),
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """The structural backstop Wave-3 finding 5 asks for: whatever a route, a dependency
    or an agent raises that nothing more specific above handled lands here instead of
    FastAPI's default bare `500 text/plain "Internal Server Error"`.

    The missing prompt template (review-2.md finding 3) was one instance of that bare
    500; eager validation in `lifespan` removed that one instance, not the class this
    handler now closes. The real exception is logged here, server-side, with a traceback
    and the `run_id` if the failing route had one — that is what the trace/log pair is
    for — and *never* echoed to the client: `detail` is a fixed, generic string, so an
    upstream error message (a stack frame, a file path, a fragment of a prompt) can never
    reach an unauthenticated caller through this path, structurally, not by convention.
    """
    run_id = _run_id_in_scope(request)
    logger.exception(
        "unhandled exception on %s %s%s",
        request.method,
        request.url.path,
        f" run_id={run_id}" if run_id else "",
        exc_info=exc,
    )
    return problem(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        title="Internal Server Error",
        detail="An unexpected error occurred while processing the request.",
        run_id=run_id,
    )


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
    # The engine is built from `policy.yaml` at startup (`deps.get_app_context`), and a
    # malformed file raises there -- so reaching this line with an engine means the policy
    # loaded. Reported off the object rather than as a constant.
    policy_loaded = bool(get_app_context().engine.spec.rules)

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


#: Strong references to in-flight background runs.
#:
#: `asyncio` keeps only a *weak* reference to a task, so a bare `create_task(...)`
#: whose result nobody holds can be garbage-collected mid-run. Both call sites here
#: previously suppressed RUF006 with the note "fire and forget; the registry
#: is the handle" -- but the registry holds a database row, not the task object, so
#: nothing kept the coroutine alive. The silenced lint was pointing at a real defect,
#: and this set is the fix RUF006 actually asks for.
_background_runs: Final[set[asyncio.Task[RunOutcome]]] = set()


async def _supervised(
    coro: Coroutine[Any, Any, RunOutcome], run_id: RunId
) -> RunOutcome:
    """Run `coro`, and record a failure that no HTTP handler could have caught.

    A background run raises *after* its `202` has been sent, so the exception has
    nowhere to go: the registry row written by `mark_in_progress` would sit at
    `in_progress` forever, and `GET /v1/runs/{run_id}` could not distinguish a dead
    run from a slow one. Marking it `failed` is the whole point.

    `CancelledError` is re-raised untouched rather than recorded: it means the
    server is shutting down, which is not the run's fault, and it is not an
    `Exception` subclass -- so the bare `except Exception` below already lets it
    through, and the explicit clause is here to say that is deliberate.
    """
    try:
        return await coro
    except asyncio.CancelledError:
        logger.info("background run %s cancelled", run_id)
        raise
    except Exception as exc:
        logger.exception("background run %s failed", run_id)
        await registry.mark_failed(run_id, str(exc))
        raise


def _spawn_run(coro: Coroutine[Any, Any, RunOutcome], run_id: RunId) -> None:
    """Start a background run and keep a strong reference until it finishes.

    Not a task supervisor, deliberately. `MemoryStore` already declares
    `heartbeat(run_id)` and `RunClaim.took_over_from`, which is the plan's real
    answer to "a run died mid-flight" -- takeover by another worker, needing the
    durable store that arrives in Phase 3. Building a supervisor now would build it
    twice, and the second one would replace this.
    """
    task = asyncio.create_task(_supervised(coro, run_id))
    _background_runs.add(task)
    task.add_done_callback(_background_runs.discard)


def _gateway_for(context: AppContext, run_context: RunContext) -> ToolGateway:
    if run_context.mode == "live":
        return context.build_live_gateway(run_context.repo)
    if run_context.scenario_dir is None:  # pragma: no cover - replay always names one
        raise ValueError("a replay run must name a scenario directory")
    return context.build_replay_gateway(run_context.scenario_dir, run_context.repo)


async def _execute(
    context: AppContext,
    request_model: RunRequest,
    run_context: RunContext,
    run_id: RunId,
) -> RunOutcome:
    """Run one request to completion under the concurrency limit.

    A run that ends `awaiting_approval` has its `ApprovalRequest` registered here, keyed
    by approval id together with `run_context`, so `POST /v1/approvals/{id}` can rebuild
    the same gateway later. The Remediator never touches storage; this is the "harness
    stores the plan" half of PLAN.md's approval state machine, in the layer that owns
    persistence.
    """
    gateway = _gateway_for(context, run_context)
    try:
        async with context.run_semaphore:
            orchestrator = context.build_orchestrator_for(gateway, run_id=run_id)
            outcome = await orchestrator.run(request_model)
    finally:
        await gateway.aclose()
    await registry.save(outcome)
    if outcome.status == "awaiting_approval":
        remediation = _remediation_of(outcome)
        if remediation is not None and remediation.pending_approval is not None:
            await approvals.save(remediation.pending_approval, run_context)
    return outcome


def _remediation_of(outcome: RunOutcome) -> RemediationResult | None:
    raw = outcome.final.get(REMEDIATION_KEY)
    if not isinstance(raw, dict):
        return None
    return RemediationResult.model_validate(raw)


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
    request.state.run_id = run_id  # so an unhandled exception below can still report it

    run_context = RunContext(mode="replay", repo=parsed["repo"], scenario_dir=scenario_dir)
    if not sync:
        await registry.mark_in_progress(
            run_id, INTEGRATION, f"/v1/runs/{run_id}/trace"
        )
        _spawn_run(_execute(context, run_request, run_context, run_id), run_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"run_id": run_id, "status": "in_progress"},
        )

    outcome = await _execute(context, run_request, run_context, run_id)
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

    parsed = parse_subject(dict(run_request.subject))
    run_context: RunContext
    if run_request.mode == "live":
        # Live mode is opt-in twice: the process must be configured for the live gateway,
        # and the repository must be allowlisted. Each refusal names which one is missing,
        # because "501" and "403" are the difference between a deployment choice and a
        # request that named a repository this deployment does not serve.
        if context.settings.gateway != "github":
            return problem(
                request, status_code=501, title="Live mode not available",
                detail=(
                    "This deployment is configured for replay only "
                    "(HARNESS_GATEWAY=replay); set mode='replay' and name a replay_fixture."
                ),
            )
        if not context.live_allowed(parsed["repo"]):
            return problem(
                request, status_code=403, title="Repository not allowlisted",
                detail="The subject's repository is not in HARNESS_ALLOWED_REPOS.",
            )
        run_context = RunContext(mode="live", repo=parsed["repo"], scenario_dir=None)
    else:
        scenario = run_request.replay_fixture
        if not scenario:
            return problem(
                request, status_code=422, title="Validation error",
                detail="mode='replay' requires replay_fixture to name a recorded scenario.",
            )
        try:
            scenario_dir, _ = _load_scenario(context, scenario)
        except (ValueError, FileNotFoundError):
            return problem(
                request, status_code=404, title="Scenario not found",
                detail=f"No recorded scenario named {scenario!r}.",
            )
        run_context = RunContext(mode="replay", repo=parsed["repo"], scenario_dir=scenario_dir)

    run_id = mint_run_id()
    request.state.run_id = run_id  # so an unhandled exception below can still report it
    await registry.mark_in_progress(run_id, run_request.integration, f"/v1/runs/{run_id}/trace")
    _spawn_run(_execute(context, run_request, run_context, run_id), run_id)
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


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class ApprovalDecision(BaseModel):
    """`POST /v1/approvals/{id}` body, per A.12."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, max_length=128)
    note: str | None = Field(None, max_length=1000)


def _escalation_after_approval(
    remediation: RemediationResult,
) -> EscalationRecord | None:
    """The same two rules `wiring.remediation_suspend` applies in-run, for a plan decided
    later through the approval route: an execution that failed escalates `tool_failure`
    (Appendix B.2); a re-evaluation that *denied* an approved plan escalates
    `policy_denied` (review finding 3 -- unreachable while the facts cannot change between
    suspension and approval, live once memory can move them). A person's rejection is
    neither: it completes the run.
    """
    approval_id = (
        remediation.pending_approval.approval_id
        if remediation.pending_approval is not None else None
    )
    failed = failed_execution(remediation)
    if failed is not None:
        return EscalationRecord(
            escalation_id="esc_" + secrets.token_hex(8),
            reason="tool_failure",
            message=failure_summary(failed),
            payload={
                "stage": "approval",
                "approval_id": approval_id,
                "action": remediation.plan.action,
                "tool": failed.tool,
                "error_kind": failed.error.kind if failed.error is not None else None,
                "executed": len(remediation.executed),
            },
            channels=["log"],
            delivered_at=datetime.now(UTC),
        )
    if remediation.status == "denied":
        return EscalationRecord(
            escalation_id="esc_" + secrets.token_hex(8),
            reason="policy_denied",
            message=denial_summary(remediation.decisions),
            payload={
                "stage": "approval",
                "approval_id": approval_id,
                "action": remediation.plan.action,
                "decisions": [
                    {"tool": d.tool, "rule_id": d.rule_id, "effect": d.effect}
                    for d in remediation.decisions
                ],
            },
            channels=["log"],
            delivered_at=datetime.now(UTC),
        )
    return None


async def _settle_run(run_id: RunId, remediation: RemediationResult) -> None:
    """Write the decided remediation back into the stored `RunOutcome`.

    Without this, `GET /v1/runs/{id}` would say `awaiting_approval` forever after the
    approval was decided -- the response to the `POST` would be the only record. The run's
    status follows the rules `_escalation_after_approval` states.
    """
    outcome = await registry.get(run_id)
    if outcome is None:
        return
    final = dict(outcome.final)
    final[REMEDIATION_KEY] = remediation.model_dump(mode="json")
    update: dict[str, Any] = {"status": "completed", "final": final}
    escalation = _escalation_after_approval(remediation)
    if escalation is not None:
        update["status"] = "escalated"
        update["escalation"] = escalation
        logger.warning(
            "run %s escalated (%s) after approval: %s",
            run_id, escalation.reason, escalation.message,
        )
    await registry.save(outcome.model_copy(update=update))


async def _execute_approved(
    context: AppContext, entry: ApprovalEntry
) -> tuple[RemediationResult, list[dict[str, Any]]]:
    """Re-evaluate the stored plan against the run's artifacts, then execute if allowed.

    PLAN.md: "the harness re-evaluates policy against the stored plan at execution time
    before running it (the diagnosis may have been superseded)". The facts are rebuilt
    from the run's own `final` -- the same `Diagnosis` and `FailureBundle` the Remediator
    read -- rather than copied from the original decisions, so a later phase that lets
    facts change between suspension and approval (memory, a re-diagnosis) changes nothing
    here. A plan the re-evaluation now denies is not executed, whatever the person said;
    the decisions in the response show why.
    """
    request = entry.request
    outcome = await registry.get(request.run_id)
    if outcome is None:
        raise LookupError(request.run_id)
    diagnosis = Diagnosis.model_validate(outcome.final["diagnosis"])
    bundle = FailureBundle.model_validate(outcome.final["bundle"])
    facts = build_facts(
        diagnosis,
        bundle,
        # Phase 4: the run's own evaluation verdict, the same value the Remediator was
        # given in-run. Spelled here rather than defaulted so that change has to happen
        # in both places at once.
        evaluation_verdict=EVALUATION_SKIPPED,
        # Nothing executed before the suspension, by construction (`plan_verdict`).
        side_effecting_actions_so_far=0,
    )
    decisions = decide_plan(context.engine, request.plan, facts)
    verdict = plan_verdict(request.plan, decisions)
    executed = []
    if verdict in ("execute", "await_approval"):
        gateway = _gateway_for(context, entry.context)
        try:
            executed = await execute_plan(
                gateway, request.plan, decisions, recorder=context.recorder.bind(request.run_id)
            )
        finally:
            await gateway.aclose()
        remediation_status: Literal["executed", "denied"] = "executed"
    else:
        remediation_status = "denied"
    remediation = RemediationResult(
        plan=request.plan,
        decisions=decisions,
        executed=executed,
        pending_approval=request,
        status=remediation_status,
    )
    return remediation, [d.model_dump(mode="json") for d in decisions]


@app.post("/v1/approvals/{approval_id}")
async def decide_approval(
    request: Request, approval_id: str, body: ApprovalDecision
) -> Response:
    """Decide a pending approval. Single-use: `409` once decided, `410` once expired.

    The transition is taken under the registry's lock *before* anything executes, so two
    concurrent decisions on one approval cannot both run the plan -- the loser sees the
    winner's state and answers `409`.
    """
    context = get_app_context()
    entry = await approvals.get(approval_id)
    if entry is None:
        return problem(
            request, status_code=404, title="Approval not found",
            detail="No approval with that id is known to this process.",
        )

    if entry.request.state == "pending" and datetime.now(UTC) >= entry.request.expires_at:
        entry, _ = await approvals.transition(approval_id, "expired")
    if entry.request.state == "expired":
        return problem(
            request, status_code=410, title="Approval expired",
            detail="This approval expired before a decision was recorded.",
            run_id=entry.request.run_id,
            extensions={"state": "expired"},
        )

    # Checked *before* the single-use transition: an approval whose run this process no
    # longer knows (both registries are in-process) cannot be executed or settled, and
    # burning the approval on the way to a 500 would leave it 409 forever with nothing
    # done (review note).
    run_outcome = await registry.get(entry.request.run_id)
    if run_outcome is None or not {"diagnosis", "bundle"} <= set(run_outcome.final):
        return problem(
            request, status_code=404, title="Run not found",
            detail="The run this approval belongs to is not known to this process.",
            run_id=entry.request.run_id,
        )

    target: Literal["approved", "rejected"] = (
        "approved" if body.decision == "approve" else "rejected"
    )
    entry, applied = await approvals.transition(
        approval_id, target, actor=body.actor, note=body.note
    )
    if not applied:
        return problem(
            request, status_code=409, title="Approval already decided",
            detail=f"This approval is already {entry.request.state}.",
            run_id=entry.request.run_id,
            extensions={"state": entry.request.state},
        )

    if target == "rejected":
        remediation = RemediationResult(
            plan=entry.request.plan,
            decisions=entry.request.decisions,
            executed=[],
            pending_approval=entry.request,
            status="rejected",
        )
        await _settle_run(entry.request.run_id, remediation)
        decisions = [d.model_dump(mode="json") for d in entry.request.decisions]
        executed: list[dict[str, Any]] = []
    else:
        remediation, decisions = await _execute_approved(context, entry)
        await _settle_run(entry.request.run_id, remediation)
        executed = [r.model_dump(mode="json") for r in remediation.executed]

    payload: dict[str, JsonValue] = {
        "approval_id": approval_id,
        "run_id": entry.request.run_id,
        "state": entry.request.state,
        "executed": list(executed),
        "decisions": list(decisions),
    }
    return JSONResponse(
        status_code=status.HTTP_200_OK, content=context.recorder.redactor.scrub(payload)
    )


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------


@app.get("/v1/escalations")
async def list_escalations(limit: int = Query(50, ge=1, le=200)) -> Response:
    """Every escalation this process has recorded, newest first.

    Read off the run registry rather than a separate store: the `EscalationRecord` is a
    field of the `RunOutcome`, and PLAN.md's durable `escalation` table arrives with the
    memory phase alongside the `run` table it references. Each item is the record plus
    `run_id` -- A.12's bare `[EscalationRecord]` would leave a reader unable to find the
    run an escalation belongs to.
    """
    context = get_app_context()
    runs = await registry.list(limit=200)
    items: list[JsonValue] = [  # type: ignore[assignment]  # dict[str, Any] is JsonValue here
        {"run_id": run.run_id, **run.escalation.model_dump(mode="json")}
        for run in runs
        if run.escalation is not None
    ][:limit]
    return JSONResponse(
        status_code=status.HTTP_200_OK, content=context.recorder.redactor.scrub(items)
    )
