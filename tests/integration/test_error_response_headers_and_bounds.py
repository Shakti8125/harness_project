"""Phase 1 fix round: re-audit findings 1, 3 and 5 against `src/api/main.py`
(`docs/progress/phase-1/api-surface.md`, "Fix round: re-audit findings 1, 3, 5").

Finding 1 (medium, the live regression): `handle_http_exception` discarded
`exc.headers`, so a 405 on a matched-but-wrong-method route lost its `Allow` header --
RFC 9110 §15.5.6 makes that header a MUST. `problem()` now takes `headers` and
`handle_http_exception` forwards `exc.headers` through it.

Finding 3 (low): the 422 handler's `detail` was unbounded -- a request body with many
(or one very large) unrecognized JSON keys could blow the response body up, since
`RunRequest` is `extra="forbid"` and each rejected key becomes part of `loc`. `detail`
is now capped at `_MAX_VALIDATION_ERRORS` rendered entries plus a "... and N more
error(s)" tail, and hard-capped at `_MAX_DETAIL_LENGTH` (2000) bytes after joining.

Finding 5 (low): `problem()` echoed a caller-controlled `run_id` path segment into the
response body without checking its shape first. It now only echoes a value that
matches `_RUN_ID_PATTERN` (the same shape `mint_run_id()` produces).

This file drives each of the three over real HTTP (`TestClient`) or by calling the
handler functions directly with a hand-built `Request`/`StarletteHTTPException`, the
same style `test_error_body_shapes.py` already uses for the surfaces those findings
share -- nothing here is trusted on the strength of a docstring's claim alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """No stub LLM wired -- every test in this file fails before any run starts
    (wrong method, malformed body, unknown run id), the same reasoning
    `test_error_body_shapes.py`'s `client` fixture already documents.
    """

    class _NeverCalledLlm:
        async def generate(self, req: object) -> object:  # pragma: no cover
            raise AssertionError("no test in this file should ever reach the model")

    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    context = AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=_NeverCalledLlm(),  # type: ignore[arg-type]
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as test_client:
        yield test_client


@pytest.fixture
def app_context(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AppContext:
    """For calling the exception-handler coroutines directly, without a full HTTP
    round trip -- needed to construct headers FastAPI's own routes never raise today
    (401 / 429 with headers), per the finding's own recipe.
    """
    settings = get_settings()
    context = AppContext(
        settings=settings,
        recorder=TraceRecorder(
            db_path=tmp_db_path,
            redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
        ),
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=None,  # type: ignore[arg-type]  # never reached by any handler below
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return context


def _bare_request(path: str = "/v1/test-route") -> Request:
    return Request(scope={"type": "http", "path": path, "headers": []})


# ---------------------------------------------------------------------------
# Finding 1 -- exc.headers reaches the client
# ---------------------------------------------------------------------------


def test_405_on_a_matched_route_carries_a_nonempty_allow_header(client: TestClient) -> None:
    """`/v1/runs` only registers POST and GET; DELETE is a matched path with the wrong
    method. RFC 9110 §15.5.6 makes `Allow` a MUST on a 405. Before the fix this header
    was silently dropped once routing went through `problem()`.
    """
    response = client.delete("/v1/runs")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    allow = response.headers.get("allow")
    assert allow is not None and allow != "", "Allow header missing or empty on a 405"
    # Verified independently against Starlette's own partial-match routing for this
    # exact route registration order (api-surface's handoff): the first path-matching
    # route for `/v1/runs` is the POST registration, so Starlette reports `Allow: POST`
    # alone, not a union of every method registered for the path.
    assert allow == "POST"


async def test_http_exception_with_www_authenticate_header_is_forwarded(
    app_context: AppContext,
) -> None:
    """No route raises a 401 today, so this constructs the case directly against the
    handler, per the finding's own recipe: a `WWW-Authenticate` header on a
    route-raised `HTTPException` must survive into the response unchanged.
    """
    request = _bare_request()
    exc = StarletteHTTPException(
        status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )

    response = await api_main.handle_http_exception(request, exc)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_http_exception_with_retry_after_header_is_forwarded(
    app_context: AppContext,
) -> None:
    """Same recipe, the other header the finding names: `Retry-After` on a 429."""
    request = _bare_request()
    exc = StarletteHTTPException(
        status_code=429, detail="Too Many Requests", headers={"Retry-After": "30"}
    )

    response = await api_main.handle_http_exception(request, exc)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


async def test_http_exception_without_headers_is_unaffected(app_context: AppContext) -> None:
    """Control: an `HTTPException` with no `headers` (the ordinary case -- every 404
    this application raises itself) must not regress just because `problem()` now
    accepts a `headers` argument.
    """
    request = _bare_request()
    exc = StarletteHTTPException(status_code=404, detail="Not Found")

    response = await api_main.handle_http_exception(request, exc)

    assert response.status_code == 404
    body = json.loads(bytes(response.body).decode())
    assert body["detail"] == "Not Found"


# ---------------------------------------------------------------------------
# Finding 3 -- 422 `detail` is bounded
# ---------------------------------------------------------------------------


def test_422_detail_is_capped_and_notes_the_remainder_under_many_extra_keys(
    client: TestClient,
) -> None:
    """`RunRequest` is `extra="forbid"`, so each unrecognized JSON key becomes its own
    "Extra inputs are not permitted" error. 300 extra keys, well past
    `_MAX_VALIDATION_ERRORS` (20), must still produce a response whose `detail` is
    bounded and that says how many entries were left out rather than silently
    dropping them.
    """
    extra_keys = {f"not_a_real_field_{i}": "x" for i in range(300)}
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd",
            "subject": {"marker": "irrelevant"},
            "idempotency_key": "a" * 16,  # valid, so it contributes no error of its own
            **extra_keys,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) <= api_main._MAX_DETAIL_LENGTH
    # 300 extra keys, 20 rendered -> 280 left out, and that count must be named.
    assert "... and 280 more error(s)" in detail
    assert detail.count("Extra inputs are not permitted") == api_main._MAX_VALIDATION_ERRORS


def test_422_detail_hard_caps_a_single_pathologically_long_key(client: TestClient) -> None:
    """The other half of the bound: not just many small errors, but one huge one. A
    single caller-chosen giant JSON key must not itself exceed `_MAX_DETAIL_LENGTH`.
    """
    huge_key = "k" * 5000
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd",
            "subject": {"marker": "irrelevant"},
            "idempotency_key": "a" * 16,
            huge_key: "x",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == api_main._MAX_DETAIL_LENGTH
    assert detail.endswith("…")
    # Only one error contributed, so there is nothing left to count into a tail.
    assert "more error(s)" not in detail


def test_422_detail_for_an_ordinary_small_request_is_unaffected_by_the_new_bounds(
    client: TestClient,
) -> None:
    """Non-vacuity control: the common case (a couple of missing fields) still reads
    exactly as `test_error_body_shapes.py` already pins -- the new caps must not
    truncate or annotate a `detail` that was never close to the limit.
    """
    response = client.post("/v1/runs", json={"integration": "cicd"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "more error(s)" not in detail
    assert not detail.endswith("…")
    assert len(detail) < api_main._MAX_DETAIL_LENGTH


# ---------------------------------------------------------------------------
# Finding 5 -- run_id is only echoed when it is shaped like a real RunId
# ---------------------------------------------------------------------------


def test_get_run_with_a_non_ulid_id_omits_run_id_from_the_body(client: TestClient) -> None:
    response = client.get("/v1/runs/not-a-real-run-id")

    assert response.status_code == 404
    body = response.json()
    assert "run_id" not in body


def test_get_run_with_a_ulid_shaped_unknown_id_still_echoes_it(client: TestClient) -> None:
    """The positive control: a value that *is* shaped like a real `RunId` (26
    Crockford-base32 characters after `run_`) is still echoed, even though the run
    itself is unknown -- finding 5 is about shape validation, not suppressing every
    `run_id` in a 404.
    """
    run_id = "run_00000000000000000000000000"
    response = client.get(f"/v1/runs/{run_id}")

    assert response.status_code == 404
    assert response.json()["run_id"] == run_id


def test_problem_omits_an_unshaped_run_id_when_called_directly(
    app_context: AppContext,
) -> None:
    """Unit-level pin on `problem()` itself, independent of any route: a `run_id` that
    does not match `_RUN_ID_PATTERN` must never reach the body, regardless of caller.
    """
    request = _bare_request("/v1/some-path")

    response = api_main.problem(
        request,
        status_code=404,
        title="Not found",
        detail="nothing here",
        run_id="../etc/passwd",
    )

    body = json.loads(bytes(response.body).decode())
    assert "run_id" not in body
