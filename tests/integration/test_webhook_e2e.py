# ruff: noqa: E501
"""`POST /webhooks/github` over HTTP -- PLAN.md Phase 5 Verify steps 3 and 4, in-process.

The model is stubbed, everything else is real: the signature check, the event filter,
the replay-mode scenario match, the claim, the background run, the redelivery. The
`sqlite3` lines of step 4 are asserted against the test's own database file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.api.main import idempotency_key_for
from src.api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm

SECRET = "a-test-webhook-secret-of-adequate-length"


def make_context(tmp_db_path: Path, llm: Any | None = None) -> AppContext:
    settings = get_settings()
    recorder = TraceRecorder(
        db_path=tmp_db_path, redactor=Redactor(build_secret_registry(settings), SECRET_PATTERNS)
    )
    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=ContextManager(
            default_budget=ContextBudget(total_chars=settings.log_char_budget), recorder=recorder
        ),
        llm=llm or ScenarioStubLlm(),
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as test_client:
        yield test_client


def webhook_bytes(scenario: str = "flaky_test") -> bytes:
    return (FIXTURES_ROOT / scenario / "webhook.json").read_bytes()


def headers(body: bytes, *, secret: str = SECRET, event: str = "workflow_run") -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        DELIVERY_HEADER: str(uuid.uuid4()),
        SIGNATURE_HEADER: sign(secret, body),
    }


def deliver(client: TestClient, body: bytes, **overrides: str) -> Any:
    return client.post("/webhooks/github", content=body, headers={**headers(body), **overrides})


def wait_for(client: TestClient, run_id: str) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    for _ in range(600):
        outcome = client.get(f"/v1/runs/{run_id}").json()
        if outcome.get("status") != "in_progress":
            return outcome
        time.sleep(0.01)
    return outcome


# ---------------------------------------------------------------------------
# Verify step 3: signature enforcement
# ---------------------------------------------------------------------------


def test_unsigned_delivery_is_401(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github", content=b"{}", headers={EVENT_HEADER: "workflow_run"}
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"].startswith("HMAC-SHA256")
    assert "X-Hub-Signature-256" in response.json()["detail"]


def test_a_forged_signature_is_401_and_the_body_is_not_parsed(client: TestClient) -> None:
    body = webhook_bytes()
    response = deliver(client, body, **{SIGNATURE_HEADER: sign("wrong-secret-entirely", body)})
    assert response.status_code == 401
    # Nothing ran: no run row exists for the delivery's key.
    assert client.get("/v1/runs").json()["items"] == []


def test_a_blank_secret_refuses_every_delivery(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = webhook_bytes()
        assert deliver(client, body, **{SIGNATURE_HEADER: sign("", body)}).status_code == 401


def test_signed_flaky_delivery_is_202_then_completed(client: TestClient) -> None:
    body = webhook_bytes()
    response = deliver(client, body)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert set(accepted) == {"run_id", "status"} and accepted["status"] == "in_progress"

    outcome = wait_for(client, accepted["run_id"])
    assert outcome["status"] == "completed"
    assert outcome["final"]["diagnosis"]["category"] == "flaky_test"
    assert outcome["final"]["remediation"]["status"] == "executed"
    assert outcome["final"]["remediation"]["executed"][0]["tool"] == "rerun_failed_jobs"

    # The delivery id rides on the run span as `requested_by`, GUID and all.
    trace = client.get(f"/v1/runs/{accepted['run_id']}/trace").json()
    run_span = next(s for s in trace["spans"] if s["name"] == "run")
    assert run_span["attributes"]["mode"] == "replay"
    assert run_span["attributes"]["requested_by"].startswith("webhook:github:")
    assert len(run_span["attributes"]["requested_by"]) == len("webhook:github:") + 36


# ---------------------------------------------------------------------------
# Verify step 4: idempotency -- redelivery does NOT run twice
# ---------------------------------------------------------------------------


def test_redelivery_is_deduplicated_and_runs_nothing(
    client: TestClient, tmp_db_path: Path
) -> None:
    body = webhook_bytes()
    first = deliver(client, body)
    assert first.status_code == 202
    run_a = first.json()["run_id"]
    assert wait_for(client, run_a)["status"] == "completed"

    second = deliver(client, body)  # same payload, a fresh delivery GUID
    assert second.status_code == 200
    dedup = second.json()
    assert {k: dedup[k] for k in ("status", "run_id", "original_run_id")} == {
        "status": "deduplicated", "run_id": run_a, "original_run_id": run_a,
    }

    key = idempotency_key_for(json.loads(body))
    with sqlite3.connect(tmp_db_path) as db:
        (runs,) = db.execute("select count(*) from run where idempotency_key = ?", (key,)).fetchone()
        (observations,) = db.execute(
            "select count(*) from observation where run_id = ?", (run_a,)
        ).fetchone()
        (all_runs,) = db.execute("select count(*) from run").fetchone()
    assert runs == 1
    assert observations == 1
    assert all_runs == 1, "the redelivery created no run row at all"
    # And only one run's worth of model calls: one trace, one set of llm spans.
    trace = client.get(f"/v1/runs/{run_a}/trace").json()
    assert len([s for s in trace["spans"] if s["name"] == "llm.attempt"]) == 3


def test_redelivery_while_in_progress_is_202_in_progress(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appendix C: an `in_progress` row with a fresh heartbeat answers 202, not a second run."""

    class SlowStub(ScenarioStubLlm):
        async def generate(self, req: Any) -> Any:
            await asyncio.sleep(0.3)
            return await super().generate(req)

    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    context = make_context(tmp_db_path, llm=SlowStub())
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        body = webhook_bytes()
        first = deliver(client, body)
        second = deliver(client, body)
        assert first.status_code == 202 and second.status_code == 202
        assert second.json() == {"run_id": first.json()["run_id"], "status": "in_progress"}
        assert wait_for(client, first.json()["run_id"])["status"] == "completed"


