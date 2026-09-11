"""PLAN.md Phase 2 Verify steps 2 and 4, over HTTP, plus `GET /v1/escalations`,
`readyz.policy_loaded`, and live mode's two refusals.

Same shape as `test_replay_e2e.py`: the model is stubbed, everything else is real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.approval_registry import ApprovalRegistry
from src.api.deps import SECRET_PATTERNS, AppContext, build_secret_registry
from src.api.run_registry import RunRegistry
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import get_settings
from tests.stubs import ScenarioStubLlm


def make_client(
    stub: ScenarioStubLlm, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
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
        llm=stub,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
    )
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    # Fresh registries per test: both are process-global by design.
    monkeypatch.setattr(api_main, "registry", RunRegistry())
    monkeypatch.setattr(api_main, "approvals", ApprovalRegistry())
    return TestClient(api_main.app)


@pytest.fixture
def client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with make_client(ScenarioStubLlm(), tmp_db_path, monkeypatch) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Verify step 2: the regression is blocked pending approval, nothing executed
# ---------------------------------------------------------------------------


def test_regression_is_blocked_pending_approval(client: TestClient) -> None:
    body = client.post("/v1/replay/real_regression").json()
    remediation = body["final"]["remediation"]

    assert body["status"] == "awaiting_approval"
    assert remediation["status"] == "awaiting_approval"
    assert remediation["decisions"][0]["effect"] == "require_approval"
    assert remediation["decisions"][0]["rule_id"] == "open-fix-pr"
    assert {d["effect"] for d in remediation["decisions"]} == {"require_approval"}
    assert len(remediation["executed"]) == 0
    approval = remediation["pending_approval"]
    assert approval["approval_id"].startswith("apr_")
    assert approval["state"] == "pending"
    assert approval["run_id"] == body["run_id"]
    assert [c["tool"] for c in approval["plan"]["tool_calls"]] == [
        "create_branch", "create_or_update_file", "open_pull_request",
    ]
    assert body["escalation"] is None
    assert body["stages"][-1]["stage"] == "remediate"
    assert "requires approval" in body["stages"][-1]["summary"]

    # The run is readable afterwards in the same state, with the same approval id.
    again = client.get(f"/v1/runs/{body['run_id']}").json()
    assert again["status"] == "awaiting_approval"
    pending_again = again["final"]["remediation"]["pending_approval"]
    assert pending_again["approval_id"] == approval["approval_id"]

    # The drafted file rides in `create_or_update_file.args.content_b64` -- base64, which
    # the Redactor's patterns cannot see through -- so at the HTTP boundary it is served as
    # length + digest, in both places the plan appears, like `patch` and `excerpt`.
    for plan in (remediation["plan"], approval["plan"]):
        file_call = next(c for c in plan["tool_calls"] if c["tool"] == "create_or_update_file")
        assert "content_b64" not in file_call["args"]
        assert file_call["args"]["content_b64_length"] > 0
        assert len(file_call["args"]["content_b64_sha256"]) == 64
    # The plaintext twin is model-authored prose, scrubbed by the whole-body pass and served.
    assert "discount" in remediation["plan"]["pr_draft"]["files"][0]["new_content"]


# ---------------------------------------------------------------------------
# Verify step 4: the approval round-trip
# ---------------------------------------------------------------------------


def test_reject_then_repost_is_409(client: TestClient) -> None:
    body = client.post("/v1/replay/real_regression").json()
    apr = body["final"]["remediation"]["pending_approval"]["approval_id"]

    first = client.post(f"/v1/approvals/{apr}", json={"decision": "reject", "actor": "shakti"})
    assert first.status_code == 200
    assert first.json()["state"] == "rejected"
    assert first.json()["executed"] == []
    assert first.json()["approval_id"] == apr

    second = client.post(f"/v1/approvals/{apr}", json={"decision": "reject", "actor": "shakti"})
    assert second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")
    assert second.json()["state"] == "rejected"
    assert second.json()["run_id"] == body["run_id"]

    # Approving after a rejection is also 409: single-use means single-use.
    third = client.post(f"/v1/approvals/{apr}", json={"decision": "approve", "actor": "shakti"})
    assert third.status_code == 409

    # The run reflects the decision.
    run = client.get(f"/v1/runs/{body['run_id']}").json()
    assert run["status"] == "completed"
    assert run["escalation"] is None, "a person's rejection is not an escalation"
    assert run["final"]["remediation"]["status"] == "rejected"
    assert run["final"]["remediation"]["pending_approval"]["state"] == "rejected"


def test_approve_re_evaluates_and_executes_through_the_gateway(client: TestClient) -> None:
    """Approve: policy is re-evaluated, then every call runs. In this phase the PR tools
    are registered but unimplemented, so the first call fails honestly and the plan stops
    there -- which is exactly the state a person approving should see."""
    body = client.post("/v1/replay/real_regression").json()
    apr = body["final"]["remediation"]["pending_approval"]["approval_id"]

    stored = body["final"]["remediation"]["decisions"]
    response = client.post(
        f"/v1/approvals/{apr}", json={"decision": "approve", "actor": "shakti", "note": "lgtm"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "approved"
    assert [d["effect"] for d in payload["decisions"]] == ["require_approval"] * 3
    # Re-evaluated, not echoed: every decision in the response is newer than the one the
    # run stored when it suspended (review finding 6).
    for fresh, old in zip(payload["decisions"], stored, strict=True):
        assert fresh["evaluated_at"] > old["evaluated_at"]
        assert fresh["rule_id"] == old["rule_id"]
    assert len(payload["executed"]) == 1
    executed = payload["executed"][0]
    assert executed["tool"] == "create_branch"
    assert executed["ok"] is False
    assert executed["error"]["kind"] == "unknown"
    assert "not implemented" in executed["error"]["message"]

    # Appendix B.2: a write that failed is a run failure. The approval was recorded, the
    # plan was executed as far as it could be, and the run says so rather than "completed".
    run = client.get(f"/v1/runs/{body['run_id']}").json()
    assert run["status"] == "escalated"
    assert run["escalation"]["reason"] == "tool_failure"
    assert run["escalation"]["payload"]["approval_id"] == apr
    assert "create_branch" in run["escalation"]["message"]
    assert run["final"]["remediation"]["status"] == "executed"
    assert run["final"]["remediation"]["pending_approval"]["state"] == "approved"
    assert len(run["final"]["remediation"]["executed"]) == 1

    listed = client.get("/v1/escalations").json()
    assert any(
        item["run_id"] == body["run_id"] and item["reason"] == "tool_failure" for item in listed
    )

    # The trace records the execution attempt against the run it belongs to.
    trace = client.get(f"/v1/runs/{body['run_id']}/trace").json()
    execute_spans = [s for s in trace["spans"] if s["name"] == "remediation.execute"]
    assert len(execute_spans) == 1
    assert execute_spans[0]["attributes"]["tool"] == "create_branch"
    assert execute_spans[0]["attributes"]["ok"] is False


def test_unknown_approval_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/approvals/apr_doesnotexist", json={"decision": "approve", "actor": "x"}
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_malformed_decision_is_422(client: TestClient) -> None:
    body = client.post("/v1/replay/real_regression").json()
    apr = body["final"]["remediation"]["pending_approval"]["approval_id"]
    response = client.post(f"/v1/approvals/{apr}", json={"decision": "maybe", "actor": "x"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    # Still pending: a malformed body decides nothing.
    ok = client.post(f"/v1/approvals/{apr}", json={"decision": "reject", "actor": "x"})
    assert ok.status_code == 200


def test_expired_approval_is_410(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`HARNESS_APPROVAL_TTL_H=0` makes every approval expire at the instant it is raised."""
    monkeypatch.setenv("HARNESS_APPROVAL_TTL_H", "0")
    get_settings.cache_clear()
    with make_client(ScenarioStubLlm(), tmp_db_path, monkeypatch) as client:
        body = client.post("/v1/replay/real_regression").json()
        apr = body["final"]["remediation"]["pending_approval"]["approval_id"]
        response = client.post(f"/v1/approvals/{apr}", json={"decision": "approve", "actor": "x"})
        assert response.status_code == 410
        assert response.json()["state"] == "expired"
        # And it stays expired: a second attempt is 410 again, never 409, never executed.
        again = client.post(f"/v1/approvals/{apr}", json={"decision": "approve", "actor": "x"})
        assert again.status_code == 410


