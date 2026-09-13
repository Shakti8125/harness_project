"""Appendix B.4: the outbound escalation webhook.

Timeout 5 s, 2 retries, then `delivery_error` and the run continues; the URL is a secret
that never reaches a log line or the stored error; the body leads with `text` so a Slack
Incoming Webhook renders it. Transport is `httpx.MockTransport`, so nothing here opens a
socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from src.api.deps import build_notifier, escalation_channels_for, webhook_url
from src.harness.contracts import EscalationRecord, RunOutcome, RunRequest, TokenUsage
from src.harness.escalation import (
    WEBHOOK_RETRIES,
    WEBHOOK_TIMEOUT_S,
    WebhookNotifier,
    _describe_failure,
    webhook_body,
)
from src.harness.memory import SqliteMemoryStore
from src.harness.observability import (
    REDACTION_PLACEHOLDER,
    Redactor,
    SecretRegistry,
    TraceRecorder,
)
from src.harness.orchestrator import Orchestrator, StageSpec
from src.settings import get_settings

URL = "https://hooks.example.com/services/T000/B000/SECRETPART"
RUN_ID = "run_01J8TESTWEBH88K0000000000A"


def record(**overrides: object) -> EscalationRecord:
    base = dict(
        escalation_id="esc_0000000000000001",
        reason="invalid_output",
        message="model output did not satisfy the contract",
        payload={"stage": "diagnose", "token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD"},
        channels=["log", "db", "webhook"],
        delivered_at=datetime.now(UTC),
    )
    base.update(overrides)
    return EscalationRecord(**base)  # type: ignore[arg-type]


class Capture:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses[min(len(self.requests) - 1, len(self.statuses) - 1)]
        if status == 0:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(status, text="ok")


def client_for(capture: Capture) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(capture.handler))


async def test_delivery_success_sets_delivered_at_and_posts_slack_shaped_json() -> None:
    capture = Capture([200])
    notifier = WebhookNotifier(URL, client=client_for(capture), backoff_s=0.0)
    before = datetime.now(UTC)

    delivered = await notifier.deliver(RUN_ID, record(delivered_at=None))

    assert delivered.delivery_error is None
    assert delivered.delivered_at is not None and delivered.delivered_at >= before
    assert len(capture.requests) == 1
    request = capture.requests[0]
    assert str(request.url) == URL
    body = json.loads(request.content)
    assert list(body)[0] == "text"
    assert body["text"].startswith(f"[harness] run {RUN_ID} escalated (invalid_output): ")
    assert body["run_id"] == RUN_ID
    assert body["escalation_id"] == "esc_0000000000000001"
    assert body["reason"] == "invalid_output"
    assert body["trace_url"] == f"/v1/runs/{RUN_ID}/trace"
    assert body["payload"]["stage"] == "diagnose"


async def test_body_is_scrubbed_through_the_redactor() -> None:
    capture = Capture([200])
    registry = SecretRegistry()
    registry.register("escalation_webhook_url", URL)
    redactor = Redactor(registry, (__import__("re").compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),))
    notifier = WebhookNotifier(URL, redactor=redactor, client=client_for(capture), backoff_s=0.0)

    await notifier.deliver(RUN_ID, record())

    body = json.loads(capture.requests[0].content)
    assert "ghp_" not in json.dumps(body)
    assert "SECRETPART" not in json.dumps(body)


async def test_failure_after_two_retries_records_delivery_error_without_the_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = Capture([500])
    notifier = WebhookNotifier(URL, client=client_for(capture), backoff_s=0.0)
    with caplog.at_level(logging.WARNING, logger="harness.escalation"):
        delivered = await notifier.deliver(RUN_ID, record())

    assert len(capture.requests) == WEBHOOK_RETRIES + 1 == 3
    assert delivered.delivered_at is None
    assert delivered.delivery_error == "webhook answered HTTP 500"
    assert "SECRETPART" not in delivered.delivery_error
    assert "SECRETPART" not in caplog.text
    assert "attempt 3/3" in caplog.text


async def test_httpx_request_log_line_never_carries_the_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit finding 2: httpx logs `HTTP Request: POST <url> "..."` at INFO on its own
    logger for every request, mock transport included -- and for the webhook the URL is
    the credential. Captured at INFO on the root logger, which is where
    `HARNESS_LOG_LEVEL=INFO` would put it."""
    capture = Capture([500, 200])
    notifier = WebhookNotifier(URL, client=client_for(capture), backoff_s=0.0)
    with caplog.at_level(logging.INFO):
        delivered = await notifier.deliver(RUN_ID, record())

    assert delivered.delivery_error is None
    httpx_lines = [r for r in caplog.records if r.name == "httpx"]
    assert httpx_lines, "httpx did log the request -- the scrub, not silence, is the fix"
    assert all("HTTP Request: POST" in r.getMessage() for r in httpx_lines)
    assert "SECRETPART" not in caplog.text
    assert "hooks.example.com" not in caplog.text
    assert all(REDACTION_PLACEHOLDER in r.getMessage() for r in httpx_lines)


