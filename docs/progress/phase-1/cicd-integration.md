## Summary

Backlog fix round: closed the four `cicd-integration`-owned items from
`docs/progress/phase-1/backlog.md` — `review.md` findings 11 and 12 (the two safety
ones, taken first as instructed), `review.md` finding 4 (the medium), and `review-2.md`
finding 4 (the observability one). All four landed as contained changes inside
`src/integrations/**`; none required a harness or test change, and none of the four
turned out to be larger than a contained fix, so nothing here escalates to "stop and
report" territory.

**Finding 11 (`review.md`, low) — `PriorHistory.retries_in_24h` fail-open.**
Appendix B.3's degraded-memory row requires the retry-cap fact to default to a
conservative `999`. First pass fixed only the one construction site
(`Investigator.build_prompt`, passing `retries_in_24h=999` explicitly alongside
`unavailable=True`). The coordinator asked for this to be structural rather than a
discipline guarantee — the same principle already chosen twice elsewhere in this repo
(the whole-body `Redactor` over per-field scrubbing; `problem()`'s scrub over
"every call site authors its own `detail`") — since Phase 2 is exactly where new
`PriorHistory` construction sites appear and is exactly what the retry cap gets wired
to. Implemented as a `model_validator(mode="after")` on `PriorHistory` itself:
`unavailable=True` now implies `retries_in_24h == 999` unless the caller explicitly
supplied a different value, using `self.model_fields_set` to distinguish "not
supplied" from "supplied as 0" (so an explicit `retries_in_24h=0` from a caller who
means it still survives). Mutation from inside the `after` validator goes through
`object.__setattr__` to get past the model's own `frozen=True` — the documented
pattern for this exact situation, and verified `frozen` still holds for every other
mutation path (see Commands run). The Pydantic field default stays `0`, matching
A.11's transcribed schema exactly — only the *implied* value for the degraded case
changed, and it now changes structurally rather than per call site.