# ---------------------------------------------------------------------------
# The event filter and the two acceptance modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "mutate"),
    [
        ("ping", lambda d: {"zen": "Keep it logically awesome.", "hook_id": 1}),
        ("check_run", lambda d: d),
        ("workflow_run", lambda d: {**d, "action": "requested"}),
        ("workflow_run", lambda d: {**d, "workflow_run": {**d["workflow_run"], "conclusion": "success"}}),
    ],
)
def test_other_events_are_204_and_run_nothing(client: TestClient, event: str, mutate: Any) -> None:
    delivery = mutate(json.loads(webhook_bytes()))
    body = json.dumps(delivery).encode()
    response = client.post("/webhooks/github", content=body, headers=headers(body, event=event))
    assert response.status_code == 204
    assert client.get("/v1/runs").json()["items"] == []


def test_a_signed_non_json_body_is_400(client: TestClient) -> None:
    body = b"not json"
    response = client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")


def test_replay_mode_refuses_a_delivery_that_matches_no_scenario(client: TestClient) -> None:
    """The recorded scenarios are a replay deployment's allowlist (dispatch decision 6)."""
    delivery = json.loads(webhook_bytes())
    delivery["workflow_run"]["id"] = 999_999_999
    body = json.dumps(delivery).encode()
    response = client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == 403
    assert "replay only" in response.json()["detail"]
    assert client.get("/v1/runs").json()["items"] == []


def test_live_mode_refuses_a_repository_not_allowlisted(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HARNESS_GATEWAY", "github")
    monkeypatch.setenv("HARNESS_ALLOWED_REPOS", '["someone-else/repo"]')
    get_settings.cache_clear()
    context = make_context(tmp_db_path)
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    with TestClient(api_main.app) as client:
        response = deliver(client, webhook_bytes())
        assert response.status_code == 403
        assert "HARNESS_ALLOWED_REPOS" in response.json()["detail"]


def test_the_signature_never_reaches_the_problem_document_or_the_trace(client: TestClient) -> None:
    body = webhook_bytes()
    signature = sign(SECRET, body)
    accepted = deliver(client, body, **{SIGNATURE_HEADER: signature})
    assert accepted.status_code == 202
    outcome = wait_for(client, accepted.json()["run_id"])
    served = json.dumps(outcome) + json.dumps(
        client.get(f"/v1/runs/{accepted.json()['run_id']}/trace").json()
    )
    assert signature[7:] not in served
    assert SECRET not in served
