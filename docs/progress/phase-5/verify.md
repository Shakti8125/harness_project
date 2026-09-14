# Phase 5 — Verify block, literal expected vs actual

Run 2026-09-14 (13:45–14:35 IST) against `66e2448` (the build commit), locally. **The
Pacific quota day was already spent** (handoff §1: 18 calls by 13:30 IST), so this record
is in two parts: what ran today without a model call — every free step, the in-process
twins of the live ones, and a smoke of the real server under `llm_bad_json:9` — and what
waits for the reset (~12:30 IST, 2026-09-15) with its budget stated. Nothing below is
claimed that did not run.

The block as amended by this phase's PLAN.md note (item 10): step 1's page and component
list are pinned in-process; step 2 is the leak test; step 3's `401` is free and its signed
delivery is three live calls; step 4's redelivery is free; step 5 is user-gated.

## Step 1 — the trace view and the component set

**In-process** (`tests/integration/test_trace_view.py::test_verify_step_1_trace_view_and_component_set`,
stub model, `POST /v1/replay/real_regression`):

```
spans: 30  (>= 12)
[.spans[].component] | unique:
["agent","context_manager","evaluator","gateway","guardrails","llm","memory","orchestrator"]
```

**Matches** — and did not before this phase: the Space's Phase 4 trace carried five of the
eight (`phase-4/verify.md` §"Deploy"). The page (`GET /runs/{id}/view`, `200 text/html`,
~30 kB) draws all 30 spans as waterfall rows, carries every stage card, the diagnosis's
`self-reported 0.92 → final 0.97` with `evidence_fully_verified +0.05` itemised, the one
citation beside its `verified · exact` verdict, and the remediate card quoting
`- id: open-fix-pr … effect: require_approval` from the loaded policy beside three
`require_approval` decisions. The escalated variant (fault
`diagnostician_fabricate_citation`) shows `evidence_refuted`, the refuted citation with
`quote not found in log:job/601234567`, and the `gated` remediate stage.

**Real server, no model** (14:27 IST; `uv run uvicorn src.api.main:app --host 127.0.0.1
--port 8000` with `HARNESS_FAULT_INJECT=llm_bad_json:9` over a scratch database): the
signed `flaky_test` delivery's run escalated `invalid_output` after 20 spans across
`agent, context_manager, gateway, llm, memory, orchestrator` (the run never reached the
evaluate or remediate stage, so those two components are correctly absent), and
`GET /runs/{id}/view` rendered it — `200 text/html`, 22.7 kB, the escalation card reading
`invalid_output`.

**Live, pending quota:** the literal step — `POST /v1/replay/real_regression` on the real
model, `Start-Process` the page, the `jq` line — is three calls.

## Step 2 — no secret ever lands anywhere

```
uv run pytest tests/test_no_secret_leak.py -q
1 passed in 10.54s
```

**Matches.** What the test does, so the number means something: sets
`HARNESS_GITHUB_TOKEN=ghp_SENTINEL…01`, `HARNESS_GEMINI_API_KEY=AIzaSENTINEL…02`,
`HARNESS_GITHUB_WEBHOOK_SECRET=whsec_SENTINEL…03` and
`HARNESS_ESCALATION_WEBHOOK_URL=https://hooks.example.com/…SENTINEL04`; runs all five
recorded scenarios through the API (`real_regression`'s log carries the pasted
`ghp_FIXTURELEAKSENTINEL…` token — 4 occurrences in the fixture); one `low_confidence`
escalation delivered to a webhook answering `500` (the notifier's request body and headers
are scanned too, and `delivery_error` is recorded without the URL); one delivery refused
`401` for a forged signature and one accepted `202`; and one live-mode delivery whose
mocked GitHub answers a `403` echoing the token, which reaches
`final.bundle.gateway_errors` as `forbidden (403): … ***REDACTED*** …`. Then zero
occurrences of every sentinel in 700+ `trace_span` rows, every `escalation` row, captured
stdout/stderr and every log record at `DEBUG`, the raw bytes of the SQLite file (WAL
included), and every JSON and HTML body served — with the placeholder present in the
stored excerpt (`Authorization: token ***REDACTED***`), so the scan is not vacuous.

**It bites:** the same test with `deps.SECRET_PATTERNS = ()` (the regex barrier off, the
registry on) fails on the planted token — `sentinel(s) found in log records:
{'ghp_FIXTURELEAKSENTINEL00000000000000000': 4}` — which is exactly the credential that
never passed through `Settings`.

## Step 3 — webhook: signature enforcement

