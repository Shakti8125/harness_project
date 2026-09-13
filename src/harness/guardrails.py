"""Declarative action policy.

Frozen transcription of PLAN.md Appendix A.7. The engine matches a supplied
:class:`ActionContext` against a supplied :class:`PolicySpec`; both the fact namespace
and the rule file belong to the integration. The engine itself contains no rule about
any particular tool -- with one exception, recorded below as a hardcoded invariant
because PLAN.md marks it explicitly not overridable by config.

Evaluation order: ``forbidden`` -> hardcoded invariants -> first matching rule in file
order -> ``default_effect``. First match wins, and the matching rule id is always
reported.

Three properties of the matcher are load-bearing and are stated here rather than left to
be inferred from the code:

1. **A condition on a fact the caller did not supply never matches.** ``when`` clauses are
   conjunctions over ``ActionContext.facts``; a missing key is not treated as ``None``,
   ``0`` or ``false``, it simply fails the clause, so the rule falls through and the
   ``default_effect`` (always ``deny``) answers. A rule cannot be satisfied by omission.
2. **The ``<default>`` decision explains itself.** When no rule matched, the reason names
   every rule that *named the tool* and the first clause of it that failed, with the
   fact's actual value. The trace then shows "the retry cap bit" as a sentence rather
   than as an unmatched rule id, which is the difference between a cap that is enforced
   and one that merely looks enforced (PLAN.md Phase 2, amendment 3).
3. **The two hardcoded invariants sit outside the YAML entirely.** A tool in
   ``forbidden`` is denied before any rule is consulted, and a side-effecting tool is
   denied once the run has already taken ``MAX_SIDE_EFFECTING_ACTIONS_PER_RUN`` actions
   (fact ``run.side_effecting_actions_so_far``; an *action* is one executed set of tool
   calls, not one call -- a single approved action may legitimately need several write
   calls, and the cap is on how many times a run acts). Neither can be relaxed by editing
   a policy file, which is exactly why they are not in one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import yaml
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


def downgrade_for_warn(decision: PolicyDecision) -> PolicyDecision:
    """PLAN.md Phase 4: an Evaluator `warn` downgrades every effect one step.

    `allow` -> `require_approval`, `require_approval` stays, `deny` stays. The only writer
    of `downgraded_from`, and it writes it only when the effect actually moved, so a
    reader of the trace sees a downgrade exactly where one happened. The *trigger* -- that
    the run's evaluation verdict was `warn` -- is the caller's to decide: the verdict
    lives in the integration's fact namespace, the ladder lives here with the vocabulary
    it rewrites.
    """
    if decision.effect != "allow":
        return decision
    return decision.model_copy(
        update={
            "effect": "require_approval",
            "downgraded_from": decision.effect,
            "reason": (
                f"{decision.reason}; downgraded from allow: the evaluator could not "
                "verify every citation (verdict warn)"
            ),
        }
    )


#: Fact the side-effecting-actions invariant reads. Named once so the engine and every
#: caller that supplies it spell it identically; a misspelt key would read as "not
#: supplied", which fails closed (see `PolicyEngine.decide`) rather than silently as 0.
FACT_SIDE_EFFECTING_ACTIONS: Final[str] = "run.side_effecting_actions_so_far"

#: `PolicyDecision.rule_id` values the engine reports when no YAML rule decided. Angle
#: brackets keep them out of the namespace a rule `id:` can occupy.
RULE_FORBIDDEN: Final[str] = "<forbidden>"
RULE_INVARIANT: Final[str] = "<invariant>"
RULE_DEFAULT: Final[str] = "<default>"

#: Matches any tool. `"read:*"` is the other wildcard, matched on `ActionContext.side_effect`.
_ANY_TOOL: Final[str] = "*"
_READ_TOOLS: Final[str] = "read:*"


def load_policy(path: Path) -> PolicySpec:
    """Parse and validate one policy file.

    Deliberately raises on anything short of a well-formed `PolicySpec`: a policy that
    fails to load must stop the process at startup, not degrade into a gateway that
    refuses nothing. The caller decides how loudly (`readyz` reports `policy_loaded`).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return PolicySpec.model_validate(raw)


