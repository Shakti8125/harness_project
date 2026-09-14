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
