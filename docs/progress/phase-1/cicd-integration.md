## Summary

Fix round against the Wave-3 audit (`ea1178f`, `docs/progress/phase-1/review.md`), two
items, both confined to `src/integrations/**`.

**Finding 1** (`gateway_degraded` firing on non-required errors): fixed by changing what
`Investigator.run` puts into `FailureBundle.gateway_errors`. That field now carries ONLY
the errors from the four required deterministic-collection calls (jobs, log, baseline,
compare) — the model's optional `additional_tool_calls` errors and the refusal the
Investigator synthesises for a requested write tool are tracked in a separate, discarded
list and never reach the bundle. `Diagnostician.signals()` reads the same field it always
did; no change was needed there beyond a comment recording the invariant, because once
the field's contents are restricted correctly, `"a required read tool returned an error"`
stops being a statement that can be false. Reproduced the reviewer's exact regression on
today's code before fixing (0.95 → 0.85, false reason string) and confirmed it no longer
happens after the fix (recipe below, for `test-verifier` to turn into a real test).

**Finding 10** (prompts as Python string constants): `prompts/investigator.py` and
`prompts/diagnostician.py` are gone; `prompts/investigator.md` and
`prompts/diagnostician.md` exist instead, each starting with a `version: 1` front-matter
line, a `---` separator, then a `string.Template` body (`$identifier` placeholders, not
`.format()` — sidesteps the brace-escaping problem the old f-string renderers had to
work around for JSON-heavy sections). `rendering.py` gained `load_prompt_template()` (parses
and caches the front matter) and now owns `render_investigator_prompt` /
`render_diagnostician_prompt`, which the two agent modules import instead of the deleted
`prompts.investigator` / `prompts.diagnostician` modules. `prompts/__init__.py`'s docstring
no longer claims a false thing. Prompt wording is unchanged — this was a format move, not
a content edit, so it doesn't confound eval numbers with a prompt rewrite in the same
commit.

The version does reach the trace, entirely from inside `src/integrations/**`: each agent's
`build_prompt` opens a small nested span (`"prompt.render"`, component `"agent"`,
attributes `{agent, prompt_version}`) after rendering. `build_prompt` runs inside
`LLMAgent.run`'s already-open `"agent.run"` span (the ambient span-id `ContextVar` is set
for the whole `async with` body, not just the harness's own frame), so the nested span
parents itself there automatically and lands in the same trace — no `AgentPrompt` field,
no harness change. Verified live (see Commands run).

## Files written

- `src/integrations/cicd/agents/investigator.py` — `gateway_errors` split into
  required (feeds the bundle) vs. optional/refused (logged, discarded); import switch;
  `prompt.render` span.
- `src/integrations/cicd/agents/diagnostician.py` — comment recording the
  `gateway_errors`-is-required-only invariant; import switch; `prompt.render` span.
- `src/integrations/cicd/schemas.py` — comment on `FailureBundle.gateway_errors`
  documenting the same invariant (structure unchanged, still verbatim A.11).
- `src/integrations/cicd/rendering.py` — `PromptTemplate`, `load_prompt_template`,
  `render_investigator_prompt`, `render_diagnostician_prompt` (moved in from the deleted
  `prompts/*.py` modules).
- `src/integrations/cicd/prompts/investigator.md` (new, `version: 1`)
- `src/integrations/cicd/prompts/diagnostician.md` (new, `version: 1`)
- `src/integrations/cicd/prompts/investigator.py` (deleted)
- `src/integrations/cicd/prompts/diagnostician.py` (deleted)
- `src/integrations/cicd/prompts/__init__.py` — docstring corrected; package now holds
  no importable prompt code, only this docstring.

Verified the two new `.md` files ship: `Dockerfile`'s `COPY src ./src` copies the whole
tree verbatim (this app is `tool.uv.package = false`, no wheel build to strip non-`.py`
files out), and `.dockerignore`'s only `*.md` exclusion is the literal repo-root
`README.md` — it does not match `src/integrations/cicd/prompts/*.md`.

## Contract deviations

None. `FailureBundle`, `InvestigationNotes`, `Diagnosis` are structurally unchanged from
PLAN.md Appendix A.11 (comments only). `policy.yaml`, `gateway_github.py`,
`claim_checkers.py`, `fingerprint.py`, `wiring.py` untouched.

## Prompt changes

