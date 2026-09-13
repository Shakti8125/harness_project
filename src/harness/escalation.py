"""Outbound escalation delivery.

DERIVED, NOT TRANSCRIBED. Appendix A names no module for this; Appendix B.4 fixes the
behaviour: an optional `POST` to `HARNESS_ESCALATION_WEBHOOK_URL` with a 5 s timeout and
2 retries, after which the failure is recorded in `escalation.delivery_error` and the run
continues. **A delivery failure never fails the run** -- the `escalation` row is the
durable record, the webhook is a convenience -- so nothing in this module raises for a
transport problem, and nothing above it has to catch one.

Why a generic webhook and not a Slack SDK (PLAN.md's decision): no dependency, and any
consumer works. The body leads with a `text` line, which is the one key a Slack Incoming
Webhook needs, and carries the structured record beside it for anything else.

Two things the webhook URL must never do: appear in a log line, or appear in the
`delivery_error` string that is stored and served. The URL is a secret (a Slack webhook
URL *is* the credential), and `httpx` spells the request URL into most of its exception
messages -- so `delivery_error` is built from the exception class and the HTTP status,
never from `str(exc)`. The composition root additionally registers the URL with the
`Redactor`, and the body is scrubbed through that redactor before it leaves.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Final, Protocol

import httpx
from pydantic import JsonValue

from src.harness.contracts import EscalationRecord, RunId
from src.harness.observability import REDACTION_PLACEHOLDER, Redactor

logger = logging.getLogger("harness.escalation")

#: The logger httpx writes `HTTP Request: <method> <url> "..."` to, at INFO, for every
#: request it sends. For every other client in this codebase the URL is public; for the
#: webhook it is the credential, so the notifier installs a filter on this logger that
#: rewrites its own URL wherever it appears (Phase 4 audit finding 2). A filter rather
#: than a level: the request line stays useful, only the secret leaves it.
HTTPX_LOGGER: Final[str] = "httpx"

#: Appendix B.4: timeout 5 s, 2 retries (three attempts in total).
WEBHOOK_TIMEOUT_S: Final[float] = 5.0
WEBHOOK_RETRIES: Final[int] = 2
#: Between attempts; short, because the run is waiting on this and the row is already
#: the durable record.
WEBHOOK_BACKOFF_S: Final[float] = 0.5


class EscalationNotifier(Protocol):
    """Delivers an escalation on one outbound channel and reports how it went."""

    async def deliver(self, run_id: RunId, record: EscalationRecord) -> EscalationRecord:
        """Return the record with `delivered_at` set on success, or `delivery_error`
        set (and `delivered_at` cleared) on failure. Never raises."""
        ...


def webhook_body(
    run_id: RunId, record: EscalationRecord, *, trace_url: str | None = None
) -> dict[str, JsonValue]:
    """The outbound payload. `text` first, so a Slack Incoming Webhook renders it."""
    text = f"[harness] run {run_id} escalated ({record.reason}): {record.message}"
    return {
        "text": text,
        "run_id": run_id,
        "escalation_id": record.escalation_id,
        "reason": record.reason,
        "message": record.message,
        "payload": dict(record.payload),
        "trace_url": trace_url,
    }


def _describe_failure(exc: BaseException) -> str:
    """A delivery error that names the failure class and status, never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"webhook answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"webhook timed out after {WEBHOOK_TIMEOUT_S:g}s"
    if isinstance(exc, httpx.HTTPError):
        return f"webhook transport error ({type(exc).__name__})"
    return f"webhook delivery failed ({type(exc).__name__})"


class _ScrubUrl(logging.Filter):
    """Rewrite one URL to the redaction placeholder in a logger's records.

    httpx passes the URL as a format argument (`record.args`), so both the arguments
    and a pre-formatted message are scrubbed. Always returns True: the record is kept,
    minus the secret.
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def _scrub(self, value: object) -> object:
        text = str(value)
        return text.replace(self.url, REDACTION_PLACEHOLDER) if self.url in text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._scrub(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._scrub(arg) for key, arg in record.args.items()}
        if isinstance(record.msg, str) and self.url in record.msg:
            record.msg = record.msg.replace(self.url, REDACTION_PLACEHOLDER)
        return True


def scrub_url_from_httpx_logs(url: str) -> None:
    """Install (once per URL) the filter that keeps `url` out of httpx's request log."""
    httpx_logger = logging.getLogger(HTTPX_LOGGER)
    if any(isinstance(f, _ScrubUrl) and f.url == url for f in httpx_logger.filters):
        return
    httpx_logger.addFilter(_ScrubUrl(url))


class WebhookNotifier:
    """`EscalationNotifier` over one outbound `POST`, per Appendix B.4."""

    def __init__(
        self,
        url: str,
        *,
        redactor: Redactor | None = None,
        trace_url_template: str = "/v1/runs/{run_id}/trace",
        timeout_s: float = WEBHOOK_TIMEOUT_S,
        retries: int = WEBHOOK_RETRIES,
        backoff_s: float = WEBHOOK_BACKOFF_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`client` is injectable for tests; otherwise one is opened per delivery, since
        escalations are rare and a long-lived client would outlive most runs idle."""
        self._url = url
        self._redactor = redactor
        self._trace_url_template = trace_url_template
        scrub_url_from_httpx_logs(url)
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = backoff_s
        self._client = client

    async def _post(self, body: dict[str, JsonValue]) -> None:
        if self._client is not None:
            response = await self._client.post(self._url, json=body, timeout=self._timeout_s)
            response.raise_for_status()
            return
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(self._url, json=body)
            response.raise_for_status()

    async def deliver(self, run_id: RunId, record: EscalationRecord) -> EscalationRecord:
        body = webhook_body(
            run_id, record, trace_url=self._trace_url_template.format(run_id=run_id)
        )
        if self._redactor is not None:
            scrubbed = self._redactor.scrub(body)
            assert isinstance(scrubbed, dict)  # a dict in, a dict out: the redactor keeps shape
            body = scrubbed

        last_error: str | None = None
        for attempt in range(1, self._retries + 2):
            try:
                await self._post(body)
            except Exception as exc:  # noqa: BLE001 - every failure class is recorded, none raised
                last_error = _describe_failure(exc)
                logger.warning(
                    "run %s: escalation %s webhook delivery attempt %d/%d failed: %s",
                    run_id, record.escalation_id, attempt, self._retries + 1, last_error,
                )
                if attempt <= self._retries:
                    await asyncio.sleep(self._backoff_s * (2 ** (attempt - 1)))
                continue
            return record.model_copy(
                update={"delivered_at": datetime.now(UTC), "delivery_error": None}
            )
        return record.model_copy(update={"delivered_at": None, "delivery_error": last_error})
