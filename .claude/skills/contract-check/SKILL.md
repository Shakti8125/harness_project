---
name: contract-check
description: Diff the implemented Pydantic models and Protocols against PLAN.md Appendix A, field by field, and report every drift. Use after any parallel build wave, and whenever two components disagree about a shape.
---

# Contract check

Usage: `/contract-check [component]` — no argument checks everything implemented so far.

Four agents code against `PLAN.md` **Appendix A** in parallel. Drift between their readings
of it is the most likely defect in this project and the one the test suite catches last,
usually as a confusing runtime error two phases later. This check finds it in minutes.

## Method

For each model or Protocol in Appendix A that exists in code, compare:

| Property | What counts as drift |
|---|---|
| Field name | any difference at all, including `id` vs `run_id` |
| Type | `str` vs `RunId`, `int` vs `float`, missing `\| None` |
| `Literal[...]` members | a missing, extra, renamed or reordered member |
| Default | present in plan, absent in code, or a different value |
| Constraint | `ge`/`le`/`max_length`/`pattern` dropped or changed |
| Config | `extra="forbid"` and `frozen=True` present (only `RunState` is mutable) |
| Generic | `AgentResult` still `Generic[TOut]` |
| Protocol signature | parameter names, order, async-ness, return type |

A fast first pass:

```bash
uv run python - <<'PY'
import json, importlib, pydantic
for mod, names in [
    ("harness.contracts", ["RunRequest","Evidence","TokenUsage","AgentError","AgentResult",
                            "StageRecord","EscalationRecord","RunOutcome"]),
    ("harness.gateway",   ["ToolSpec","ToolCall","ToolError","ToolResult"]),
    ("harness.memory",    ["SignatureKey","SignatureRecord","Observation","MemoryQuery",
                            "MemoryHit","RunClaim"]),
    ("harness.evaluator", ["Claim","ClaimVerdict","EvaluationReport"]),
    ("harness.guardrails",["Condition","Rule","PolicySpec","ActionContext","PolicyDecision"]),
    ("harness.observability", ["Span","TraceResponse"]),
    ("harness.llm",       ["LlmRequest","RawLlmResponse"]),
    ("harness.recovery",  ["RetryPolicy","AttemptRecord"]),
    ("harness.context_manager", ["ContextBudget","ContextRequest","Section",
                                  "TruncationReport","ContextBundle"]),
]:
    m = importlib.import_module(mod)
    for n in names:
        c = getattr(m, n, None)
        if c is None:
            print(f"MISSING  {mod}.{n}"); continue
        f = {k: str(v.annotation) for k, v in c.model_fields.items()} if issubclass(c, pydantic.BaseModel) else {}
        print(f"{mod}.{n}: {json.dumps(f, indent=None)}")
PY
```

Then read the printed shapes against Appendix A by eye. Do the same for
`integrations.cicd.schemas` against A.11 and the gateway catalog against the A.4 table
(thirteen tools, each with `side_effect` and `idempotent` declared).

## Two checks beyond field shapes

**Gemini schema survival.** Every agent output model must survive flattening:

```bash
uv run python -c "
from harness.llm import to_gemini_schema
from integrations.cicd.schemas import InvestigationNotes, Diagnosis, RemediationPlan
for m in (InvestigationNotes, Diagnosis, RemediationPlan):
    to_gemini_schema(m); print('ok', m.__name__)"
```

A failure here is a contract problem, not an LLM problem — the model is too nested and must
be flattened per the plan's cross-cutting decision on structured output.

**Numeric constants.** No literal from PLAN.md's "Concrete numbers in one place" may be
inlined at a call site; each must come from `settings` or a named constant mirroring the
table. Grep for `0.70`, `0.75`, `0.85`, `0.92`, `120_000`, `0.6`, `999` outside those places.

## Report

```
## VERDICT        CLEAN | DRIFT
## Drift
   model.field — plan says X, code has Y — owner: <agent> — consequence: <what breaks>
## Missing        in Appendix A, not in code (expected for phases not yet built — say which)
## Extra          in code, not in Appendix A — scope creep or a needed addition to the plan
## Inlined constants   file:line → value → which table row it duplicates
```

If a contract in the plan is genuinely wrong, say so and propose the amended shape. Do not
let code and plan diverge silently — PLAN.md is the reference the user works from.
