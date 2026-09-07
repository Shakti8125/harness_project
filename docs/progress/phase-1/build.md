# Phase 1 — build notes

Investigator + Diagnostician, end to end, over HTTP.

## How this phase was actually run

The three-wave agent protocol was started (Wave 1a: `harness-core` signature pass +
`fixtures-eval`) and **both agents were stopped by the user mid-flight**. `harness-core`
had landed `src/harness/agent.py` and the constant block in `llm.py` before it was
stopped; `fixtures-eval` had done nothing. The remainder was built directly in the main
session rather than by re-dispatching agents the user had just killed.

That changes who wrote the code, not what it had to satisfy: the ownership rulings in
`dispatch.md` were followed as written, the territories were respected, and the Verify
block is the same gate.

## The three contract gaps, as resolved

| Gap | Resolution |
|---|---|
| `harness/agent.py` — no Appendix A section defines it | `Agent[TOut]` Protocol + `LLMAgent[TOut]` base, generic over the payload. Marked DERIVED in its own docstring. `cicd`'s two agents subclass `LLMAgent`; neither sketched a base class. |
| `Orchestrator` / `ContextManager` / `TraceRecorder` constructors | All three written before anything consumed them. `deps.py` constructs all three and nothing else does. |
| `SpanHandle` + the trace read path | `SpanHandle` finalised as a Protocol (`span_id`, `set_attribute`, `set_error`, `set_tokens`). The read path is **`TraceRecorder.read_trace`**, per the dispatch ruling — `MemoryStore` does not exist until Phase 3, and the recorder already owns span persistence. |

## Deviations from Appendix A — recorded, not discovered

1. **`TraceRecorder.span` is `async def`.** A.9 writes `@asynccontextmanager def span(...)`.
   `asynccontextmanager` decorates an *async generator*, so `async def` is forced. No
   caller is affected: `async with recorder.span(...)` reads identically. (This was the
   deviation the Phase 0 handoff asked to be recorded here rather than found in Phase 5.)
2. **`retry_structured` cannot turn two of Appendix B.1's knobs.** Its signature is frozen
   and `call` takes only a prompt, so `max_output_tokens x 1.5` on `MAX_TOKENS` and
   "re-assemble context at budget x 0.5" on a 400 are both unreachable. Both conditions
   are handled with the one knob available — the prompt: `MAX_TOKENS` retries with an
   explicit brevity instruction, and an oversized request retries with the prompt halved
   head-and-tail. Same attempt budgets, same terminal error kinds. Details in the module
   docstring.
3. **`Adjustment` in PLAN.md Appendix A.11 was a stale duplicate listing.** Replaced with
   the import that is actually the contract. This was the one PLAN.md edit authorised
   this phase.

## Decisions worth knowing about

**The confidence gate lives on the `remediate` stage, which has no agent yet.** A.2 makes
the short-circuit a `StageSpec.gate` on the remediate stage. Phase 1 has no Remediator, so
the stage is declared with `required=False` and its gate, and the orchestrator runs the
gate, then records the stage as `skipped`. The escalation path is therefore live and
tested now, and Phase 2 fills in the agent without moving the condition. A `required`
stage with no agent raises at construction, so this cannot silently swallow a typo.

**The trace's ambient run id.** The composition root builds one `TraceRecorder` and hands
it to every agent at startup, long before a run id exists. Binding only inside the
orchestrator produced a trace containing the `run` span and nothing else — the agents'
spans had no run to belong to and were dropped. `TraceRecorder.run_scope(run_id)` now
publishes the id in a `ContextVar` that unbound recorders read. Caught by
`test_trace_is_persisted_and_readable`, which asserts the span tree, not just a 200.

**Token totals are summed from `llm` spans only.** The same counts appear again on the
enclosing `agent` span as a per-stage roll-up; summing both doubled every figure.

**The Investigator's stage succeeds even when its model call fails.** The bundle is
emitted with `notes=None` and `investigator_notes` in `degraded_components`. The
deterministic collection is the load-bearing half and has already happened; the
Diagnostician escalates on its own if the model is genuinely unreachable.

**Run outcomes live in an in-process dict** (`src/api/run_registry.py`), not a new table.
`MemoryStore.save_run` is the plan's home for this and arrives in Phase 3; building a
parallel schema now means designing it twice. Cost, stated: outcomes do not survive a
restart. The span trace *does* — it is in SQLite.

**Phase 1 synthesizes the `read-only-always` policy decision.** There is no `PolicyEngine`
until Phase 2, and every tool this phase calls is read-only, which `policy.yaml` allows
unconditionally. The synthesized decision carries `rule_id="read-only-always"` so it
cannot be mistaken in a trace for an engine-issued one, and the gateway re-checks the
forbidden set regardless — that check is tested against a forged `allow`.

## Carry-forward for Phase 2

- The `remediate` stage is already declared with its gate. Registering a `remediator`
  agent under that key and flipping `required=True` is the whole wiring change.
- `ReplayToolGateway.catalog()` returns read tools only. The write half arrives with the
  Remediator — that is the first thing that could call one.
- `readyz` still reports `policy_loaded: False` and will until the engine loads
  `policy.yaml`. `load_forbidden()` in `wiring.py` reads exactly one key from that file
  and should be absorbed by the real loader rather than left as a second reader.
- `parse_dependency_changes` covers pip/npm/go/cargo manifests and lockfiles; `maven` is
  recognised as an ecosystem but not parsed. Nothing in the four canned scenarios needs
  it, so it reports nothing rather than guessing.
