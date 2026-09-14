# Phase 5 — gate verdict

**PASS** on `66e2448` (the build commit), coordinator-run 2026-09-14 14:20 IST, as Phases
2–4's gates were (dispatch, "How this phase is being run").

```
uv run ruff check .                     All checks passed!
uv run mypy --strict src/harness        Success: no issues found in 17 source files
uv run mypy src                         Found 8 errors in 4 files   (the same 8 as Phase 2–4:
                                        gateway_replay ×2, investigator ×3, diagnostician ×2,
                                        wiring ×1 -- none in a file this phase added)
uv run pytest -q                        854 passed, 2 skipped in 97s
```

744 → 854: 110 new tests, 2 rewritten, 0 removed.

## What is new, by file

| File | Tests | Pins |
|---|---|---|
| `tests/unit/test_webhook_signature.py` | 27 | `sign`/`verify_signature` round trip, upper-case hex, seven malformed headers, wrong secret, different bytes, blank secret verifies nothing; `classify_event` accept / seven ignores / four malformed shapes; `delivery_id` GUID-only |
| `tests/unit/test_trace_view.py` | 9 | waterfall depth-first order, bar offsets/widths and the floor, missing trace; citations joined by index; the evaluate card's verbatim reason; rules quoted from the engine (`open-fix-pr`, `<forbidden>`, `<default>`, unknown); header/stages; `delivery_error`; autoescaping of a `<script>` in a citation |
| `tests/unit/test_gateway_github_writes.py` | 22 | Appendix C per tool: branch created / existing `cached` / `422` race / dry run / `404` on a write; file with the current sha / new file without / `409` hard stop with no retry / dry run never echoes `content_b64`; PR returned for the head / draft + labels / `422` under `errors[].message` and under `message`; issue commented on via the marker / filed with the marker / dry run; per-key write cache; forbidden refused with zero requests; missing args ×4 |
| `tests/unit/test_gateway_replay_writes.py` | 7 | every synthesized write's shape, deterministic ids, `dry_run=false`, key cache, invalid args, `gateway.invoke` spans including a refusal, silence without a recorder |
| `tests/unit/test_trace_completeness.py` | 6 | the four `memory.*` spans and their attributes, a locked store's error span, bookkeeping untraced, no recorder → nothing; `context.assemble` with the truncation report, `assemble_traced == assemble` unrecorded |
| `tests/unit/test_escalation_reason_drift_guard.py` | +2 | `evidence_unverifiable` in both Literals; the gate's reason follows the counts |
| `tests/integration/test_webhook_e2e.py` | 14 | Verify steps 3–4 over HTTP: `401` unsigned / forged / blank secret; `202` → `completed` with the GUID on the run span; redelivery `deduplicated` with the `sqlite3` counts (1, 1) and one run's `llm.attempt` spans; `202 in_progress` while running; four `204`s; `400`; replay-mode `403`; live-mode `403`; the signature never served |
| `tests/integration/test_idempotency.py` | 1 | Appendix C's "Verified by": five concurrent signed deliveries on the live path, one run, one row, one observation, one `rerun-failed-jobs` POST, a sixth delivery deduplicated |
| `tests/integration/test_trace_view.py` | 4 | Verify step 1 in-process (≥ 12 spans, the eight components, the page's contents); the escalated run's page; `404` problem document; rendered from the stored, scrubbed body |
| `tests/integration/test_record_fixture.py` | 5 | the recording replays like the committed scenario (same job, diff, anchors; layout file for file; the echoed token scrubbed; `scenario.yaml` keys); allowlist and overwrite refusals; `scrub_text` keeps the sentinel and rewrites `ghp_`, a PEM block, `api_key=`; `--check` reports and rewrites nothing, the committed tree is clean |
| `tests/test_no_secret_leak.py` | 1 | Verify step 2 (`verify.md` §2 says what it covers and that it fails with a barrier removed) |

Rewritten: `test_approvals_e2e.py::test_approve_re_evaluates_and_executes_through_the_gateway`
(three dry-run executions and a completed run, not a "not implemented" failure);
`test_replay_e2e.py::test_trace_is_persisted_and_readable` (agent spans hang off stage
spans, stage spans off the run span); `test_gateway_github.py::test_unimplemented_write_tools_…`
(narrows `IMPLEMENTED_WRITE_TOOLS` to exercise the path);
`test_background_run_supervision.py` (three `_spawn_run` call sites).

## Ordering, for the record

Every fix this phase made to something the suite already covered was made *after* the
test that pins it was seen to fail on the previous state: the trace-shape test failed on
the pre-stage-span tree; the approval test failed the moment the write tools gained a body;
the `422 errors[].message` test was written asserting the *old* behaviour (a tool failure),
passed, and was flipped together with the `_message_of` widening so the second run proved
the fix; the leak gate was run once with `deps.SECRET_PATTERNS = ()` and failed on the
planted token before the record above was taken.

## Fix round -- gate on `9e3b4df`

**PASS**, coordinator-run 2026-09-14 18:05 IST (the audit's fix round; provenance in
`backlog.md`).

```
uv run ruff check .                     All checks passed!
uv run mypy --strict src/harness        Success: no issues found in 17 source files
uv run mypy src                         Found 8 errors in 4 files   (the same 8)
uv run pytest -q                        892 passed, 2 skipped in 98s
```

854 -> 892: 38 new tests, 5 rewritten, 0 removed.

| File | Tests | Pins |
|---|---|---|
| `tests/unit/test_baseline.py` | 9 | Appendix D's four (`branch_green`, `default_green`, `head_commit_only`, `none`) plus: `parse_subject` carries `default_branch`; the default branch is not asked twice when it is the failing branch; a failing `get_commit` is a degraded `diff`, not a crash; every baseline call carries `before` |
| `tests/unit/test_redaction_of_execution_inputs.py` | 8 | the assignment shapes scrubbed in plain text and *not* through base64; a registered token and a vendor shape still scrubbed through base64 with the ordinary lines beside them intact; the heuristic tier is exactly the assignment shape; `carries_redaction` plain / nested / base64 / clean; the audit's `save_approval`/`get_approval` round trip keeps `DB_PASSWORD = …`; `execute_plan` refuses a placeholder-carrying argument (`invalid_args`, not retryable, the branch executed, the PR never reached) and runs a clean plan untouched |
| `tests/unit/test_log_redaction.py` | 3 | records scrubbed at creation for our logger, a third party's and a `%(key)s` dict; installing twice keeps one factory and the latest redactor; a mismatched format string is left for `logging` |
| `tests/unit/test_fixture_delivery_keys.py` | 2 | one Appendix C key per recorded scenario; `_delivery_key` on `inf` / `nan` / strings / `None` |
| `tests/unit/test_seed_script_invariants.py` | 6 | `main` never force-pushed and the re-seed builds on `FETCH_HEAD`; only `demo/*` replaced; the header's `--force` semantics; `--webhook-only` and the printed next step; the secret never echoed; `bash -n` |
| `tests/unit/test_gateway_github_writes.py` | +6 | labels reconciled on the existing PR (only the missing ones posted), nothing posted when all present, dry run reports `labels_missing`, the label failure names `#9` and its URL; the issue search sends `labels=` and pages to page 2; stops at `ISSUE_SEARCH_MAX_PAGES` and files |
| `tests/integration/test_webhook_e2e.py` | +4 | the ignore log line carries `not_workflow_run` / `not_completed` and never the header text; `1e400`, `-1e400`, `NaN` in a numeric field are `400` |
| `tests/test_no_secret_leak.py` | stage 5 | live mode, `HARNESS_DRY_RUN=false`, `infra_timeout` on a third attempt; the rerun `403` echoes the token under `errors[].message`; the run escalates `tool_failure` with the placeholder in the escalation; then the same scans |

Rewritten: `test_open_pull_request_returns_the_open_pr_for_the_same_head` (the existing
PR gets the labels it lacks); `test_completed_failure_is_accepted` and
`test_everything_else_is_ignored` (the verdict `code`); `test_recording_replays_like_the_committed_scenario`
(`commit` is a commented hint); `test_scrub_rewrites_credentials_and_keeps_the_planted_sentinel`
(the script's own `build_redactor`, which carries the heuristic tier).

## Ordering, for the record

The new tests were run against `deef51e` before any fix: 20 failed, 48 passed across the
eight importable files, with `test_redaction_of_execution_inputs.py` and
`test_log_redaction.py` failing at import (`HEURISTIC_SECRET_PATTERNS`,
`install_log_redaction` did not exist). The four `test_baseline.py` tests that passed on
the old tree are the ones the old chain already satisfied (`branch_green`, `none`, the
single-call case, `before`); `default_green`, `head_commit_only`, the degraded-diff edge
and `parse_subject`'s `default_branch` failed. The leak test's stage 5 was proved to bite
by running it with the log record factory disabled (a scratch module monkeypatching
`deps.install_log_redaction` to a no-op): `sentinel(s) found in log records:
{'ghp_SENTINEL…01': 2}` -- the two lines finding 4 named -- then passes with it on.
