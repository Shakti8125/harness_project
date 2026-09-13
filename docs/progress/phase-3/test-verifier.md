# Phase 3 — gate verdict

**Verdict: PASS** (coordinator-run, as in Phase 2 — see `dispatch.md`, "How this phase is
being run"; the audit in `review.md` is the independent pass).

Recorded 2026-09-13 at `66208d0`; re-run on the fix-round tree (see the last section) with
the same verdict.

## Gate

| Step | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run mypy --strict src/harness` | Success: no issues found in 15 source files |
| `uv run pytest -q` | **577 passed, 2 skipped** at `66208d0` (was 492 + 2 at `phase-2-green`); **603 passed, 2 skipped** after the fix round |
| `uv run mypy src` (informational, not gated) | 8 errors, all present at `phase-2-green` (19 there); none in files this phase added |

The two skips are unchanged: the `gradio`-dependent `app.py` test, and the one that was
already skipped in Phase 2.

## Tests named by PLAN.md and the phase-run brief

| Brief item | Test |
|---|---|
| fingerprint stability | `tests/unit/test_fingerprint.py::test_stable_across_noise`, `::test_distinguishes_real_difference` (+23 more: each normalisation rule, the three fixture logs, excerpt-equals-raw) |
| repeat flakiness across 4 runs | `tests/integration/test_memory_e2e.py::test_the_fourth_flaky_run_knows_it_has_seen_this_before` (occurrences 3, `likely_flaky`, `memory_agreement` +0.10, the `sqlite3` line verbatim, the prompt carrying the prior-not-evidence clause) |
| retry cap biting on the 3rd | `::test_the_retry_cap_bites_on_the_third_run` (allow, allow, deny, deny; the deny names `memory.retries_for_signature_24h: 2`) |
| memory-outage degrade | `::test_memory_outage_degrades_and_the_cap_fails_closed` (`sqlite_locked`: escalated, `degraded == ["memory"]`, 999, key still computed, no bonus, `503` on the read route) and `::test_a_write_failure_after_diagnosis_degrades_but_keeps_the_diagnosis` |
| pending → passed_on_retry | `::test_a_pending_retry_is_resolved_from_the_rerun_and_infra_stays_pending` |
| cold_start fixture | `::test_cold_start_replay` |
| claim protocol | `::test_replay_with_fresh_false_dedupes_a_redelivery`, `::test_create_run_dedupes_by_idempotency_key`, `tests/unit/test_memory_store.py::test_stale_heartbeat_is_taken_over` (+ fresh-heartbeat hold, chain continuation) |
| approvals re-query memory; `policy_denied`-on-approve pinned | `::test_approval_re_evaluates_against_fresh_memory_and_can_deny`, `::test_approvals_survive_a_new_process` |
| heartbeat | `::test_the_orchestrator_heartbeats_while_a_run_is_active` |
| store, method by method | `tests/unit/test_memory_store.py` (31 tests: lookup/upsert/observation/window/lookback, claim/dedup/takeover/heartbeat, save_run/get_run/list_runs/list_escalations, approvals single-use under `gather`, the B.3 ladder — transient lock retried, non-busy error not retried, fault exhausts) |
| migrations | `tests/unit/test_storage_migrations.py` (10 tests: create, idempotent, pragmas, legacy `spans` carry-over, corrupt-file quarantine, failing migration raises and records nothing, file naming, ships with the image) |

## Tests changed by this phase, and why

- `test_approvals_e2e.py::test_escalations_lists_the_denied_retry_with_its_run_id` — the
  first `flaky_test` replay now *executes* its retry; the escalation moves to the third
  sighting inside 24 h. The registry monkeypatches are gone (nothing is process-global).
- `test_background_run_supervision.py` — the same five properties, read back through a
  `SqliteMemoryStore` on the per-test file; the drain loops wait on the clock because the
  failure record is now a SQLite write.
- `test_error_body_shapes.py` — patches `store.save_run` where it patched `registry.save`.
- `test_replay_response_scrubbing.py` — enters the `TestClient` context so the lifespan
  (migrations) runs; the replay route claims a row before anything else.
- `test_startup_validates_prompt_templates_under_mount.py` — the positive case hand-calls
  `context.initialize()` the way `app.py` does; the reproduction asserts the SQLite table
  name does not leak alongside the prompt failure; the fake context exposes `initialize`.
- `test_replay_e2e.py::test_async_run_is_accepted_then_readable` — polls with a 10 ms pause.
- `test_prompt_templates_ship_with_the_image.py` — template version `2`.
- `test_remediation_stage.py` — docstring only: the no-store orchestrator fails closed.

## Verify block

`verify.md`: steps 1–4 as amended, all as expected, 15 live model calls; Docker boots with
`migrations_applied: true` and the migration file in the image.

## Fix round (after the audit)

Re-run on the fix-round tree, same three gates: `ruff` all checks passed, `mypy --strict
src/harness` no issues in 15 files, `mypy src` 8 errors (the pre-existing ones; the round
added none), **603 passed, 2 skipped**. Twenty-five tests were added, one per audit finding
or sub-case, each run against the stashed `de1365d` source during the round and confirmed
to fail there:

| Finding | Test(s) |
|---|---|
| 1 chain vs `#fresh` rows | `test_memory_store.py::test_chain_membership_is_the_bare_key_or_a_numbered_takeover`, `::test_a_fresh_replay_row_never_answers_for_the_real_key`, `::test_a_stale_fresh_replay_row_is_not_taken_over_by_the_real_key` |
| 2 queued claim goes stale | `test_memory_e2e.py::test_a_queued_run_keeps_its_claim_alive` |
| 3 `failed_again` unread | `test_history.py` (first three), `test_memory_e2e.py::test_a_failed_rerun_withholds_the_flaky_prior_and_the_bonus` |
| 4 gated verdicts build the prior | `test_memory_store.py::test_upsert_without_a_verdict_counts_the_sighting_only`, `test_memory_e2e.py::test_three_gated_verdicts_do_not_lift_a_fourth_over_the_gate`, `test_remediation_stage.py::test_the_wiring_hands_the_gate_threshold_to_the_diagnostician` |
| 5 one page of jobs | `test_history.py` (four probe tests), `test_gateway_github.py::test_list_workflow_run_jobs` (+`per_page`) |
| 6 setup error before the failure | `test_fingerprint.py` (five section tests) |
| 7 approved write unrecorded | `test_history.py` (two `action_observation` tests), `test_memory_e2e.py::test_an_approved_retry_is_recorded_and_counted_by_the_cap` |
| 8 base64 at rest | `test_memory_store.py::test_base64_file_content_is_scrubbed_through_the_encoding` |
| 9 replay script unguarded | `test_replay_script.py::test_a_replay_still_prints_its_outcome_when_the_store_is_down` |
| 10 check-then-act cap | not fixed -- `backlog.md`, "Open" |

`verify.md` is not re-run live (quota); `backlog.md` records why its values stand.
