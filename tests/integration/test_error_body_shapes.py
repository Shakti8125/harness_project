"""Wave-3 audit findings 5 and 6 (`review.md`), verified rather than trusted off
api-surface's handover (`docs/progress/phase-1/api-surface.md`).

Finding 5: no `RequestValidationError` handler, so a malformed request got FastAPI's
default `422 application/json` with the entire submitted body echoed back inside each
error's `"input"` key. Finding 6: `problem()`'s `detail` never passed through the
`Redactor` -- only the convention that every call site authors a constant kept it safe.

`src/api/main.py` now registers three exception handlers (`RequestValidationError`,
`StarletteHTTPException`, the catch-all `Exception`) and routes `problem()`'s whole body
through `get_app_context().recorder.redactor.scrub(...)`. This file drives each of those
four surfaces directly over real HTTP (`TestClient`), the same way `test_replay_e2e.py`
and `test_replay_response_scrubbing.py` already verify the rest of the response shape --
no source file here is trusted on the strength of its docstring alone.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import REDACTION_PLACEHOLDER, Redactor, TraceRecorder
from src.settings import get_settings
from tests.integration.test_replay_e2e import StubLlm

SECRET_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # same shape as
# test_replay_response_scrubbing.py's SECRET_TOKEN -- 36 chars after ghp_


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """No stub LLM wired -- none of the tests below that use this fixture ever reach the
    model (validation errors, unmatched routes, wrong methods all fail before any run
    starts), so the real `GeminiClient` from `get_app_context()`'s default construction
    would work too, except it also requires `HARNESS_GEMINI_API_KEY`, which is not this
    file's concern to set up. Point `get_app_context` at a `AppContext` built the same
    way the other integration tests do, with an LLM that raises loudly if ever called.
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


# ---------------------------------------------------------------------------
# Finding 5a -- RequestValidationError -> RFC 9457, no body echo
# ---------------------------------------------------------------------------


def test_422_is_problem_json_and_does_not_echo_the_submitted_body(
    client: TestClient,
) -> None:
    """A distinctive, unmistakable planted value in the invalid request must not survive
    into the response -- FastAPI's default 422 handler echoes each error's `"input"` key
    (the caller's submitted value) verbatim, which is exactly what this handler must not
    do (`RunRequest.subject` is arbitrary caller-supplied JSON).
    """
    sentinel = "sentinel-value-be9a2c17-should-never-be-echoed"
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd",
            "not_a_field": sentinel,
            "subject": {"marker": sentinel},
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"

    raw_text = response.text
    assert sentinel not in raw_text, (
        "the planted value leaked into the 422 body -- the request's own submitted "
        "content is being echoed back, exactly what finding 5 forbids"
    )

    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert body["instance"] == "/v1/runs"
    # Built from loc/msg only: naming the offending fields and why, never the value.
    # `not_a_field` (the *field name*) is expected to appear -- `loc` is legitimate --
    # but the sentinel *value* the client put inside it must not.
    assert body["detail"] == (
        "body.idempotency_key: Field required; "
        "body.not_a_field: Extra inputs are not permitted"
    )


