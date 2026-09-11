"""Canned model outputs shared by the integration tests.

Replay mode stubs the *gateway*; the model is the one boundary a replay cannot supply, so
each test stubs it with a canned response per agent, dispatched on the prompt's own
preamble. The Remediator's canned plans live here because every replay test now reaches
that stage: a diagnosis that clears the gate is always followed by a plan, and a stub that
does not know the Remediator's prompt fails the run rather than the assertion.
"""

from __future__ import annotations

from typing import Any

HEAD_SHA = "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"
RUN_ID = 501234567

FIX_BRANCH = f"agent/fix/{HEAD_SHA[:8]}"

FIXED_DISCOUNT_PY = (
    "def discount(price: int, percent: int) -> int:\n"
    '    """\n'
    "    Apply a percent discount to price, rounded down to the nearest integer.\n"
    '    """\n'
    "    return price - (price * percent) // 100\n"
)


def remediation_plan(action: str = "open_fix_pr", *, with_calls: bool = True) -> dict[str, Any]:
    """A well-formed `RemediationPlan` for `action`, as the model would return it.

    `with_calls=False` leaves `tool_calls` empty so a test can exercise the harness's
    canonical derivation from the drafts.
    """
    if action == "retry_job":
        calls: list[dict[str, Any]] = [
            {
                "call_id": "tc_model000001",
                "tool": "rerun_failed_jobs",
                "args": {"run_id": RUN_ID, "attempt": 1},
                "idempotency_key": None,
            }
        ]
        return {
            "rationale": "The failure is not attributable to the change; re-run the job.",
            "action": "retry_job",
            "tool_calls": calls if with_calls else [],
            "pr_draft": None,
            "ticket_draft": None,
        }
    if action == "file_ticket":
        calls = [
            {
                "call_id": "tc_model000001",
                "tool": "create_issue",
                "args": {
                    "title": "CI failure needs a human",
                    "body": "No safe automated change exists.",
                    "labels": ["agent-triage"],
                },
                "idempotency_key": None,
            }
        ]
        return {
            "rationale": "Real, but no bounded automated fix is safe.",
            "action": "file_ticket",
            "tool_calls": calls if with_calls else [],
            "pr_draft": None,
            "ticket_draft": {
                "title": "CI failure needs a human",
                "body": "No safe automated change exists.",
                "labels": ["agent-triage"],
            },
        }
    if action == "no_action":
        return {
            "rationale": "The diagnosis does not support an action.",
            "action": "no_action",
            "tool_calls": [],
            "pr_draft": None,
            "ticket_draft": None,
        }
    if action == "forbidden":
        # A hallucinated plan: the model asks for the one thing the policy forbids.
        return {
            "rationale": "Merge the fix straight away.",
            "action": "open_fix_pr",
            "tool_calls": [
                {
                    "call_id": "tc_model000001",
                    "tool": "merge_pull_request",
                    "args": {"number": 1},
                    "idempotency_key": None,
                }
            ],
            "pr_draft": None,
            "ticket_draft": None,
        }
    # open_fix_pr (default)
    pr_draft = {
        "branch": FIX_BRANCH,
        "base": "main",
        "title": "Fix off-by-one in discount()",
        "body": "Removes the stray `+ 1` that made discount(100, 10) return 91.",
        "files": [
            {
                "path": "src/pricing/discount.py",
                "new_content": FIXED_DISCOUNT_PY,
                "rationale": "Drop the `+ 1` added by the failing commit.",
            }
        ],
        "labels": ["agent-generated"],
        "draft": True,
    }
    calls = [
        {
            "call_id": "tc_model000001",
            "tool": "create_branch",
            "args": {"name": FIX_BRANCH, "from_sha": HEAD_SHA},
            "idempotency_key": None,
        },
        {
            "call_id": "tc_model000002",
            "tool": "create_or_update_file",
            "args": {
                "branch": FIX_BRANCH,
                "path": "src/pricing/discount.py",
                "content_b64": "ZGVmIGRpc2NvdW50KCk6IC4uLg==",
                "message": "Fix off-by-one in discount()",
            },
            "idempotency_key": None,
        },
        {
            "call_id": "tc_model000003",
            "tool": "open_pull_request",
            "args": {
                "head": FIX_BRANCH,
                "base": "main",
                "title": "Fix off-by-one in discount()",
                "body": "Removes the stray `+ 1`.",
                "draft": True,
                "labels": ["agent-generated"],
            },
            "idempotency_key": None,
        },
    ]
    return {
        "rationale": "A one-line arithmetic error in a commit with a legitimate intent.",
        "action": "open_fix_pr",
        "tool_calls": calls if with_calls else [],
        "pr_draft": pr_draft,
        "ticket_draft": None,
    }
