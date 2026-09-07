# Phase 1 — dispatch decisions (written BEFORE Wave 1)

Normative for this phase. Where this file and an agent's instinct disagree, this file wins.
Settled upstream, not open questions — do not re-derive, do not re-litigate:

- **`Diagnosis.reasoning` and `RemediationPlan.rationale` are FIRST**, via `propertyOrdering`
  derived from Pydantic field order by `to_gemini_schema()`. This is already landed in
  Appendix A.11 itself. Reference it; never "fix" it back.
- **`Adjustment` is defined in `src/harness/confidence.py`, NOT in
  `src/integrations/cicd/schemas.py`.** `schemas.py` imports and re-exports it. The literal
  `class Adjustment` listing inside Appendix A.11 is **stale documentation** predating the
  Phase 0 fix. See ruling 4 below.

## Sole-owner assignments for the three carried-forward contract gaps

Appendix A does not define these. They get built for real this phase. The owner is
exclusive: no other agent defines, sketches, stubs or "temporarily" duplicates them.

### 1. `src/harness/agent.py` — the Agent protocol. Owner: **harness-core**.
PLAN.md:306 puts `harness/agent.py` in Phase 1's Built list and PLAN.md:96 describes it as
"Agent protocol, LLMAgent base". No Appendix A section specifies it at all.

- harness-core defines `Agent` (Protocol) and `LLMAgent` (base), generic over the output
  model, returning `AgentResult[TOut]` (A.1).
- **cicd-integration** `agents/investigator.py` and `agents/diagnostician.py` **import and
  implement it**. They do NOT write their own base class, their own `run()` signature, or a
  local Protocol "until harness-core lands". If the signature is not yet in hand, block and
  ask — do not sketch.
- api-surface and fixtures-eval: do not define an agent abstraction of any kind.

Two conflicting `Agent` protocols in one parallel wave is the single most likely way to
repeat Phase 0's `Adjustment` break.

### 2. Constructors: `Orchestrator.__init__`, `ContextManager.__init__`, `TraceRecorder.__init__`.
Owner: **harness-core**. A.2 / A.3 / A.9 specify methods only — no constructors anywhere.

`src/api/deps.py` (the composition root) constructs all three, so **api-surface consumes
these signatures and must not guess them**. Sequencing is mandatory: harness-core publishes
the exact signatures in Wave 1a; api-surface does not begin wiring `deps.py` until it has
them in hand. This is deliberately NOT a blind parallel wave.

### 3. `SpanHandle` + the trace read path. Owner: **harness-core**. Same sequencing as #2.
- `SpanHandle` is referenced by A.9 only as the yield type of `TraceRecorder.span()` and is
  defined nowhere. Phase 0 derived a minimal Protocol in `observability.py`; harness-core
  finalises it.
- A.12 requires `GET /v1/runs/{run_id}/trace -> TraceResponse`, and nothing in the tree
  returns a `TraceResponse`. **Ruling, made here in writing so neither agent assumes the
  other built it: the read path belongs to `TraceRecorder`, not `MemoryStore`.**
  `TraceRecorder` already owns span persistence to SQLite this phase; `MemoryStore` does not
  exist until Phase 3. harness-core implements the read method; api-surface wires the route
  to it.

## 4. The stale Appendix A.11 `Adjustment` listing. Owner: **cicd-integration**.

`src/integrations/cicd/schemas.py:21` already does the right thing
(`from src.harness.confidence import Adjustment`). Appendix A.11 still shows a duplicate
`class Adjustment(BaseModel): name: str; delta: float; reason: str`, which is what a future
agent would transcribe from. Whoever touches `schemas.py` fixes the listing; that is
cicd-integration.

**Narrow write exception:** cicd-integration may edit PLAN.md for this one listing only —
replace the duplicate class body with the import line, keeping the surrounding listing
untouched. We are still close enough to the freeze point that this is a one-line
documentation correction, not a re-freeze. No other PLAN.md edit is authorised for any agent
this phase.

## Environment (verified at dispatch time, 2026-09-06)

| Tool | State |
|---|---|
| `jq` | 1.8.2 — present |
| `docker` | 29.4.3, daemon responding |
| `uv` | 0.12.10 — present (`export PATH="/c/Users/Shakti/.local/bin:$PATH"` in Bash) |
| `HARNESS_GEMINI_API_KEY` | **PLACEHOLDER** (`AIza-local-placeholder-not-a-real-key`) — no live LLM |
| `flyctl` | **NOT INSTALLED** |

Consequence, flagged now rather than discovered in Wave 2: Verify steps 2, 3 and 4 issue a
real Gemini call (replay mode stubs the *gateway*, not the LLM — the fixture format records
no LLM responses), and step 5 is `fly deploy`. Those four are **BLOCKED on environment, not
FAIL on code**, unless a real key and a Fly account arrive. Verify step 1
(`test_context_manager.py`, including `test_error_lines_never_trimmed`) is fully runnable and
is the load-bearing test of this phase.

**No agent may invent a fake/canned LLM client to route around this.** PLAN.md does not
specify one in Phase 1, the fixture format has no slot for recorded LLM responses, and a
stubbed LLM would make the e2e verification assert nothing. Build against the real
`GeminiClient`; let the gate report BLOCKED honestly.
