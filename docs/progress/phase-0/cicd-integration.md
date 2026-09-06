## Summary

Froze the CI/CD domain contract for Phase 0: all seventeen payload models in
`src/integrations/cicd/schemas.py` (Appendix A.11), the verbatim `policy.yaml`
(Phase 2 content, PLAN.md lines 404-450), and the package skeleton for
`src/integrations/cicd/**` including empty/docstring-only placeholders for every
Phase 1+ module named in the repo layout. No implementation logic was added
anywhere — Phase 0 is scaffold plus contract freeze only.

All schema models were round-trip tested against the real `src.harness` modules
once harness-core's files landed mid-task: import succeeds, `frozen=True` and
`extra="forbid"` are enforced at runtime, `Diagnosis`'s field order matches
Appendix A.11 verbatim (reasoning before the conclusion fields, the two
harness-added fields last with their defaults), and `policy.yaml` validates
against `PolicySpec.model_validate()` from `src.harness.guardrails`.

## Files written

- `src/integrations/__init__.py`
- `src/integrations/cicd/__init__.py`
- `src/integrations/cicd/schemas.py` — all 17 models from Appendix A.11
- `src/integrations/cicd/policy.yaml` — verbatim transcription of PLAN.md lines 404-450
- `src/integrations/cicd/fingerprint.py` — docstring-only placeholder (Phase 3)
- `src/integrations/cicd/gateway_github.py` — docstring-only placeholder (Phase 1)
- `src/integrations/cicd/gateway_replay.py` — docstring-only placeholder (Phase 1)
- `src/integrations/cicd/claim_checkers.py` — docstring-only placeholder (Phase 4)
- `src/integrations/cicd/wiring.py` — docstring-only placeholder (Phase 1+)
- `src/integrations/cicd/agents/__init__.py`
- `src/integrations/cicd/agents/investigator.py` — docstring-only placeholder (Phase 1)
- `src/integrations/cicd/agents/diagnostician.py` — docstring-only placeholder (Phase 1)
- `src/integrations/cicd/agents/remediator.py` — docstring-only placeholder (Phase 2)
- `src/integrations/cicd/prompts/__init__.py` — empty package marker; no `.md` prompt
  files were written yet since no agent that consumes them exists this phase
- `docs/progress/phase-0/cicd-integration.md` — this file

I did **not** create any files under `src/harness/**`, `src/settings.py`,
`pyproject.toml`, `Dockerfile`, `fixtures/**`, `scripts/**`, or `tests/**`.

## Contract deviations

None. Every field name, `Literal` member, default, and ordering in
`schemas.py` is a verbatim transcription of PLAN.md Appendix A.11, and
`policy.yaml` is byte-for-byte the block at PLAN.md lines 404-449 (diffed by
hand against the source range).

## Prompt changes

None this phase. `src/integrations/cicd/prompts/` exists as an empty package
(only `__init__.py`); no agent consumes a prompt yet, so no `*.md` files were
invented ahead of need. Phase 1 (Investigator/Diagnostician) is where the
first `prompts/*.md` files with `version:` front-matter should appear,
following the cold-start clause and memory-as-prior rules from Appendix D.

## Points where Appendix A.11 was silent or ambiguous, and what I chose

1. **`JsonValue`'s source.** Appendix A uses `dict[str, JsonValue]` throughout
   without ever defining it in the excerpts I read. `schemas.py` itself does
   not need to reference `JsonValue` directly (every field that uses it lives
   inside a harness-owned model I only import), so this was moot for my file.
   Confirmed by inspection that `src/harness/gateway.py` and
   `src/harness/guardrails.py` (as landed by harness-core) both do
   `from pydantic import ..., JsonValue` — i.e. pydantic's own `JsonValue`
   alias, not a bespoke harness type. No action needed on my side.
2. **Import path convention (`src.harness.*` vs `harness.*`).** Confirmed
   against the now-landed `pyproject.toml` (`[tool.uv] package = false`,
   `pythonpath = ["."]`, mypy overrides naming `"src.harness.*"`) that the
   intended dotted path is `src.harness.*` / `src.integrations.*` with the
   repo root as the sole import root. Used that convention throughout.
3. **Which harness module owns which re-exported name.** Appendix A.11 says
   only "from `src.harness.*`". I mapped each name to the module the repo
   layout implies and confirmed against the landed files:
   `RunId` → `src.harness.contracts`, `TruncationReport` →
   `src.harness.context_manager`, `ToolCall`/`ToolError`/`ToolResult` →
   `src.harness.gateway`, `PolicyDecision` → `src.harness.guardrails`.
4. **`prompts/` needing an `__init__.py`.** The task explicitly listed
   `prompts/` among the directories to give an `__init__.py`, even though it
   holds only markdown resources, not Python modules. Did it literally as
   instructed rather than second-guessing it — costs nothing, keeps the
   directory a package if a loader ever wants `importlib.resources` against it.
