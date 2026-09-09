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

---

# Incremental re-gate — the mounted-shape hand-call fix (2026-09-10)

## VERDICT           PASS

Two changes landed since the `fe7ba2d` PASS above: `app.py`'s `main()` now hand-calls
`validate_prompt_templates()` before the recorder work (closing the re-audit finding that
`test_startup_validates_prompt_templates.py` only pins the Docker/lifespan path, which the
`Mount`-based Space never runs), and a comment/docstring-only correction in
`src/integrations/cicd/agents/investigator.py` (no behaviour change, confirmed by diff —
reviewed, nothing to test). One new sibling test file was written and gated below.

## Gate results

**1. Full suite.**
```
$ uv run pytest -q
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 64%]
........................................................................ [ 86%]
.............................................s                           [100%]
333 passed, 1 skipped, 2 warnings in 26.70s
```
Expected: baseline 328 + 5 new pure-Python tests = 333. **Matches exactly.** The 1 skip is
`test_app_py_main_hand_calls_validate_prompt_templates_before_launch`
(`pytest.importorskip("gradio")`) — see "Notes for the reviewer" below; y for the 333, and
the skip is disclosed rather than hidden in a passing count.

**2. `ruff check src/ tests/ app.py`**
```
$ uv run ruff check src/ tests/ app.py
All checks passed!
```
y

**3. `mypy --strict src/harness`**
```
$ uv run mypy --strict src/harness      # 2.3.1, the pin of record
Success: no issues found in 14 source files

$ mypy --strict src/harness             # bare PATH mypy, 1.14.1
Success: no issues found in 14 source files
```
y — both versions agree this round, no divergence to flag.

## Tests written

`D:\Documents\harness_project\tests\unit\test_startup_validates_prompt_templates_under_mount.py`
(new file, 6 tests):

