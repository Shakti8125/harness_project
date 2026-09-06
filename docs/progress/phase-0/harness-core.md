# Phase 0 — harness-core

## Summary

`src/harness/` now exists as a frozen contract surface: 13 modules, 42 Pydantic models, 5
Protocols, 4 concrete stub classes and 3 module-level functions, transcribed field-for-field
from PLAN.md Appendix A.1–A.10 with no method bodies (`raise NotImplementedError` for concrete
members, `...` for Protocol members, exactly as Appendix A writes them). Two modules named by
the repository layout but absent from Appendix A — `confidence.py` and `errors.py` — are
*derived* rather than transcribed, and their derivation is spelled out below for audit. The
layer does not leak: an AST import scan finds nothing outside stdlib + pydantic, and the
PLAN.md line-66 denylist (`github`, `workflow_run`, `pytest`, `pull_request`, `ci`) appears
nowhere in `src/harness/**`, neither as an import target nor as a string literal nor anywhere
in the raw file text. `ruff check src/harness` and `mypy --strict src/harness` are both clean
on Python 3.12.14 / pydantic 2.13.5. **There are zero contract deviations.**

## Files written

- `src/harness/__init__.py` — package marker; deliberately re-exports nothing.
- `src/harness/contracts.py` — A.1: `RunId`, `RunRequest`, `Evidence`, `TokenUsage`, `AgentError`, `TOut`, `AgentResult[TOut]`, `StageRecord`, `EscalationRecord`, `RunOutcome`.
- `src/harness/orchestrator.py` — A.2: `StageSpec`, `GateDecision`, `RunState`, `Orchestrator`; heartbeat constants.
- `src/harness/context_manager.py` — A.3: `ContextBudget`, `ContextRequest`, `Section`, `TruncationReport`, `ContextBundle`, `ContextManager`; budget constants.
- `src/harness/gateway.py` — A.4: `ToolSpec`, `ToolCall`, `ToolError`, `ToolResult`, `ToolGateway` Protocol. No tool catalog: that is the integration's.
- `src/harness/memory.py` — A.5: `SignatureKey`, `SignatureRecord`, `Observation`, `MemoryQuery`, `MemoryHit`, `MemoryStore` Protocol, `RunClaim`; SQLite and prior-strength constants.
- `src/harness/evaluator.py` — A.6: `Claim`, `ClaimVerdict`, `EvaluationReport`, `ClaimChecker` Protocol, `Evaluator`; `MIN_VERIFIED_SHARE`.
- `src/harness/guardrails.py` — A.7: `Condition`, `Rule`, `PolicySpec`, `ActionContext`, `PolicyDecision`, `PolicyEngine`; `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN`.
- `src/harness/recovery.py` — A.8: `RetryPolicy`, `AttemptRecord`, `retry_structured`; attempt/backoff constants.
- `src/harness/observability.py` — A.9: `Span`, `TraceResponse`, `TraceRecorder`, `Redactor`, plus derived `SpanHandle` and `SecretRegistry`; `REDACTION_PLACEHOLDER`.
- `src/harness/llm.py` — A.10: `LlmRequest`, `RawLlmResponse`, `LlmClient` Protocol, `to_gemini_schema`.
- `src/harness/confidence.py` — DERIVED: `Adjustment`, `ConfidenceModel`, `calibrate()`, `DEFAULT_ADJUSTMENT_DELTAS`, clamp bounds.
- `src/harness/errors.py` — DERIVED: `HarnessError`, `ConfigurationError`, `PolicySpecError`, `SchemaTranslationError`, `ContractViolationError`.

## Contract deviations

**None.** Every field name, type, `Literal` member, default and constraint in Appendix A.1–A.10
is reproduced exactly. Verified by a model-by-model field-count and config audit (42/42 models,
all `frozen=True, extra="forbid"` except `RunState`, which is `frozen=False, extra="allow"` as
the plan requires).

Three changes are *typing precision required by `mypy --strict`*, not contract changes — they
alter no name, no runtime type and no default, but a reviewer should still see them:

