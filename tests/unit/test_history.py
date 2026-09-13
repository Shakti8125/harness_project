"""`src/integrations/cicd/history.py`: the prior's words, and how a retry's result is learned.

Pinned by the Phase 3 fix round. Findings 3, 5 and 7 of the audit were each a rule this
module states in prose and nothing enforced: a `failed_again` observation nobody read, a
rerun judged from one page of its jobs, and an action-counting rule spelled twice. No
store, no model, no HTTP here -- `MemoryHit`s are built by hand and the gateway is a
one-tool fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.harness.contracts import RunOutcome, TokenUsage
from src.harness.gateway import ToolCall, ToolResult
from src.harness.guardrails import PolicyDecision
from src.harness.memory import (
    MemoryHit,
    Observation,
    SignatureKey,
    SignatureRecord,
    observation_id_for,
    signature_id_for,
)
from src.integrations.cicd.history import (
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_PENDING,
    RETRY_TOOL,
    action_observation,
    last_retry_outcome,
    memory_agrees,
    prior_history_from,
    resolve_pending_outcomes,
)
from src.integrations.cicd.rendering import render_prior_history
from src.integrations.cicd.schemas import Diagnosis, JobRef
from tests.stubs import diagnosis_for

KEY = SignatureKey(scope="repo:octo-org/demo", subject_key="CI|test|t", fingerprint="f" * 64)
SID = signature_id_for(KEY)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _run_id(n: int) -> str:
    return "run_" + f"{n:026d}".replace("0", "A")[:26]


def _obs(
    n: int, *, verdict: str = "flaky_test", action: str | None = None,
    outcome: str | None = None, confidence: float = 0.9,
) -> Observation:
    return Observation(
        observation_id=observation_id_for(SID, _run_id(n)),
        signature_id=SID,
        run_id=_run_id(n),
        occurred_at=T0 - timedelta(hours=n),  # n=0 is the newest
        verdict=verdict,
        confidence=confidence,
        action_taken=action,
        action_outcome=outcome,  # type: ignore[arg-type]
        commit_sha=None,
    )


def _hit(recent: list[Observation], *, occurrences: int, counts: dict[str, int]) -> MemoryHit:
    return MemoryHit(
        record=SignatureRecord(
            signature_id=SID, scope=KEY.scope, subject_key=KEY.subject_key,
            fingerprint=KEY.fingerprint, first_seen_at=T0 - timedelta(days=3),
            last_seen_at=T0, occurrences=occurrences, verdict_counts=counts,
            last_verdict="flaky_test", last_run_id=_run_id(0),
        ),
        recent=recent,  # newest first, as the store returns them
        actions_in_window={RETRY_TOOL: sum(1 for o in recent if o.action_taken == RETRY_TOOL)},
    )


# ---------------------------------------------------------------------------
# Audit finding 3: `failed_again` is read, and it outranks an older pass
# ---------------------------------------------------------------------------


def test_last_retry_outcome_is_the_newest_resolved_retry() -> None:
    assert last_retry_outcome([]) is None
    assert last_retry_outcome([_obs(0, action=RETRY_TOOL, outcome=OUTCOME_PENDING)]) is None
    assert last_retry_outcome([
        _obs(0, action=RETRY_TOOL, outcome=OUTCOME_PENDING),
        _obs(1, action=RETRY_TOOL, outcome=OUTCOME_FAILED),
        _obs(2, action=RETRY_TOOL, outcome=OUTCOME_PASSED),
    ]) == OUTCOME_FAILED
    assert last_retry_outcome([
        _obs(0, action=RETRY_TOOL, outcome=OUTCOME_PASSED),
        _obs(1, action=RETRY_TOOL, outcome=OUTCOME_FAILED),
    ]) == OUTCOME_PASSED
    # Another tool's outcome is not a retry's.
    assert last_retry_outcome([_obs(0, action="create_issue", outcome=OUTCOME_PASSED)]) is None


def test_a_rerun_that_failed_again_withdraws_likely_flaky_and_the_bonus() -> None:
    """The reviewer's scenario: 3x flaky_test with a pass (likely_flaky) -> the test then
    genuinely breaks -> the retry fails again -> the next sighting must not still read
    `likely_flaky` with +0.10, or the same two retries fire every day forever."""
    established = _hit(
        [
            _obs(1, action=RETRY_TOOL, outcome=OUTCOME_PASSED),
            _obs(2), _obs(3),
        ],
        occurrences=3, counts={"flaky_test": 3},
    )
    before = prior_history_from(established, KEY)
    assert before.prior_hint == "likely_flaky"
    assert before.last_retry_outcome == OUTCOME_PASSED
    assert memory_agrees(before, "flaky_test")

    contradicted = _hit(
        [
            _obs(0, action=RETRY_TOOL, outcome=OUTCOME_FAILED),
            _obs(1, action=RETRY_TOOL, outcome=OUTCOME_PASSED),
            _obs(2), _obs(3),
        ],
        occurrences=4, counts={"flaky_test": 4},
    )
    after = prior_history_from(contradicted, KEY)
    assert after.prior_hint == "unknown"
    assert after.last_retry_outcome == OUTCOME_FAILED
    assert not memory_agrees(after, "flaky_test"), "the counts still say flaky; the rerun said no"
    assert after.verdict_counts == {"flaky_test": 4}, "the counts themselves are not rewritten"

    # A later rerun that passes restores both.
    recovered = _hit(
        [_obs(0, action=RETRY_TOOL, outcome=OUTCOME_PASSED)] + contradicted.recent,
        occurrences=5, counts={"flaky_test": 5},
    )
    assert prior_history_from(recovered, KEY).prior_hint == "likely_flaky"
    assert memory_agrees(prior_history_from(recovered, KEY), "flaky_test")


def test_the_rendered_prior_says_whether_the_last_retry_passed() -> None:
    """`prompts/diagnostician.md` promises memory says "whether an automatic retry of it
    passed"; the block must say so in both directions, and say when it does not know."""
    passed = prior_history_from(
        _hit([_obs(0, action=RETRY_TOOL, outcome=OUTCOME_PASSED), _obs(1), _obs(2)],
             occurrences=3, counts={"flaky_test": 3}),
        KEY,
    )
    assert "most recent automatic retry of it PASSED" in render_prior_history(passed)

    failed = prior_history_from(
        _hit([_obs(0, action=RETRY_TOOL, outcome=OUTCOME_FAILED), _obs(1), _obs(2)],
             occurrences=3, counts={"flaky_test": 3}),
        KEY,
    )
    text = render_prior_history(failed)
    assert "most recent automatic retry of it FAILED AGAIN" in text
    assert "Deterministic prior from those counts: unknown." in text

    unknown = prior_history_from(
        _hit([_obs(0, action=RETRY_TOOL, outcome=OUTCOME_PENDING), _obs(1)],
             occurrences=2, counts={"flaky_test": 2}),
        KEY,
    )
    assert "no automatic retry of it has a known result yet" in render_prior_history(unknown)