- `test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500` (parametrized over both
  mount orders — mounted before the outer app's own `TestClient` context is entered, and
  mounted after, matching `app.py`'s actual `demo.launch()`-then-`mount()` sequence).
  Reproduces the reviewer's exact repro byte-for-byte on the mounted shape: `GET /healthz`
  → `200 {"status": "ok", ...}`, then `POST /v1/replay/real_regression` → `500`,
  `content-type: text/plain; charset=utf-8`, body literally `"Internal Server Error"`
  (asserted via `pytest.raises(json.JSONDecodeError)` on `.json()`, plus the literal string
  equality), never the RFC 9457 `problem+json` body every deliberately-handled route error
  returns. Verified empirically first via a standalone probe script before writing the
  test, to confirm the exact byte shape rather than assume it.
- `test_hand_call_before_mount_raises_at_boot_instead_of_reaching_the_route` — calling
  `validate_prompt_templates()` explicitly (app.py's fix) against the same broken
  directory raises `OSError` synchronously, before any app/route exists to be reached at
  all — the contrast case for the two 500s above.
- `test_validate_prompt_templates_succeeds_against_the_real_shipped_prompts_dir` — the
  non-vacuity control for this file (item 5 of the recipe).
- `test_hand_call_then_mount_serves_correctly_with_a_real_prompts_dir` — the positive,
  end-to-end close of the finding: hand-call first (must not raise against the real
  `prompts/` dir), then mount, then a real request through the mounted shape — asserts
  `200`, not the `500` from the first test. Same topology, only the directory differs,
  isolating exactly what the fix changes.
- `test_app_py_main_hand_calls_validate_prompt_templates_before_launch` — imports `app.py`
  (scoped inside the test function, per the gotcha flagged in the task: importing it at
  module scope would build the `gr.Blocks` demo for every test in the file, whether or not
  gradio is even installed). Stubs `demo.launch` / `demo.block_thread` (the real ones bind
  a port and block; not what this test is about) and asserts, from a recorded call order,
  that `validate_prompt_templates()` runs strictly before `demo.launch()`, and that
  `demo.app.mount("/", app_module.api)` and `demo.block_thread()` both still ran — i.e.
  `main()` actually performs the hand-call, in the right position, without this test
  needing to let the real `launch()` execute.

## Failures

None. No source defect found this round beyond the one already fixed (the finding this
round exists to close).

## Coverage gaps

None against this round's stated recipe (items 1-5 plus the bonus `main()` assertion are
all covered by name).

## Notes for the reviewer

**The one skip, and why it is not gate-softening.** `app.py` does `import gradio as gr` at
module scope, and `gradio` is deliberately **not** a project dependency —
`requirements.txt`'s own comment states it explicitly ("`gradio` and `uvicorn` are absent
on purpose — the Space installs both itself"), it is absent from `pyproject.toml` and from
`uv.lock`, and `docs/progress/phase-1/handoff-space-deploy.md:301` independently records
"Not imported by the app, the image or the tests." Under the standard `uv run pytest`
environment this task's gate uses, `import gradio` raises `ModuleNotFoundError` — confirmed
directly:
```
$ PYTHONPATH=. uv run python -c "import gradio"
ModuleNotFoundError: No module named 'gradio'
```
So `test_app_py_main_hand_calls_validate_prompt_templates_before_launch` guards itself with
`pytest.importorskip("gradio")` and is the suite's only skip. This is not a softened
assertion — nothing about its content was weakened to pass — it is an honest report that
the environment this gate runs in cannot exercise this one test, consistent with what the
project's own docs already say about `app.py`. I did not stop at asserting that; I verified
the test is real and would pass given the dependency, using `uv run --with gradio --with
spaces` (an ephemeral overlay that does not touch the project's own `.venv` or `uv.lock` —
confirmed by `git status` and `ls .venv/Lib/site-packages | grep gradio` before/after
showing no change):
```
$ PYTHONPATH=. uv run --with gradio --with spaces pytest -q \
    tests/unit/test_startup_validates_prompt_templates_under_mount.py \
    -k test_app_py_main_hand_calls -v
tests\unit\test_startup_validates_prompt_templates_under_mount.py .      [100%]
1 passed, 5 deselected, 2 warnings in 5.18s
```
Recommendation, not acted on (out of my territory — `pyproject.toml` is not `tests/**`):
if this test is meant to run unconditionally in CI, `gradio` (and `spaces`) need adding to
the `dev` dependency group, which trades away the isolation `requirements.txt`'s own
comment argues for (gradio's pydantic ceiling vs. this project's pydantic floor). I left
that trade to whoever owns `pyproject.toml`, rather than making it myself by editing a file
outside `tests/**`.

**`src/integrations/cicd/agents/investigator.py`'s diff** is comment/docstring-only —
diffed it directly (`git diff`) rather than trusting the handoff's description, confirmed
no line outside a comment/docstring changed, so no new test is owed for it.

**Zero live network, zero Gemini quota this round.** Every new test uses either a stub
`LlmClient` that raises `AssertionError` if ever called (for the two "must fail before
reaching the model" tests), or a minimal `StubLlm` returning canned JSON (for the one
positive round-trip test) — the same pattern `test_replay_e2e.py` already established. No
`respx` was needed since nothing under test makes an outbound HTTP call.

**`_guard_real_db_untouched` / `isolated_settings`** (autouse, `tests/conftest.py`) cover
every test in the new file automatically — no test opens `./data/harness.db`, all use
`tmp_db_path` via `AppContext`.
- `D:\Documents\harness_project\tests\unit\test_retry_delay_extraction.py` (edited — 7 stale values under the new 20s ceiling, 1 assertion updated to demonstrate the clamp instead of verbatim passthrough)

---

# Final Phase 1 gate — RFC 9457 error paths + detail scrubbing (findings 5, 6)

## VERDICT           PASS

`review.md` findings 5 and 6 landed in `src/api/main.py`: three new exception handlers
(`RequestValidationError`, `StarletteHTTPException`, catch-all `Exception`) and a
`Redactor.scrub()` pass over `problem()`'s whole body. One pre-existing test
(`test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500`, both parametrisations)
pinned the old bare-500 shape as a negative control and needed updating — done, keeping
its actual claim (the mount defect still fails the request) while updating only the body
shape. Eight new tests close the two findings directly. Full suite, `ruff`, and `mypy
--strict` (both the 2.3.1 pin of record and the bare-PATH 1.14.1) are all clean.

## Gate results

**1. Full suite.**
```
$ uv run pytest -q
........................................................................ [ 21%]
........................................................................ [ 42%]
........................................................................ [ 63%]
........................................................................ [ 84%]
.....................................................s                   [100%]
341 passed, 1 skipped, 2 warnings in 26.89s
```
Expected: baseline 333 passed / 1 skipped + 8 new tests (`tests/integration/
test_error_body_shapes.py`) = 341 passed / 1 skipped. **Matches exactly.** The two
previously-failing parametrisations of `test_mounted_subapp_lifespan_never_fires_
healthz_then_bare_500` are back to passing (updated, not deleted or weakened — see
below); no other test's pass/fail status changed. The 1 skip is the pre-existing,
disclosed `gradio`-absent skip from the prior round, unchanged. y

**2. `ruff check src/ tests/ app.py`**
```
$ uv run ruff check src/ tests/ app.py
All checks passed!
```
y

**3. `mypy --strict src/harness`**
```
$ uv run mypy --version
mypy 2.3.1 (compiled: yes)
$ uv run mypy --strict src/harness
Success: no issues found in 14 source files

$ mypy --version                    # bare PATH, not the pin of record
mypy 1.14.1 (compiled: yes)
$ mypy --strict src/harness
Success: no issues found in 14 source files
```
y — both versions agree, no divergence to flag. (`mypy --strict` was run against
`src/harness` only, per the gate's literal invocation; `src/api/main.py`'s new handlers
are covered by `ruff` and by the test suite, not by this specific mypy invocation — the
same scope the prior two gates used.)

## Tests written

`D:\Documents\harness_project\tests\integration\test_error_body_shapes.py` (new file, 8
tests) — drives every one of the four new/changed surfaces over real HTTP with
`TestClient`, verifying api-surface's handover rather than trusting its docstrings or its
handoff report:

- **`test_422_is_problem_json_and_does_not_echo_the_submitted_body`** — plants a
  distinctive sentinel (`sentinel-value-be9a2c17-should-never-be-echoed`) as both an
  unknown field's *name-adjacent* value and inside `subject`, posts a malformed
  `POST /v1/runs`, and asserts the sentinel is absent from the raw response text (not
  just from a re-serialized subset of it) — the exact FastAPI-default-422 body-echo
  finding 5 names. Also pins `content-type == "application/problem+json"` byte-for-byte
  and the exact `detail` string finding 5's handler builds
  (`"body.idempotency_key: Field required; body.not_a_field: Extra inputs are not
  permitted"`) — tight enough that a regression to the bracketed-list default shape, or
  a change in wording, fails this test rather than passing on a loose substring check.
- **`test_422_error_detail_names_missing_fields_by_location_only`** — isolates the
  `loc`/`msg`-only claim from the no-echo claim above with a request that supplies no
  caller value at all, and asserts the body is not the bracketed-list shape
  (`not detail.startswith("[")`).
- **`test_unmatched_route_returns_problem_json_404_not_the_framework_default`** and
  **`test_wrong_method_on_a_known_route_returns_problem_json_405`** — the bonus item
  api-surface's handover names ("worth a test-verifier assertion, not previously
  covered"): a genuinely unmatched path and a matched path with the wrong method now
  both return `problem()`'s shape (`title`, `instance`, `application/problem+json`)
  rather than FastAPI's default `{"detail": "Not Found"}` `application/json`.
- **`test_no_existing_route_depends_on_the_frameworks_default_404_shape`** — the
  instruction to verify api-surface's judgement call rather than accept it. Confirmed:
  every 404 this application's own routes deliberately produce (`GET /v1/runs/{id}`,
  `GET /v1/runs/{id}/trace`, `POST /v1/replay/{unknown}`) already went through
  `problem()` — and was RFC 9457-shaped — *before* this round, so nothing in this suite
  had a contract riding on the framework's bare default. Cross-checked directly with
  `grep -rn "raise HTTPException" src/ app.py` → zero matches (only a docstring mentions
  the string `HTTPException(409, ...)` as a *future* example), confirming the "no route
  raises `HTTPException` today" premise the reasoning rests on, not just the shape of
  the 404s it produces.
