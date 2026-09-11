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


def remediation_plan(
    action: str = "open_fix_pr", *, with_calls: bool = True, run_id: int = RUN_ID
) -> dict[str, Any]:
    """A well-formed `RemediationPlan` for `action`, as the model would return it.

    `with_calls=False` leaves `tool_calls` empty so a test can exercise the harness's
    canonical derivation from the drafts.
    """
    if action == "retry_job":
        calls: list[dict[str, Any]] = [
            {
                "call_id": "tc_model000001",
                "tool": "rerun_failed_jobs",
                "args": {"run_id": run_id, "attempt": 1},
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


# ---------------------------------------------------------------------------
# Scenario-aware stub model
# ---------------------------------------------------------------------------

#: `run_id` as rendered by `render_job` -- the one token that tells the scenarios apart
#: inside every agent's prompt.
SCENARIO_RUN_IDS = {
    "real_regression": "501234567",
    "flaky_test": "501234890",
    "infra_timeout": "501235102",
}


def scenario_of(prompt: str) -> str:
    for name, run_id in SCENARIO_RUN_IDS.items():
        if f"run: {run_id} " in prompt:
            return name
    raise AssertionError("prompt names no known scenario")


def notes_for(scenario: str) -> dict[str, Any]:
    observations = {
        "real_regression": [
            "tests/test_pricing.py::test_discount_applies fails with assert 91 == 90",
            "The diff touches exactly one file, src/pricing/discount.py",
        ],
        "flaky_test": [
            "tests/test_scheduler.py::test_job_runs_within_deadline fails with "
            "AssertionError: job took 1.207s, expected < 1.0s",
            "The diff touches README.md and src/pricing/format.py only",
        ],
        "infra_timeout": [
            "pip install fails: ReadTimeoutError: HTTPSConnectionPool(host='pypi.org', "
            "port=443): Read timed out",
            "The diff contains no files",
        ],
    }[scenario]
    return {
        "observations": observations,
        "additional_tool_calls": [],
        "narrative": "; ".join(observations),
    }


def diagnosis_for(scenario: str, self_confidence: float = 0.92) -> dict[str, Any]:
    if scenario == "real_regression":
        return {
            "reasoning": "The log shows assert 91 == 90 and the diff changes only discount().",
            "category": "real_regression",
            "summary": "An off-by-one in discount() returns 91 instead of 90.",
            "self_confidence": self_confidence,
            "citations": [
                {"claim_kind": "quote_exists", "locator": "log:job/601234567",
                 "quote": "assert 91 == 90", "note": "the failing assertion"},
            ],
            "suspected_commit_sha": HEAD_SHA,
            "suspected_test_ids": ["tests/test_pricing.py::test_discount_applies"],
            "suspected_package": None,
            "suggested_action": "open_fix_pr",
        }
    if scenario == "flaky_test":
        return {
            "reasoning": "A wall-clock deadline slipped by 0.2s on a shared runner; the diff "
                         "touches only formatting code the scheduler does not import.",
            "category": "flaky_test",
            "summary": "test_job_runs_within_deadline is timing-dependent; the diff is unrelated.",
            "self_confidence": self_confidence,
            "citations": [
                {"claim_kind": "test_in_log", "locator": "log:job/601234890",
                 "quote": "AssertionError: job took 1.207s, expected < 1.0s", "note": ""},
            ],
            "suspected_commit_sha": None,
            "suspected_test_ids": ["tests/test_scheduler.py::test_job_runs_within_deadline"],
            "suspected_package": None,
            "suggested_action": "retry",
        }
    if scenario == "infra_timeout":
        return {
            "reasoning": "pip could not reach pypi.org; everything after is a consequence. "
                         "The diff is empty.",
            "category": "infra_transient",
            "summary": "Package registry timeout during install; no code changed.",
            "self_confidence": self_confidence,
            "citations": [
                {"claim_kind": "quote_exists", "locator": "log:job/601235102",
                 "quote": "Read timed out. (read timeout=15)", "note": ""},
            ],
            "suspected_commit_sha": None,
            "suspected_test_ids": [],
            "suspected_package": None,
            "suggested_action": "retry",
        }
    raise AssertionError(scenario)


PLAN_ACTION_FOR_SCENARIO = {
    "real_regression": "open_fix_pr",
    "flaky_test": "retry_job",
    "infra_timeout": "retry_job",
}


class ScenarioStubLlm:
    """Answers all three agents for any of the three scenarios, dispatching on the prompt.

    `plan_action` overrides the Remediator's action for every scenario (a hallucinated
    `forbidden` plan, `no_action`, ...); `self_confidence` sets the Diagnostician's.
    """

    def __init__(
        self,
        *,
        self_confidence: float = 0.92,
        plan_action: str | None = None,
        plan_with_calls: bool = True,
    ) -> None:
        self.self_confidence = self_confidence
        self.plan_action = plan_action
        self.plan_with_calls = plan_with_calls
        self.prompts: list[str] = []

    async def generate(self, req: Any) -> Any:
        from src.harness.contracts import TokenUsage
        from src.harness.llm import RawLlmResponse

        self.prompts.append(req.prompt)
        scenario = scenario_of(req.prompt)
        if "You are the Investigator" in req.prompt:
            payload: Any = notes_for(scenario)
        elif "You are the Diagnostician" in req.prompt:
            payload = diagnosis_for(scenario, self.self_confidence)
        elif "You are the Remediator" in req.prompt:
            payload = remediation_plan(
                self.plan_action or PLAN_ACTION_FOR_SCENARIO[scenario],
                with_calls=self.plan_with_calls,
                run_id=int(SCENARIO_RUN_IDS[scenario]),
            )
        else:  # pragma: no cover
            raise AssertionError("unrecognised prompt reached the stub model")
        import json

        return RawLlmResponse(
            text=json.dumps(payload),
            tokens=TokenUsage(prompt=1200, completion=300, total=1500),
            finish_reason="STOP",
            model=req.model,
            latency_ms=12,
        )