| Where | Appendix A | Written as | Why |
|---|---|---|---|
| `Redactor.__init__` | `patterns: Sequence[re.Pattern]` | `Sequence[re.Pattern[str]]` | bare generic is a strict-mode error; `str` is the only possible parameter given `scrub` operates on strings |
| `TraceRecorder.span` | `**attrs` | `**attrs: JsonValue` | unannotated `**kwargs` is a strict-mode error; `JsonValue` matches `Span.attributes: dict[str, JsonValue]` |
| forward refs | `list["Section"]`, `Callable[["RunState"], "GateDecision"]`, `-> "RunClaim"` | unquoted | ruff UP037; `from __future__ import annotations` makes all annotations lazy, so this is textual only |

Defaults that also appear in PLAN.md "Concrete numbers in one place" are written as named
module constants rather than inline literals, per the standing rule that the table has one
representation in code. **The values are identical to Appendix A** and were asserted at
runtime: `ContextBudget()` gives `total_chars=120000, anchor_window_lines=20, head_lines=200,
tail_lines=400, reserve_chars=8000`; `RetryPolicy()` gives `max_attempts=3,
transient_max_attempts=4, backoff_base_s=0.5, backoff_max_s=8.0, timeout_s=60.0`;
`LlmRequest(...).timeout_s == 60.0`.

## Where Appendix A was silent or self-inconsistent, and what I chose

This is the audit-critical section. Each item is either DERIVED (invented shape, needs sign-off)
or a GAP (deliberately not invented, needs a later contract addendum).

1. **`SpanHandle` — DERIVED.** A.9 makes it the yield type of `TraceRecorder.span` and never
   defines it. Written as a `Protocol` with the minimum its use implies: `span_id: str` (so a
   child span can parent itself), `set_attribute(key: str, value: JsonValue) -> None`, and
   `set_error(error: dict[str, JsonValue]) -> None` (mirroring `Span.error`). Flagged in the
   module docstring. **Needs reviewer sign-off; Phase 5 will exercise it.**

2. **`SecretRegistry` — DERIVED.** Named in the repository layout and referenced by
   `Redactor.__init__`, but never defined. Written as a plain class:
   `__init__(values: Iterable[str] = ())`, `register(name: str, value: str) -> None`,
   `registered_values() -> frozenset[str]`. Two things PLAN.md describes were deliberately
   left *out* of the harness:
   - the field-name rule `(token|key|secret|password|dsn|credential|webhook_url)` — it inspects
     `Settings` field names, and only the composition root can see those;
   - the regex denylist at PLAN.md lines 832–836 — it names specific vendor credential formats,
     one of which is literally on the layering denylist. `Redactor` takes `patterns` by
     injection precisely so those can live outside the harness.

   `REDACTION_PLACEHOLDER = "***REDACTED***"` is transcribed verbatim from PLAN.md line 830.

3. **`TraceRecorder` has no read path — GAP.** A.9 gives it only `span()`, yet A.12 exposes
   `GET /v1/runs/{run_id}/trace -> TraceResponse` and A.9 defines `TraceResponse`. Nothing was
   invented. Something must later gain `async def get_trace(run_id: RunId) -> TraceResponse`,
   either on `TraceRecorder` or on `MemoryStore`. Named in Handoffs.

4. **Constructors — GAP, not invented.** Appendix A gives `__init__` only for `Evaluator`
   (`checkers: Sequence[ClaimChecker]`), `PolicyEngine` (`spec: PolicySpec`) and `Redactor`
   (`registry`, `patterns`); those are transcribed. `Orchestrator`, `ContextManager` and
   `TraceRecorder` are given with their single method and no constructor, so they are written
   that way. Phase 1 defines them. Inventing an `Orchestrator.__init__` here is exactly the
   failure mode Phase 0 exists to prevent.

5. **`harness/agent.py` — NOT CREATED, GAP.** The repository layout (PLAN.md line 101) names
   `agent.py` with an `Agent` protocol and an `LLMAgent` base, and PLAN.md line 306 lists it as
   Phase 1 work. Appendix A specifies no `Agent` protocol anywhere. I did not invent one, and
   the Phase 0 module list I was given also omits it. Named in Handoffs.

6. **`harness/memory/migrations/` — NOT CREATED.** The layout names a migrations directory under
   `memory.py`. The Phase 0 brief excludes SQL. Phase 3 owns it.