- **`test_unhandled_route_exception_returns_problem_body_with_run_id_and_no_leaked_message`**
  — a real, successful replay run all the way through the orchestrator (stub `LlmClient`,
  real `ReplayToolGateway`, real fixture), with `registry.save(outcome)` — the one step
  in `_execute` that runs *after* `request.state.run_id` is set — replaced with a
  function that raises `RuntimeError("unmistakable-exception-message-3fae91-must-not-leak")`.
  Asserts `500`, `application/problem+json`, the fixed generic `detail` string
  byte-for-byte, the marker absent from the full response text, and `run_id` present and
  ULID-shaped. Deliberately reaches the catch-all through a real HTTP round trip with a
  real `run_id` in scope, rather than duplicating the mount test's prompts-directory
  reproduction (that one stays owned by
  `test_startup_validates_prompt_templates_under_mount.py`).
- **`test_problem_detail_scrubs_a_credential_shaped_string`** — calls `api_main.problem()`
  directly (a plain function, no HTTP needed) with `detail=f"upstream said: {SECRET_TOKEN}"`
  using the identical `ghp_`-shaped token `test_replay_response_scrubbing.py` already
  uses; asserts the raw token is absent from the response body, `REDACTION_PLACEHOLDER`
  (`"***REDACTED***"`, imported from `src.harness.observability`, not restated as a
  literal) is present, and `detail` equals `"upstream said: ***REDACTED***"` exactly.
  Calling the function directly rather than only through a route pins the guarantee
  finding 6 asks for structurally — it must hold for *any* `detail`, not only the
  constants every call site happens to author today.