async def test_transient_failure_then_success() -> None:
    capture = Capture([503, 0, 200])
    notifier = WebhookNotifier(URL, client=client_for(capture), backoff_s=0.0)
    delivered = await notifier.deliver(RUN_ID, record())
    assert len(capture.requests) == 3
    assert delivered.delivery_error is None and delivered.delivered_at is not None


async def test_timeout_is_described_without_the_url() -> None:
    capture = Capture([0])
    notifier = WebhookNotifier(URL, client=client_for(capture), backoff_s=0.0)
    delivered = await notifier.deliver(RUN_ID, record())
    assert delivered.delivery_error == f"webhook timed out after {WEBHOOK_TIMEOUT_S:g}s"


def test_describe_failure_never_echoes_httpx_messages() -> None:
    request = httpx.Request("POST", URL)
    response = httpx.Response(404, request=request)
    status = httpx.HTTPStatusError(
        "Client error '404' for url " + URL, request=request, response=response
    )
    assert _describe_failure(status) == "webhook answered HTTP 404"
    connect = httpx.ConnectError("boom " + URL, request=request)
    assert _describe_failure(connect) == "webhook transport error (ConnectError)"
    assert _describe_failure(RuntimeError(URL)) == "webhook delivery failed (RuntimeError)"
    assert "SECRETPART" not in _describe_failure(status)


def test_webhook_body_leads_with_text() -> None:
    body = webhook_body(RUN_ID, record(), trace_url="/t")
    assert list(body)[:2] == ["text", "run_id"]


# ---------------------------------------------------------------------------
# Through the orchestrator and into the store
# ---------------------------------------------------------------------------


class Nothing(BaseModel):
    pass


class FailingAgent:
    key = "failer"

    async def run(self, state: object) -> object:
        from src.harness.contracts import AgentError, AgentResult

        return AgentResult[Nothing](
            agent=self.key, status="invalid_output", output=None, confidence=None,
            evidence=[], attempts=3, latency_ms=1, tokens=TokenUsage(),
            prompt_sha256=None, model=None,
            error=AgentError(kind="invalid_output", message="junk", attempts=3),
        )


class StubNotifier:
    def __init__(self, *, error: str | None = None, raise_: bool = False) -> None:
        self.error = error
        self.raise_ = raise_
        self.seen: list[tuple[str, EscalationRecord]] = []

    async def deliver(self, run_id: str, record: EscalationRecord) -> EscalationRecord:
        self.seen.append((run_id, record))
        if self.raise_:
            raise RuntimeError("notifier exploded")
        if self.error is not None:
            return record.model_copy(update={"delivered_at": None, "delivery_error": self.error})
        return record.model_copy(update={"delivered_at": datetime.now(UTC), "delivery_error": None})


async def run_with(
    notifier: StubNotifier | None, tmp_db_path: Path
) -> tuple[RunOutcome, TraceRecorder]:
    recorder = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), ()))
    await recorder.initialize()
    orchestrator = Orchestrator(
        stages=[StageSpec(name="only", agent_key="failer", output_model=Nothing)],
        agents={"failer": FailingAgent()},
        recorder=recorder,
        escalation_channels=("log", "db", "webhook") if notifier else ("log", "db"),
        notifier=notifier,
    )
    outcome = await orchestrator.run(
        RunRequest(
            integration="cicd", subject={}, idempotency_key="cicd:webhook-test", mode="replay"
        )
    )
    return outcome, recorder