def _is_number(value: JsonValue) -> bool:
    # `bool` is an `int` subclass; `true >= 0.75` must not read as a confidence.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same(a: JsonValue, b: JsonValue) -> bool:
    """Equality that refuses `True == 1`; the fact namespace mixes booleans and numbers."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _failed_clause(condition: Condition, value: JsonValue | None, present: bool) -> str | None:
    """The first operator of `condition` that `value` does not satisfy, or None if all do.

    Returns a short human-readable clause for the `<default>` reason. Every operator is a
    conjunct; a missing fact fails every operator, including `ne` and `nin`, because
    "the fact is unknown" is not the same as "the fact is known not to be X".
    """
    if not present:
        return "fact not supplied"
    if condition.eq is not None and not _same(value, condition.eq):
        return f"{value!r} fails {{eq: {condition.eq!r}}}"
    if condition.ne is not None and _same(value, condition.ne):
        return f"{value!r} fails {{ne: {condition.ne!r}}}"
    if condition.in_ is not None and not any(_same(value, item) for item in condition.in_):
        return f"{value!r} fails {{in: {condition.in_!r}}}"
    if condition.nin is not None and any(_same(value, item) for item in condition.nin):
        return f"{value!r} fails {{nin: {condition.nin!r}}}"
    numeric: tuple[tuple[str, float | None, bool], ...] = ()
    if _is_number(value):
        assert isinstance(value, int | float)  # narrowed by `_is_number`
        numeric = (
            ("gte", condition.gte, condition.gte is None or value >= condition.gte),
            ("gt", condition.gt, condition.gt is None or value > condition.gt),
            ("lte", condition.lte, condition.lte is None or value <= condition.lte),
            ("lt", condition.lt, condition.lt is None or value < condition.lt),
        )
    else:
        numeric = (
            ("gte", condition.gte, condition.gte is None),
            ("gt", condition.gt, condition.gt is None),
            ("lte", condition.lte, condition.lte is None),
            ("lt", condition.lt, condition.lt is None),
        )
    for name, threshold, holds in numeric:
        if not holds:
            qualifier = "" if _is_number(value) else " (not a number)"
            return f"{value!r}{qualifier} fails {{{name}: {threshold!r}}}"
    return None


class PolicyEngine:
    """Decides whether a proposed action is allowed, needs approval, or is denied."""

    def __init__(self, spec: PolicySpec) -> None:
        self.spec = spec
        self.forbidden: frozenset[str] = frozenset(spec.forbidden)
        self._rules_by_id: dict[str, Rule] = {rule.id: rule for rule in spec.rules}
        duplicates = len(spec.rules) - len(self._rules_by_id)
        if duplicates:
            # A second rule with the same id would be unreachable by lookup and, worse,
            # would let a trace quote the wrong rule under a shared name.
            raise ValueError(f"policy {spec.integration!r} has {duplicates} duplicate rule id(s)")

    def rule(self, rule_id: str) -> Rule | None:
        """The rule a decision quoted, so a caller can put its text in the trace verbatim."""
        return self._rules_by_id.get(rule_id)

    def _tool_matches(self, rule: Rule, ctx: ActionContext) -> bool:
        for pattern in rule.tools:
            if pattern == _ANY_TOOL or pattern == ctx.tool:
                return True
            if pattern == _READ_TOOLS and ctx.side_effect == "read":
                return True
        return False

    def _first_failure(self, rule: Rule, facts: Mapping[str, JsonValue]) -> str | None:
        """`None` when every `when` clause holds; otherwise which one failed and how."""
        for key, condition in rule.when.items():
            present = key in facts
            failure = _failed_clause(condition, facts.get(key), present)
            if failure is not None:
                return f"{key}: {failure}"
        return None

    def decide(self, ctx: ActionContext) -> PolicyDecision:
        now = datetime.now(UTC)

        # 1. The forbidden set, before anything else. Not overridable by any rule below,
        #    and not by the caller's facts either -- nothing after this line is consulted.
        if ctx.tool in self.forbidden:
            return PolicyDecision(
                tool=ctx.tool,
                rule_id=RULE_FORBIDDEN,
                effect="deny",
                reason=f"{ctx.tool!r} is in the forbidden set; denied unconditionally",
                evaluated_at=now,
            )

        # 2. Hardcoded invariants. `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN` applies to every
        #    tool that is not a read, and the count must be *supplied*: a caller that does
        #    not say how many actions the run has taken is not granted the benefit of
        #    "presumably none" -- that is the fail-open PLAN.md's amendment 3 closes one
        #    layer up, and the invariant would otherwise look enforced while being
        #    unreachable.
        if ctx.side_effect != "read":
            taken = ctx.facts.get(FACT_SIDE_EFFECTING_ACTIONS)
            if not _is_number(taken):
                return PolicyDecision(
                    tool=ctx.tool,
                    rule_id=RULE_INVARIANT,
                    effect="deny",
                    reason=(
                        f"{FACT_SIDE_EFFECTING_ACTIONS} was not supplied for a "
                        f"{ctx.side_effect} tool; the per-run action cap cannot be "
                        "checked, so the action is denied"
                    ),
                    evaluated_at=now,
                )
            assert isinstance(taken, int | float)  # narrowed by `_is_number`
            if taken >= MAX_SIDE_EFFECTING_ACTIONS_PER_RUN:
                return PolicyDecision(
                    tool=ctx.tool,
                    rule_id=RULE_INVARIANT,
                    effect="deny",
                    reason=(
                        f"the run has already taken {int(taken)} side-effecting "
                        f"action(s); the hard cap is {MAX_SIDE_EFFECTING_ACTIONS_PER_RUN} "
                        "per run and is not overridable by policy"
                    ),
                    evaluated_at=now,
                )

        # 3. First matching rule, in file order.
        near_misses: list[str] = []
        for rule in self.spec.rules:
            if not self._tool_matches(rule, ctx):
                continue
            failure = self._first_failure(rule, ctx.facts)
            if failure is None:
                return PolicyDecision(
                    tool=ctx.tool,
                    rule_id=rule.id,
                    effect=rule.effect,
                    reason=f"matched rule {rule.id!r}",
                    obligations=list(rule.obligations),
                    evaluated_at=now,
                )
            near_misses.append(f"{rule.id!r} did not match ({failure})")

        # 4. Nothing matched. Say which rules named this tool and why each fell through,
        #    so the trace reads as a decision rather than as an absence of one.
        detail = "; ".join(near_misses) if near_misses else "no rule names this tool"
        return PolicyDecision(
            tool=ctx.tool,
            rule_id=RULE_DEFAULT,
            effect=self.spec.default_effect,
            reason=f"no rule matched, default_effect is {self.spec.default_effect!r}: {detail}",
            evaluated_at=now,
        )
