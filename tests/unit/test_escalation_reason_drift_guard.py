# ruff: noqa: E501
"""`EscalationRecord.reason` (contracts.py) and `Orchestrator`'s `EscalationReason` alias
(orchestrator.py) are two independently-declared `Literal`s that mypy cannot cross-check
against each other -- they live in different modules and neither references the other's
type. The dangerous drift direction is the alias growing a member the contract does not
have: `_OUTCOME_FOR_ERROR_KIND` would then map some error kind onto a reason string that
fails `EscalationRecord` validation the moment a run actually tries to escalate that way,
which is a `ValidationError` surfacing at the worst possible time rather than at review
time or in CI.

This is a synchronous, structural test, not a scenario: it reads both `Literal`s back out
of the type system and asserts their member sets are identical. Wanted most, per
harness-core's own report on this fix round -- the `llm_upstream` widening is the reason
this test exists at all.
"""

from __future__ import annotations

from typing import get_args, get_type_hints

from src.harness.contracts import EscalationRecord
from src.harness.orchestrator import EscalationReason


def test_orchestrator_escalation_reason_matches_the_contract_exactly() -> None:
    contract_members = set(get_args(get_type_hints(EscalationRecord)["reason"]))
    alias_members = set(get_args(EscalationReason))

    assert alias_members == contract_members


def test_llm_upstream_is_a_member_of_both() -> None:
    """The specific widening this fix round made -- pinned by name, not just by set."""
    contract_members = set(get_args(get_type_hints(EscalationRecord)["reason"]))
    alias_members = set(get_args(EscalationReason))

    assert "llm_upstream" in contract_members
    assert "llm_upstream" in alias_members


def test_evidence_unverifiable_is_a_member_of_both() -> None:
    """Phase 5 (A.1 amended): the share-rule `fail` has its own reason now."""
    contract_members = set(get_args(get_type_hints(EscalationRecord)["reason"]))
    alias_members = set(get_args(EscalationReason))

    assert "evidence_unverifiable" in contract_members
    assert "evidence_unverifiable" in alias_members


def test_the_remediation_gate_names_the_reason_the_verdicts_support() -> None:
    """A `fail` with any refuted claim is `evidence_refuted`; a `fail` on the share rule
    alone -- nothing refuted, too little verified -- is `evidence_unverifiable`. The gate
    reads the report's counts, not its verdict string alone (Phase 4 audit S2)."""
    from src.harness.contracts import RunRequest
    from src.harness.evaluator import ClaimVerdict, EvaluationReport
    from src.harness.orchestrator import RunState
    from src.integrations.cicd.schemas import Diagnosis
    from src.integrations.cicd.wiring import make_remediation_gate

    gate = make_remediation_gate(0.70)
    diagnosis = Diagnosis(
        reasoning="r", category="real_regression", summary="s", self_confidence=0.9,
        citations=[], suggested_action="open_fix_pr", final_confidence=0.9,
    )

    def report(*, refuted: int, unverifiable: int, reason: str) -> EvaluationReport:
        verdicts = [
            ClaimVerdict(claim_id=f"cl_{i}", kind="quote_exists", result="refuted", detail="x") for i in range(refuted)
        ] + [
            ClaimVerdict(claim_id=f"cl_u{i}", kind="quote_exists", result="unverifiable", detail="no log")
            for i in range(unverifiable)
        ]
        return EvaluationReport(
            verdicts=verdicts, verified=0, refuted=refuted, unverifiable=unverifiable,
            verdict="fail", confidence_delta=-0.15 if refuted else 0.0, reason=reason,
        )

    def state(evaluation: EvaluationReport) -> RunState:
        return RunState(
            run_id="run_01J8ABCDEFGHJKMNPQRSTVWXYZ",
            request=RunRequest(integration="cicd", subject={}, idempotency_key="cicd:gate-test"),
            artifacts={"diagnosis": diagnosis, "evaluation": evaluation},
            degraded=[], stages=[],
        )

    refuted = gate(state(report(refuted=1, unverifiable=0, reason="1 of 1 claim(s) refuted")))
    assert not refuted.proceed and refuted.escalate_as == "evidence_refuted"
    assert refuted.reason == "evaluator verdict fail: 1 of 1 claim(s) refuted"

    share = gate(state(report(refuted=0, unverifiable=1, reason="only 0 of 1 claim(s) verified")))
    assert not share.proceed and share.escalate_as == "evidence_unverifiable"
    assert share.reason == "evaluator verdict fail: only 0 of 1 claim(s) verified"