- **`test_problem_detail_without_a_secret_is_unaffected_by_the_scrub_pass`** — the
  non-vacuity control: an ordinary `detail`/`run_id` pair (the shape every real call
  site in `src/api/main.py` actually produces) round-trips byte-for-byte, so the test
  above is pinning "secrets get scrubbed", not "the scrub pass mangles everything".

**Updated (not source, not weakened):**
`D:\Documents\harness_project\tests\unit\test_startup_validates_prompt_templates_under_mount.py::
test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500` (both parametrisations).
Was pinning the *old* bare `500 text/plain "Internal Server Error"` as the mount defect's
symptom; the catch-all handler this round adds now converts that same defect into `500
application/problem+json`. Reproduced the exact new body directly first (`uv run python`
against the real mounted-app fixture) before touching the test, confirming it matches
api-surface's handover byte-for-byte:
```
500
{'content-length': '219', 'content-type': 'application/problem+json'}
{"type":"about:blank","title":"Internal Server Error","status":500,"detail":"An unexpected error occurred while processing the request.","instance":"/v1/replay/real_regression","run_id":"run_01M23RQSPHYRE0ZYD923RAEDG9"}
```
Updated assertions: status stays `500`; `content-type` is now `application/problem+json`;
the body is valid JSON with `type`/`title`/`status`/`instance`/`detail`/`run_id`; `detail`
is the fixed generic string (`"Internal Server Error"` moved to `title`); `run_id` is now
asserted *present* and ULID-shaped (inverted from the old `"run_id" not in replay.text`);
and — new, not present in the old version — `replay.text` is asserted to contain neither
`"does-not-exist"` (the broken directory's name) nor `"FileNotFoundError"` nor the string
`"investigator"` (case-insensitively), closing the loop on "the exception's own message
does not leak" for this specific reproduction too, not only for the new dedicated test.
The test still proves what it was written to prove — the request still fails, because the
mounted sub-app's lifespan still never ran and `validate_prompt_templates()` still never
executed; a regression that removed `app.py`'s hand-call would still be caught by this
test (it would still 500, unchanged), and a regression that made the catch-all itself
swallow the failure into a 200 would also still be caught (status asserted `== 500`).
Docstring rewritten to state this explicitly, so a future reader does not mistake the
polite new body for the mount defect having been fixed.

## Failures

None against source. The one pre-existing test failure named in the dispatch
(`test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500`, both params) was
expected and is now fixed under `tests/**`, confirmed via `git status --porcelain`
touching only `tests/unit/test_startup_validates_prompt_templates_under_mount.py`
(edited) and `tests/integration/test_error_body_shapes.py` (new) — no source file edited
to reach green.

## Coverage gaps

None against the dispatch's explicit ask. All three items from "Tests owed for findings
5 and 6" are covered by name (422 no-echo, catch-all run_id + no leaked message,
credential-shaped `detail` scrubbed), plus the two bonus items api-surface's handover
flagged (verify-don't-trust the `HTTPException` routing reasoning; the unmatched-path
404/405 case). Broader Phase-1-out-of-scope items
(`test_guardrails.py`, `test_fingerprint.py`, `test_evaluator.py`, `test_no_secret_leak.py`,
`test_idempotency.py`, `contract/test_tool_gateway_contract.py`) remain correctly absent,
as recorded in every prior section of this file — no Guardrails, Evaluator, Memory, or
multi-gateway contract exist yet.

