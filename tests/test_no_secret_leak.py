# ruff: noqa: E501
"""PLAN.md Phase 5 Verify step 2 / Appendix E: no secret ever lands anywhere.

Sentinel values are injected for every `Settings` secret, every recorded scenario is run
through the API on the stub model -- `real_regression`'s log carries a pasted `ghp_…`
token that never passed through `Settings` -- plus one escalated run delivered to a
failing escalation webhook, one webhook delivery refused for its signature and one
accepted, and one live-mode run whose mocked GitHub answers a 403 that echoes the token
back. Then every sentinel must appear ZERO times in: every `trace_span` row, every
`escalation` row, captured stdout/stderr and log output, the raw bytes of the SQLite file
(WAL included), and every JSON and HTML body the API served.

This is a CI gate: it must be green before the repository is public.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import REDACTION_PLACEHOLDER, Redactor, TraceRecorder
from src.settings import get_settings
from tests.integration.test_idempotency import API, mock_github
from tests.stubs import ScenarioStubLlm

REPO_ROOT = Path(__file__).resolve().parent.parent

GITHUB_TOKEN = "ghp_SENTINELSENTINELSENTINELSENTINEL01"
GEMINI_KEY = "AIzaSENTINELSENTINELSENTINELSENTINEL02"
WEBHOOK_SECRET = "whsec_SENTINELSENTINELSENTINELSENTINEL03"
ESCALATION_URL = "https://hooks.example.com/services/T0/B0/SENTINELSENTINEL04"
HOOK_HOST = "hooks.example.com"


def _planted_sentinels() -> tuple[str, ...]:
    """The token pasted into a fixture log, from the one place it is declared."""
    spec = importlib.util.spec_from_file_location(
        "scrub_fixtures", REPO_ROOT / "scripts" / "scrub_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrub_fixtures"] = module
    spec.loader.exec_module(module)
    sentinels: tuple[str, ...] = module.PLANTED_SENTINELS
    return sentinels


PLANTED = _planted_sentinels()
SENTINELS = (GITHUB_TOKEN, GEMINI_KEY, WEBHOOK_SECRET, ESCALATION_URL, *PLANTED)


def _client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[TestClient, AppContext]:
    monkeypatch.setenv("HARNESS_GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("HARNESS_GEMINI_API_KEY", GEMINI_KEY)
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("HARNESS_ESCALATION_WEBHOOK_URL", ESCALATION_URL)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
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
    return TestClient(api_main.app), context


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.harness.escalation as escalation

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(escalation.asyncio, "sleep", no_sleep)


def _count_all(haystack: str) -> dict[str, int]:
    return {s: haystack.count(s) for s in SENTINELS}


def _assert_clean(where: str, haystack: str) -> None:
    hits = {s: n for s, n in _count_all(haystack).items() if n}
    assert not hits, f"sentinel(s) found in {where}: {hits}"


def test_no_secret_ever_lands_anywhere(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _no_backoff(monkeypatch)
    caplog.set_level("DEBUG")
    fixture_log = (FIXTURES_ROOT / "real_regression" / "logs" / "job_601234567.txt").read_text(
        encoding="utf-8"
    )
    assert all(token in fixture_log for token in PLANTED), "the planted token must be in the fixture"

    served: list[str] = []
    run_ids: list[str] = []

    def serve(client: TestClient, path: str) -> Any:
        response = client.get(path)
        served.append(response.text)
        return response.json() if "json" in response.headers.get("content-type", "") else None

    # --- 1. every recorded scenario, replay mode, plus one escalated run ---------------
    client, context = _client(tmp_db_path, monkeypatch)
    with respx.mock(assert_all_mocked=False) as router:
        hook = router.post(url__regex=rf"https://{HOOK_HOST}/.*").mock(
            return_value=httpx.Response(500, text="hooks: internal error")
        )
        with client:
            scenarios = sorted(p.name for p in FIXTURES_ROOT.iterdir() if (p / "webhook.json").is_file())
            assert len(scenarios) >= 4
            for scenario in scenarios:
                response = client.post(f"/v1/replay/{scenario}")
                assert response.status_code == 200, response.text
                served.append(response.text)
                run_ids.append(response.json()["run_id"])

            # An escalated run: the model is unsure, the run escalates `low_confidence`,
            # the notifier posts to the (failing) webhook and records `delivery_error`.
            context.llm.self_confidence = 0.2  # type: ignore[attr-defined]
            response = client.post("/v1/replay/real_regression")
            assert response.status_code == 200
            body = response.json()
            served.append(response.text)
            run_ids.append(body["run_id"])
            assert body["status"] == "escalated" and body["escalation"]["reason"] == "low_confidence"
            assert "webhook" in body["escalation"]["channels"]
            assert body["escalation"]["delivery_error"] is not None
            assert hook.call_count >= 1
            # What left for the webhook was scrubbed too.
            for call in hook.calls:
                _assert_clean("escalation webhook body", call.request.content.decode("utf-8", "replace"))
                _assert_clean("escalation webhook headers", str(dict(call.request.headers)))
            context.llm.self_confidence = 0.92  # type: ignore[attr-defined]

            # --- 2. the webhook route: one forged signature, one accepted delivery -----
            payload = (FIXTURES_ROOT / "flaky_test" / "webhook.json").read_bytes()
            forged = client.post(
                "/webhooks/github", content=payload,
                headers={EVENT_HEADER: "workflow_run", SIGNATURE_HEADER: sign("not-the-secret-at-all", payload)},
            )
            assert forged.status_code == 401
            served.append(forged.text)
            accepted = client.post(
                "/webhooks/github", content=payload,
                headers={
                    "Content-Type": "application/json", EVENT_HEADER: "workflow_run",
                    DELIVERY_HEADER: str(uuid.uuid4()), SIGNATURE_HEADER: sign(WEBHOOK_SECRET, payload),
                },
            )
            assert accepted.status_code == 202, accepted.text
            served.append(accepted.text)
            run_ids.append(accepted.json()["run_id"])
            for _ in range(600):
                outcome = serve(client, f"/v1/runs/{accepted.json()['run_id']}")
                if outcome and outcome.get("status") != "in_progress":
                    break
                import time

                time.sleep(0.01)

            # --- 3. every read route, JSON and HTML ---------------------------------
            for run_id in run_ids:
                serve(client, f"/v1/runs/{run_id}")
                serve(client, f"/v1/runs/{run_id}/trace")
                serve(client, f"/runs/{run_id}/view")
            serve(client, "/v1/runs")
            serve(client, "/v1/escalations")
            serve(client, "/readyz")

    # --- 4. live mode: GitHub's error body echoes the token -------------------------
    client, context = _client(
        tmp_db_path, monkeypatch,
        HARNESS_GATEWAY="github", HARNESS_ALLOWED_REPOS=json.dumps(["octo-org/harness-demo-repo"]),
    )
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
        router.post(url__regex=rf"https://{HOOK_HOST}/.*").mock(return_value=httpx.Response(500))
        mock_github(router)
        # `cold_start`'s delivery (its own run id since Phase 5): the flaky key above is
        # already claimed in this database and would be deduplicated, not run.
        router.get(f"{API}/actions/runs/501234891/attempts/1/jobs").mock(
            return_value=httpx.Response(
                403,
                json={"message": f"Resource not accessible by integration; token {GITHUB_TOKEN} lacks actions:read"},
                headers={"x-accepted-oauth-scopes": "repo"},
            )
        )
        with client:
            payload = (FIXTURES_ROOT / "cold_start" / "webhook.json").read_bytes()
            accepted = client.post(
                "/webhooks/github", content=payload,
                headers={
                    "Content-Type": "application/json", EVENT_HEADER: "workflow_run",
                    DELIVERY_HEADER: str(uuid.uuid4()), SIGNATURE_HEADER: sign(WEBHOOK_SECRET, payload),
                },
            )
            assert accepted.status_code == 202, accepted.text
            live_run = accepted.json()["run_id"]
            run_ids.append(live_run)
            for _ in range(600):
                outcome = serve(client, f"/v1/runs/{live_run}")
                if outcome and outcome.get("status") != "in_progress":
                    break
                import time

                time.sleep(0.01)
            assert outcome is not None and outcome["status"] != "in_progress"
            # The 403 reached the bundle as a gateway error -- and was scrubbed there.
            errors = json.dumps(outcome.get("final", {}).get("bundle", {}).get("gateway_errors", []))
            assert "forbidden (403)" in errors
            assert REDACTION_PLACEHOLDER in errors
            serve(client, f"/v1/runs/{live_run}/trace")
            serve(client, f"/runs/{live_run}/view")
            serve(client, "/v1/escalations")

    # --- the assertions ----------------------------------------------------------------
    assert len(run_ids) >= 7
    with sqlite3.connect(tmp_db_path) as db:
        spans = db.execute(
            "select run_id, name, attributes_json, coalesce(error_json, '') from trace_span"
        ).fetchall()
        escalations = db.execute(
            "select run_id, reason, payload_json, channel, coalesce(delivery_error, '') from escalation"
        ).fetchall()
        runs = db.execute("select count(*) from run").fetchone()[0]
    assert len(spans) > 100 and len(escalations) >= 1 and runs >= 7
    for row in spans:
        _assert_clean(f"trace_span {row[0]} {row[1]}", " ".join(str(v) for v in row))
    for row in escalations:
        _assert_clean(f"escalation {row[0]} {row[1]}", " ".join(str(v) for v in row))
        assert row[4], "the failing webhook recorded a delivery_error"
        assert HOOK_HOST not in row[4] and "SENTINEL" not in row[4]

    for index, body in enumerate(served):
        _assert_clean(f"served body #{index}", body)

    captured = capfd.readouterr()
    _assert_clean("stdout", captured.out)
    _assert_clean("stderr", captured.err)
    _assert_clean("log records", caplog.text)

    raw = tmp_db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        sidecar = tmp_db_path.with_name(tmp_db_path.name + suffix)
        if sidecar.exists():
            raw += sidecar.read_bytes()
    for sentinel in SENTINELS:
        assert sentinel.encode() not in raw, f"{sentinel[:12]}… is in the raw database bytes"
    # Not vacuous: the pasted token's line was stored -- with the token replaced.
    assert REDACTION_PLACEHOLDER.encode() in raw
    assert b"Authorization: token " + REDACTION_PLACEHOLDER.encode() in raw