5. **Placeholder file granularity.** Chose one-line-docstring modules (rather
   than fully empty files or `raise NotImplementedError` stubs) for every
   Phase 1+ file named in the repo layout, since a bare empty `.py` gives no
   signal to the next phase's implementer and a `NotImplementedError` stub
   would need an actual function/class signature I'd be guessing at (and
   PLAN.md doesn't specify those signatures anywhere I was told to freeze
   this phase). This mirrors the style harness-core used for its own
   not-yet-implemented methods (e.g. `PolicyEngine.decide` raises
   `NotImplementedError`, but that's a signature Appendix A.7 *does* freeze —
   mine don't have frozen signatures yet, so a docstring is the honest
   placeholder).

## Commands run

```
$ python -c "import pydantic; print(pydantic.VERSION)"                  -> 2.13.4 (system Python 3.13)
$ python -c "from pydantic import JsonValue; print('JsonValue ok')"     -> JsonValue ok
$ python -c "import yaml; print(yaml.__version__)"                      -> 6.0.3
$ python -m ruff --version                                              -> ruff 0.16.5
$ python -m pytest --version                                            -> pytest 9.1.1

$ python -c "yaml.safe_load(open('src/integrations/cicd/policy.yaml'))" -> parses cleanly, dumped as JSON, six forbidden entries + four rules confirmed

$ python -m ruff check src/integrations                                 -> 1 error (import-sort), fixed with --fix, then "All checks passed!"

# after src/harness/* landed from harness-core mid-task:
$ python -c "from src.integrations.cicd import schemas; print(schemas.__all__)"
    -> import ok, all 17 names present

$ python -c "... PolicySpec.model_validate(yaml.safe_load(policy.yaml)) ..."
    -> "PolicySpec OK"; rules=['retry-suspected-flaky','open-fix-pr','file-ticket','read-only-always'];
       forbidden=[merge_pull_request, force_push, delete_branch, delete_workflow_run,
                  create_deployment, update_branch_protection]

$ python -c "<construct every schemas.py model, plus mutation/extra-field negative tests>"
    -> all constructed OK; Diagnosis field order =
       ['category','summary','reasoning','self_confidence','citations','suspected_commit_sha',
        'suspected_test_ids','suspected_package','suggested_action','final_confidence',
        'confidence_adjustments']  (matches Appendix A.11 order exactly);
       frozen enforced: ValidationError; extra=forbid enforced: ValidationError

$ export PATH="/c/Users/Shakti/.local/bin:$PATH"; uv run --no-sync ruff check .   -> 1 error (import-sort,
    project's ruff version groups first-party `src.*` imports separately from third-party `pydantic`),
    fixed with `uv run --no-sync ruff check --fix .`, then "All checks passed!"

$ uv run --no-sync pytest -q
    -> "No files were found in testpaths" (tests/ not yet created by test-verifier at time of this run);
       0 collected, no failures

$ uv run --no-sync python -c "from src.integrations.cicd import schemas; ..."
    -> import ok against the project's actual .venv (pydantic 2.13.5)
```

I polled (bounded, ~5 min total across three rounds) for `src/harness/**` and
`tests/test_layering.py` to appear from the parallel harness-core / test-verifier
agents so I could do real verification instead of guessing at signatures.
`src/harness/**` landed in full (contracts.py, gateway.py, guardrails.py,
context_manager.py, llm.py, memory.py, evaluator.py, observability.py,
orchestrator.py, errors.py) and every model my `schemas.py` depends on matches
Appendix A exactly, byte for byte on the fields I re-derive from. Confirmed
`to_gemini_schema()` in `src/harness/llm.py` is present but currently
`raise NotImplementedError` — that's expected for Phase 0/1 scaffolding and is
harness-core's territory, not a deviation on my side. `tests/test_layering.py`
had **not** landed by the time I stopped polling (test-verifier's work is still
in flight); see Handoffs.

## Handoffs

- **test-verifier**: I could not run `tests/test_layering.py` because it did
  not exist yet as of my last check. Please confirm, once it lands, that it
  passes with `src/integrations/cicd/**` present in its current (Phase-0,
  mostly-placeholder) form — none of my files import anything from
  `src.harness` privately (no underscore-prefixed names), none construct a
  `PolicyDecision` directly (the one place `PolicyDecision` appears in my code
  is as a type annotation on `ApprovalRequest.decisions` /
  `RemediationResult.decisions`, never instantiated), and nothing in
  `src/integrations/**` is imported by `src/harness/**` (the seam only runs
  one direction, from my side into the harness).
