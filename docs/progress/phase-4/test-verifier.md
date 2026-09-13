# Phase 4 — gate verdict

**Verdict: PASS** (coordinator-run, as in Phases 2 and 3 — see `dispatch.md`, "How this
phase is being run"; the audit in `review.md` is the independent pass), **with two live
Verify steps pending the provider's quota reset** (`verify.md`, steps 3a and 3c — both
pinned in-process, both to be re-run before the tag).

Recorded 2026-09-14 at `dbc63ab`.

## Gate

| Step | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run mypy --strict src/harness` | Success: no issues found in 17 source files (15 at `phase-3-green`; `escalation.py` and `faults.py` are new) |
| `uv run pytest -q` | **736 passed, 2 skipped** (was 603 + 2 at `phase-3-green`) |
| `uv run mypy src` (informational, not gated) | 8 errors, all present at `phase-3-green`; none in files this phase added or touched |

The two skips are unchanged: the `gradio`-dependent `app.py` test, and the one that was
already skipped in Phase 2.

## Tests named by PLAN.md and the phase-run brief

| Brief item | Test |
|---|---|
| fabricated-citation refuted (PLAN step 1) | `tests/unit/test_evaluator.py::test_fabricated_quote_refuted`, `::test_paraphrased_quote_verified_fuzzy`, `::test_missing_artifact_is_unverifiable_not_refuted` (+17: the verdict rule parametrised over every tally, the 0.5 boundary, fail-by-share, an unregistered kind, duplicate checkers, the delta table, claim ids, A.6's shape) |
| all five claim kinds, the 0.92 line, missing artifact → unverifiable not refuted | `tests/unit/test_claim_checkers.py` (39 tests: availability; `quote_exists` exact/ANSI-and-timestamp/diff-patch/wrong-locator/multi-line/fuzzy/refuted/empty/missing-log/missing-diff; `file_in_diff` exact/suffix/refuted-with-count/truncated/no-diff/empty-diff; `dependency_bump` seven quote shapes, wrong version, unknown package, suspected-package fallback, no diff, new dependency; `test_in_log` anchor/non-anchor/missing; `commit_in_range` head/prefix/base-not-in-range/no-sha/unknown-range) |
| remediator never invoked; step 2 end to end | `tests/integration/test_evaluator_e2e.py::test_fabricated_citation_escalates_evidence_refuted_and_skips_the_remediator` (status, verdict, reason, `stages[].agent`, the `-0.15`, two model calls, the `evaluation.claim` span, the `db` row) |
| recovery recovers at 2 and escalates at 9 (PLAN step 3) | `::test_llm_bad_json_2_recovers_with_three_attempts_and_one_real_call_per_agent`, `::test_llm_bad_json_9_never_recovers_and_spends_nothing` (incl. the `sqlite3` line), `::test_llm_429_3_completes_on_the_fourth_attempt`, `::test_faults_are_per_run_not_per_process`; the loop alone in `tests/unit/test_faults.py` (17: `parse_fault`, the `env`/unknown/blank guards on `build_fault` and on `AppContext`, per-agent counting with zero inner calls, 429 with no stated delay, recovery at N+1, exhaustion at 9, the fourth-attempt 429 recovery) |
| `warn` downgrades | `::test_warn_downgrades_an_allowed_retry_to_require_approval` (a `flaky_test` copy without its log: `warn`, `require_approval`, `downgraded_from: allow`, nothing executed); `tests/unit/test_guardrails.py::test_warn_verdict_matches_both_write_rules_so_the_harness_can_downgrade`, `::test_downgrade_for_warn_moves_allow_one_step_and_nothing_else` |
| memory written after evaluation (dispatch decision 1) | `::test_the_verdict_is_remembered_after_the_penalty_not_before` (0.80 − 0.15 = 0.65: sighting counted, verdict not tallied, observation at 0.65), `::test_a_verified_diagnosis_is_remembered_with_its_bonus`; `tests/integration/test_remediation_stage.py::test_the_wiring_hands_the_gate_threshold_to_the_evaluate_stage` |
| the approval route reads the stored verdict (decision 8) | `::test_approval_re_evaluates_under_the_stored_evaluation_verdict` (the stored `pass` flipped to `fail` in the store; approve → every decision `deny` naming `evaluation.verdict: 'fail'`, `policy_denied`, nothing executed) |
| escalation channel / webhook (B.4) | `tests/unit/test_escalation_webhook.py` (13: success and the Slack-shaped body, redaction, three attempts then `delivery_error` without the URL in the error or the log, transient-then-success, timeout, `_describe_failure` never echoing httpx's message, the orchestrator awaiting the notifier, a failed or raising delivery never failing the run, `save_run` writing `delivered_at`/`delivery_error`, the composition root's channels and blank-URL handling, a real client against a closed port) |
| `MAX_TOKENS` × 1.5 (handoff §5.5) | `tests/unit/test_recovery_output_budget.py` (6: growth and ceiling, the loop growing the budget the next call reads with the `max_output_tokens` span attribute, the Phase 1 path without a budget, `LLMAgent` sharing its budget and resetting per call, a non-`MAX_TOKENS` validation error leaving it alone) |
| `dependency_break` fixture | `::test_dependency_break_replays_to_its_label`; `tests/integration/test_eval_script.py` (5/5 in stub mode) |
| `scripts/eval.py` as a CI gate (PLAN step 4) | `tests/integration/test_eval_script.py` (4: the stub run's report shape and gate, pricing flags, an unknown scenario as a usage error, `score` on category/effect/commit misses and forbidden executions from the outcome or the trace) |
| `skipped` out of the policy, `warn` in (Phase 2 amendment 2) | `tests/unit/test_guardrails.py::test_skipped_verdict_no_longer_matches_any_write_rule`, `::test_shipped_policy_is_the_amended_plan_text` |
| surface `evaluation` in the run outcome | every e2e test above reads `final.evaluation`; `test_replay_e2e.py::test_trace_is_persisted_and_readable` asserts the fourth `agent.run` span and the `evaluation.claim` name |

## Tests changed by this phase, and why

- `tests/unit/test_guardrails.py` — `facts()` defaults to `verdict="pass"`: a real verdict
  exists now and `skipped` matches no write rule. `test_the_amended_open_fix_pr_rule_matches_
  a_skipped_verdict` became `test_skipped_verdict_no_longer_matches_any_write_rule` (the
  inverse, with `file-ticket` still matching); the shipped-policy pin reads `[pass, warn]`
  on both rules.
- `tests/integration/test_remediation_stage.py` — the stage list is four long
  (`investigate, diagnose, evaluate, remediate`); hand-built `RunState`s carry an
  `evaluation` artifact (`state_with(verdict=...)`, default `pass`), because a state
  without one is judged under `skipped`; the threshold test asserts the evaluate agent
  holds `verdict_threshold` and the Diagnostician holds neither it nor `memory`.
- `tests/integration/test_gateway_error_scoping.py` ×2 — `0.95` → `0.99` and the
  adjustment list is `[evidence_fully_verified]`: the stub's citations all verify, so
  PLAN's +0.05 row fires and clamps at the ceiling. The property under test (no
  `gateway_degraded`, nothing pulled the figure down) is unchanged.
- `tests/integration/test_memory_e2e.py::test_three_gated_verdicts_do_not_lift_a_fourth_
  over_the_gate` — the fourth run's `self_confidence` is 0.60 (0.65 after +0.05), so the
  `< 0.70` assertion and the "+0.10 would clear the gate" premise both still hold.
- `tests/integration/test_replay_e2e.py::test_trace_is_persisted_and_readable` — four
  `agent.run` spans, and `evaluation.claim` among the names.
- `tests/unit/test_prompt_templates_ship_with_the_image.py` — the Diagnostician template is
  version 3.
- `tests/stubs.py` — `dependency_break` (run id 501235417, four citations of four kinds,
  `open_fix_pr`) joins `SCENARIO_RUN_IDS`, `notes_for`, `diagnosis_for` and
  `PLAN_ACTION_FOR_SCENARIO`; `cold_start` shares `flaky_test`'s id and answer.
- `tests/test_layering.py` needed no change, but it caught the first draft of
  `src/harness/faults.py` naming the integration's agents in its docstring (`CI/CD`); the
  wording was made domain-neutral.

## Proof against the pre-build tree

Every new test file fails to import on `phase-3-green` (the modules and symbols it imports
— `harness.escalation`, `harness.faults`, `agents.evaluator`, `claim_checkers.
build_claim_checkers`, `recovery.OutputBudget`, `guardrails.downgrade_for_warn` — do not
exist there), which is the trivial direction. The non-trivial proof is the other way: the 21
pre-existing tests that failed on the new build before their expectations were updated
(`ruff`/`pytest` at the first full run, before any test edit) are the ones listed above, and
each failure was the phase's own change — a fourth stage, `+0.05` on verified citations,
`skipped` leaving the policy — not a regression. No pre-existing test was deleted.

## The fix round (`e580aad`), coordinator-verified

`review.md` (FIX FIRST, five findings) was answered the same day. Per the standing rule of
one independent audit per phase, this round is **coordinator-verified, not re-audited**;
what it contains and how each fix was proven, so a later reader can weigh it:

| Finding | Fix | Test, and the failure it produced on the unfixed tree |
|---|---|---|
| 1 (medium) a `fail` report still tallied a signature verdict above 0.70 | `EvidenceEvaluator._counts_as_verdict(diagnosis, report)` returns False on `report.verdict == "fail"` | `test_evaluator_e2e.py::test_a_refuted_verdict_is_never_tallied_even_above_the_gate` — `failure_signature` read `(3, 'real_regression', '{"real_regression": 3}')`; now `(3, None, "{}")` |
| 2 (medium) httpx logs the webhook URL at INFO | `escalation._ScrubUrl` filter on the `httpx` logger, installed once per URL by the notifier | `test_escalation_webhook.py::test_httpx_request_log_line_never_carries_the_url` — `SECRETPART` and the host appeared in caplog at INFO; now the request line carries `***REDACTED***` |
| 3 (medium) trailing punctuation refuted true `dependency_bump` claims | `_versions_in` strips `.+-` off the token's tail | `test_claim_checkers.py::test_dependency_bump_verified_shapes[pydantic 1.10.13->2.9.2]` and `[... to 2.9.2.]` — both `refuted` ("not 1.10.13-, 2.9.2"); now `verified` (+2 more shapes that already passed, kept as coverage) |
| 4 (low) the v3 prompt promised a commit list the rendering never showed | `render_diff_summary` lists `commit_shas` oldest first, or says the range is unknown | `test_claim_checkers.py::test_diff_summary_renders_the_commit_range_the_prompt_promises` — the intermediate sha was absent from the rendering |
| 5 (low) `scripts/eval.py` built its store before the fault was parsed | `build_context` leaves the store to `AppContext.__post_init__` | `test_eval_script.py::test_build_context_routes_the_store_fault` — `heartbeat` succeeded on a store that should have failed; now raises `MemoryStoreError` after the B.3 ladder |

The proof is by ordering rather than by stash this time: the eight tests were written and
run first, against the tree with no fix applied (`6 failed, 9 passed` — the two extra
`dependency_bump` shapes passed already), then the fixes were applied and the same
selection ran green. Both runs are in the session log; `git stash` was not needed because
no source file had uncommitted edits when the tests were written.

Also in the round: PLAN.md A.8 now shows `OutputBudget` and the `output_budget` keyword
(the audit's contract-drift item), A.2's illustrative gate reason matches the code, and
`faults.py`'s docstring no longer names the integration's fault (residual note).

Gate after the round: ruff clean; `mypy --strict src/harness` clean (17 files); **744
passed, 2 skipped** (736 + 8).