- `prompts/investigator.md` — version 1 (new file; content is the prior
  `SYSTEM_PREAMBLE` + section template from `prompts/investigator.py`, transcribed with
  no wording changes, `{...}`/f-string composition replaced by `$identifier`
  placeholders).
- `prompts/diagnostician.md` — version 1 (new file; content is the prior
  `SYSTEM_PREAMBLE` + `ACTION_GUIDANCE` + `CONFIDENCE_RUBRIC` + section template from
  `prompts/diagnostician.py`, transcribed with no wording changes, same placeholder
  substitution; the old JSON-key example rendered `{{"claim_kind", ...}}` with
  double-braces to escape an f-string — the template needs no escaping, so it now reads
  `{"claim_kind", ...}` directly, which is the literal text the model was always shown).

I deliberately did **not** add the "prior history is a prior, not evidence" / "absence of
history is NOT evidence" clauses called out under Appendix D — see Notes for the reviewer,
new finding below. Adding new prompt substance wasn't in this fix round's scope (findings
1 and 10 only), and both prompts currently render `prior_history_summary` as a
Phase-1-only stand-in string built at call time in the agent (memory doesn't exist until
Phase 3), so this is a real gap but not one this round should paper over silently.

## Commands run

| Command | Result |
|---|---|
| `uv run ruff check src/integrations` | All checks passed |
| `uv run pytest tests/test_layering.py -q` | 71 passed |
| `uv run pytest tests -q` | 247 passed (253 before this round; the 6-test drop is `test_settings_construction_gate.py` / `test_no_env_access.py` parametrizing over `REPO_ROOT.rglob("*.py")` — deleting the two `prompts/*.py` modules removed their parametrized instances, not a regression) |
| `uv run pytest tests/integration/test_replay_e2e.py -q` | 13 passed |
| `uv run python -c "to_gemini_schema(InvestigationNotes/Diagnosis/RemediationPlan)"` | all three survive; `Diagnosis.propertyOrdering[0] == "reasoning"` confirmed still first |
| Reproduction script (below) against stashed-out pre-fix code | `final_confidence: 0.85`, `adjustments: [{'name': 'gateway_degraded', 'delta': -0.1, 'reason': "a required read tool returned an error (not_found)"}]` |
| Same script against fixed code | `final_confidence: 0.95`, `adjustments: []`, `bundle.gateway_errors: []` |
| Standalone trace check | `prompt.render {'agent': 'investigator', 'prompt_version': '1'}` and same for `diagnostician`, read back from `GET /v1/runs/{id}/trace` |

## Finding-1 reproduction recipe (for `test-verifier`)

In-process, same shape as `tests/integration/test_replay_e2e.py`'s `StubLlm` fixture — no
live model quota spent.

**Inputs**: `POST /v1/replay/real_regression` (the existing Phase-1 fixture). Stub the
Investigator's response with a valid `InvestigationNotes` payload whose
`additional_tool_calls` contains exactly one entry:
```json
{"call_id": "tc_0000000000ab", "tool": "get_file_contents",
 "args": {"path": "src/pricing.py", "ref": "e2cdf1b44e7ca3dd9eca76b1caf3e9dc837846df"}}
```
(`src/pricing.py` has no recorded `api/GET_repos-octo-org-harness-demo-repo-contents-src-pricing.py.json`
file in `fixtures/scenarios/real_regression/`, so the replay gateway returns
`ToolError(kind="not_found")` for it — confirmed by listing that scenario's `api/`
directory, which has exactly three files and none of them a `contents-` fixture.) Stub the
Diagnostician's response with `self_confidence: 0.95` and the two citations from
`test_replay_e2e.py`'s existing `diagnosis()` helper (or reuse that helper verbatim).

**Expected `final_confidence` and adjustments, before this fix** (i.e. on `ea1178f`):
`final_confidence == 0.85`, `confidence_adjustments == [{"name": "gateway_degraded",
"delta": -0.10, "reason": "a required read tool returned an error (not_found)"}]`,
`body["final"]["bundle"]["gateway_errors"]` containing the `not_found` entry.

**Expected after this fix**: `final_confidence == 0.95`, `confidence_adjustments == []`,
`body["final"]["bundle"]["gateway_errors"] == []`.

A durable test should assert the "after" numbers as the passing case, and — if it wants
to also pin the regression it closes — assert them against a stashed/checked-out copy of
the pre-fix files rather than hardcoding "0.85" as a magic number anywhere in the suite
(that number is an artifact of the specific scenario's `self_confidence` and one `-0.10`
delta, not a contract).

