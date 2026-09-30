# ruff: noqa: E501
"""The step-5 plan's Stage 1e over HTTP: SEC-01, SEC-08, SEC-02/04/06 and SEC-07.

Each test is the finding's reproduction, driven through the real app:

- SEC-01: a `422` scrubbed 120 KB of caller-chosen text with a quadratic pattern before
  bounding it, which took 14.5 s on the one event loop.
- SEC-08: no route bounded its body; the webhook buffered any body before checking its
  signature.
- SEC-02/04/06: on a live deployment `POST /v1/runs` and `POST /v1/approvals/{id}` were
  anonymous, so anyone could start live runs, decide approvals under any name, or
  pre-claim a real delivery's idempotency key so that the signed delivery deduplicated
  against the squatter's run.
- SEC-07: nothing limited how many runs were accepted, only how many executed.

Replay-mode behaviour is unchanged; the existing suites (ten files post `cicd:` keys to
`/v1/runs` without a credential) are the evidence for that.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.deps import FIXTURES_ROOT, SECRET_PATTERNS, AppContext, build_secret_registry
from src.api.main import idempotency_key_for
from src.api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.observability import Redactor, TraceRecorder
from src.settings import Settings, get_settings
from tests.integration.test_idempotency import mock_github
from tests.stubs import ScenarioStubLlm

REPO = "octo-org/harness-demo-repo"
SECRET = "a-test-webhook-secret-of-adequate-length"
TOKEN = "an-operator-token-of-adequate-length-0123456789"
MIB = 1024 * 1024
MARKER = "-----BEGIN RSA PRIVATE KEY-----"


def make_context(settings: Settings, tmp_db_path: Path, llm: Any | None = None) -> AppContext:
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


def use(context: AppContext, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.setattr(api_main, "get_app_context", lambda: context)
    return context


def live_settings(monkeypatch: pytest.MonkeyPatch, *, token: str | None = TOKEN, allowed: list[str] | None = None) -> Settings:
    monkeypatch.setenv("HARNESS_GATEWAY", "github")
    monkeypatch.setenv("HARNESS_ALLOWED_REPOS", json.dumps([REPO] if allowed is None else allowed))
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HARNESS_GITHUB_TOKEN", "ghp_" + "t" * 36)
    if token is not None:
        monkeypatch.setenv("HARNESS_OPERATOR_TOKEN", token)
    else:
        monkeypatch.delenv("HARNESS_OPERATOR_TOKEN", raising=False)
    get_settings.cache_clear()
    return get_settings()


def webhook(scenario: str = "flaky_test") -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES_ROOT / scenario / "webhook.json").read_text(encoding="utf-8"))
    return body


def run_body(mode: str = "replay", key: str = "cicd:test-security-gate", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"integration": "cicd", "subject": webhook(), "idempotency_key": key, "mode": mode}
    if mode == "replay":
        body["replay_fixture"] = "flaky_test"
    body.update(extra)
    return body


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def replay_client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    use(make_context(get_settings(), tmp_db_path), monkeypatch)
    with TestClient(api_main.app) as client:
        yield client


# ---------------------------------------------------------------------------
# SEC-01
# ---------------------------------------------------------------------------


def test_a_422_carrying_120_kb_of_pem_markers_answers_in_under_a_second(replay_client: TestClient) -> None:
    hostile = MARKER * (120_000 // len(MARKER))

    started = time.perf_counter()
    response = replay_client.post("/v1/runs", json=run_body(**{hostile: 1}))
    elapsed = time.perf_counter() - started

    assert response.status_code == 422
    assert elapsed < 1.0, f"{elapsed:.2f} s"


# ---------------------------------------------------------------------------
# SEC-08
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/webhooks/github", "/v1/runs", "/v1/approvals/apr_0000000000000000"])
def test_a_body_over_one_mebibyte_is_413(replay_client: TestClient, path: str) -> None:
    response = replay_client.post(
        path, content=b"{" + b" " * MIB + b"}", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413, response.text
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_body_without_a_length_is_counted_as_it_streams(replay_client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b" " * (MIB // 10)

    response = replay_client.post(
        "/webhooks/github", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413, response.text


def test_a_real_workflow_run_delivery_is_still_accepted(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_GITHUB_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    use(make_context(get_settings(), tmp_db_path), monkeypatch)
    body = (FIXTURES_ROOT / "flaky_test" / "webhook.json").read_bytes()
    with TestClient(api_main.app) as client:
        response = client.post(
            "/webhooks/github", content=body,
            headers={
                "Content-Type": "application/json", EVENT_HEADER: "workflow_run",
                DELIVERY_HEADER: str(uuid.uuid4()), SIGNATURE_HEADER: sign(SECRET, body),
            },
        )
    assert response.status_code == 202, response.text


# ---------------------------------------------------------------------------
# SEC-02, SEC-04, SEC-06
# ---------------------------------------------------------------------------


def test_a_live_deployment_without_an_operator_token_refuses_both_routes(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(make_context(live_settings(monkeypatch, token=None), tmp_db_path), monkeypatch)
    with TestClient(api_main.app) as client:
        runs = client.post("/v1/runs", json=run_body(), headers=bearer())
        approval = client.post(
            "/v1/approvals/apr_0000000000000000", json={"decision": "approve", "actor": "x"}, headers=bearer()
        )
    assert runs.status_code == 403, runs.text
    assert approval.status_code == 403, approval.text
    for response in (runs, approval):
        assert "HARNESS_" not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": f"Basic {TOKEN}"}])
def test_a_missing_or_wrong_credential_is_401(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    use(make_context(live_settings(monkeypatch), tmp_db_path), monkeypatch)
    with TestClient(api_main.app) as client:
        runs = client.post("/v1/runs", json=run_body(mode="live"), headers=headers)
        approval = client.post(
            "/v1/approvals/apr_0000000000000000", json={"decision": "approve", "actor": "x"}, headers=headers
        )
    for response in (runs, approval):
        assert response.status_code == 401, response.text
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert TOKEN not in response.text and "HARNESS_" not in response.text


def test_the_right_credential_starts_a_run_and_decides_as_the_operator(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = use(make_context(live_settings(monkeypatch), tmp_db_path), monkeypatch)
    with TestClient(api_main.app) as client:
        started = client.post("/v1/runs", json=run_body(), headers=bearer())
        assert started.status_code == 202, started.text

        held = client.post("/v1/replay/real_regression").json()
        assert held["status"] == "awaiting_approval"
        approval_id = held["final"]["remediation"]["pending_approval"]["approval_id"]

        anonymous = client.post(f"/v1/approvals/{approval_id}", json={"decision": "reject", "actor": "shakti"})
        assert anonymous.status_code == 401
        decided = client.post(
            f"/v1/approvals/{approval_id}", json={"decision": "reject", "actor": "shakti"}, headers=bearer()
        )
        assert decided.status_code == 200, decided.text
        record = asyncio.run(context.store.get_approval(approval_id))
    assert record is not None
    assert record.state == "rejected"
    assert record.decided_by == "operator"


def test_an_approval_for_a_repository_no_longer_served_live_is_refused_and_kept(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = use(make_context(live_settings(monkeypatch, allowed=["someone-else/repo"]), tmp_db_path), monkeypatch)
    with TestClient(api_main.app) as client:
        held = client.post("/v1/replay/real_regression").json()
        approval_id = held["final"]["remediation"]["pending_approval"]["approval_id"]
        stored = asyncio.run(context.store.get_approval(approval_id))
        assert stored is not None
        live_id = "apr_" + "1" * 16
        asyncio.run(context.store.save_approval(stored.model_copy(update={
            "approval_id": live_id, "context": {"mode": "live", "repo": REPO, "scenario_dir": None},
        })))

        response = client.post(
            f"/v1/approvals/{live_id}", json={"decision": "approve", "actor": "x"}, headers=bearer()
        )
        after = asyncio.run(context.store.get_approval(live_id))
    assert response.status_code == 403, response.text
    assert after is not None and after.state == "pending"


async def test_a_squatted_idempotency_key_no_longer_suppresses_the_signed_delivery(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-06's reproduction: the key the delivery will compute, claimed first."""
    context = use(make_context(live_settings(monkeypatch), tmp_db_path), monkeypatch)
    await context.initialize()
    body = (FIXTURES_ROOT / "flaky_test" / "webhook.json").read_bytes()
    key = idempotency_key_for(json.loads(body))

    async with respx.MockRouter(assert_all_mocked=True, assert_all_called=False) as router:
        mock_github(router)
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
            squat = await client.post("/v1/runs", json=run_body(mode="live", key=key))
            assert squat.status_code == 401

            delivery = await client.post(
                "/webhooks/github", content=body,
                headers={
                    "Content-Type": "application/json", EVENT_HEADER: "workflow_run",
                    DELIVERY_HEADER: str(uuid.uuid4()), SIGNATURE_HEADER: sign(SECRET, body),
                },
            )
            assert delivery.status_code == 202, delivery.text
            assert delivery.json()["status"] == "in_progress"
            run_id = delivery.json()["run_id"]
            for _ in range(600):
                outcome = (await client.get(f"/v1/runs/{run_id}")).json()
                if outcome["status"] != "in_progress":
                    break
                await asyncio.sleep(0.01)
    assert outcome["status"] == "completed", outcome.get("escalation")