- **harness-core**: no gaps found — `contracts.py`, `gateway.py`,
  `guardrails.py`, and `context_manager.py` all landed with exactly the
  shapes Appendix A promises, so `schemas.py` needed zero adjustments to
  compile against them. Flagging for awareness only (not asking you to change
  anything now): `to_gemini_schema()` is still `raise NotImplementedError`,
  which is fine for Phase 0 but is on the Phase 1 critical path for me since
  every `InvestigationNotes`/`Diagnosis`/`RemediationPlan` schema needs to
  survive it.
- **api-surface**: `pyproject.toml`'s `[tool.uv] package = false` +
  `pythonpath = ["."]` convention (import as `src.harness.*` /
  `src.integrations.*`) is what I coded against; please keep that convention
  stable since `schemas.py`'s imports are load-bearing on it.
- **fixtures-eval**: no fixtures were touched or required this phase.

## Notes for the reviewer

- `policy.yaml` rule order is preserved exactly as written
  (`retry-suspected-flaky`, `open-fix-pr`, `file-ticket`, `read-only-always`)
  since first-match-wins order is semantically significant per PLAN.md line 1328.
- I deliberately did not touch `to_gemini_schema()`, `PolicyEngine.decide()`,
  or any other harness internals, even though they currently `raise
  NotImplementedError` and would block a real end-to-end run — that
  implementation is harness-core's territory in Phase 1+, not mine.
- `merge_pull_request`'s "body must raise if ever reached" requirement
  applies to `gateway_github.py`, which is an empty Phase-1 placeholder in
  this phase by design; noting it here so it isn't forgotten when that file
  is actually implemented.
- Ran full-tree `ruff check .` (not just `src/integrations`) with both the
  system Python and the project's `uv`-managed venv/ruff version to catch any
  environment-specific lint drift (there was one: import-block grouping of
  first-party `src.*` vs third-party `pydantic` differed by ruff version/config
  detection); both now pass clean.

## Wave 3 fixes

Two findings from the Wave 3 audit (`docs/progress/phase-0/review.md`) landed against my
files. Both fixed in `src/integrations/cicd/schemas.py` only; nothing else touched.

### Finding 1 [HIGH] — duplicate `Adjustment` class

**Diff.** Deleted the local `class Adjustment(BaseModel): ...` body (was at old line
135-140: `name: str`, `delta: float`, `reason: str`, `model_config = _MODEL_CONFIG`) and
replaced it with an import:

```python
from src.harness.confidence import Adjustment
```

`__all__` is unchanged — `"Adjustment"` still resolves, now to the re-exported harness
class rather than a locally-declared one. Also updated the module docstring's list of
harness-owned building blocks to add `Adjustment`, with one sentence explaining it is
re-exported (not redeclared) so `Diagnosis.confidence_adjustments` accepts
`calibrate()`'s output directly.

This was purely a subtraction-plus-import in my file; I did not touch
`src/harness/confidence.py` (harness-core's territory). I read the current version
before importing — it now defines `UNREGISTERED_SIGNAL_DELTA` /
`UNREGISTERED_SIGNAL_REASON` and a fail-visible (not fail-open) `calibrate()` contract per
finding 3 of the audit; none of that changes the shape of `Adjustment` itself
(`name: str`, `delta: float`, `reason: str`, `extra="forbid"`, `frozen=True`), so the
import is a drop-in replacement.

### Finding 2 [MEDIUM] — `reasoning` field order in `Diagnosis`

**Diff.** Moved `reasoning: str = Field(max_length=1200)` from third position (after
`category`, `summary`) to first position. New order, exactly as specified in the
dispatch:

```
reasoning, category, summary, self_confidence, citations, suspected_commit_sha,
suspected_test_ids, suspected_package, suggested_action,
# --- added by the harness ... ---
final_confidence, confidence_adjustments
```

No type, constraint, or default changed on any field — `reasoning` keeps
`max_length=1200`, every other field is byte-identical to before the move. The inline
comment on `reasoning` was rewritten from "ordered BEFORE the conclusion via
propertyOrdering" to "ordered first, via propertyOrdering, so the model reasons before
concluding" (split onto its own line above the field to stay under ruff's `E501` 100-char
limit — a formatting necessity, not a wording change of substance).

### Verification

