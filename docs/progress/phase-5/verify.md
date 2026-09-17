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

## Fix round (2026-09-14, `9e3b4df`)

The audit's fix round changes none of the block's expected values. What it changes
about the steps above, so the record stays honest:

- **Step 1**: the component list is unchanged (eight). A replay of `cold_start` now makes
  one more read (`get_commit`, from the new fixture) and its trace one more
  `gateway.invoke` span; `baseline_kind` stays `none`, nothing degraded.
- **Step 2**: `tests/test_no_secret_leak.py` has a fifth stage -- a live-mode write whose
  `403` echoes the token and escalates `tool_failure` -- so the log-record scan covers the
  two lines the audit found unscrubbed. With log redaction off it fails on exactly those
  (`test-verifier.md`, "Ordering"). Still `1 passed`, now in 13.5 s.
- **Steps 3-4**: unchanged; the in-process twins pass on the fixed tree.
- **Step 5**: the seed script's `--force` and `--webhook-only` semantics changed
  (amendment 14); the branch-push scenarios now resolve to `default_green` (amendment
  12), which is what makes the step's `real_regression` reachable at all.

The stub gate on the fixed tree, 18:02 IST: `eval.py --runs 1 --concurrency 1 --llm stub`
-> `category_accuracy 1.00 (5/5)`, `forbidden_actions_executed 0`, `cold_start 1/1
correct, 0 label miss(es), 1 escalated` (`policy_denied` by the cold-start clause, as
labelled), no degraded component on any row. `scrub_fixtures.py --check`: clean.

## Live attempt 1 — 2026-09-15 21:08–21:25 IST (`2c79035`, the Pacific day's first spend)

**Deploy.** `git push space master:main` (`bb0abf3..3216a41`, then `..2c79035`) on the
user's instruction. The Phase 5 image answered about a minute after each push;
`scripts/check_space.py --wait-for-new-build` at 21:08 and again after the second push:
`healthz` ok, `readyz` with `migrations_applied`, both lists empty (no volume, fresh
container), the unsigned `POST /webhooks/github` **`401`** with
`WWW-Authenticate: HMAC-SHA256 realm="webhooks/github"` (the old image had no such route
and answered `404`), an unknown run's `/view` `404 application/problem+json`, no
credential shape in the 505 bytes served. The Hub reports the Space `RUNNING` at
`2c79035ce6cb…` on `zero-a10g` (21:18 IST).