7. **`confidence.py` — FULLY DERIVED. Read this one carefully.** Appendix A has no A-section for
   it; the module is named only by the repository layout. What I wrote:
   - `Adjustment{name: str, delta: float, reason: str}` — shape copied exactly from Appendix
     A.11 (line ~1486) so the integration and the harness share one model rather than defining
     two. **Handoff: `integrations/cicd/schemas.py` should import `Adjustment` from
     `src.harness.confidence` instead of redeclaring it.**
   - `CONFIDENCE_FLOOR = 0.0`, `CONFIDENCE_CEILING = 0.99` — PLAN.md line 261, verbatim.
   - `ConfidenceModel{deltas: dict[str, float], floor: float, ceiling: float}` — the adjustment
     table is modelled as *data* (a name to delta mapping) rather than as seven named fields.
   - `DEFAULT_ADJUSTMENT_DELTAS` — six of the seven rows of the PLAN.md table verbatim:
     `memory_agreement +0.10`, `evidence_fully_verified +0.05`, `evidence_refuted -0.15`,
     `no_citations -0.10`, `cold_start -0.05`, `gateway_degraded -0.10`.
   - **The seventh row, `empty_diff_contradiction -0.10`, is deliberately NOT in the harness.**
     Its condition ("category is `real_regression` but `DiffSummary.files == []`") names a
     category and an artifact type that belong to the first integration, and its own name
     contains a domain word. Parameterising it is the standing instruction for anything
     CI-shaped. The integration must add `{"empty_diff_contradiction": -0.10}` to
     `ConfidenceModel.deltas`. **This is the single judgement call in the module and the one I
     most want confirmed.**
   - `calibrate(self_confidence: float, signals: Mapping[str, str], model: ConfidenceModel) ->
     tuple[float, list[Adjustment]]`. `signals` maps the name of each adjustment that fired to
     its human-readable reason; the deltas come from `model`, the clamp from
     `model.floor`/`model.ceiling`, and the returned `Adjustment` list is what goes into the
     trace. The *conditions* are not modelled here at all — evaluating them requires reading
     domain artifacts, so the caller decides which signals fired. **An alternative signature
     (`signals: Sequence[str]`, reasons generated internally) would have forced condition text
     into the harness; I rejected it for that reason.**

8. **`errors.py` — FULLY DERIVED.** PLAN.md line 199 says only programming errors raise, so the
   hierarchy is small: `HarnessError` (base), `ConfigurationError` (wiring/startup),
   `PolicySpecError(ConfigurationError)` (a policy file that fails to load or breaks a hardcoded
   invariant), `SchemaTranslationError` (PLAN.md line 174 — `to_gemini_schema` must reject a
   nested discriminated union at import time), `ContractViolationError` (a broken invariant
   between harness components). No exception exists for a tool or LLM failure, by design:
   those are `ToolError` / `AgentError` data.

9. **Protocol bodies use `...`, concrete bodies use `raise NotImplementedError`.** Appendix A
   writes `...` for Protocol members and concrete methods alike. Protocols are structural and
   never instantiated, so `...` is correct and idiomatic there; concrete classes
   (`Orchestrator.run`, `ContextManager.assemble`, `Evaluator.*`, `PolicyEngine.*`,
   `TraceRecorder.span`, `Redactor.*`, `SecretRegistry.*`) and the module functions
   (`retry_structured`, `to_gemini_schema`, `calibrate`) raise `NotImplementedError`.

10. **Declaration order kept as printed, plus explicit rebuilds.** `ContextRequest` precedes
    `Section` and `StageSpec` precedes `RunState`/`GateDecision` in Appendix A. I kept the
    order rather than reshuffling, and added `ContextRequest.model_rebuild()` /
    `StageSpec.model_rebuild()` at the end of those modules so the models are fully built at
    import time instead of on first validation.

11. **Extra module constants that Appendix A does not carry.** The numbers table assigns
    several values to harness modules that no Appendix A model holds. Rather than let them be
    inlined later, they are named now — no logic, just the table:
    `orchestrator.HEARTBEAT_INTERVAL_S = 15.0` / `HEARTBEAT_STALE_AFTER_S = 120.0`;
    `memory.SQLITE_BUSY_TIMEOUT_MS = 5000` / `WRITE_RETRY_BACKOFF_MS = (100, 200, 400)` /
    `PRIOR_MIN_OCCURRENCES = 3` / `PRIOR_DOMINANT_VERDICT_SHARE = 0.6` /
    `ACTION_WINDOW_HOURS = 24`; `evaluator.MIN_VERIFIED_SHARE = 0.5`;
    `guardrails.MAX_SIDE_EFFECTING_ACTIONS_PER_RUN = 1`.
    Note that `PRIOR_MIN_OCCURRENCES` / `PRIOR_DOMINANT_VERDICT_SHARE` are the domain-neutral
    renaming of the table's "3 or more occurrences AND ratio at least 0.6" row, whose original
    label uses a denylisted word; the same two numbers are also the condition for the
    `memory_agreement` adjustment.