The full working script I used for the "before" and "after" runs (git-stash-based, not
committed to `tests/`) is not part of this repo; reconstructing it from the recipe above
plus `tests/integration/test_replay_e2e.py`'s existing fixtures is direct — the `client`,
`stub_llm`, `AppContext` wiring there is exactly what's needed, just with `NOTES`
extended by the one `additional_tool_calls` entry above.

## Policy-refusal confidence-signal decision

A refused write-tool request (the model naming e.g. `merge_pull_request` in its optional
`additional_tool_calls`) carries **no confidence signal at all**, in either direction.
Reasoning:

- It is not a gateway failure — the Investigator's read-only restriction rejects it
  before any `ToolCall` reaches `ToolGateway.invoke`, so zero requests hit the wire. Folding
  it into `gateway_degraded` (as the pre-fix code did) mischaracterizes a client-side
  refusal as an external system degrading.
- Nothing in PLAN.md's adjustment table (the seven rows under "How `final_confidence` is
  derived") covers "the model asked for something it wasn't allowed to have." Inventing a
  new adjustment for this in this fix round would be exactly the kind of scope creep the
  standing instruction warns against — if a case can be made for one, it belongs in
  PLAN.md as a new row, not built silently here.
- It is arguably a signal about the *model's* calibration (a model that asks for
  `merge_pull_request` while investigating a test failure is doing something a well-behaved
  Investigator shouldn't), but that is a model-quality question for eval, not a
  fact about the evidence bundle's reliability — the two are different measurements and
  conflating them would make `gateway_degraded` mean two things.

It is logged (`logger.info`, not raised, not appended anywhere the Diagnostician reads)
so it stays visible for debugging without affecting the served number.

## Handoffs

- **harness-core**: none required. Both fixes landed entirely inside
  `src/integrations/**`; the trace-version requirement was met without touching
  `src/harness/agent.py`'s `AgentPrompt`.
- **fixtures-eval**: none for this round.
- **test-verifier**: the finding-1 reproduction recipe above is ready to become a real
  test (likely alongside `test_calibration_penalises_a_diagnosis_with_no_citations` in
  `tests/integration/test_replay_e2e.py`, same shape — extend the stub's `NOTES` and add
  one `get_file_contents` call the fixture can't satisfy).

## Notes for the reviewer

- **New finding, not fixed in this round** (scope: only findings 1 and 10 were in
  scope): the Investigator and Diagnostician prompts do not carry the two clauses this
  fix round's own brief calls "load-bearing" — "prior history is a prior, not evidence;
  you must still cite something from this run's log or diff; if this run's evidence
  contradicts the prior, follow the evidence and say so" and, on a cold start, "absence
  of history is NOT evidence that this failure is real." Today's `prior_history_summary`
  string (built in each agent's `build_prompt`, not in the template) says "Do not treat
  the absence of history as evidence of either flakiness or novelty" (Diagnostician) or
  nothing at all on the point (Investigator) — a partial version of the second clause,
  missing the first entirely. This is dormant risk, not a live defect: Phase 1 has no
  memory store, so prior history is always absent and the "contradicts the prior" branch
  can never fire yet. It becomes load-bearing the moment Phase 3 wires real
  `PriorHistory` in, per the review's own Wave-3 finding 11 concern about the retry cap
  failing open. Recommend it lands as part of that Phase 3 work, when there is an actual
  prior to word the clause around, rather than added speculatively now to two prompt
  files that would then need editing again anyway.
- The `prompt.render` span's placement (a whole `async with ... : pass` opened solely to
  attach an attribute) is a slightly unusual pattern; I chose it because it's the only
  surface available from inside `src/integrations/**` — `AgentPrompt` has no field for it
  and the span the harness opens around `build_prompt` isn't handed to the subclass. If a
  cleaner mechanism becomes possible after a `src/harness/agent.py` change (e.g.
  `AgentPrompt.prompt_version: str | None`), that's harness-core's call, not mine to make
  unilaterally.
- `_call_tool`'s `errors` parameter is now used with two different lists at two call
  sites in `Investigator.run` (`collected.gateway_errors`'s copy for the required calls
  made during `build_prompt`, a fresh `additional_errors` list for the optional calls made
  during `run`) — same method, deliberately different accumulators, documented inline at
  both sites.
