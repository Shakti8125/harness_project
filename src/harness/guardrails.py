"""Declarative action policy.

Frozen transcription of PLAN.md Appendix A.7. The engine matches a supplied
:class:`ActionContext` against a supplied :class:`PolicySpec`; both the fact namespace
and the rule file belong to the integration. The engine itself contains no rule about
any particular tool -- with one exception, recorded below as a hardcoded invariant
because PLAN.md marks it explicitly not overridable by config.

Evaluation order: ``forbidden`` -> hardcoded invariants -> first matching rule in file
order -> ``default_effect``. First match wins, and the matching rule id is always
reported.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# PLAN.md "Concrete numbers in one place": hard cap on side-effecting actions per run,
# set here rather than in a policy file and explicitly not overridable by config.
MAX_SIDE_EFFECTING_ACTIONS_PER_RUN: Final[int] = 1


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    eq: JsonValue | None = None
    ne: JsonValue | None = None
    in_: list[JsonValue] | None = Field(None, alias="in")
    nin: list[JsonValue] | None = None
    gte: float | None = None
    gt: float | None = None
    lte: float | None = None
    lt: float | None = None


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    tools: list[str]                       # exact names, "read:*", or "*"
    effect: Literal["allow", "require_approval", "deny"]
    when: dict[str, Condition] = {}        # dotted keys into ActionContext.facts
    obligations: list[str] = []


class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    integration: str
    default_effect: Literal["deny"] = "deny"
    forbidden: list[str] = []
    rules: list[Rule]


class ActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    side_effect: Literal["read", "write", "destructive"]
    facts: dict[str, JsonValue]            # flat dotted keys; namespace chosen by the caller


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    rule_id: str                           # "<default>" or "<forbidden>" when no rule matched
    effect: Literal["allow", "require_approval", "deny"]
    reason: str
    obligations: list[str] = []
    evaluated_at: datetime
    downgraded_from: str | None = None     # set when an Evaluator "warn" downgraded the effect


class PolicyEngine:
    """Decides whether a proposed action is allowed, needs approval, or is denied."""

    def __init__(self, spec: PolicySpec) -> None:
        raise NotImplementedError

    def decide(self, ctx: ActionContext) -> PolicyDecision:
        raise NotImplementedError