## Known wrinkles — how each was handled

- **`LlmRequest.schema`.** Kept verbatim, required, no default. Pydantic v2 emits a
  `UserWarning` because `schema` shadows the deprecated `BaseModel.schema` classmethod; the
  class body sits inside a `warnings.catch_warnings()` block with a filter narrowed to
  `r'Field name "schema".*shadows an attribute'` / `UserWarning`, so no other warning is
  swallowed. mypy additionally reports an incompatible override, silenced with a targeted
  `# type: ignore[assignment]` on that one line. Verified with `python -W error::UserWarning`:
  the module imports clean, `LlmRequest(...).schema` round-trips, and omitting `schema` still
  raises `ValidationError` (i.e. it did not silently become optional).
- **`Condition.in_`.** `Field(None, alias="in")` with `populate_by_name=True`. Both
  `Condition(**{"in": [1, 2]})` and `Condition(in_=[1, 2])` construct and both yield
  `in_ == [1, 2]`.
- **`StageSpec` / `RunState`.** `arbitrary_types_allowed=True`. `StageSpec(output_model=P,
  gate=fn)` is accepted; `RunState.artifacts` round-trips a `BaseModel` *subclass* instance
  unchanged (pydantic does not re-validate it down to bare `BaseModel`).
- **`AgentResult`.** Kept `class AgentResult(BaseModel, Generic[TOut])` with the module-level
  `TOut = TypeVar("TOut", bound=BaseModel)`. ruff's UP046 wants PEP 695 syntax; suppressed with
  `# noqa: UP046` and a three-line comment, because PEP 695 would delete the named `TOut` that
  Appendix A declares and that `recovery.retry_structured` imports.
- **`Rule.when: dict[str, Condition]` and `PolicySpec.default_effect: Literal["deny"]`** —
  transcribed as written, including the deliberately one-member `Literal`.

## Layering audit

AST scan of `src/harness/**`: import targets are stdlib (`__future__`, `collections.abc`,
`contextlib`, `datetime`, `re`, `types`, `typing`, `warnings`) plus `pydantic` plus intra-package
`src.harness.*`. No `src.integrations`; httpx and aiosqlite are allowed but unused at Phase 0.

PLAN.md line-66 enforced denylist (`github`, `workflow_run`, `pytest`, `pull_request`, `ci`):
**zero hits** as import targets, **zero hits** as string literals, **zero hits** in raw file
text (word-boundary, case-insensitive).

Against the *broader* prime-directive denylist, four locations survive. Every one is Appendix A
verbatim and cannot be removed without a contract deviation:

| Location | Token | Status |
|---|---|---|
| `contracts.py` — `Evidence.source: Literal["log", "diff", "config", "memory", "tool"]` | `diff` | Appendix A.1 verbatim — the only denylisted **string literal** left in the package |
| `memory.py` — `Observation.commit_sha: str \| None` | `commit` | Appendix A.5 verbatim field name |
| `memory.py` — `MemoryHit.actions_in_window` (and two comments referencing it) | `actions` | Appendix A.5 verbatim field name |
| `guardrails.py` — `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN` (and one comment) | `actions` | mirrors the numbers-table row wording; "action" is a core generic noun in this design (`ActionContext`, `PolicyDecision`, `action_taken`, `action_outcome` are all Appendix A) |

Everything else that could have leaked was rewritten: Appendix A's illustrative comments naming
domain locators, stage names, tool names and integration keys were replaced with domain-neutral
prose, and the A.4 tool catalog table is not reproduced anywhere in the harness.

## Commands run