**The regression the first live request found.** Step 1's first `POST
/v1/replay/real_regression` (21:10 IST) came back `escalated (llm_upstream)` -- and the
server log held a `--- Logging error --- ValueError: not enough values to unpack
(expected 5, got 0)` traceback per request: the fix round's log record factory cleared
`record.args` and uvicorn's `AccessFormatter` unpacks them. Fixed, tested (three tests,
one of them a five-tuple access record), proved against a real uvicorn on port 8011
(`0` logging errors; `"GET /v1/runs?***REDACTED*** HTTP/1.1" 200` served -- the token
shape in the query string redacted), 898 passed, committed as `2c79035`, redeployed.
The record of it is `backlog.md`, "Audit provenance".

**Step 1 did not complete: the provider was overloaded and the overload burned the
day's quota.** Five replays between 21:10 and 21:22 IST: every Investigator or
Diagnostician attempt but one answered `503` (Gemini's overload, no `Retry-After`), the
client retried each four times with sub-second backoff, and the harness escalated
`llm_upstream` each time -- correctly, and at no token cost. But **Google counts a
`503`'d request against the free tier's 20 requests/day**: the local database shows 20
`llm.attempt` spans on the Pacific day (1 ok, 18 × `503`, 1 × `429`), and a direct
probe at 21:23 IST answered `429 RESOURCE_EXHAUSTED … generate_content_free_tier_requests,
limit: 20, model: gemini-3.6-flash`. Nothing else ran on the model today. Steps 1, 3 and
4 and the eval's two rows are pushed to the next Pacific day (~12:30 IST, 2026-09-16),
and a `503` storm is now a known way to lose a day (`backlog.md`).

Free of the model, on the fixed tree: the local server's own access log was clean
across the five replays (`0` logging errors, `4` served `POST` lines); the last run's
trace shows the Investigator succeeding (`llm.attempt` ok, 1) before the Diagnostician's
`503`, `503`, `429` -- the recovery path doing exactly what Appendix B.1 says, on a
provider that then charged for it.

## Live — 2026-09-17 23:37–23:45 IST (`3a0b1f6`; the day's first spend, 21 requests)

A one-word probe answered `OK` first (1 request). Then, on a local server over
`./data/harness.db` with the real model:

**Step 1.** `POST /v1/replay/real_regression` -> `200` in 37.8 s: `run_01M2R8VWQMXD36ATHMKCD08G5F`,
**`awaiting_approval`**, category **`real_regression`**, self-reported 0.95 -> final
**0.99**, verdict `pass` with **5 verified, 0 refuted, 0 unverifiable** (`commit_in_range`
on `e2cdf1b4…`, `file_in_diff` and `quote_exists` on `src/pricing/discount.py`, two
`test_in_log` on `log:job/601234567`), `suspected_commit_sha` the `+ 1` commit, the fix
PR held under `open-fix-pr` (three `require_approval` decisions). One `llm.attempt`
answered `503` and the retry succeeded (4 requests).

```
[.spans[].component] | unique
["agent","context_manager","evaluator","gateway","guardrails","llm","memory","orchestrator"]
spans: 35
```

**Matches**: eight components, >= 12 spans. `GET /runs/{id}/view`: `200 text/html`,
35 345 bytes, 35 waterfall rows, five `verified` pills beside the citations, the
`5 verified / 0 refuted / 0 unverifiable` counts, the `pass` verdict, the
`evidence_fully_verified` adjustment itemised, `open-fix-pr` quoted from the policy
beside the three `require_approval` decisions, and the one `error` pill on the `503`'d
attempt -- opened in the browser (`Start-Process`).

**Step 3.**
```
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json --wait
202 {"run_id":"run_01M2R8YVS0TT2MB1W99236Q3D5","status":"in_progress"}
run_01M2R8YVS0TT2MB1W99236Q3D5: completed (flaky_test 0.97)
```
**Matches** (3 requests, all ok): `flaky_test` at 0.97, verdict `pass`, `rerun_failed_jobs`
allowed by `retry-suspected-flaky` and executed dry-run; the run span's `requested_by` is
`webhook:github:06e730b2-f198-483d-9e6b-b0e89b1c380c`, the delivery's GUID.

**Step 4.**
```
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json
200 {"run_id":"run_01M2R8YVS0TT2MB1W99236Q3D5","status":"deduplicated","original_run_id":"run_01M2R8YVS0TT2MB1W99236Q3D5"}
sqlite3 ./data/harness.db "select count(*) from run where idempotency_key='cicd:c6b95a115fa82abbf819069aba193c70';"        -> 1
sqlite3 ./data/harness.db "select count(*) from observation where run_id='run_01M2R8YVS0TT2MB1W99236Q3D5';"                -> 1
```
**Matches**: one run, one observation, the trace holding one run's three `llm.attempt`
spans, no model call on the redelivery. The server's access log: `0` logging errors.

**The eval's two rows** (7 requests, one of them a `503` retry): `category_accuracy 1.00
(2/2)`, `cold_start` escalated `policy_denied` on the cold-start clause as labelled,
`dependency_break` right on category with one `effect` label miss (the Remediator filed
a ticket where the label expects the fix PR held) -- `docs/progress/phase-4/verify.md`
§4 has the report and the reading; the live category number is **5/5** across the two
days.

**The Space** (`scripts/check_space.py --replay real_regression`, 23:44 IST): every free
check passed again; the replay through the real model got `503`, `503`, then an
Investigator success, then `429` twice at the Diagnostician -- the 21st request of the
day -- and escalated **`rate_limited`**, correctly. 19 spans across six components (the
evaluate and remediate stages never ran), `GET /runs/{id}/view` **`200`**, 20 560 bytes,
the escalation card reading `rate_limited`, and no credential shape in the 38 501 bytes
served. The deployed model path is proven as far as the quota allowed: the Space called
Gemini, got an answer, and rendered the outcome. The eight-component line on the Space is
the one thing this closing did not see live; the local run above is its record.

## Step 5 — not run, and the project closes here

The demo repository was never created, no PAT minted, the Space never switched to
`HARNESS_GATEWAY=github`. Step 5 stays user-gated and undone; the user closed the
project on 2026-09-17 with Phase 6 not started. `phase-5-green` is tagged on the
commit that records this section, with that stated.
