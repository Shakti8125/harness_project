"""`POST /webhooks/github`'s pure half: signature verification and event classification.

Kept out of `main.py` so the two decisions that gate an unauthenticated, internet-facing
route can be tested as functions of bytes and headers, with no app, no store and no
network behind them. The route in `main.py` is what remains: the claim, the `202`, and
the background run it shares with `POST /v1/runs`.

**Verify before you parse.** `verify_signature` takes the raw request bytes -- exactly
what GitHub signed -- and answers before the body is decoded as JSON, so a caller who
cannot produce a valid signature learns nothing about how the body would have been read.
`hmac.compare_digest` keeps the comparison constant-time; the header's `sha256=` prefix
and hex case are normalised, nothing else is. A blank secret verifies nothing: the
function answers `False` for every input rather than accepting a signature computed
over an empty key, and the route logs the misconfiguration once.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Final, Literal

#: The header GitHub signs deliveries with (HMAC-SHA256 of the raw body, hex, prefixed).
SIGNATURE_HEADER: Final[str] = "X-Hub-Signature-256"
EVENT_HEADER: Final[str] = "X-GitHub-Event"
DELIVERY_HEADER: Final[str] = "X-GitHub-Delivery"

#: The only event and (action, conclusion) pair the harness triages (PLAN.md Phase 5).
ACCEPTED_EVENT: Final[str] = "workflow_run"
ACCEPTED_ACTION: Final[str] = "completed"
ACCEPTED_CONCLUSION: Final[str] = "failure"

_SIGNATURE_RE: Final[re.Pattern[str]] = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
#: GitHub's delivery id is a GUID. Only that shape is ever reflected into a run's
#: `requested_by`; anything else in the header is dropped, not echoed.
_DELIVERY_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def sign(secret: str, body: bytes) -> str:
    """The `X-Hub-Signature-256` value GitHub would send for `body` under `secret`.

    Shared with `scripts/replay.py --post-signed` and the tests, so the client and the
    verifier agree on one spelling of the header.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """True iff `header` is a well-formed `sha256=<hex>` HMAC of `body` under `secret`.

    Fails closed on every input that is not exactly that: a missing header, a different
    algorithm prefix, a truncated digest, and -- deliberately -- a blank secret, because a
    verifier with no key is not a verifier.
    """
    if not secret or not secret.strip() or header is None:
        return False
    match = _SIGNATURE_RE.match(header.strip())
    if match is None:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), match.group(1).lower().encode("ascii"))


@dataclass(frozen=True)
class EventVerdict:
    """What the route does with a delivery, and why -- one line for the log."""

    outcome: Literal["accept", "ignore", "malformed"]
    reason: str


def classify_event(event: str | None, payload: object) -> EventVerdict:
    """PLAN.md Phase 5: accept only `workflow_run` / `completed` / `failure`; ignore the rest.

    `malformed` is reserved for a body that is not a JSON object at all -- a `400`, so a
    misconfigured sender sees the problem. A well-formed event the harness does not
    triage (a `ping`, a `check_run`, a successful run, a `requested` action) is `ignore`
    -- a `204`, so GitHub records a clean delivery and nothing runs.
    """
    if not isinstance(payload, dict):
        return EventVerdict("malformed", "the body is not a JSON object")
    if event != ACCEPTED_EVENT:
        return EventVerdict("ignore", f"event {event!r} is not {ACCEPTED_EVENT!r}")
    if payload.get("action") != ACCEPTED_ACTION:
        return EventVerdict("ignore", f"action {payload.get('action')!r} is not 'completed'")
    run = payload.get("workflow_run")
    if not isinstance(run, dict):
        return EventVerdict("malformed", "the body carries no workflow_run object")
    conclusion = run.get("conclusion")
    if conclusion != ACCEPTED_CONCLUSION:
        return EventVerdict("ignore", f"conclusion {conclusion!r} is not 'failure'")
    return EventVerdict("accept", "workflow_run completed with conclusion failure")


def delivery_id(header: str | None) -> str | None:
    """The `X-GitHub-Delivery` GUID, or `None` for anything not shaped like one."""
    if header is None:
        return None
    value = header.strip()
    return value.lower() if _DELIVERY_ID_RE.match(value) else None
