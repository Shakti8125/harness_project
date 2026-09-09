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