The Investigator's explicit `retries_in_24h=999` is now redundant and was dropped
(judgment call, noted in-line): the construction site is back to
`PriorHistory(signature_id=None, unavailable=True)`, and the validator supplies the
999. This is deliberate — leaving the explicit value in would have re-created exactly
the discipline-guarantee shape the fix exists to remove (a future reader could copy
that call site without the explicit value and quietly regress, except now they can't,
because the model itself won't let them). Per the coordinator's framing: the
**prompt** half of this finding (the dormant clauses) is deliberately not touched — it
is settled to land with Phase 3 per `handoff-fixround2.md` §6, and I did not write any
prompt clause about retry priors.

**Finding 12 (`review.md`, low) — `ReplayToolGateway(forbidden=())` fail-open at
construction.** Removed the default; `forbidden: tuple[str, ...]` is now a required
keyword argument. Checked every construction site first: `src/api/deps.py:95` already
passes `forbidden=load_forbidden()`, and every test that constructs
`ReplayToolGateway` already passes `forbidden=` explicitly (including the ones that
pass `forbidden=()`, which is a stated decision, not an accidental one). So this is a
pure tightening — nothing in the tree relied on the old default, and nothing broke.

**Finding 4 (`review.md`, medium) — `Diagnosis` schema asks the model for the two
harness-added fields.** `prompts/diagnostician.md` was already correct — it lists only
the nine model-authored keys and never asks for `final_confidence` or
`confidence_adjustments` — so no prompt edit, and no `version:` bump, was needed.
The gap was entirely in the wire schema `to_gemini_schema(Diagnosis)` produces, which
still emitted both fields in `properties`/`propertyOrdering` because `LLMAgent` derives
the schema straight from `output_model`. Per A.11's instruction to keep both fields on
the Pydantic model (the harness still populates them via `model_copy` after
calibration) while no longer requesting them from the model, I added a
`Diagnostician`-local schema translator (`_diagnosis_schema` in `diagnostician.py`)
that wraps `to_gemini_schema` and strips exactly `final_confidence` and
`confidence_adjustments` from `properties`, `propertyOrdering` and `required` before
the schema reaches the model, then wired it in via `LLMAgent`'s existing
`schema_translator` injection point (already provider-coupled, already designed to be
swapped per agent — no harness change). `output_model=Diagnosis` is unchanged, so
`retry_structured`'s validation of the model's JSON response is untouched: both fields
carry Pydantic defaults (`0.0` and `[]`), so a response that omits them (because the
schema no longer asks for them) still validates. Verified:
`to_gemini_schema(Diagnosis)["propertyOrdering"]` still ends in both fields (the
harness-level function is intentionally unchanged — that half of the finding is
harness-core's to close, and the finding itself says so: "harness-core (`to_gemini_schema`
has no way to exclude a field)"); `Diagnostician`'s actual outgoing schema
(`_diagnosis_schema(Diagnosis)`) no longer contains either key.
I judged this in scope and contained rather than a contract change: `Diagnosis` itself
is byte-identical to A.11, `LLMAgent`'s public surface is unchanged, and the fix is
confined to one new private function plus one new keyword argument at one call site.

**Finding 4 (`review-2.md`, low) — optional tool calls unobservable.** Per the
coordinator's settled framing (`handoff-fixround2.md` §6): this is a bundle field, not
a confidence adjustment, and I did not touch `ConfidenceModel`/`calibrate`/the
adjustment table. Added `AdditionalToolCallOutcome` to `schemas.py`
(`tool: str`, `outcome: Literal["obtained","refused","failed"]`, `error: ToolError |
None`, `reason: str`) and a `FailureBundle.additional_tool_outcomes: list[...] = []`
field. `Investigator.run` now appends one entry per optional call the model named (at
most 3): `"refused"` when the tool isn't in the read-only catalog (never reaches the
gateway), `"obtained"` on success, `"failed"` with the `ToolError` attached on a
gateway-level failure (e.g. `not_found`). The dead `executed: list[str]` local is gone
— superseded by the new field, which is real, observable data on the served
`RunOutcome` rather than a log line. `result.data` is still not carried onto the
bundle: PLAN.md's raw-content leak class (the urgent bundle's finding 3/`review-2`
finding 2) is exactly why I kept this to outcome/kind rather than surfacing the tool's
raw response body — that would need the same length+digest treatment `LogExcerpt`/
`FileChange` get at the API boundary, which is api-surface's layer, not mine, and
would be a redesign rather than the proportionate visibility fix asked for.

## Files written

- `src/integrations/cicd/schemas.py` — added `AdditionalToolCallOutcome`;
  `FailureBundle.additional_tool_outcomes: list[AdditionalToolCallOutcome] = []`;
  export added to `__all__`. No existing field touched, no default changed.
- `src/integrations/cicd/agents/investigator.py` — degraded `PriorHistory` now passes
  `retries_in_24h=999` explicitly; the optional-tool-call loop now builds
  `additional_outcomes` (obtained/refused/failed) instead of the dead `executed` list,
  and files it onto the constructed `FailureBundle`.
- `src/integrations/cicd/agents/diagnostician.py` — added `_HARNESS_ADDED_FIELDS` and
  `_diagnosis_schema()` (wraps `to_gemini_schema`, strips the two harness-added keys);
  `Diagnostician.__init__` now passes `schema_translator=_diagnosis_schema` to
  `super().__init__`.
- `src/integrations/cicd/gateway_replay.py` — `ReplayToolGateway.__init__`'s
  `forbidden` parameter lost its `= ()` default and is now required; docstring extended
  to state why.

No file outside `src/integrations/**` was written. No prompt file
(`src/integrations/cicd/prompts/*.md`) was changed — the Diagnostician's prompt was
already correct for finding 4, and finding 11's prompt half is deliberately deferred to
Phase 3.

## Contract deviations

