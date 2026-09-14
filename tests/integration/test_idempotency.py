# ruff: noqa: E501
"""PLAN.md Appendix C, "Verified by": `test_concurrent_duplicate_deliveries` fires five
identical signed webhooks concurrently and asserts exactly one `run` row, one
`observation` row, and one `rerun_failed_jobs` call recorded by `respx`.

The live path, end to end: `HARNESS_GATEWAY=github`, the repository allowlisted,
`HARNESS_DRY_RUN=false` so the re-run POST is real (and mocked), the model stubbed. The
app is driven through an in-process ASGI transport rather than `TestClient` so the five
requests are genuinely concurrent on one event loop -- which is also where the
background runs execute -- and `respx` sees only the GitHub host (the ASGI transport is
not an HTTP transport, so it is never intercepted).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.api.main import idempotency_key_for
from src.api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm

REPO = "octo-org/harness-demo-repo"
API = f"https://api.github.com/repos/{REPO}"
SECRET = "a-test-webhook-secret-of-adequate-length"
SCENARIO = FIXTURES_ROOT / "flaky_test"


def _fixture(name: str) -> Any:
    return json.loads((SCENARIO / "api" / name).read_text(encoding="utf-8"))


def mock_github(router: respx.MockRouter) -> respx.Route:
    """The flaky_test fixtures, served as GitHub would serve them."""
    router.get(f"{API}/actions/runs/501234890/attempts/1/jobs").mock(
        return_value=httpx.Response(
            200, json=_fixture("GET_repos-octo-org-harness-demo-repo-actions-runs-501234890-attempts-1-jobs.json")
        )
    )
    router.get(f"{API}/actions/jobs/601234890/logs").mock(
        return_value=httpx.Response(
            200, content=(SCENARIO / "logs" / "job_601234890.txt").read_bytes(),
            headers={"content-type": "text/plain"},
        )
    )
    router.get(f"{API}/actions/workflows/9001/runs").mock(
        return_value=httpx.Response(
            200, json=_fixture("GET_repos-octo-org-harness-demo-repo-actions-workflows-9001-runs.json")
        )
    )
    router.get(url__regex=rf"{API}/compare/.*").mock(
        return_value=httpx.Response(
            200,
            json=_fixture(
                "GET_repos-octo-org-harness-demo-repo-compare-"
                "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678-4f1e2d3c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e.json"
            ),
        )
    )
    router.get(f"{API}/actions/runs/501234890").mock(
        return_value=httpx.Response(200, json={"id": 501234890, "run_attempt": 1})
    )
    return router.post(f"{API}/actions/runs/501234890/rerun-failed-jobs").mock(
        return_value=httpx.Response(201, json={})
    )


@pytest.fixture
def live_context(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HARNESS_GATEWAY", "github")
    monkeypatch.setenv("HARNESS_ALLOWED_REPOS", json.dumps([REPO]))
    monkeypatch.setenv("HARNESS_DRY_RUN", "false")
    monkeypatch.setenv("HARNESS_GITHUB_TOKEN", "ghp_" + "t" * 36)
    get_settings.cache_clear()
    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path, redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS)
    )
    context = AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget), recorder=recorder
        ),
        llm=ScenarioStubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return context


async def _wait_for(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    for _ in range(600):
        response = await client.get(f"/v1/runs/{run_id}")
        outcome: dict[str, Any] = response.json()
        if outcome.get("status") != "in_progress":
            return outcome
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} never finished")


async def test_concurrent_duplicate_deliveries(live_context: AppContext, tmp_db_path: Path) -> None:
    await live_context.initialize()
    body = (SCENARIO / "webhook.json").read_bytes()

    def headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            EVENT_HEADER: "workflow_run",
            DELIVERY_HEADER: str(uuid.uuid4()),  # five deliveries, five GUIDs, one key
            SIGNATURE_HEADER: sign(SECRET, body),
        }

    async with respx.MockRouter(assert_all_mocked=True, assert_all_called=False) as router:
        rerun = mock_github(router)
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
            responses = await asyncio.gather(
                *(client.post("/webhooks/github", content=body, headers=headers()) for _ in range(5))
            )
            statuses = sorted(r.status_code for r in responses)
            assert statuses == [202, 202, 202, 202, 202], [r.text for r in responses]
            run_ids = {r.json()["run_id"] for r in responses}
            assert len(run_ids) == 1, "five deliveries, one run"
            (run_id,) = run_ids
            assert all(r.json()["status"] == "in_progress" for r in responses)

            outcome = await _wait_for(client, run_id)
            assert outcome["status"] == "completed", outcome.get("escalation")
            executed = outcome["final"]["remediation"]["executed"]
            assert [e["tool"] for e in executed] == ["rerun_failed_jobs"]
            assert executed[0]["ok"] is True and executed[0]["dry_run"] is False

            # A sixth delivery, after the fact, is the Appendix C dedup -- still one run.
            late = await client.post("/webhooks/github", content=body, headers=headers())
            assert late.status_code == 200 and late.json()["status"] == "deduplicated"
            assert late.json()["original_run_id"] == run_id

    assert rerun.call_count == 1, "exactly one rerun_failed_jobs call reached GitHub"

    key = idempotency_key_for(json.loads(body))
    with sqlite3.connect(tmp_db_path) as db:
        (runs,) = db.execute("select count(*) from run where idempotency_key = ?", (key,)).fetchone()
        (all_runs,) = db.execute("select count(*) from run").fetchone()
        (observations,) = db.execute(
            "select count(*) from observation where run_id = ?", (run_id,)
        ).fetchone()
    assert runs == 1 and all_runs == 1
    assert observations == 1
