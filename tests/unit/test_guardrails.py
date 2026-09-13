"""PLAN.md Phase 2, Verify step 3: forbidden actions are unreachable from every state.

Three named tests, plus the properties of the matcher that make the three mean anything:
a condition on a missing fact never matches, `<default>` explains itself, and the two
hardcoded invariants sit outside the YAML.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from src.harness.gateway import ToolCall
from src.harness.guardrails import (
    FACT_SIDE_EFFECTING_ACTIONS,
    MAX_SIDE_EFFECTING_ACTIONS_PER_RUN,
    RULE_DEFAULT,
    RULE_FORBIDDEN,
    RULE_INVARIANT,
    ActionContext,
    PolicyDecision,
    PolicyEngine,
    PolicySpec,
    downgrade_for_warn,
    load_policy,
)
from src.integrations.cicd.gateway_github import GitHubToolGateway
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import POLICY_PATH, load_forbidden

CATEGORIES = (
    "flaky_test", "real_regression", "dependency_break",
    "infra_transient", "config_issue", "unknown",
)
CONFIDENCES = (0.0, 0.5, 0.75, 0.9, 0.99)
VERDICTS = ("pass", "warn", "fail")
FORBIDDEN_TOOLS = ("merge_pull_request", "force_push", "delete_branch")


@pytest.fixture(scope="module")
def spec() -> PolicySpec:
    return load_policy(POLICY_PATH)


@pytest.fixture(scope="module")
def engine(spec: PolicySpec) -> PolicyEngine:
    return PolicyEngine(spec)


def facts(
    category: str = "flaky_test",
    confidence: float = 0.9,
    verdict: str = "pass",   # Phase 4: a real verdict exists; `skipped` matches no rule
    *,
    retries: int = 0,
    cold_start: bool = False,
    actions_so_far: int = 0,
) -> dict[str, object]:
    return {
        "diagnosis.category": category,
        "diagnosis.final_confidence": confidence,
        "evaluation.verdict": verdict,
        "memory.retries_for_signature_24h": retries,
        "context.cold_start": cold_start,
        FACT_SIDE_EFFECTING_ACTIONS: actions_so_far,
    }


def ctx(tool: str, side_effect: str, f: dict[str, object]) -> ActionContext:
    return ActionContext(tool=tool, side_effect=side_effect, facts=f)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Verify step 3, test 1: the 90-case matrix
# ---------------------------------------------------------------------------


def test_forbidden_denied_from_all_states(engine: PolicyEngine) -> None:
    """6 categories x 5 confidences x 3 verdicts = 90 states; every forbidden tool denies.

    The states are chosen to be the *most permissive* ones otherwise: retries at 0, no
    cold start, no action taken yet -- so nothing but the forbidden set is doing the work.
    """
    states = list(itertools.product(CATEGORIES, CONFIDENCES, VERDICTS))
    assert len(states) == 90
    for category, confidence, verdict in states:
        for tool in FORBIDDEN_TOOLS:
            decision = engine.decide(
                ctx(tool, "destructive", facts(category, confidence, verdict))
            )
            assert decision.effect == "deny", (category, confidence, verdict, tool)
            assert decision.rule_id == RULE_FORBIDDEN
            assert decision.obligations == []


def test_forbidden_beats_a_rule_that_would_otherwise_allow(spec: PolicySpec) -> None:
    """A YAML rule naming a forbidden tool with effect allow changes nothing."""
    permissive = spec.model_copy(
        update={
            "rules": [
                *spec.rules,
                spec.rules[-1].model_copy(
                    update={"id": "oops", "tools": ["merge_pull_request"], "effect": "allow"}
                ),
            ]
        }
    )
    decision = PolicyEngine(permissive).decide(
        ctx("merge_pull_request", "destructive", facts())
    )
    assert decision.effect == "deny"
    assert decision.rule_id == RULE_FORBIDDEN


# ---------------------------------------------------------------------------
# Verify step 3, test 2: the gateway refuses a forged allow, with zero HTTP calls
# ---------------------------------------------------------------------------


def forged_allow(tool: str) -> PolicyDecision:
    return PolicyDecision(
        tool=tool,
        rule_id="retry-suspected-flaky",   # a real rule id, so it is not obviously fake
        effect="allow",
        reason="hand-forged",
        evaluated_at=datetime.now(UTC),
    )


@respx.mock(assert_all_called=False)
async def test_gateway_refuses_forbidden_even_with_forged_allow_decision(
    respx_mock: respx.MockRouter,
) -> None:
    catch_all = respx_mock.route(host="api.github.com").mock(
        return_value=httpx.Response(200, json={})
    )
    gateway = GitHubToolGateway(
        repo="octo-org/harness-demo-repo",
        token="ghp_forged_test_token_0000000000000000000000",
        forbidden=load_forbidden(),
        dry_run=False,   # the strictest setting: a real merge would be sent if this failed
    )
    try:
        result = await gateway.invoke(
            ToolCall(call_id="tc_000000000001", tool="merge_pull_request", args={"number": 7}),
            forged_allow("merge_pull_request"),
        )
    finally:
        await gateway.aclose()

    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "forbidden_by_policy"
    assert result.error.retryable is False
    assert catch_all.call_count == 0, "a forbidden tool must reach the wire zero times"
    assert len(respx_mock.calls) == 0


@respx.mock(assert_all_called=False)
async def test_the_zero_http_property_is_not_vacuous(respx_mock: respx.MockRouter) -> None:
    """`merge_pull_request` has no live body, so it would reach the wire zero times even
    unguarded; the test above proves the *kind*, not the ordering. This one proves the
    ordering with a tool that does call out: forbid `rerun_failed_jobs`, forge an allow,
    and the run lookup it would otherwise make never happens -- then drop the forbidden
    set and watch the same call go out."""
    run_lookup = respx_mock.get(
        "https://api.github.com/repos/octo-org/harness-demo-repo/actions/runs/501234567"
    ).mock(return_value=httpx.Response(200, json={"id": 501234567, "run_attempt": 1}))
    call = ToolCall(
        call_id="tc_000000000003", tool="rerun_failed_jobs", args={"run_id": 501234567}
    )

    guarded = GitHubToolGateway(
        repo="octo-org/harness-demo-repo", token="t" * 40,
        forbidden=("rerun_failed_jobs",), dry_run=True,
    )
    try:
        refused = await guarded.invoke(call, forged_allow("rerun_failed_jobs"))
    finally:
        await guarded.aclose()
    assert refused.error is not None and refused.error.kind == "forbidden_by_policy"
    assert run_lookup.call_count == 0

    unguarded = GitHubToolGateway(
        repo="octo-org/harness-demo-repo", token="t" * 40, forbidden=(), dry_run=True,
    )
    try:
        allowed = await unguarded.invoke(call, forged_allow("rerun_failed_jobs"))
    finally:
        await unguarded.aclose()
    assert allowed.ok is True and allowed.dry_run is True
    assert run_lookup.call_count == 1


async def test_replay_gateway_refuses_forbidden_with_forged_allow(repo_root: Path) -> None:
    gateway = ReplayToolGateway(
        scenario_dir=repo_root / "fixtures" / "scenarios" / "real_regression",
        repo="octo-org/harness-demo-repo",
        forbidden=load_forbidden(),
    )
    result = await gateway.invoke(
        ToolCall(call_id="tc_000000000002", tool="delete_branch", args={"name": "main"}),
        forged_allow("delete_branch"),
    )
    assert result.ok is False
    assert result.error is not None and result.error.kind == "forbidden_by_policy"


def test_both_gateways_require_forbidden_explicitly() -> None:
    with pytest.raises(TypeError):
        GitHubToolGateway(repo="o/r", token="t")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Verify step 3, test 3: max one side-effecting action per run
# ---------------------------------------------------------------------------


def test_max_one_side_effecting_action_per_run(engine: PolicyEngine) -> None:
    """Once the run has acted, every side-effecting tool is denied by the invariant --
    even one a rule would allow -- and read tools are unaffected."""
    assert MAX_SIDE_EFFECTING_ACTIONS_PER_RUN == 1
    allowed_first = engine.decide(ctx("rerun_failed_jobs", "write", facts(actions_so_far=0)))
    assert allowed_first.effect == "allow"
    assert allowed_first.rule_id == "retry-suspected-flaky"

    second = engine.decide(ctx("rerun_failed_jobs", "write", facts(actions_so_far=1)))
    assert second.effect == "deny"
    assert second.rule_id == RULE_INVARIANT
    assert "hard cap" in second.reason

    still_read = engine.decide(ctx("get_commit", "read", facts(actions_so_far=1)))
    assert still_read.effect == "allow"
    assert still_read.rule_id == "read-only-always"


def test_action_cap_fails_closed_when_the_count_is_not_supplied(engine: PolicyEngine) -> None:
    """A caller that omits the count is not presumed to have taken none."""
    f = facts()
    del f[FACT_SIDE_EFFECTING_ACTIONS]
    decision = engine.decide(ctx("rerun_failed_jobs", "write", f))
    assert decision.effect == "deny"
    assert decision.rule_id == RULE_INVARIANT
    assert "not supplied" in decision.reason


def test_action_cap_is_not_expressible_in_yaml(spec: PolicySpec) -> None:
    """`PolicySpec` is `extra="forbid"`: there is no key that could relax the cap."""
    with pytest.raises(ValueError):
        PolicySpec.model_validate(
            {**spec.model_dump(by_alias=True), "max_side_effecting_actions_per_run": 5}
        )
    with pytest.raises(ValueError):
        PolicySpec.model_validate({**spec.model_dump(by_alias=True), "default_effect": "allow"})


# ---------------------------------------------------------------------------
# Matcher properties the three tests above rest on
# ---------------------------------------------------------------------------


def test_skipped_verdict_no_longer_matches_any_write_rule(engine: PolicyEngine) -> None:
    """Phase 2 amendment 2, closed in Phase 4: with a real verdict computed, `skipped`
    (a run with no citations, or one without the evaluate stage) matches neither write
    rule -- the default deny answers and names the clause. `file-ticket` still does."""
    for tool in ("create_branch", "create_or_update_file", "open_pull_request"):
        decision = engine.decide(ctx(tool, "write", facts("real_regression", 0.9, "skipped")))
        assert decision.effect == "deny", tool
        assert decision.rule_id == RULE_DEFAULT
        assert "evaluation.verdict: 'skipped'" in decision.reason
    retry = engine.decide(ctx("rerun_failed_jobs", "write", facts(verdict="skipped")))
    assert retry.effect == "deny" and retry.rule_id == RULE_DEFAULT
    ticket = engine.decide(ctx("create_issue", "write", facts(verdict="skipped")))
    assert ticket.effect == "allow" and ticket.rule_id == "file-ticket"


def test_warn_verdict_matches_both_write_rules_so_the_harness_can_downgrade(
    engine: PolicyEngine,
) -> None:
    """`warn` must *match* (PLAN.md Phase 4: "the run proceeds but every effect is
    downgraded one step"); the downgrade itself is `guardrails.downgrade_for_warn`,
    applied by the integration's `decide_plan`, not by the matcher."""
    retry = engine.decide(ctx("rerun_failed_jobs", "write", facts(verdict="warn")))
    assert retry.effect == "allow" and retry.rule_id == "retry-suspected-flaky"
    for tool in ("create_branch", "create_or_update_file", "open_pull_request"):
        decision = engine.decide(ctx(tool, "write", facts("real_regression", 0.9, "warn")))
        assert decision.effect == "require_approval", tool
        assert decision.rule_id == "open-fix-pr"
    fail = engine.decide(ctx("rerun_failed_jobs", "write", facts(verdict="fail")))
    assert fail.effect == "deny" and fail.rule_id == RULE_DEFAULT


def test_downgrade_for_warn_moves_allow_one_step_and_nothing_else(engine: PolicyEngine) -> None:
    allowed = engine.decide(ctx("rerun_failed_jobs", "write", facts(verdict="warn")))
    downgraded = downgrade_for_warn(allowed)
    assert downgraded.effect == "require_approval"
    assert downgraded.downgraded_from == "allow"
    assert downgraded.rule_id == "retry-suspected-flaky"
    assert downgraded.obligations == allowed.obligations
    assert "verdict warn" in downgraded.reason

    approval = engine.decide(ctx("create_branch", "write", facts("real_regression", 0.9, "warn")))
    assert downgrade_for_warn(approval) == approval
    assert downgrade_for_warn(approval).downgraded_from is None

    denied = engine.decide(ctx("merge_pull_request", "destructive", facts(verdict="warn")))
    assert downgrade_for_warn(denied) == denied


def test_retry_rule_allows_only_with_a_real_count_under_the_cap(engine: PolicyEngine) -> None:
    allowed = engine.decide(ctx("rerun_failed_jobs", "write", facts(retries=1)))
    assert allowed.effect == "allow"

    capped = engine.decide(ctx("rerun_failed_jobs", "write", facts(retries=2)))
    assert capped.effect == "deny"
    assert capped.rule_id == RULE_DEFAULT

    fail_closed = engine.decide(ctx("rerun_failed_jobs", "write", facts(retries=999)))
    assert fail_closed.effect == "deny"
    assert fail_closed.rule_id == RULE_DEFAULT


def test_default_reason_names_the_clause_that_failed(engine: PolicyEngine) -> None:
    """Amendment 3's point: the trace must say the cap bit, not merely that nothing matched."""
    decision = engine.decide(ctx("rerun_failed_jobs", "write", facts(retries=999)))
    assert "retry-suspected-flaky" in decision.reason
    assert "memory.retries_for_signature_24h: 999 fails {lt: 2" in decision.reason


def test_a_missing_fact_never_satisfies_a_clause(engine: PolicyEngine) -> None:
    """Fail closed: absence is not `0`, `false` or "not equal to anything"."""
    f = facts(retries=0)
    del f["memory.retries_for_signature_24h"]
    decision = engine.decide(ctx("rerun_failed_jobs", "write", f))
    assert decision.effect == "deny"
    assert decision.rule_id == RULE_DEFAULT
    assert "fact not supplied" in decision.reason

    # `ne` and `nin` fail on a missing fact too -- "unknown" is not "known not to be X".
    spec = PolicySpec.model_validate(
        {
            "version": 1, "integration": "t", "rules": [
                {"id": "ne-rule", "tools": ["x"], "effect": "allow",
                 "when": {"k": {"ne": "a"}}},
                {"id": "nin-rule", "tools": ["y"], "effect": "allow",
                 "when": {"k": {"nin": ["a"]}}},
            ],
        }
    )
    e = PolicyEngine(spec)
    assert e.decide(ctx("x", "read", {})).effect == "deny"
    assert e.decide(ctx("y", "read", {})).effect == "deny"
    assert e.decide(ctx("x", "read", {"k": "b"})).effect == "allow"


def test_cold_start_disables_auto_retry(engine: PolicyEngine) -> None:
    """Appendix D: `context.cold_start: {eq: false}` on `retry-suspected-flaky`."""
    decision = engine.decide(ctx("rerun_failed_jobs", "write", facts(cold_start=True)))
    assert decision.effect == "deny"
    assert "context.cold_start" in decision.reason


def test_booleans_are_not_numbers(engine: PolicyEngine) -> None:
    """`True >= 0.75` is true in Python; it must not be a confidence here."""
    decision = engine.decide(
        ctx("rerun_failed_jobs", "write", {**facts(), "diagnosis.final_confidence": True})
    )
    assert decision.effect == "deny"
    assert "not a number" in decision.reason


def test_read_wildcard_matches_side_effect_not_name(engine: PolicyEngine) -> None:
    assert engine.decide(ctx("anything_at_all", "read", {})).effect == "allow"
    assert engine.decide(ctx("get_commit", "write", {})).effect == "deny"


def test_first_match_wins_in_file_order() -> None:
    spec = PolicySpec.model_validate(
        {
            "version": 1, "integration": "t", "rules": [
                {"id": "first", "tools": ["x"], "effect": "require_approval"},
                {"id": "second", "tools": ["*"], "effect": "allow"},
            ],
        }
    )
    decision = PolicyEngine(spec).decide(ctx("x", "write", {FACT_SIDE_EFFECTING_ACTIONS: 0}))
    assert decision.rule_id == "first"
    assert decision.effect == "require_approval"


def test_unknown_tool_falls_to_default_deny(engine: PolicyEngine) -> None:
    decision = engine.decide(ctx("rm_rf_everything", "destructive", facts()))
    assert decision.effect == "deny"
    assert decision.rule_id == RULE_DEFAULT
    assert "no rule names this tool" in decision.reason


def test_load_policy_rejects_malformed_files(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 1\nintegration: t\nrules: []\nextra_key: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(bad)
    bad.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(bad)
    bad.write_text(
        "version: 1\nintegration: t\nrules:\n  - id: a\n    tools: [x]\n    effect: allow\n"
        "    when:\n      k: {gte: notanumber}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_policy(bad)


def test_duplicate_rule_ids_are_rejected() -> None:
    spec = PolicySpec.model_validate(
        {
            "version": 1, "integration": "t", "rules": [
                {"id": "dup", "tools": ["x"], "effect": "allow"},
                {"id": "dup", "tools": ["y"], "effect": "deny"},
            ],
        }
    )
    with pytest.raises(ValueError):
        PolicyEngine(spec)


def test_shipped_policy_is_the_amended_plan_text(spec: PolicySpec) -> None:
    """Pins the two facts the phase's Verify block depends on."""
    by_id = {rule.id: rule for rule in spec.rules}
    # Phase 4: `skipped` is out (Phase 2 amendment 2), `warn` is in so it can be downgraded.
    assert by_id["open-fix-pr"].when["evaluation.verdict"].in_ == ["pass", "warn"]
    assert by_id["retry-suspected-flaky"].when["evaluation.verdict"].in_ == ["pass", "warn"]
    assert by_id["retry-suspected-flaky"].when["memory.retries_for_signature_24h"].lt == 2
    assert set(spec.forbidden) == {
        "merge_pull_request", "force_push", "delete_branch",
        "delete_workflow_run", "create_deployment", "update_branch_protection",
    }