**Correction accepted from the coordinator, recorded here:** my first pass said "none".
That was wrong. `FailureBundle` is specified field-by-field in Appendix A
(`PLAN.md:1468`), so adding `additional_tool_outcomes` to it — and adding the new
`AdditionalToolCallOutcome` model — is a deviation from the transcribed schema, even
though the fix itself was pre-agreed (the coordinator's brief named "a bundle field"
explicitly). The coordinator has amended Appendix A directly to add both, matching this
round's shape (`tool`, `outcome: Literal["obtained","refused","failed"]`,
`error: ToolError | None`, and — per this repo's own field, `reason: str`, not
independently re-verified against the coordinator's amendment text). Noted for next
time: adding a field to a model Appendix A transcribes is always a deviation to
record, independent of whether the change itself was settled in advance.

`Diagnosis` (Appendix A.11) is unchanged field-for-field — not a deviation.
`PriorHistory`'s Pydantic field default (`retries_in_24h: int = 0`) is also unchanged
and matches A.11 verbatim; the `model_validator` added for finding 11 changes what the
*constructed instance* holds in the `unavailable=True` case, not the schema's declared
default, so I am treating this as a behavioural fix within A.11's transcribed shape
rather than a further deviation — flagging the reasoning here in case the coordinator
weighs it differently, given the correction above.

## Prompt changes

None. `prompts/diagnostician.md` (`version: 1`) already omitted `final_confidence` and
`confidence_adjustments` from its requested-keys list before this round; verified by
reading the file rather than assumed. `prompts/investigator.md` untouched.

## Commands run

- `uv run ruff check src/integrations` → all checks passed.
- `uv run ruff check src/` → all checks passed (re-run after the finding-11 validator
  change; still clean).
- `uv run mypy src/integrations/cicd` → 9 errors both before and after the finding-11
  validator change (down from 10 on `HEAD`, verified by `git stash`/`git stash pop`
  around a clean-tree run). All 9 remaining are the pre-existing ones the brief said
  are not mine (`gateway_replay.py` `JsonValue` narrowing ×3, the `**dict[str,float]`
  kwargs-unpacking pattern in `investigator.py`/`diagnostician.py`, `wiring.py:81`
  missing return annotation). One of the pre-existing `diagnostician.py` errors (the
  `Callable[[type[BaseModel]], dict[str, JsonValue]]` arg-type complaint on the
  `**({"timeout_s": ...})` unpack) is gone as a side effect of passing
  `schema_translator=_diagnosis_schema` as an explicit keyword argument ahead of the
  `**` unpack, which resolves that argument's type unambiguously. The `model_validator`
  itself introduces no new mypy error.
- `uv run pytest tests/test_layering.py -q` → 71 passed (re-run after the validator
  change).
- `uv run pytest tests/ -q -k "cicd or investigator or diagnostician or gateway_replay or replay"`
  → 69 passed (re-run after the validator change).
- `uv run pytest tests/ -q` (full suite, before the finding-11 revision) → 340 passed,
  1 failed, 1 skipped. The one failure,
  `tests/unit/test_llm_schema_and_errors.py::test_unsupported_keywords_are_stripped`,
  asserts `"maxLength" not in repr(to_gemini_schema(...))`; `git diff -- src/harness/llm.py`
  shows harness-core mid-flight on `review.md` finding 8 (adding `minLength`/`maxLength`
  to `_ALLOWED_SCHEMA_KEYS`, i.e. deliberately making that assertion false). Confirmed
  this is not caused by anything in `src/integrations/**`: the cicd-filtered subset above
  is 100% green, and `src/harness/llm.py`, `orchestrator.py`, `recovery.py` were already
  modified in the working tree before I started (concurrent harness-core work), untouched
  by me. Not fixed, not mine to fix, and outside my write territory (`tests/**`).
- Verified `to_gemini_schema` still succeeds for every exported model in
  `src/integrations/cicd/schemas.py`, including the new `AdditionalToolCallOutcome`, by
  import-time construction (ad hoc script, not committed).
- Ad hoc verification of the `PriorHistory` validator (ad hoc script, not committed):
  `unavailable=True` with no explicit `retries_in_24h` → `999`; `unavailable=True,
  retries_in_24h=0` → `0` (survives); `unavailable=False` (default) → `0`;
  `unavailable=True, retries_in_24h=5` → `5` (survives); direct post-construction
  mutation (`instance.retries_in_24h = 1`) still raises — `frozen=True` is intact
  outside the validator.