| Command | Result |
|---|---|
| `uv run --no-sync python -c "import ..."` for all 12 modules | all import clean |
| `uv run --no-sync ruff check src/harness --output-format=concise` | `All checks passed!` |
| `uv run --no-sync mypy --strict src/harness` | `Success: no issues found in 13 source files` (plus an unrelated note: `pyproject.toml: note: unused section(s): module = ['harness.*']`) |
| `uv run --no-sync pytest -q` | `1 warning in 0.01s` — **no tests exist yet**; `tests/` had not been created by test-verifier at the time of writing, so `tests/test_layering.py` could not be run |
| `uv run --no-sync python -W error::UserWarning -c "..."` | import plus construction of `LlmRequest`, `AgentResult[P]`, `StageSpec`, `RunState`, `ContextRequest`, `Condition`, `PolicySpec`, `ConfidenceModel` — no warning escaped |
| ad-hoc model audit (`model_json_schema()` + `model_config` for every model) | 42 models; all `frozen=True, extra="forbid"` except `RunState` (`frozen=False, extra="allow"`); `StageSpec`/`RunState` schema generation skipped by design (arbitrary types) |
| ad-hoc AST import + denylist scan (local reimplementation of `tests/test_layering.py`) | clean on the enforced denylist; the four Appendix-A-verbatim residues above on the broader list |

Environment: `uv` at `C:\Users\Shakti\.local\bin\uv.exe`, venv Python **3.12.14**, pydantic
**2.13.5**. `uv sync` / `uv add` were **not** run (api-surface owns the lock); `--no-sync` was
used throughout. No dependency was added and `pyproject.toml` was not touched.

## Could not verify

- `tests/test_layering.py` — did not exist yet. I reimplemented its documented behaviour
  (AST import walk plus bare-string-literal denylist grep) locally and it passes; the real test
  is test-verifier's to run.
- `docker compose up` / `/healthz` — not my territory, not attempted.
- Nothing in `src/harness/` is executable yet by design, so there is no behavioural test to run.

## Handoffs

- **test-verifier** — `tests/test_layering.py` should use the PLAN.md line-66 denylist
  (`github`, `workflow_run`, `pytest`, `pull_request`, `ci`) with **word-boundary** matching. A
  naive substring match on `ci` matches `decision`, `explicit`, `specific` and `citation`, and
  will fail the whole package. If the denylist is widened beyond line 66, the four
  Appendix-A-verbatim residues listed in the layering audit will trip it — that is a contract
  question, not a harness bug, and should come back to me before anything is renamed.
- **cicd-integration** — (a) import `Adjustment` from `src.harness.confidence`, do not redeclare
  it in `schemas.py`; (b) add `{"empty_diff_contradiction": -0.10}` to `ConfidenceModel.deltas`
  when constructing it — that seventh adjustment row is intentionally not in the harness;
  (c) `ContextRequest.anchor_patterns` and the tool catalog are yours to supply — the harness
  has no default for either; (d) `ClaimChecker` implementations live in `claim_checkers.py` and
  are injected via `Evaluator(checkers=...)`.
- **api-surface** — (a) the `Redactor` regex denylist from PLAN.md lines 832–836 must be built at
  the composition root and passed as `Redactor(registry, patterns)`; it cannot live in the
  harness because one of its literals is a denylisted word. Same for the `SecretRegistry`
  field-name rule `(token|key|secret|password|dsn|credential|webhook_url)`. (b) `mypy` prints
  `unused section(s): module = ['harness.*']` when invoked as `mypy --strict src/harness`; the
  `src.harness.*` override is the one doing the work, so the `harness.*` entry can be dropped.
  (c) No new dependency is needed for anything I wrote.
- **Phase 1 owner** — three contract gaps must be closed before they can be coded against:
  `harness/agent.py` (the `Agent` protocol and `LLMAgent` base — named in the layout, absent
  from Appendix A); constructors for `Orchestrator`, `ContextManager` and `TraceRecorder`; and a
  read path for traces (`TraceResponse` exists and `GET /v1/runs/{run_id}/trace` is specified,
  but no interface returns one). None were invented here.

## Notes for the reviewer

- The one judgement call I would most like a second opinion on is item 7: splitting the
  confidence adjustment table so six rows are harness defaults and the seventh is
  integration-supplied. It is the correct call under "if a concept feels CI-shaped, parameterise
  it", but it does mean PLAN.md's adjustment table is not reproduced in one place in code.