```
$ export PATH="/c/Users/Shakti/.local/bin:$PATH"
$ uv run --no-sync ruff check .                          -> All checks passed!
$ uv run --no-sync pytest tests/test_layering.py -q       -> 53 passed
$ uv run --no-sync pytest -q                              -> 83 passed

$ uv run --no-sync python -c "
    from src.harness.confidence import Adjustment as HarnessAdjustment
    from src.integrations.cicd import schemas
    adj = HarnessAdjustment(name='cold_start', delta=-0.05, reason='no baseline')
    d = schemas.Diagnosis(
        reasoning='the log shows an assertion failure on line 91 comparing 91 to 90',
        category='real_regression', summary='off-by-one in checkout total',
        self_confidence=0.8,
        citations=[schemas.Citation(claim_kind='test_in_log', locator='log:job/2001',
                                     quote='assert 91 == 90')],
        suggested_action='open_fix_pr', confidence_adjustments=[adj],
    )
    assert schemas.Adjustment is HarnessAdjustment
    assert d.confidence_adjustments[0] is adj
    d2 = schemas.Diagnosis.model_validate(d.model_dump())   # round trip, per the audit's
                                                             # 'workaround is worse' scenario
    fields = list(schemas.Diagnosis.model_fields.keys())
    prop_order = list(schemas.Diagnosis.model_json_schema()['properties'].keys())
    print(fields); print(prop_order)"
-> Finding 1 OK: schemas.Adjustment is harness.confidence.Adjustment -> True
   Finding 1 OK: cross-class instance accepted, identity preserved -> True
   round-trip model_validate OK, adjustments -> [Adjustment(name='cold_start', delta=-0.05, reason='no baseline')]
   Diagnosis field order: ['reasoning', 'category', 'summary', 'self_confidence',
     'citations', 'suspected_commit_sha', 'suspected_test_ids', 'suspected_package',
     'suggested_action', 'final_confidence', 'confidence_adjustments']
   model_json_schema property order: ['reasoning', 'category', 'summary',
     'self_confidence', 'citations', 'suspected_commit_sha', 'suspected_test_ids',
     'suspected_package', 'suggested_action', 'final_confidence', 'confidence_adjustments']
   ALL CHECKS PASSED
```

`Diagnosis.model_json_schema()`'s `properties` dict iterates in the same order as
`model_fields` (both dict-ordered by declaration), so `reasoning` is first in the schema
that `to_gemini_schema()` will eventually derive `propertyOrdering` from. Confirmed
`to_gemini_schema()` itself is still `raise NotImplementedError` (harness-core's Phase 1
territory, unchanged since the original Phase 0 report) — every model in
`schemas.__all__`, including the reordered `Diagnosis`, fails identically with
`NotImplementedError` and no other exception type, so there is no new failure mode
introduced by the reorder; this will need re-verification once `to_gemini_schema()` is
implemented.

## Contract deviations (Wave 3)

None. Finding 1's fix is exactly the one-line replacement the audit specified. Finding
2's fix is exactly the field order the dispatch specified, with no field's type,
`Field(...)` constraint, or default touched — verified above field-by-field against the
pre-fix listing recorded in this document's original "Commands run" section (the
`['category', 'summary', 'reasoning', 'self_confidence', ...]` order captured before this
wave).

## Handoffs (Wave 3)

- **PLAN.md maintainer**: per the dispatch, PLAN.md Appendix A.11 is being amended in
  parallel to match this reorder. I did not touch PLAN.md myself (out of my write
  territory); `git status` shows `PLAN.md` already modified in the working tree, which I
  did not create — no action needed from me, flagging only so the reviewer knows I saw it
  and left it alone.
- **harness-core**: no new ask. Confirmed `src/harness/confidence.py`'s current
  `calibrate()` fail-closed contract (`UNREGISTERED_SIGNAL_DELTA` /
  `UNREGISTERED_SIGNAL_REASON`) does not change `Adjustment`'s shape, so my import
  continues to satisfy `Diagnosis.confidence_adjustments: list[Adjustment]` unchanged.
  Still outstanding for me, not you: registering
  `{"empty_diff_contradiction": -0.10}` in the composed `ConfidenceModel.deltas` at
  Phase 1/3 wiring time (`ConfidenceModel` itself needs no change from you for that —
  it's a value I supply into an existing mutable-default-free field).
- **test-verifier**: `tests/test_layering.py` (53 cases) and the full suite (83 cases)
  both still pass after these two edits; no new import of a harness private name was
  introduced (the new `from src.harness.confidence import Adjustment` imports a public,
  non-underscore-prefixed class).

## Notes for the reviewer (Wave 3)

- The reorder comment on `reasoning` had to move to its own line above the field to stay
  under `pyproject.toml`'s ruff `E501` (line length 100); this is the only textual change
  beyond what the dispatch specified, and it changes no semantics.
- Carrying forward, per the dispatch, for Phase 1 composition and `gateway_replay.py`
  (not implemented this wave): `get_job_logs` has no `api/*.json` fixture recording — the
  replay gateway must read `fixtures/scenarios/<name>/logs/job_<id>.txt` directly; and
  `max_bytes` truncation keeps the **last** N bytes, not the first, per PLAN.md:222 (the
  proximate failure is near the end of the log). Also still outstanding at composition
  time: register `{"empty_diff_contradiction": -0.10}` into `ConfidenceModel.deltas`,
  since that row is deliberately absent from the harness's `DEFAULT_ADJUSTMENT_DELTAS`.