# ---------------------------------------------------------------------------
# Escalations, readiness, live mode
# ---------------------------------------------------------------------------


def test_escalations_lists_the_denied_retry_with_its_run_id(client: TestClient) -> None:
    flaky = client.post("/v1/replay/flaky_test").json()
    assert flaky["status"] == "escalated"
    assert flaky["escalation"]["reason"] == "policy_denied"
    assert flaky["final"]["remediation"]["status"] == "denied"

    response = client.get("/v1/escalations")
    assert response.status_code == 200
    items = response.json()
    assert isinstance(items, list)
    mine = [item for item in items if item["run_id"] == flaky["run_id"]]
    assert len(mine) == 1
    assert mine[0]["reason"] == "policy_denied"
    assert mine[0]["payload"]["decisions"][0]["tool"] == "rerun_failed_jobs"
    assert "memory.retries_for_signature_24h: 999" in mine[0]["message"]

    limited = client.get("/v1/escalations?limit=1").json()
    assert len(limited) <= 1


def test_readyz_reports_the_loaded_policy(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["policy_loaded"] is True


def test_live_mode_is_501_when_the_gateway_is_replay(client: TestClient) -> None:
    import json

    webhook = json.loads(
        (Path(__file__).resolve().parents[2] / "fixtures/scenarios/real_regression/webhook.json")
        .read_text(encoding="utf-8")
    )
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd", "subject": webhook,
            "idempotency_key": "cicd:test-live-501", "mode": "live",
        },
    )
    assert response.status_code == 501
    assert "HARNESS_GATEWAY" in response.json()["detail"]


def test_live_mode_is_403_for_a_repo_not_allowlisted(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setenv("HARNESS_GATEWAY", "github")
    monkeypatch.setenv("HARNESS_ALLOWED_REPOS", '["someone-else/repo"]')
    get_settings.cache_clear()
    webhook = json.loads(
        (Path(__file__).resolve().parents[2] / "fixtures/scenarios/real_regression/webhook.json")
        .read_text(encoding="utf-8")
    )
    with make_client(ScenarioStubLlm(), tmp_db_path, monkeypatch) as client:
        response = client.post(
            "/v1/runs",
            json={
                "integration": "cicd", "subject": webhook,
                "idempotency_key": "cicd:test-live-403", "mode": "live",
            },
        )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")


def test_replay_mode_without_a_fixture_is_422(client: TestClient) -> None:
    import json

    webhook = json.loads(
        (Path(__file__).resolve().parents[2] / "fixtures/scenarios/real_regression/webhook.json")
        .read_text(encoding="utf-8")
    )
    response = client.post(
        "/v1/runs",
        json={
            "integration": "cicd", "subject": webhook,
            "idempotency_key": "cicd:test-replay-422", "mode": "replay",
        },
    )
    assert response.status_code == 422
