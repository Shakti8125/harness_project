# ruff: noqa: E501
"""Phase 5 audit finding 4: a log line is a sink like any other.

`ToolError.message` carries whatever GitHub's error body said -- since Phase 5 every
`errors[].message` too -- and two log lines (`remediation stopped at …`, `run … escalated
(tool_failure): …`) wrote it verbatim while the row, the served body and the notifier
payload were scrubbed. PLAN's "Secrets never reach the trace" mechanism 2 names log lines.
The fix installs the `Redactor` at record creation, so every logger in the process --
ours, `httpx`'s, `aiosqlite`'s -- is covered without a per-logger filter.
"""

from __future__ import annotations

import logging

import pytest

from src.api.deps import HEURISTIC_SECRET_PATTERNS, SECRET_PATTERNS
from src.harness.observability import (
    REDACTION_PLACEHOLDER,
    Redactor,
    SecretRegistry,
    install_log_redaction,
)

TOKEN = "ghp_" + "Z" * 36
SECRET = "whsec_registered_value_0123456789"


def _redactor() -> Redactor:
    registry = SecretRegistry()
    registry.register("github_token", SECRET)
    return Redactor(registry, SECRET_PATTERNS, heuristic_patterns=HEURISTIC_SECRET_PATTERNS)


def test_log_records_are_scrubbed_at_creation(caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction(_redactor())
    caplog.set_level(logging.DEBUG)
    logging.getLogger("src.integrations.cicd.remediation").warning(
        "remediation stopped at %r: %s", "rerun_failed_jobs",
        f"forbidden (403): Resource not accessible; token {TOKEN} lacks actions:write",
    )
    logging.getLogger("third.party").info("posting with secret=%s to %s", SECRET, "https://example")
    logging.getLogger("dict.style").info("api_key=%(key)s", {"key": "abcdefghijklmnop"})
    text = caplog.text
    assert TOKEN not in text and SECRET not in text
    assert f"token {REDACTION_PLACEHOLDER} lacks" in text
    assert f"secret={REDACTION_PLACEHOLDER} to https://example" in text
    assert "abcdefghijklmnop" not in text, "the assignment heuristic applies to a log line"


def test_installing_twice_keeps_one_factory_and_the_latest_redactor(caplog: pytest.LogCaptureFixture) -> None:
    first = _redactor()
    install_log_redaction(first)
    factory_after_first = logging.getLogRecordFactory()
    other = SecretRegistry()
    other.register("x", "second_secret_value_98765")
    install_log_redaction(Redactor(other, ()))
    assert logging.getLogRecordFactory() is factory_after_first, "idempotent: no chain of factories"
    caplog.set_level(logging.INFO)
    logging.getLogger("t").info("a %s b %s", "second_secret_value_98765", SECRET)
    assert "second_secret_value_98765" not in caplog.text
    assert SECRET in caplog.text, "the latest redactor wins; the earlier registry is not consulted"
    install_log_redaction(first)


def test_a_record_that_cannot_format_is_left_for_logging_to_report() -> None:
    """A bad format string is the caller's bug; the factory leaves the record as it was
    (msg and args intact) for `logging.Handler.handleError` to report, and raises nothing
    itself. (pytest's capture handler re-raises such errors, so the record is made
    directly rather than logged.)"""
    install_log_redaction(_redactor())
    factory = logging.getLogRecordFactory()
    record = factory("t", logging.INFO, __file__, 1, "%s %s", ("only-one-arg",), None)
    assert record.msg == "%s %s" and record.args == ("only-one-arg",)
    scrubbed = factory("t", logging.INFO, __file__, 1, "secret %s", (SECRET,), None)
    assert scrubbed.msg == f"secret {REDACTION_PLACEHOLDER}" and scrubbed.args == ()