```
curl.exe -s -o nul -w "%{http_code}" -X POST 127.0.0.1:8000/webhooks/github -H "X-GitHub-Event: workflow_run" -d '{}'
401
```

**Matches** (14:27 IST, the real server). The body carries `WWW-Authenticate: HMAC-SHA256
realm="webhooks/github"` and a problem document naming the header. Under `llm_bad_json:9`:

```
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json --wait
202 {"run_id":"run_01M2FJ5CA5SZMQWB6BQ13J3T1K","status":"in_progress"}
run_01M2FJ5CA5SZMQWB6BQ13J3T1K: escalated
```

`202` matches; `escalated` is the fault (no model call was made; `invalid_output` after
three junk answers per agent), where the live run reads `completed`. The in-process twin
(`test_webhook_e2e.py::test_signed_flaky_delivery_is_202_then_completed`) pins
`202` → `completed`, `flaky_test`, `rerun_failed_jobs` executed dry-run, and
`requested_by = webhook:github:<the delivery GUID>` on the run span.

**Live, pending quota:** the signed delivery on the real model is three calls.

## Step 4 — idempotency: redelivery does NOT run twice

```
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json
200 {"run_id":"run_01M2FJ5CA5SZMQWB6BQ13J3T1K","status":"deduplicated","original_run_id":"run_01M2FJ5CA5SZMQWB6BQ13J3T1K"}
select count(*) from run where idempotency_key='cicd:c6b95a115fa82abbf819069aba193c70';   -> 1
select count(*) from observation where run_id='run_01M2FJ5CA5SZMQWB6BQ13J3T1K';            -> 0
```

**Matches** on the response and the `run` count (14:28 IST, the same server, the same
file — `GET /v1/runs` lists one run). The `observation` count is `0` *on this server*
because the fault-injected run died at the Diagnostician and an observation needs a
diagnosis; the in-process twin
(`test_webhook_e2e.py::test_redelivery_is_deduplicated_and_runs_nothing`) pins `1` and
`1` on a completed run, and pins that the trace holds exactly one run's three `llm.attempt`
spans. Appendix C's "Verified by" test,
`tests/integration/test_idempotency.py::test_concurrent_duplicate_deliveries`, fires
**five identical signed deliveries concurrently** on the live-gateway path
(`HARNESS_GATEWAY=github`, `HARNESS_DRY_RUN=false`, GitHub mocked) and pins five `202`s
naming one run, one `run` row, one `observation` row, exactly one
`POST …/rerun-failed-jobs` reaching GitHub, and a sixth, later delivery answering
`deduplicated`.

**Live, pending quota:** the redelivery itself is free; it follows step 3's live run.

## Step 5 — live end to end

**Not run. User-gated**, in four parts, none of which this session may take alone:
the demo repository (`scripts/seed_demo_repo.sh <you>/harness-demo-repo` creates a public
repository on the user's account), a fine-grained PAT (Appendix E's scopes), the Space's
secrets and variables (`HARNESS_GATEWAY=github`, `HARNESS_ALLOWED_REPOS`, the token, the
webhook secret — a change to the Space's exposure: a real repository and a real token,
writes still dry-run), and the redeploy of this tree to the Space. Then
`gh workflow run flaky.yml`, Recent Deliveries `202`, Redeliver `202` with no second run,
and the `curl … /v1/runs | jq` line: three live calls. The script is written and
syntax-checked; its usage and refusal paths were exercised (`--help`, a bogus flag); it
was not run against GitHub.

## The eval's two queued rows (carried from Phase 4)

`uv run python scripts/eval.py --runs 1 --llm gemini --scenario cold_start --scenario dependency_break`
— six calls, pending the same reset. The stub gate still passes on this tree with the
`cold_start` run id change:

```
uv run python scripts/eval.py --runs 5 --concurrency 1 --llm stub
eval (stub, 5 run(s) x 5 scenario(s), concurrency 1, db fresh-per-run)
  category_accuracy          1.00 (25/25)
  forbidden_actions_executed 0
  escalation_rate            0.20
  cold_start         5/5 correct, 0 label miss(es), 5 escalated, verdicts {'pass': 5}
  ... (the other four 5/5, 0 escalated)
exit 0
```

(2026-09-14 14:32 IST, on the build commit; `eval_report.json` says `llm: stub`.)

## Gate

Recorded in `test-verifier.md`: ruff clean, `mypy --strict src/harness` clean (17 files),
`mypy src` at the 8 pre-existing errors, **854 passed, 2 skipped**.