## Handoffs

- **harness-core**: `test_unsupported_keywords_are_stripped` will need updating (or
  splitting) once `review.md` finding 8 lands — it currently pins the pre-fix behaviour
  and is failing against your in-flight change, not mine. Not something I can act on;
  flagging since it's live in the tree right now.
- **harness-core**: the other half of `review.md` finding 4 stays open by design —
  `to_gemini_schema` still has no general field-exclusion mechanism. I worked around it
  with a per-agent `schema_translator` wrapper local to `diagnostician.py`, which is
  sufficient for `Diagnosis` alone. If a second output model ever needs the same
  treatment, a harness-level exclusion marker (e.g. reading a `json_schema_extra` key)
  would be the reusable version; not requested by this round's brief, so not built.
- **api-surface**: `FailureBundle.additional_tool_outcomes[].error` carries a
  `ToolError` (same type already present in `gateway_errors`), so it should already
  fall under whatever `Redactor`/length-digest handling `_serialize_run_outcome`
  already gives `gateway_errors` — no new raw-content field was added, so I don't
  believe this needs a new API-layer case, but it's worth a glance since it's a new
  array on a model that route now serializes.
- **test-verifier**: no tests were written or touched, per territory. A reproduction
  recipe for each of the four findings is below for whichever tests get written against
  this diff.

## Notes for the reviewer

Reproduction recipe per finding:

- **`review.md` 11 (end-to-end)** — construct an `Investigator`, run it with no memory
  store wired (the current Phase 1 wiring always takes this path), inspect
  `FailureBundle.prior_history.retries_in_24h`; expect `999`, not `0`.
  `git show HEAD:src/integrations/cicd/agents/investigator.py` for the before-state.
- **`review.md` 11 (validator, unit)** —
  `PriorHistory(signature_id=None, unavailable=True).retries_in_24h == 999`
  (not supplied → fails closed) and
  `PriorHistory(signature_id=None, unavailable=True, retries_in_24h=0).retries_in_24h == 0`
  (explicitly supplied as 0 → survives, `model_fields_set` distinguishes the two). Both
  in `src/integrations/cicd/schemas.py::PriorHistory`.
- **`review.md` 12** — `ReplayToolGateway(scenario_dir=..., repo=...)` with no
  `forbidden=` now raises `TypeError: __init__() missing 1 required keyword-only
  argument: 'forbidden'` at construction, rather than building a gateway that refuses
  nothing.
- **`review.md` 4** — `from src.integrations.cicd.agents.diagnostician import
  _diagnosis_schema; from src.integrations.cicd.schemas import Diagnosis;
  _diagnosis_schema(Diagnosis)["propertyOrdering"]` no longer contains
  `"final_confidence"` or `"confidence_adjustments"`; `to_gemini_schema(Diagnosis)`
  directly (the harness-level function, unwrapped) still does, which is expected and
  unchanged. A live `Diagnostician.run` still returns a `Diagnosis` with both fields
  populated post-hoc (unchanged behaviour, re-verify via the existing calibration
  tests).
- **`review-2.md` 4** — drive the Investigator with a stub LLM whose
  `InvestigationNotes.additional_tool_calls` names one allowed read tool that 404s in
  the fixture and one disallowed (write) tool; inspect the resulting
  `FailureBundle.additional_tool_outcomes`: expect two entries, `outcome="failed"`
  with `error.kind == "not_found"` for the first, `outcome="refused"` for the second,
  and neither present in `bundle.gateway_errors` (unchanged scoping, so
  `gateway_degraded` still does not fire on either).

Boundary note: I did not touch `tests/**`, `src/harness/**`, `src/api/**`, or `app.py`.
The concurrent modifications visible in `git status` under `src/harness/` (`llm.py`,
`orchestrator.py`, `recovery.py`) are harness-core's in-progress work, not mine —
confirmed by `git diff` before I made any edit and left untouched throughout.