## Notes for the reviewer

**On the `StarletteHTTPException` routing decision, verified rather than accepted, as
asked:** `grep -rn "raise HTTPException\|HTTPException(" src/ app.py` finds exactly one
hit, a docstring in `src/api/main.py` itself describing a *future* Phase-2 example
(`raise HTTPException(409, ...)`), not a live call site. No route or test in this repo
depends on FastAPI's default `{"detail": ...}` 404/405 shape today —
`test_no_existing_route_depends_on_the_frameworks_default_404_shape` pins this
positively (every existing app-generated 404 already went through `problem()` before
this round) and `test_unmatched_route_returns_problem_json_404_not_the_framework_default`
/ `test_wrong_method_on_a_known_route_returns_problem_json_405` pin the two
framework-generated cases this round newly changes. **api-surface's reasoning holds** —
this is not a contract change against anything this suite (or, so far as `grep` can show,
this codebase) relies on. Worth naming for the record: this is a verification against
*this repository's current state*, not a guarantee about the Hugging Face Space's own
history or any external consumer that might have scraped the old 404 shape — outside
what a test suite can check.

**`_execute`'s `registry.save` monkeypatch is instance-level, not a route hand-call.**
`api_main.registry` is a module-level singleton shared across the whole process
(`RunRegistry()`, `src/api/main.py:46`); `monkeypatch.setattr(api_main.registry, "save",
...)` patches the instance method for the duration of the test only, auto-restored by
the `monkeypatch` fixture teardown like every other `monkeypatch.setattr` in this suite.
Chosen over adding a throwaway route or breaking the prompts directory again because it
reaches the catch-all through the *real* success path (real orchestrator, real gateway,
real fixture, `run_id` genuinely present) with a distinctive, unambiguous failure
message, and because editing `src/api/main.py` to add a test-only route is out of this
agent's territory.

**Zero live network, zero Gemini quota this round.** Every new test uses either a stub
`LlmClient` (`StubLlm`, imported from `test_replay_e2e`, or a local
`_NeverCalledLlm` that raises `AssertionError` if ever reached — none of the
validation/404/405/direct-`problem()` tests should ever get close to the model) or calls
`problem()` directly with no I/O at all. No `respx` needed; nothing under test makes an
outbound HTTP call.

**`_guard_real_db_untouched` / `isolated_settings`** (autouse, `tests/conftest.py`) cover
every test in the new file automatically — every fixture uses `tmp_db_path` via
`AppContext`, confirmed by the full-suite run above completing without that guard's
assertion firing.

## Files touched (all under `tests/**`)

- `D:\Documents\harness_project\tests\integration\test_error_body_shapes.py` (new, 8 tests)
- `D:\Documents\harness_project\tests\unit\test_startup_validates_prompt_templates_under_mount.py`
  (edited — updated `test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500` for
  the new RFC 9457 catch-all body shape; module docstring updated to explain why; no
  other test in the file touched)
