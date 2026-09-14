"""`src/api/webhook.py` -- the pure half of `POST /webhooks/github` (PLAN.md Phase 5).

Signature verification is a function of bytes, a secret and a header; event
classification a function of a header and a parsed body. Both are pinned here without an
app behind them, so the route test can concentrate on the claim and the run.
"""

from __future__ import annotations

import json

import pytest

from src.api.webhook import (
    EventVerdict,
    classify_event,
    delivery_id,
    sign,
    verify_signature,
)

SECRET = "a-webhook-secret-that-is-long-enough"
BODY = json.dumps({"action": "completed", "workflow_run": {"conclusion": "failure"}}).encode()


def test_sign_then_verify_round_trips() -> None:
    header = sign(SECRET, BODY)
    assert header.startswith("sha256=") and len(header) == len("sha256=") + 64
    assert verify_signature(SECRET, BODY, header) is True


def test_verify_accepts_upper_case_hex() -> None:
    header = sign(SECRET, BODY)
    assert verify_signature(SECRET, BODY, "sha256=" + header[7:].upper()) is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha1=" + "0" * 40,
        "sha256=" + "0" * 63,
        "sha256=" + "z" * 64,
        "0" * 64,
    ],
)
def test_verify_fails_closed_on_malformed_headers(header: str | None) -> None:
    assert verify_signature(SECRET, BODY, header) is False


def test_verify_rejects_a_signature_under_another_secret() -> None:
    assert verify_signature(SECRET, BODY, sign("another-secret-entirely", BODY)) is False


def test_verify_rejects_a_signature_over_different_bytes() -> None:
    header = sign(SECRET, BODY)
    assert verify_signature(SECRET, BODY + b" ", header) is False


@pytest.mark.parametrize("secret", ["", "   "])
def test_a_blank_secret_verifies_nothing(secret: str) -> None:
    """A verifier with no key is not a verifier: a signature computed over an empty key
    is not accepted, whatever the caller sends (dispatch decision 6)."""
    assert verify_signature(secret, BODY, sign(secret, BODY)) is False


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------


def _delivery(action: str = "completed", conclusion: str | None = "failure") -> dict[str, object]:
    return {"action": action, "workflow_run": {"id": 1, "conclusion": conclusion}}


def test_completed_failure_is_accepted() -> None:
    verdict = classify_event("workflow_run", _delivery())
    assert verdict == EventVerdict("accept", "workflow_run completed with conclusion failure")


@pytest.mark.parametrize(
    ("event", "payload", "reason_fragment"),
    [
        ("ping", {"zen": "Keep it logically awesome.", "hook_id": 1}, "'ping'"),
        ("check_run", _delivery(), "'check_run'"),
        ("workflow_run", _delivery(action="requested", conclusion=None), "'requested'"),
        ("workflow_run", _delivery(action="in_progress", conclusion=None), "'in_progress'"),
        ("workflow_run", _delivery(conclusion="success"), "'success'"),
        ("workflow_run", _delivery(conclusion="cancelled"), "'cancelled'"),
        (None, _delivery(), "None"),
    ],
)
def test_everything_else_is_ignored(
    event: str | None, payload: dict[str, object], reason_fragment: str
) -> None:
    verdict = classify_event(event, payload)
    assert verdict.outcome == "ignore"
    assert reason_fragment in verdict.reason


@pytest.mark.parametrize("payload", [[], "text", 42, None])
def test_a_non_object_body_is_malformed(payload: object) -> None:
    assert classify_event("workflow_run", payload).outcome == "malformed"


def test_a_completed_event_without_a_workflow_run_object_is_malformed() -> None:
    assert classify_event("workflow_run", {"action": "completed"}).outcome == "malformed"
    assert classify_event(
        "workflow_run", {"action": "completed", "workflow_run": "nope"}
    ).outcome == "malformed"


# ---------------------------------------------------------------------------
# Delivery id
# ---------------------------------------------------------------------------


def test_delivery_id_accepts_only_a_guid() -> None:
    assert delivery_id("72D3162E-CC78-11E3-81AB-4C9367DC0958") == (
        "72d3162e-cc78-11e3-81ab-4c9367dc0958"
    )
    assert delivery_id(None) is None
    assert delivery_id("") is None
    assert delivery_id("not-a-guid") is None
    assert delivery_id("<script>alert(1)</script>") is None