# ---------------------------------------------------------------------------
# Audit finding 5: one page of jobs is not the run
# ---------------------------------------------------------------------------


class _FakeMemory:
    """`get_run` returns a bundle-bearing outcome; `update_observation_outcome` is recorded."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self.bundle = bundle
        self.updates: list[tuple[str, str]] = []

    async def get_run(self, run_id: str) -> RunOutcome:
        return RunOutcome(
            run_id=run_id, integration="cicd", status="completed", created_at=T0,
            completed_at=T0, duration_ms=1, stages=[], total_tokens=TokenUsage(),
            final={"bundle": self.bundle}, trace_url=f"/v1/runs/{run_id}/trace",
        )

    async def update_observation_outcome(self, observation_id: str, outcome: str) -> None:
        self.updates.append((observation_id, outcome))


class _FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[ToolCall] = []

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool=call.tool, ok=True, data=self.data, latency_ms=1
        )


def _job() -> JobRef:
    return JobRef(
        repo="octo-org/demo", workflow_name="CI", workflow_id=1, run_id=501, run_attempt=1,
        job_id=601, job_name="test", head_sha="a" * 40, branch="main", event="push",
        started_at=T0, completed_at=T0, conclusion="failure",
    )


def _bundle() -> dict[str, Any]:
    from src.integrations.cicd.schemas import DiffSummary, FailureBundle, PriorHistory

    return FailureBundle(
        job=_job(),
        logs=[],
        diff=DiffSummary(baseline_kind="none", base_sha=None, head_sha="a" * 40),
        prior_history=PriorHistory(signature_id=SID, key=KEY),
        collected_at=T0,
    ).model_dump(mode="json")


def _decision() -> PolicyDecision:
    return PolicyDecision(
        tool="list_workflow_run_jobs", rule_id="read-only-always", effect="allow",
        reason="test", evaluated_at=T0,
    )


async def _resolve(jobs_payload: dict[str, Any]) -> tuple[MemoryHit, _FakeMemory, _FakeGateway]:
    pending = _obs(0, action=RETRY_TOOL, outcome=OUTCOME_PENDING)
    hit = _hit([pending], occurrences=1, counts={"flaky_test": 1})
    memory = _FakeMemory(_bundle())
    gateway = _FakeGateway(jobs_payload)
    resolved = await resolve_pending_outcomes(
        memory, gateway, hit, decision=_decision()  # type: ignore[arg-type]
    )
    return resolved, memory, gateway


async def test_a_complete_green_job_list_resolves_to_passed() -> None:
    resolved, memory, gateway = await _resolve(
        {"total_count": 2, "jobs": [{"conclusion": "success"}, {"conclusion": "success"}]}
    )
    assert resolved.recent[0].action_outcome == OUTCOME_PASSED
    assert memory.updates == [(resolved.recent[0].observation_id, OUTCOME_PASSED)]
    assert gateway.calls[0].args == {"run_id": 501, "attempt": 2}


async def test_a_green_page_of_an_incomplete_job_list_stays_pending() -> None:
    """31 jobs, 30 on the page, all green -- the failing one may be on page 2."""
    resolved, memory, _ = await _resolve(
        {"total_count": 31, "jobs": [{"conclusion": "success"}] * 30}
    )
    assert resolved.recent[0].action_outcome == OUTCOME_PENDING
    assert memory.updates == []


async def test_a_failure_on_an_incomplete_page_is_still_a_failure() -> None:
    resolved, memory, _ = await _resolve(
        {"total_count": 31, "jobs": [{"conclusion": "success"}] * 29 + [{"conclusion": "failure"}]}
    )
    assert resolved.recent[0].action_outcome == OUTCOME_FAILED
    assert memory.updates == [(resolved.recent[0].observation_id, OUTCOME_FAILED)]


async def test_a_job_list_without_a_total_is_judged_on_what_it_shows() -> None:
    """The replay gateway's recordings carry `total_count`; a source that does not is not
    penalised for it -- there is nothing to compare the page against."""
    resolved, _, _ = await _resolve({"jobs": [{"conclusion": "success"}]})
    assert resolved.recent[0].action_outcome == OUTCOME_PASSED


# ---------------------------------------------------------------------------
# Audit finding 7: one rule for "what counts as an action", for both executors
# ---------------------------------------------------------------------------


def _result(tool: str, *, ok: bool = True) -> ToolResult:
    return ToolResult(call_id="tc_0000000000ab", tool=tool, ok=ok, latency_ms=1)


def _diagnosis() -> Diagnosis:
    return Diagnosis.model_validate({**diagnosis_for("flaky_test"), "final_confidence": 0.9})


def test_action_observation_names_the_terminal_write() -> None:
    obs = action_observation(
        key=KEY, run_id=_run_id(0), diagnosis=_diagnosis(), job=_job(),
        executed=[_result("get_job_logs"), _result("create_branch"),
                  _result("create_or_update_file"), _result("open_pull_request")],
    )
    assert obs is not None
    assert obs.action_taken == "open_pull_request"
    assert obs.action_outcome == OUTCOME_PENDING
    assert obs.observation_id == observation_id_for(SID, _run_id(0))
    assert obs.verdict == "flaky_test" and obs.confidence == 0.9


def test_action_observation_is_none_for_no_plan_a_failed_plan_or_reads_only() -> None:
    kwargs: dict[str, Any] = dict(key=KEY, run_id=_run_id(0), diagnosis=_diagnosis(), job=_job())
    assert action_observation(executed=[], **kwargs) is None
    assert action_observation(
        executed=[_result("rerun_failed_jobs", ok=False)], **kwargs
    ) is None, "a retry that never happened must not consume the cap"
    assert action_observation(executed=[_result("get_job_logs")], **kwargs) is None
