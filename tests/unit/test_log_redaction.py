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
    assert scrubbed.msg == "secret %s" and scrubbed.args == (REDACTION_PLACEHOLDER,)
    assert scrubbed.getMessage() == f"secret {REDACTION_PLACEHOLDER}"


def test_args_keep_their_shape_for_formatters_that_unpack_them() -> None:
    """uvicorn's `AccessFormatter` reads `record.args` positionally (five values); a
    factory that folded them into `msg` broke every access line with a logging error
    (found by the first live Verify run after the fix round). Scrub in place instead."""
    install_log_redaction(_redactor())
    factory = logging.getLogRecordFactory()
    args = ("127.0.0.1:1234", "GET", f"/v1/runs?token={SECRET}", "1.1", 200)
    record = factory("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    client, method, path, version, status = record.args
    assert (client, method, version, status) == ("127.0.0.1:1234", "GET", "1.1", 200)
    assert SECRET not in path and REDACTION_PLACEHOLDER in path
    # The registry replaces the value, then the assignment heuristic eats `token=` too.
    assert record.getMessage() == f'127.0.0.1:1234 - "GET /v1/runs?{REDACTION_PLACEHOLDER} HTTP/1.1" 200'


def test_a_credential_that_straddles_msg_and_args_folds_the_record() -> None:
    """`api_key=%s` is an assignment shape only once formatted; that one record gives up
    its `args` shape rather than print the value."""
    install_log_redaction(_redactor())
    factory = logging.getLogRecordFactory()
    record = factory("t", logging.INFO, __file__, 1, "api_key=%s ok", ("abcdefghijklmnop",), None)
    assert record.args == () and record.getMessage() == f"{REDACTION_PLACEHOLDER} ok"
    clean = factory("t", logging.INFO, __file__, 1, "n=%d %s", (3, "fine"), None)
    assert clean.args == (3, "fine"), "a clean record keeps its shape"


def test_dict_args_and_exception_args_are_scrubbed_too() -> None:
    install_log_redaction(_redactor())
    factory = logging.getLogRecordFactory()
    record = factory("t", logging.INFO, __file__, 1, "key=%(k)s n=%(n)d", ({"k": TOKEN, "n": 3},), None)
    assert record.getMessage() == f"key={REDACTION_PLACEHOLDER} n=3"
    exc = RuntimeError(f"upstream said: token {SECRET} is invalid")
    record = factory("t", logging.ERROR, __file__, 1, "failed: %s", (exc,), None)
    assert SECRET not in record.getMessage() and REDACTION_PLACEHOLDER in record.getMessage()
