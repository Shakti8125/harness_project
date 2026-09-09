"""Wave-3 audit finding 3: `final.<artifact>.logs[].excerpt` and
`final.<artifact>.diff.files[].patch` must never reach the HTTP response verbatim.

Unit-level, no HTTP, no model call: `_digest_str_field` and `_serialize_run_outcome` are
pure functions of a `RunOutcome` (or a bare `dict`), so the whole fix is exercised here
without a FastAPI client. The HTTP-level regression -- the actual route, driven by a
stubbed LLM -- lives in `tests/integration/test_replay_response_scrubbing.py`; this file
pins the transformation itself, including the branch that cannot be inferred from the
string case: a `None` patch must stay `None`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from src.api.main import _digest_str_field, _serialize_run_outcome
from src.harness.contracts import RunOutcome, TokenUsage

RUN_ID = "run_00000000000000000000000001"


def _outcome(final: dict[str, object]) -> RunOutcome:
    return RunOutcome(
        run_id=RUN_ID,
        integration="cicd",
        status="completed",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=42,
        stages=[],
        total_tokens=TokenUsage(),
        final=final,
        trace_url=f"/v1/runs/{RUN_ID}/trace",
    )


# --- _digest_str_field, in isolation ------------------------------------------------


def test_digest_str_field_replaces_a_string_with_length_and_sha256() -> None:
    container = {"path": "y", "excerpt": "hello world"}
    _digest_str_field(container, "excerpt")

    assert "excerpt" not in container
    assert container["excerpt_length"] == 11
    assert container["excerpt_sha256"] == hashlib.sha256(b"hello world").hexdigest()


def test_digest_str_field_is_a_no_op_on_none() -> None:
    """The branch that cannot be inferred from the string case: GitHub omits `patch` for
    binary/too-large files, and a `None` patch must stay `None` -- never become a digest
    of the empty string or of the string "None".
    """
    container = {"path": "x", "patch": None, "status": "added"}
    _digest_str_field(container, "patch")

    assert container == {"path": "x", "patch": None, "status": "added"}
    assert "patch_length" not in container
    assert "patch_sha256" not in container


def test_digest_str_field_is_a_no_op_when_the_key_is_absent() -> None:
    container = {"path": "z"}
    _digest_str_field(container, "patch")

    assert container == {"path": "z"}


# --- _serialize_run_outcome, over a whole RunOutcome --------------------------------


def test_excerpt_is_scrubbed_and_other_log_fields_survive() -> None:
    outcome = _outcome(
        {
            "bundle": {
                "logs": [
                    {
                        "job_id": 1,
                        "total_lines": 100,
                        "included_lines": 40,
                        "excerpt": "a" * 40_638,
                        "anchor_line_numbers": [12345],
                        "truncation": {"original_lines": 100, "kept_lines": 40},
                    }
                ],
                "diff": {"files": []},
            }
        }
    )

    body = _serialize_run_outcome(outcome)
    log_entry = body["final"]["bundle"]["logs"][0]

    assert "excerpt" not in log_entry
    assert log_entry["excerpt_length"] == 40_638
    assert log_entry["excerpt_sha256"] == hashlib.sha256(b"a" * 40_638).hexdigest()
    assert len(log_entry["excerpt_sha256"]) == 64
    # Untouched siblings.
    assert log_entry["total_lines"] == 100
    assert log_entry["included_lines"] == 40
    assert log_entry["anchor_line_numbers"] == [12345]
    assert log_entry["truncation"] == {"original_lines": 100, "kept_lines": 40}


def test_patch_is_scrubbed_when_non_empty_and_other_file_fields_survive() -> None:
    outcome = _outcome(
        {
            "bundle": {
                "logs": [],
                "diff": {
                    "files": [
                        {
                            "path": "src/pricing/discount.py",
                            "status": "modified",
                            "additions": 1,
                            "deletions": 1,
                            "patch": "@@ -1,3 +1,3 @@\n-old\n+new\n",
                        }
                    ]
                },
            }
        }
    )

    body = _serialize_run_outcome(outcome)
    file_entry = body["final"]["bundle"]["diff"]["files"][0]

    assert "patch" not in file_entry
    assert file_entry["patch_length"] == len("@@ -1,3 +1,3 @@\n-old\n+new\n")
    assert len(file_entry["patch_sha256"]) == 64
    assert file_entry["path"] == "src/pricing/discount.py"
    assert file_entry["status"] == "modified"
    assert file_entry["additions"] == 1
    assert file_entry["deletions"] == 1


def test_a_none_patch_is_never_digested_end_to_end() -> None:
    """The explicit fixture the api-surface report calls for: a `FileChange` whose
    `patch` GitHub omitted (binary or too-large file)."""
    outcome = _outcome(
        {
            "bundle": {
                "logs": [],
                "diff": {
                    "files": [
                        {
                            "path": "assets/logo.png",
                            "status": "modified",
                            "additions": 0,
                            "deletions": 0,
                            "patch": None,
                        }
                    ]
                },
            }
        }
    )

    body = _serialize_run_outcome(outcome)
    file_entry = body["final"]["bundle"]["diff"]["files"][0]

    assert file_entry["patch"] is None
    assert "patch_length" not in file_entry
    assert "patch_sha256" not in file_entry


def test_citations_are_never_touched_even_though_they_carry_a_quote_field() -> None:
    """`Citation.quote` is the harness's designed evidence surface and must survive
    byte-for-byte -- the scrub is a field-name walk keyed on `excerpt`/`patch` inside
    `logs[]`/`diff.files[]` specifically, and must not widen to any string field."""
    outcome = _outcome(
        {
            "bundle": {"logs": [], "diff": {"files": []}},
            "diagnosis": {
                "category": "real_regression",
                "final_confidence": 0.9,
                "citations": [
                    {
                        "claim_kind": "quote_exists",
                        "locator": "log:job/1",
                        "quote": "assert 91 == 90",
                        "note": "the failing assertion",
                    }
                ],
            },
        }
    )

    body = _serialize_run_outcome(outcome)

    assert body["final"]["diagnosis"]["citations"][0]["quote"] == "assert 91 == 90"


def test_multiple_artifacts_and_multiple_entries_are_all_scrubbed() -> None:
    """The walk is keyed on the field name inside `final.<artifact>`, not hardcoded to
    the artifact key `"bundle"` -- `wiring.ARTIFACT_KEYS` is integration-owned."""
    outcome = _outcome(
        {
            "some_other_artifact": {
                "logs": [
                    {"excerpt": "first"},
                    {"excerpt": "second"},
                ],
                "diff": {
                    "files": [
                        {"path": "a", "patch": "patch-a"},
                        {"path": "b", "patch": None},
                    ]
                },
            }
        }
    )

    body = _serialize_run_outcome(outcome)
    logs = body["final"]["some_other_artifact"]["logs"]
    files = body["final"]["some_other_artifact"]["diff"]["files"]

    assert all("excerpt" not in entry for entry in logs)
    assert all(entry["excerpt_length"] > 0 for entry in logs)
    assert "patch" not in files[0]
    assert "patch_sha256" in files[0]
    assert files[1]["patch"] is None
    assert "patch_sha256" not in files[1]


@pytest.mark.parametrize("missing_key", ["diff", "logs"])
def test_missing_logs_or_diff_key_does_not_raise(missing_key: str) -> None:
    artifact: dict[str, object] = {"logs": [], "diff": {"files": []}}
    del artifact[missing_key]
    outcome = _outcome({"bundle": artifact})

    # Must not raise.
    _serialize_run_outcome(outcome)