- The second is `calibrate`'s `signals: Mapping[str, str]` signature. It is derived, not
  specified, and Phase 1 is the first caller — if that shape is awkward in practice, change it
  now rather than after four agents have coded against it.
- `Evidence.source` still carries `"diff"` as a `Literal` member. That is the only denylisted
  string literal left in the package and it is Appendix A.1 verbatim. Renaming it would be a
  contract deviation, so I did not. If the layering test is widened, this is the collision.
- `RunState` is `extra="allow"`, so a typo'd attribute assignment will silently succeed. That is
  what the plan asks for; worth remembering when Phase 1 starts writing artifacts into it.

---

# Wave 3 fixes (audit response)

Appended after the Wave 3 audit (`docs/progress/phase-0/review.md`) returned FIX FIRST. Two
findings were mine, both in `src/harness/confidence.py`. Nothing above this line is rewritten —
it is the Phase 0 record. Only `src/harness/confidence.py` changed; `calibrate()`'s body is still
`raise NotImplementedError`, because Phase 0 is a contract freeze and neither remedy needs a
working body to be binding on Phase 1.

The reviewer approved the six-plus-one confidence split and the `calibrate` signature as-is.
Neither was redesigned. Both fixes are to the contract *around* that approved design.

## Finding 3 [MEDIUM] — `calibrate()` was fail-open on an unknown signal name

**Remedy chosen: the zero-delta `Adjustment`, not `ContractViolationError`.**

The reviewer offered both and called either defensible. I took the recorded-adjustment route for
four reasons, in descending weight:

1. **A signal name is runtime data supplied by the caller, and this layer's rule is that such
   failures are returned, not raised.** `errors.py` reserves raising for "a state the process
   cannot reason its way out of". An unregistered name is not that: the harness knows exactly
   what happened, knows the run is still coherent, and has a return channel built for saying so.
   Raising would make `calibrate()` the one function in the package that aborts on caller data.
