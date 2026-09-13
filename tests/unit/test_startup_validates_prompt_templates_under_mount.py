"""Sibling to `test_startup_validates_prompt_templates.py`, covering the shape that file's
own end-to-end test cannot reach: the Hugging Face Space.

`test_app_startup_fails_loudly_instead_of_serving_a_green_healthz` in that file enters
`src.api.main.app`'s ASGI **lifespan** directly (what `TestClient(api_main.app)` does as a
context manager, and what `uvicorn src.api.main:app` does at process start under Docker).
That is real for the Docker deployment. It is fiction for the Space: `app.py:239` is
`demo.app.mount("/", api)`, and a Starlette `Mount` never forwards lifespan events to the
sub-app it wraps — nothing ever opens `api`'s `async with lifespan(app): yield` block, so
`validate_prompt_templates()` (and `recorder.initialize()` beside it) never ran there
regardless of how carefully the lifespan itself was tested.

This file reproduces review-2.md finding 3's exact symptom on the *mounted* shape --
`200` on `/healthz`, then the first `/v1/replay` call failing on the missing template --
as the thing that happens *without* app.py's hand-call (`validate_prompt_templates()`
called explicitly in `main()`, ordered before the recorder work -- see that function's
docstring), and confirms the hand-call prevents it. `app.py` itself is not imported here:
it pulls in `gradio`, which is deliberately not a project dependency (`requirements.txt`'s
own comment: "gradio ... is absent on purpose -- the Space installs it itself"), so this
file matches app.py:228-239's *shape* -- a plain outer ASGI app with `outer.mount("/",
api)`, `api = src.api.main.app` -- without needing gradio to be installed to prove the
Mount-and-lifespan claim. `test_app_py_main_hand_calls_validate_prompt_templates_before_launch`
below does import `app.py`, and is skipped where gradio is absent (see its own docstring).

Wave-3 finding 5 (`review.md`) added a catch-all `Exception` handler to `src/api/main.py`
that converts *every* unhandled route exception -- including this one -- into `500
application/problem+json` with a generic `detail` and a `run_id` when one is in scope,
instead of letting it fall through to FastAPI/Starlette's bare `500 text/plain "Internal
Server Error"`. That changed the *shape* of the symptom this file pins
(`test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500`, below) but not the
*defect*: the mounted sub-app's lifespan still never runs, `validate_prompt_templates()`
still never executes, and the missing template still reaches the route and still fails
the request -- the catch-all only makes the failure legible, it does not call
`validate_prompt_templates()` on the sub-app's behalf. The test now asserts the politer
failure shape (`problem+json`, a `run_id`, no leaked exception detail) while keeping its
original job: proving the request still fails, so it still distinguishes "the mount
defect is present, validation never ran" from "the hand-call fixed it, validation ran and
the route served 200" (the latter is `test_hand_call_then_mount_serves_correctly_with_a_
real_prompts_dir`, below, unchanged).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import TokenUsage
from src.harness.llm import LlmRequest, RawLlmResponse
from src.harness.observability import Redactor, TraceRecorder
from src.integrations.cicd import rendering
from src.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_template_cache() -> None:
    rendering._template_cache.clear()
    yield
    rendering._template_cache.clear()


class _NeverCalledLlm:
    """A model client that fails the test if the pipeline ever reaches it.

    The failure this file pins happens while rendering the Investigator's prompt --
    before any model call -- so a real StubLlm is unnecessary noise; if this stub's
    `generate` ever runs, the test's premise (the failure is in template loading, not
    downstream of it) is wrong and should fail loudly rather than silently return
    something plausible.
    """

    async def generate(self, req: LlmRequest) -> RawLlmResponse:  # pragma: no cover
        raise AssertionError(
            "the model should never be called: the failure this test pins happens "
            "earlier, while rendering the Investigator's prompt template"
        )


def _build_context(tmp_db_path: Path) -> AppContext:
    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path,
        redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS),
    )
    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget)
        ),
        llm=_NeverCalledLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )


@pytest.fixture
def broken_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same seam `test_startup_validates_prompt_templates.py` uses: an empty /
    nonexistent prompts directory, so `load_prompt_template` fails the same way a missing
    or malformed `prompts/*.md` would in production.
    """
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(rendering, "_PROMPTS_DIR", missing)
    return missing


