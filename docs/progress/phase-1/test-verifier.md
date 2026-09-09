## VERDICT           PASS

Wave 2 re-gate of the Phase 1 urgent fix bundle (`handoff-fixround2.md` §4;
`review-2.md` findings 1, 2, 3, 5). All three specifically-requested tests (a, b, c) are
written and pass, the startup-validation test is written and passes, the full suite
passes, `ruff` is clean, and `mypy --strict src/harness` is clean with the cache cleared
(`--no-incremental`) so the verdict is not resting on a stale cache. Two pre-existing
tests were found broken by the legitimate, in-scope constant change
(`MAX_RETRY_AFTER_S` 60→20) and are fixed below — not a source defect, a test that pinned
the old ceiling.

## Gate results

**1. Full suite.**

```
$ python -m pytest -q
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
........................................                                 [100%]
328 passed in 27.83s
```
Expected: baseline 316 + 12 new tests written this round = 328. **Matches exactly** (2 in
`test_investigator_upstream_failure_ends_run.py` + 4 in `test_retry_delay_budget.py` + 4
in `test_startup_validates_prompt_templates.py` + 2 net-new in
`test_replay_response_scrubbing.py`). y

**2. `ruff check src/ tests/`**
```
$ python -m ruff check src/ tests/
All checks passed!
```
y — also re-ran as `ruff check src tests app.py` (the coordinator's literal invocation in
`review-2.md`), same result.

**3. `mypy --strict src/harness`, cache cleared.**
```
$ python -m mypy --version
mypy 1.14.1 (compiled: yes)
$ rm -rf .mypy_cache && python -m mypy --strict --no-incremental src/harness
Success: no issues found in 14 source files
```
y — the `orchestrator.py:312` `.get()`-default widening `EscalationReason` to `str` that
`review-2.md` recorded as a mypy-1.14.1-only artifact is **gone**: harness-core landed a
real fix while this gate ran (`_StageOutcome` type alias +
`_DEFAULT_STAGE_OUTCOME: Final[_StageOutcome]`, unpacked via `outcome[0]`/`outcome[1]`
instead of straight into `status, reason`). Diffed it: the dict values and the fallback
tuple are byte-identical to before, so this is a type-narrowing fix, not a behaviour
change or a suppression — confirmed by re-running the full suite after, still 328 passed.

## Tests written

**(a) — citation/observation path cannot carry a planted token, with a stub that
actually quotes the log (`tests/integration/test_replay_response_scrubbing.py`):**
- `QuotingStubLlm` — new stub class. Unlike `test_replay_e2e.StubLlm`'s fixed citation,
  it finds the exact line carrying the planted `ghp_…` token inside the prompt text it
  was handed and quotes it back verbatim, in both the Investigator's `observations` and
  the Diagnostician's `citations[].quote` — exactly what `prompts/diagnostician.md:16`
  ("do not paraphrase inside `quote`") instructs a real model to do.
- `test_the_verbatim_quote_premise_the_token_reaches_citations_and_observations` — proves
  the raw, pre-serialisation `RunOutcome` really carries the token through
  `diagnosis.citations[].quote` and `bundle.notes.observations[]` specifically (not just
  through `logs[].excerpt`, which a different, already-tested mechanism handles), so the
  leak test below cannot pass vacuously.
- `test_a_verbatim_quoted_citation_never_reaches_the_served_response` — drives the real
  orchestrator + `_serialize_run_outcome`, asserts `SECRET_TOKEN` is absent from the full
  served JSON text, from `citations`, and from `observations` specifically. Pins the
  full-body `Redactor.scrub()` pass in `_serialize_run_outcome`, which is the only thing
  that closes this path (the two field-name digest substitutions never touch
  `citations`/`observations`).
- `_run_against` / new `_run_orchestrator` helper factored to accept an injectable `llm`,
  reused by both the pre-existing excerpt/patch tests and the two new ones.

**(b) — failure status is reported, and the Diagnostician never runs**
(`tests/integration/test_investigator_upstream_failure_ends_run.py`, new file):
- `AlwaysRateLimitedForInvestigatorLlm` — every Investigator call raises
  `LlmRateLimited`; if the Diagnostician is ever asked anything at all, the stub itself
  raises `AssertionError` rather than silently answering, so a regression back to the old
  behaviour fails loudly rather than merely producing a wrong number somewhere.
- `test_investigator_upstream_exhaustion_ends_the_run_before_diagnostician_runs` —
  asserts the `investigate` `StageRecord.status != "ok"` (the failure is actually
  reported), `outcome.status == "escalated"` with `outcome.escalation.reason ==
  "rate_limited"` (the reason `Orchestrator._OUTCOME_FOR_ERROR_KIND` derives for
  `llm_rate_limited`), and that no `diagnose` stage record exists at all.
- `PermanentlyInvalidNotesLlm` / `test_investigator_invalid_output_still_degrades_and_continues`
  — the control case: a permanently schema-invalid (not upstream-unreachable) notes
  response must NOT trip the short-circuit — `investigate` stays `"ok"`,
  `"investigator_notes"` is in `degraded_components`, and `diagnose` runs and produces a
  real diagnosis. Pins the `_UPSTREAM_ERROR_KINDS` boundary in both directions.