def test_422_error_detail_names_missing_fields_by_location_only(
    client: TestClient,
) -> None:
    """A second, narrower check on the same surface: the exact `loc`/`msg` shape the
    handler builds `detail` from, for a request missing required fields entirely (no
    caller-supplied value in scope at all here, isolating the "location and reason,
    never the value" claim from the no-echo claim above).
    """
    response = client.post("/v1/runs", json={"integration": "cicd"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    detail = response.json()["detail"]
    assert "subject" in detail
    assert "idempotency_key" in detail
    # loc/msg joined with "; ", each as "dotted.path: message" -- not a JSON array of
    # error objects (FastAPI's default shape), and not the bracketed repr of a list.
    assert not detail.startswith("[")
    assert "Field required" in detail


# ---------------------------------------------------------------------------
# Finding 5b -- StarletteHTTPException -> RFC 9457 (framework 404 / 405)
# ---------------------------------------------------------------------------


def test_unmatched_route_returns_problem_json_404_not_the_framework_default(
    client: TestClient,
) -> None:
    """api-surface's judgement call (route the framework's own 404/405 through
    `problem()` too), verified rather than accepted: before this round FastAPI's default
    for an unmatched path is `404 application/json {"detail": "Not Found"}`. This checks
    the actual live shape, not the docstring's claim about it.
    """
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["instance"] == "/v1/does-not-exist"
    # The old default shape must be gone, not merely un-asserted.
    assert list(response.json().keys()) != ["detail"]


def test_wrong_method_on_a_known_route_returns_problem_json_405(
    client: TestClient,
) -> None:
    """The other framework-generated case the same handler now covers: a matched path,
    wrong method. `/healthz` only supports GET.
    """
    response = client.post("/healthz")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 405
    assert body["instance"] == "/healthz"


def test_no_existing_route_depends_on_the_frameworks_default_404_shape(
    client: TestClient,
) -> None:
    """api-surface's stated reasoning for routing `StarletteHTTPException` through
    `problem()`: "no route currently raises `HTTPException`... so today this only
    reaches framework-generated 404/405". Verified directly rather than trusted: every
    404 this application's own routes deliberately produce (`GET /v1/runs/{id}`,
    `GET /v1/runs/{id}/trace`, `POST /v1/replay/{unknown scenario}`) already goes through
    `problem()` and was RFC 9457-shaped *before* this round -- so there is no route or
    prior test in this suite whose 404 contract this change could have silently broken.
    """
    for response in (
        client.get("/v1/runs/run_00000000000000000000000000"),
        client.get("/v1/runs/run_00000000000000000000000000/trace"),
        client.post("/v1/replay/no_such_scenario_at_all"),
    ):
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        # Every one of these bodies already had "title" (via problem()) before this
        # round landed -- unlike the framework-generated case, which had "detail" only.
        assert "title" in response.json()


# ---------------------------------------------------------------------------
# Finding 5c -- the catch-all Exception handler
# ---------------------------------------------------------------------------


EXCEPTION_MARKER = "unmistakable-exception-message-3fae91-must-not-leak"


@pytest.fixture
def client_that_fails_after_run_id_is_minted(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A real, successful replay run all the way through the orchestrator, with the one
    remaining step -- `registry.save(outcome)`, called from `_execute` after
    `request.state.run_id` was already set by the route -- replaced with something that
    raises a distinctive `RuntimeError`. This reaches the catch-all with a `run_id`
    genuinely in scope, via a real HTTP round trip, without needing to break the prompts
    directory (`test_startup_validates_prompt_templates_under_mount.py` already owns
    that reproduction) or add a throwaway route to `src/api/main.py`, which is out of
    this agent's territory.
    """
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
        llm=StubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)

    async def _raise_after_the_run_completed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(EXCEPTION_MARKER)

    monkeypatch.setattr(api_main.registry, "save", _raise_after_the_run_completed)

    # raise_server_exceptions=False: the point of this fixture is to see what a real
    # client over the wire gets back (a 500 problem body), not to have the exception
    # re-raised into the test process the way TestClient does by default.
    with TestClient(api_main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_unhandled_route_exception_returns_problem_body_with_run_id_and_no_leaked_message(
    client_that_fails_after_run_id_is_minted: TestClient,
) -> None:
    response = client_that_fails_after_run_id_is_minted.post(
        "/v1/replay/real_regression"
    )

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert body["status"] == 500
    assert body["instance"] == "/v1/replay/real_regression"
    # The fixed, generic detail -- never the real exception's own message, structurally,
    # not by convention.
    assert body["detail"] == "An unexpected error occurred while processing the request."
    assert EXCEPTION_MARKER not in response.text
    # request.state.run_id, set by the route immediately after minting, reaches the
    # catch-all and is attached to the problem body.
    assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", body["run_id"])


# ---------------------------------------------------------------------------
# Finding 6 -- problem()'s detail passes through the Redactor
# ---------------------------------------------------------------------------


@pytest.fixture
def app_context_for_direct_problem_call(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AppContext:
    """`problem()` reads `get_app_context()` directly rather than taking the context as
    a parameter, so calling it as a plain function (no HTTP round trip needed -- it does
    no I/O and is not a coroutine) still requires `get_app_context` to resolve to a real
    `Redactor` built the same way the app builds one.
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
        llm=None,  # type: ignore[arg-type]  # problem() never touches .llm
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return context


def test_problem_detail_scrubs_a_credential_shaped_string(
    app_context_for_direct_problem_call: AppContext,
) -> None:
    """The same `ghp_`-shaped approach `test_replay_response_scrubbing.py` uses,
    applied to `problem()`'s `detail` directly -- the exact surface finding 6 names.
    Calling `problem()` as a plain function pins the guarantee structurally (it must
    hold for *any* `detail`, not just the constants every call site happens to author
    today), which is finding 6's point: the prior guarantee was a convention, not a
    barrier.
    """
    request = Request(scope={"type": "http", "path": "/v1/test-route", "headers": []})

    response = api_main.problem(
        request,
        status_code=500,
        title="Upstream failure",
        detail=f"upstream said: {SECRET_TOKEN}",
    )

    raw_body = bytes(response.body).decode()
    assert SECRET_TOKEN not in raw_body
    assert REDACTION_PLACEHOLDER in raw_body
    payload = json.loads(raw_body)
    assert payload["detail"] == f"upstream said: {REDACTION_PLACEHOLDER}"


def test_problem_detail_without_a_secret_is_unaffected_by_the_scrub_pass(
    app_context_for_direct_problem_call: AppContext,
) -> None:
    """Non-vacuity control: an ordinary, credential-free `detail` -- the shape every
    real call site in `src/api/main.py` actually uses -- must round-trip unchanged, so
    the test above is pinning "secrets get scrubbed", not "scrub garbles everything".
    """
    request = Request(scope={"type": "http", "path": "/v1/runs/abc", "headers": []})

    response = api_main.problem(
        request,
        status_code=404,
        title="Run not found",
        detail="No run with that id is known to this process.",
        run_id="run_00000000000000000000000000",
    )

    payload = json.loads(bytes(response.body).decode())
    assert payload["detail"] == "No run with that id is known to this process."
    assert payload["run_id"] == "run_00000000000000000000000000"