@pytest.fixture
def mounted_client_before(
    broken_prompts_dir: Path, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Mount order A: `outer.mount("/", api)` happens *before* the outer app starts.

    `raise_server_exceptions=False` is required, not a preference: with the default
    `True`, `TestClient` re-raises whatever escaped the ASGI app into the *test process*,
    which would make this test fail with a `FileNotFoundError` instead of letting it
    observe the actual HTTP response a real client gets -- a bare 500.
    """
    monkeypatch.setattr(api_main, "get_app_context", lambda: _build_context(tmp_db_path))
    outer = FastAPI()
    outer.mount("/", api_main.app)
    with TestClient(outer, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def mounted_client_after(
    broken_prompts_dir: Path, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Mount order B: the outer app is already "running" (its own -- empty -- lifespan has
    already entered) *before* `api` is mounted onto it, matching app.py's actual sequence:
    `demo.launch()` runs (and gradio's own app object comes into being) before
    `demo.app.mount("/", api)` on the next line. Starlette resolves routes per request off
    the live route list, so a route mounted after the app object exists is still reachable
    -- which is exactly why the missing-lifespan bug is invisible from the outside until
    the first request that needs what the lifespan would have set up.
    """
    monkeypatch.setattr(api_main, "get_app_context", lambda: _build_context(tmp_db_path))
    outer = FastAPI()
    with TestClient(outer, raise_server_exceptions=False) as client:
        outer.mount("/", api_main.app)
        yield client


@pytest.mark.parametrize(
    "client_fixture", ["mounted_client_before", "mounted_client_after"]
)
def test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500(
    client_fixture: str, request: pytest.FixtureRequest
) -> None:
    """The reviewer's exact reproduction, both mount orders: `/healthz` stays green --
    it does no template work -- and the first `/v1/replay` still fails, because the
    mounted sub-app's lifespan never ran and `validate_prompt_templates()` never
    executed. That the *request still fails* is the thing this test exists to prove --
    it is the tell that nothing upstream of the route ran `validate_prompt_templates()`;
    the Docker lifespan path raises `OSError` before the app finishes starting (see
    `test_startup_validates_prompt_templates.py`) and would never let a client reach a
    route at all, let alone one that fails this way.

    What changed under Wave-3 finding 5 is only the failure's *shape*: `src/api/main.py`'s
    new catch-all `Exception` handler now converts the `FileNotFoundError` this route
    raises into `500 application/problem+json` with a `run_id`, rather than letting it
    fall through to a bare `text/plain "Internal Server Error"`. Asserted below, in the
    order finding 5 promises: a real problem document, a `run_id` (this route sets
    `request.state.run_id` before `_execute` can fail, so the catch-all has one to
    attach), and -- the same "nothing an upstream detail leaks" guarantee the 422 and
    scrubbing tests pin elsewhere -- neither the exception's own message
    (`FileNotFoundError`'s str, which would name a filesystem path) nor the broken
    directory's name reaches the client. The catch-all makes the symptom politer; it is
    still a genuine request failure, still distinguishable from the 200 the positive
    control below gets once the hand-call actually runs.
    """
    client: TestClient = request.getfixturevalue(client_fixture)

    healthz = client.get("/healthz")
    assert healthz.status_code == 200
    assert healthz.json()["status"] == "ok"

    replay = client.post("/v1/replay/real_regression")

    assert replay.status_code == 500
    assert replay.headers["content-type"] == "application/problem+json"
    body = replay.json()  # must be valid JSON now -- the opposite of the old bare body
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert body["status"] == 500
    assert body["instance"] == "/v1/replay/real_regression"
    # The fixed, generic detail -- never the real FileNotFoundError's message, which
    # would otherwise name a real filesystem path back to an unauthenticated caller.
    assert body["detail"] == "An unexpected error occurred while processing the request."
    assert "does-not-exist" not in replay.text
    assert "investigator" not in replay.text.lower()
    assert "FileNotFoundError" not in replay.text
    # A run_id *is* now present -- `replay()` sets `request.state.run_id` right after
    # the claim, before `_execute` can fail, so the catch-all has one to attach. This is
    # the inverse of the old assertion (`"run_id" not in replay.text`): the whole point
    # of the fix this round is that a run that failed this deep is still look-up-able.
    #
    # Phase 3 put a step in front of the prompts: the route claims a `run` row first,
    # and the table it needs is created by the migrations the same never-fired lifespan
    # would have applied. The claim is memory, memory is a soft dependency, so the route
    # runs the request unclaimed under a locally minted id (`_claim_or_degrade`) and
    # then fails on the prompts exactly as before -- the third startup step the Phase 2
    # handoff predicted a mounted sub-app would miss, degrading rather than changing the
    # failure's shape. The SQLite message must not leak either: it names a table.
    assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", body["run_id"])
    assert "no such table" not in replay.text
    assert "MemoryStoreError" not in replay.text


def test_hand_call_before_mount_raises_at_boot_instead_of_reaching_the_route(
    broken_prompts_dir: Path,
) -> None:
    """The fix, isolated from the ASGI machinery entirely: calling
    `validate_prompt_templates()` explicitly -- app.py's hand-call, ordered before
    `demo.launch()` / the mount -- raises synchronously, before any app object is built
    or any route could ever be reached. Contrast with the two tests above: there, the
    same broken directory produces a 500 three network hops deep, on whichever request
    happens to be first to need a template. Here it aborts `main()` at the very first
    line -- "fail loudly at boot", not "fail late on whichever request loses the race".
    """
    mounted = False

    with pytest.raises(OSError):
        rendering.validate_prompt_templates()
        mounted = True  # pragma: no cover - unreachable if the fix holds

    assert mounted is False, (
        "execution continued past validate_prompt_templates() -- the boot-time guard "
        "did not actually stop anything"
    )


def test_validate_prompt_templates_succeeds_against_the_real_shipped_prompts_dir() -> None:
    """Positive control for this file, same role as the sibling test of the same name in
    `test_startup_validates_prompt_templates.py`: without it, every test above could pass
    vacuously if `validate_prompt_templates()` (or the fixture that breaks its directory)
    were broken in a way that made it always raise, or never raise, regardless of input.
    """
    rendering.validate_prompt_templates()  # must not raise


def test_hand_call_then_mount_serves_correctly_with_a_real_prompts_dir(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closes the loop end to end: with the real, valid `prompts/` directory, doing what
    app.py's `main()` now does -- call `validate_prompt_templates()`, *then* build/mount
    the app -- serves a real request successfully through the mounted shape. This is the
    fix's positive case: the same topology as the two failing tests above, minus the
    broken directory, produces a 200 rather than a 500. A stub model is used (per this
    integration's replay contract: the fixture format carries no recorded model response),
    but nothing about template loading, the gateway, or the route is faked.
    """

    class _StubLlm:
        async def generate(self, req: LlmRequest) -> RawLlmResponse:
            if "You are the Investigator" in req.prompt:
                payload: object = {
                    "observations": ["a stubbed observation"],
                    "additional_tool_calls": [],
                    "narrative": "stubbed narrative",
                }
            else:
                payload = {
                    "reasoning": "stubbed reasoning citing the stubbed observation",
                    "category": "real_regression",
                    "summary": "stubbed summary",
                    "self_confidence": 0.9,
                    "citations": [],
                    "suspected_commit_sha": None,
                    "suspected_test_ids": [],
                    "suspected_package": None,
                    "suggested_action": "open_fix_pr",
                }
            return RawLlmResponse(
                text=json.dumps(payload),
                tokens=TokenUsage(prompt=10, completion=10, total=20),
                finish_reason="STOP",
                model=req.model,
                latency_ms=1,
            )

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
        llm=_StubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)

    # app.py's actual order: the hand-call first, raising here (it must not, against the
    # real directory) before anything else runs; then the startup routine that applies
    # the migrations (Phase 3) -- also a hand-call on the Space, for the same reason.
    rendering.validate_prompt_templates()
    asyncio.run(context.initialize())

    outer = FastAPI()
    with TestClient(outer, raise_server_exceptions=False) as client:
        outer.mount("/", api_main.app)
        response = client.post("/v1/replay/real_regression")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status"] in ("completed", "escalated")


def test_app_py_main_hand_calls_validate_prompt_templates_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the actual line that closes the finding: `app.py`'s `main()` calls
    `validate_prompt_templates()`, and does so before `demo.launch()` -- not after, which
    would defer the same failure past process start again, just one call later.

    `demo.launch()` and `demo.block_thread()` are replaced with recording stubs: the real
    `launch()` binds a port and starts a Gradio/uvicorn server, which is not what this test
    is about and would hang the suite. `main()`'s reference to `demo` is a module-global
    lookup resolved at call time (not a closure over an import-time value), so replacing
    `app_module.demo` here is visible to `main()` without touching `app.py`.

    Skipped where `gradio` is not installed: `app.py` imports it at module scope, and
    gradio is deliberately not a project dependency (see this file's module docstring).
    Verified separately with `uv run --with gradio --with spaces pytest -k
    test_app_py_main_hand_calls`, reported alongside this run.
    """
    pytest.importorskip("gradio")
    from unittest.mock import MagicMock

    import app as app_module

    calls: list[str] = []
    monkeypatch.setattr(
        app_module, "validate_prompt_templates", lambda: calls.append("validate")
    )

    class _FakeContext:
        async def initialize(self) -> None:
            calls.append("context.initialize")

    monkeypatch.setattr(app_module, "get_app_context", lambda: _FakeContext())

    fake_demo = MagicMock(name="demo")
    fake_demo.launch.side_effect = lambda **_kwargs: calls.append("demo.launch")
    fake_demo.block_thread.side_effect = lambda: calls.append("demo.block_thread")
    monkeypatch.setattr(app_module, "demo", fake_demo)

    app_module.main()

    assert calls[0] == "validate", (
        "validate_prompt_templates() must run before anything else in main()"
    )
    assert calls.index("validate") < calls.index("demo.launch"), (
        "the hand-call must happen before demo.launch(), or a broken prompts directory "
        "would surface after the Space has already started accepting traffic"
    )
    fake_demo.launch.assert_called_once()
    fake_demo.app.mount.assert_called_once_with("/", app_module.api)
    fake_demo.block_thread.assert_called_once()