# ---------------------------------------------------------------------------
# SEC-07
# ---------------------------------------------------------------------------


class GatedLlm:
    """The stub model, held at a gate so that accepted runs stay in flight."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.inner = ScenarioStubLlm()

    async def generate(self, req: Any) -> Any:
        await self.gate.wait()
        return await self.inner.generate(req)


async def test_runs_beyond_the_wait_list_are_refused_with_429(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run executing and two waiting fill `max_concurrent_runs=1`; the fourth is 429."""
    llm = GatedLlm()
    settings = get_settings().model_copy(update={"max_concurrent_runs": 1})
    context = use(make_context(settings, tmp_db_path, llm), monkeypatch)
    await context.initialize()
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        accepted = [
            await client.post("/v1/replay/flaky_test", params={"sync": "false"}) for _ in range(3)
        ]
        assert [r.status_code for r in accepted] == [202, 202, 202]
        refused = await client.post("/v1/replay/flaky_test", params={"sync": "false"})
        assert refused.status_code == 429, refused.text
        assert refused.headers["retry-after"]

        llm.gate.set()
        for response in accepted:
            run_id = response.json()["run_id"]
            for _ in range(1000):
                if (await client.get(f"/v1/runs/{run_id}")).json()["status"] != "in_progress":
                    break
                await asyncio.sleep(0.01)
        # Every place is given back when its run ends.
        for _ in range(100):
            if context.runs_admitted.in_flight == 0:
                break
            await asyncio.sleep(0.01)
        assert context.runs_admitted.in_flight == 0
        again = await client.post("/v1/replay/flaky_test", params={"sync": "false"})
        assert again.status_code == 202
        run_id = again.json()["run_id"]
        for _ in range(1000):
            if (await client.get(f"/v1/runs/{run_id}")).json()["status"] != "in_progress":
                break
            await asyncio.sleep(0.01)


async def test_a_deduplicated_request_gives_its_place_back(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = use(make_context(get_settings(), tmp_db_path), monkeypatch)
    await context.initialize()
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        first = await client.post("/v1/replay/flaky_test", params={"fresh": "false"})
        assert first.status_code == 200
        for _ in range(5):
            again = await client.post("/v1/replay/flaky_test", params={"fresh": "false"})
            assert again.json()["status"] == "deduplicated"
    assert context.runs_admitted.in_flight == 0