2. **Raising destroys the evidence at the moment it is most useful.** `calibrate()` runs at the
   *end* of a diagnosis, after the tool calls and the model spend. A `ContractViolationError`
   there discards the whole diagnosis and leaves the operator a stack trace naming a missing
   dictionary key — strictly less information than a trace containing the diagnosis *and* a row
   saying which signal had no delta. The failure mode the finding describes ("nothing in the
   trace to explain it") is fixed by putting a row in the trace, which is where the operator
   actually looks.
3. **A crash is a worse fail-closed than a visible zero.** Under the raising remedy, a single
   missing table row takes the service from "acts on a slightly wrong number" to "processes
   nothing at all", and it does so at runtime rather than at startup — the blast radius of a
   one-line wiring omission becomes every run. The zero-delta row keeps the run alive, keeps the
   escalation path working, and makes the omission legible on the first run instead of the first
   page.
4. **The remaining numeric risk is smaller than the finding's worst case and cannot be closed
   here anyway.** The finding's scenario stays at `0.80` under my remedy, as it would under any
   remedy that does not invent a penalty. Inventing one (say, forcing the score below the
   escalation cutoff) would put a policy threshold inside the calibrator, which is exactly the
   leak this package exists to prevent, and would fabricate a delta the PLAN.md table never
   authorised. What is now impossible is the *silent* version: the adjustment list is exactly as
   long as the signal set, so an omission cannot be invisible.

**Hardening beyond the reviewer's text.** The reviewer's suggested reason string is prose, and
prose written in one module and matched in another drifts. I lifted the two values into named
module constants so the condition is machine-detectable:

```python
UNREGISTERED_SIGNAL_DELTA: Final[float] = 0.0
UNREGISTERED_SIGNAL_REASON: Final[str] = "no delta registered for this signal"
```

The string is the reviewer's wording verbatim. The sentinel reason **replaces** the caller's
reason for that adjustment, so `reason == UNREGISTERED_SIGNAL_REASON` is an exact-equality test.
No signature changed, no model field was added, and no numeric constant from PLAN.md's
"Concrete numbers in one place" was inlined.

The `calibrate()` docstring now states as steps 1-3 of a numbered contract: every name in
`signals` produces exactly one `Adjustment` in iteration order; `len(adjustments) == len(signals)`
holds for every input; a name present in `model.deltas` uses that delta and the caller's reason; a
name absent from it uses the two sentinels and "is never dropped and never raised". The old
sentence "A name absent from `model.deltas` contributes nothing" is gone.

The module docstring also now says why the split survives the fix:

> That split is only safe while omitting a row is *loud*. If the integration never registers its
> row, the penalty must not quietly evaporate into an unchanged score: `calibrate()` still emits
> an `Adjustment` for the signal, carrying `UNREGISTERED_SIGNAL_DELTA` and
> `UNREGISTERED_SIGNAL_REASON`, so the wiring omission reaches the trace instead of the
> operator's blind spot.

## Finding 4 [MEDIUM] — clamping order was unspecified

PLAN.md line 261 is normative and singular, so the docstring now says so in words that cannot be
read per-step. Exact wording, step 4 of the contract:

> 4. **Sum first, then clamp once.** Add the `delta` of every adjustment from steps 2 and 3 to
>    `self_confidence` to form a single running total, and apply
>    `min(max(total, model.floor), model.ceiling)` to that total once, as the final operation.
>    Intermediate totals are never clamped, never rounded and never inspected; a running total may
>    legitimately exceed `model.ceiling` or fall below `model.floor` and come back inside the
>    bounds before the end. This is PLAN.md's `clamp(self_confidence + sum(deltas), floor,
>    ceiling)` and nothing else. Worked example that separates the two readings:
>    `self_confidence=0.95` with deltas `+0.05` and `-0.15` sums to `0.85` and clamps to `0.85`;
>    clamping per step would yield `0.84`, and `0.84` is wrong.

The summary line changed from "Apply every signalled adjustment to `self_confidence` and clamp the
result" — which read either way — to "Sum the deltas of every signalled adjustment, then clamp the
total exactly once." The reviewer's own failure scenario is embedded as the worked example, with
the wrong answer named, so a Phase 1 implementer who gets it backwards has a numeric check
sitting in the docstring they are implementing from. A closing line fixes the tuple order:
"Returns the clamped score first, then the adjustments that produced it."

## Verification

| command | result |
|---|---|
| `uv run --no-sync ruff check src/harness` | `All checks passed!` |
| `uv run --no-sync mypy --strict src/harness` | `Success: no issues found in 13 source files` (plus the known `unused section(s): module = ['harness.*']` note) |
| `uv run --no-sync pytest tests/test_layering.py -q` | `53 passed` |
| `uv run --no-sync pytest -q` | `83 passed` — unchanged from the phase gate |

The new prose was written against the denylist: neither sentinel constant, neither docstring, nor
the worked example contains a domain word. No test targets `confidence.py` yet (finding 3's
behaviour is the obvious first unit test to write in Phase 1).

## Handoffs created by these fixes

- **cicd-integration** — unchanged and still required: register
  `{"empty_diff_contradiction": -0.10}` in `ConfidenceModel.deltas` at the composition root. The
  consequence of forgetting is now a visible zero-delta row rather than nothing, which makes it
  detectable but not harmless. Also still open: import `Adjustment` from `src.harness.confidence`
  (audit finding 1).
- **Phase 1 implementer of `calibrate()`** — the body is fully specified by the four numbered
  steps; implement them literally, including the single clamp. Two unit tests fall straight out:
  the `0.95 / +0.05 / -0.15 -> 0.85` case, and an unregistered name producing an `Adjustment`
  whose `reason == UNREGISTERED_SIGNAL_REASON` and whose presence keeps
  `len(adjustments) == len(signals)`.
- **api-surface / trace view** — `UNREGISTERED_SIGNAL_REASON` is a fixed sentinel and worth
  rendering distinctly (it means "this run was mis-wired"), not as an ordinary adjustment row.
- **fixtures-eval** — an eval assertion of the form "no adjustment carries
  `UNREGISTERED_SIGNAL_REASON`" is a cheap, domain-agnostic wiring check for every scenario.

---

# Post-ship residue (re-audit finding 4)

Phase 0 is tagged `phase-0-green`; the record above is the phase deliverable and is unchanged.
This section records one post-ship, docstring-only fix. No behaviour, no field, no signature
changed — `invoke`'s signature is still Appendix A.4 verbatim, and `calibrate()`'s body is still
`NotImplementedError`.

## Finding 4 [LOW] — `ToolGateway.invoke` carried `decision` but stated no re-check obligation

PLAN.md:454-459 makes the gateway the second of two independent enforcement points: it "requires a
`PolicyDecision` argument and **re-checks the tool name against `forbidden` itself**. The gateway
is authoritative." The signature carried the argument; nothing in `src/harness/gateway.py` told the
implementer to use it. A Phase 2 agent writing a concrete gateway from the Protocol alone could
accept `decision`, ignore it, and ship a second enforcement point that is structurally present and
behaviourally absent — worse than one honest check, because the trace then shows a `PolicyDecision`
was consulted next to a forbidden call that ran.

Fixed by giving `ToolGateway.invoke` a four-step contract docstring in the same shape as
`calibrate()`'s: obligation stated in implementation order, the wrong answer named explicitly, and
the failure scenario embedded so the implementer reads it while implementing. Framing: `decision`
is passed in **to be recorded, not to be believed**; the forbidden set comes from
`PolicySpec.forbidden` held by the implementation, never out of `decision`; a forbidden tool is
refused **even when `decision.effect == "allow"`** (the hand-forged-decision case PLAN.md:499-503
tests); the refusal is **returned** as
`ToolResult(ok=False, error=ToolError(kind="forbidden_by_policy", retryable=False, ...))`, not
raised, per PLAN.md:199; and because the check runs first, a forbidden tool **costs zero outbound
requests** — nothing reaches the wire, which is what the transport-layer assertion in PLAN.md's
verify block observes.

Deliberately **not** written into the contract: any obligation about `decision.effect` for
non-forbidden tools (e.g. whether a gateway reached with `deny` / `require_approval` should also
refuse). PLAN.md mandates the re-check against `forbidden` only; legislating the rest here would be
new behaviour invented at the contract freeze and would compete with the orchestrator's own
sequencing. Flagged below as a handoff instead.

Layering: the prose is domain-neutral. No tool name from any integration catalog appears — the
concrete example in PLAN.md's paragraph is a catalog tool whose name is itself a denylisted word.
`grep -InE '\b(github|workflow|workflow_run|pull_request|PR|commit|branch|repo|pytest|flaky|CI|job|test_name|actions|diff)\b'` over `src/harness/**` returns only the three pre-existing
Appendix-A-verbatim identifiers already recorded in `tests/test_layering.py`'s own docstring
(`Evidence.source` `"diff"` member, `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN` comment,
`MemoryHit.actions_in_window` comment). The new text adds zero matches.

## Verification (post-ship)

| command | result |
|---|---|
| `uv run --no-sync ruff check src/harness` | `All checks passed!` |
| `uv run --no-sync mypy --strict src/harness` | `Success: no issues found in 13 source files` |
| `uv run --no-sync pytest tests/test_layering.py -q` | `66 passed` |
| `uv run --no-sync pytest -q` | `99 passed` |

(Counts are higher than the phase-gate table above because api-surface and cicd-integration added
tests concurrently; the harness contribution to both runs is unchanged and green.)

## Handoffs created by this fix

- **Phase 2 implementer of any concrete `ToolGateway`** — the forbidden re-check is now a numbered
  obligation, not an inference. The implementation must hold its own copy of `PolicySpec.forbidden`
  (from the same spec the engine was built from) so that step 1 is possible without consulting
  `decision`; wire that at the composition root. PLAN.md:499-503's
  `..._refuses_forbidden_even_with_forged_allow_decision` test is the acceptance check, and it
  asserts zero outbound requests as well as the error kind — so the check must precede client
  construction, not merely precede the request.
- **Orchestrator author** — the contract deliberately says nothing about what a gateway should do
  when reached with `effect="deny"` / `"require_approval"` for a non-forbidden tool. Today that is
  the caller's invariant: do not invoke. If Phase 2 wants the gateway to enforce it too, that is a
  PLAN.md change routed back through this contract, not a local decision in one gateway.
- **Unresolved (no action)** — the reviewer declined the offered harness half of the
  unregistered-signal fail-safe: `PolicyEngine.decide` already consumes integration-supplied
  `facts` and the condition matcher already supports `eq`, so the close is pure data (integration
  emits the count; the policy file adds `{eq: 0}` to each permissive rule). Putting that concept
  into `guardrails.py` would import a domain notion into the harness. Scheduled for the Phase 2
  brief; the shipped remedy above stands.