- `asyncio.sleep` patched to a no-op (`_no_real_backoff_sleep`, same pattern as
  `test_llm_upstream_escalation.py`) — zero wall time, zero live quota.

**(c) — a fully rate-limited run terminates inside the new budget**
(`tests/unit/test_retry_delay_budget.py`, new file):
- Three fast, deterministic boundary tests (`asyncio.sleep` mocked to a recorder):
  `test_a_delay_that_exactly_fits_the_remaining_budget_is_still_slept` (the `>` in
  `total_delay_s + delay > RETRY_DELAY_BUDGET_S` is strict — a delay landing exactly on
  the budget is honoured), `test_a_delay_that_would_land_one_over_the_budget_ends_the_loop_without_sleeping`
  (the mirror case — zero sleeps, the loop ends before calling `asyncio.sleep` at all,
  which is what makes this a true ceiling rather than an overshoot-by-one),
  `test_the_budget_ends_the_loop_on_the_same_error_kind_the_attempt_budget_would` (the
  terminal `AgentError.kind` is unchanged by which budget ended the loop).
- **`test_a_fully_rate_limited_call_terminates_inside_the_real_delay_budget`** — the
  genuine wall-clock assertion, no mocked sleep, against the actual shipped constants
  (`MAX_RETRY_AFTER_S == 20.0`, `RETRY_DELAY_BUDGET_S == 20.0`, both asserted as a
  premise so a future constant change fails this test loudly instead of silently
  invalidating its arithmetic). Provider states exactly `MAX_RETRY_AFTER_S` on every
  attempt; asserts real elapsed time is `>= 19s` (one honoured sleep happened) and
  `< 35s` (a second ~20s sleep did not also happen). **Measured: 20.16s real**, exactly
  the one-sleep prediction, run in isolation:
  ```
  $ python -m pytest tests/unit/test_retry_delay_budget.py::test_a_fully_rate_limited_call_terminates_inside_the_real_delay_budget -q
  .                                                                        [100%]
  1 passed in 20.16s
  ```

**Startup validation** (`tests/unit/test_startup_validates_prompt_templates.py`, new
file, the "cheap win" the dispatch also asked for):
- `test_validate_prompt_templates_succeeds_against_the_real_shipped_templates` — positive
  control, so the negative tests below cannot pass because the function silently
  swallows everything.
- `test_validate_prompt_templates_raises_for_a_missing_directory` (`OSError`) and
  `test_validate_prompt_templates_raises_for_a_malformed_template` (`ValueError`,
  matching `"version"`) — the function itself.
- `test_app_startup_fails_loudly_instead_of_serving_a_green_healthz` — the end-to-end
  claim: entering `TestClient(api_main.app)`'s context (the ASGI lifespan) with a broken
  template directory raises `OSError` at that point, reproducing `review-2.md` finding
  3's exact scenario as the *prevented* case — no client can complete the startup
  handshake at all, let alone reach a green `/healthz` followed by a bare 500.

## Failures found and fixed (my territory, not a source defect)

Two pre-existing test files pinned the **old** `MAX_RETRY_AFTER_S = 60.0` and broke on
the legitimate, in-scope, reviewed lowering to `20.0` — this is the fix bundle doing
exactly what it was dispatched to do; the tests were asserting stale behaviour, not
correct behaviour the source regressed on. Both are fixed to test the same properties
("delay honoured verbatim, not jittered backoff" / "a hostile-input value is parsed
correctly") with values that stay meaningful under the new, lower ceiling:

- `tests/unit/test_recovery_retry_delay.py::test_a_429_with_a_stated_retry_delay_is_slept_verbatim_every_time`
  — was asserting `slept == [41.0, 41.0, 41.0]`; 41.0 now exceeds `MAX_RETRY_AFTER_S`
  *and* would trip the new `RETRY_DELAY_BUDGET_S` after one sleep, so it no longer tests
  "honoured verbatim on every attempt". Changed the stated delay to `6.0` (three sleeps =
  18.0s, inside the 20.0s delay budget), same assertions otherwise.
- `tests/unit/test_retry_delay_extraction.py` — ten failures, all `41`/`23`/`30`-second
  values that used to pass through `MAX_RETRY_AFTER_S = 60` unclamped and now get clamped
  to `20`. Seven parametrized/body/header cases changed to values ≤ 20s (9s, 12s, 15s,
  17s, 15s-future-date) so they keep testing verbatim parsing, not the clamp (which has
  its own dedicated tests, untouched and still passing).
  `test_classify_a_real_429_extracts_and_clamps_the_retry_delay` — the one case built
  from the exact 41s shape `harness-core.md` records seeing live — now legitimately
  demonstrates the clamp instead of verbatim passthrough; updated the assertion to
  `MAX_RETRY_AFTER_S` and the docstring to say so, rather than picking a different number
  and losing the real-shape coverage.

Both are territory I own (`tests/**`); confirmed with `git diff` that no source file was
touched to make either pass — only the expected/input values inside the test bodies.

## Coverage gaps

- **The HF proxy's actual timeout ceiling remains unverified** (`review-2.md` finding 1
  itself says so — "≥57s tolerated, live-measured; ceiling unverified and not something
  to design against"). No test can close this offline; it is outside this gate's scope
  and was already flagged as unverifiable by the re-audit.
- **A route-level `asyncio.wait_for` was not chosen** as part of this bundle (the
  dispatch chose "bound the retry" over "route-level ceiling" as the minimum sufficient
  fix), so there is no test for a full four-agent-stage worst case beyond the
  single-`retry_structured`-call wall-clock test above. Given fix (b) — the Investigator
  short-circuit — a "fully rate-limited run" now only ever burns one agent's retry
  budget, which is what test (c) measures; I did not additionally write a two-agent
  wall-clock test because after (b) there is no code path left that reaches a second
  agent's retry loop while genuinely upstream-unreachable, and asserting a path is
  unreachable by *not* writing a test for it would be weaker than the explicit
  `AssertionError`-if-called guard already in `AlwaysRateLimitedForInvestigatorLlm`.
- **Findings 4, 5, 6, 7, 8, 9, 11, 12 from `review.md`** and the low-severity finding 4
  from `review-2.md` (optional tool call observability) are explicitly out of scope for
  this dispatch (`handoff-fixround2.md` §4: "the 8 older findings stay out of it") and
  have no new tests here.
- **Broader PLAN.md test-suite items** named in the general test-verifier brief
  (`test_layering.py`, `test_guardrails.py`, `test_fingerprint.py`, `test_evaluator.py`,
  `test_no_secret_leak.py`, `test_idempotency.py`, `contract/test_tool_gateway_contract.py`)
  are not part of Phase 1's CI/CD-only slice (no Guardrails, Evaluator, Memory, or
  multi-gateway contract exist yet per PLAN.md and this handoff) and are correctly absent
  — not a gap in this gate, a gap in a later phase.