async def test_orchestrator_awaits_the_notifier_and_the_record_carries_its_verdict(
    tmp_db_path: Path,
) -> None:
    notifier = StubNotifier()
    outcome, _ = await run_with(notifier, tmp_db_path)
    assert outcome.status == "escalated" and outcome.escalation is not None
    assert outcome.escalation.channels == ["log", "db", "webhook"]
    assert outcome.escalation.delivery_error is None
    assert outcome.escalation.delivered_at is not None
    assert notifier.seen[0][0] == outcome.run_id


async def test_a_failed_delivery_never_fails_the_run(tmp_db_path: Path) -> None:
    outcome, _ = await run_with(StubNotifier(error="webhook answered HTTP 500"), tmp_db_path)
    assert outcome.status == "escalated" and outcome.escalation is not None
    assert outcome.escalation.reason == "invalid_output"
    assert outcome.escalation.delivery_error == "webhook answered HTTP 500"
    assert outcome.escalation.delivered_at is None

    outcome, _ = await run_with(StubNotifier(raise_=True), tmp_db_path)
    assert outcome.status == "escalated" and outcome.escalation is not None
    assert outcome.escalation.delivery_error == "notifier raised"


async def test_without_a_notifier_the_record_is_as_before(tmp_db_path: Path) -> None:
    outcome, _ = await run_with(None, tmp_db_path)
    assert outcome.escalation is not None
    assert outcome.escalation.channels == ["log", "db"]
    assert outcome.escalation.delivered_at is not None and outcome.escalation.delivery_error is None


async def test_save_run_writes_the_delivery_columns(tmp_db_path: Path) -> None:
    outcome, recorder = await run_with(StubNotifier(error="webhook answered HTTP 502"), tmp_db_path)
    store = SqliteMemoryStore(tmp_db_path, redactor=recorder.redactor)
    await store.initialize()
    await store.save_run(outcome)

    with sqlite3.connect(tmp_db_path) as db:
        row = db.execute(
            "select reason, channel, delivered_at, delivery_error from escalation"
        ).fetchone()
    assert row == ("invalid_output", "db", None, "webhook answered HTTP 502")

    delivered, _ = await run_with(StubNotifier(), tmp_db_path)
    await store.save_run(delivered)
    with sqlite3.connect(tmp_db_path) as db:
        row = db.execute(
            "select delivered_at, delivery_error from escalation where run_id = ?",
            (delivered.run_id,),
        ).fetchone()
    assert row[0] is not None and row[1] is None
    # The stored record round-trips with the new field.
    _, listed = (await store.list_escalations(limit=10))[0]
    assert listed.delivery_error in (None, "webhook answered HTTP 502")


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_channels_and_notifier_follow_the_setting() -> None:
    settings = get_settings()
    assert escalation_channels_for(settings) == ("log", "db")
    assert build_notifier(settings, Redactor(SecretRegistry(), ())) is None

    from pydantic import SecretStr

    configured = settings.model_copy(update={"escalation_webhook_url": SecretStr(URL)})
    assert escalation_channels_for(configured) == ("log", "db", "webhook")
    assert isinstance(build_notifier(configured, Redactor(SecretRegistry(), ())), WebhookNotifier)
    assert webhook_url(configured) == URL

    blank = settings.model_copy(update={"escalation_webhook_url": SecretStr("  ")})
    assert escalation_channels_for(blank) == ("log", "db")
    assert build_notifier(blank, Redactor(SecretRegistry(), ())) is None


def test_default_notifier_opens_its_own_client_per_delivery() -> None:
    """No injected client: each delivery opens and closes one -- exercised against a
    URL nothing listens on, which must come back as a transport error, not raise."""
    notifier = WebhookNotifier("http://127.0.0.1:9/nothing", timeout_s=0.2, retries=0)
    delivered = asyncio.run(notifier.deliver(RUN_ID, record()))
    assert delivered.delivered_at is None
    assert delivered.delivery_error is not None
    assert delivered.delivery_error.startswith("webhook")
    assert "127.0.0.1" not in delivered.delivery_error