- **Live redeploy verification** — not run, and correctly not run: the dispatch's
  explicit instruction was "spend zero live Gemini quota," and this gate needed none of
  the 20/day budget to reach PASS. If the coordinator wants live-Space re-verification
  after redeploy, that is a separate, later check this report does not cover.

## Notes for the reviewer

- All four builders' changes are present and internally consistent: `MAX_RETRY_AFTER_S`
  is 20.0 (`src/harness/llm.py:366`), `RETRY_DELAY_BUDGET_S` is 20.0
  (`src/harness/recovery.py:89`) and enforced with the "return before sleeping" shape the
  dispatch specified (verified directly in the boundary tests above), the `logger.info`
  clamp exists, and the `retryDelay` object-form docstring note is present
  (`_duration_to_seconds` docstring, `llm.py`). `_UPSTREAM_ERROR_KINDS` in
  `investigator.py` is exactly the five kinds named in the dispatch
  (`llm_rate_limited`, `llm_timeout`, `llm_auth`, `llm_upstream`, `internal`), with
  `invalid_output` deliberately excluded — both directions are now pinned by test (b).
  `_serialize_run_outcome` ends with `get_app_context().recorder.redactor.scrub(body)`,
  additive to the field-name walk — pinned by test (a) at the real HTTP boundary, not
  just as a pure-function unit test. `validate_prompt_templates()` is called first thing
  in `main.py`'s `lifespan`, before `recorder.initialize()`, and left to raise.
  `diagnostician.md`'s backticks are restored (byte-faithful to the pre-port render), so
  `version: 1` correctly still names one prompt.
- One thing worth the coordinator's attention even though it's not a defect: the
  `_StageOutcome` mypy fix landed in `orchestrator.py` *during* this gate (harness-core
  was still working per the dispatch's concurrency note). I re-ran the full suite after
  it landed and got the same 328/328 pass — the fix is behaviour-preserving — but it
  means this gate's mypy result reflects the tree as of the last commands above, not a
  snapshot taken before harness-core finished. If anything else lands after this report
  is written, the gate should be re-run rather than trusted as still current.
- Everything driven through this gate used `ReplayToolGateway`, stub `LlmClient`
  implementations, and `TestClient` — zero live HTTP, zero Gemini quota spent, consistent
  with `test_replay_e2e.py`'s existing shape.

## Files touched (all under `tests/**`)

- `D:\Documents\harness_project\tests\integration\test_replay_response_scrubbing.py` (edited — QuotingStubLlm + 2 new tests + `_run_orchestrator` refactor)
- `D:\Documents\harness_project\tests\integration\test_investigator_upstream_failure_ends_run.py` (new)
- `D:\Documents\harness_project\tests\unit\test_retry_delay_budget.py` (new)
- `D:\Documents\harness_project\tests\unit\test_startup_validates_prompt_templates.py` (new)
- `D:\Documents\harness_project\tests\unit\test_recovery_retry_delay.py` (edited — stale 41.0 value fixed to 6.0)
- `D:\Documents\harness_project\tests\unit\test_retry_delay_extraction.py` (edited — 7 stale values under the new 20s ceiling, 1 assertion updated to demonstrate the clamp instead of verbatim passthrough)
