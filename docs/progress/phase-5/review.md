# Phase 5 -- audit (phase-reviewer, read-only)

Audited at HEAD `deef51e` (build `66e2448`) against `docs/progress/phase-5/dispatch.md`
(normative) and PLAN.md Phase 5 + amendment, A.1, A.3, A.4, A.9, A.12, B.2, C, E.
Every finding below was reproduced by reading the code path end to end or by running the
component in-process; the two HIGH findings were executed (Python, this tree).

## VERDICT              FIX FIRST

Finding 1 silently rewrites the file content an approved fix PR commits; finding 2 means
Verify step 5 as designed cannot produce the scenario it is meant to demonstrate, and
catching it now saves the user the PAT, the redeploy and three live calls.

## Findings

1. [HIGH] src/api/deps.py:86, src/harness/observability.py:213-231, src/harness/memory.py:764, src/api/main.py:1414-1442 -- the new `(api[_-]?key|token|password)\s*[:=]\s*\S{8,}` pattern, applied through the base64 scrub at rest, rewrites ordinary source lines inside the approval row's `content_b64`, and `_execute_approved` executes that stored plan.
   Failure scenario: Remediator drafts `x.py` containing `DB_PASSWORD = os.environ.get("DB_PASSWORD")` -> run suspends `awaiting_approval` -> `save_approval` stores `plan_json` through `_dump_json` (Redactor) -> `_scrub_base64` decodes, matches, re-encodes -> `POST /v1/approvals/{id}` approve executes `create_or_update_file` from `record.plan` -> with `HARNESS_DRY_RUN=false` the committed file reads `DB_***REDACTED***` (also `token = settings.github_token.get_secret_value()` -> `***REDACTED***`, `Client(api_key=settings.api_key)` -> `Client(***REDACTED***` -- the closing paren is eaten). Reproduced through `SqliteMemoryStore.save_approval`/`get_approval` on this tree. PLAN amendment 6 records this as a display-boundary false positive; it is a mutation of an execution input. Under the default `dry_run=true` nothing is sent and `would_have` omits the content, so no existing test can see it.
   Owner: api-surface (storage/scrub placement) with harness-core (Redactor)

2. [HIGH] scripts/seed_demo_repo.sh:205-239,283-296 and dispatch decision 12 vs src/integrations/cicd/agents/investigator.py:419-446 -- the demo's two branch-push scenarios depend on a `default_green` baseline fallback that does not exist anywhere in `src/`.
   Failure scenario: seeded repo, push to `demo/regression` -> `regression.yml` fails with `head_branch: demo/regression` -> webhook -> Investigator calls `find_last_successful_run(workflow_id, branch="demo/regression", before=head)` -> zero success runs on that branch -> `cold_start=True`, `baseline_kind="none"`, no `compare_commits`, empty diff -> Appendix D prompt forbids `real_regression` and caps confidence at 0.70 -> step 5's `category: real_regression` is unreachable (same for `demo/dependency`). Appendix D's chain (`integrations/cicd/baseline.py`, `tests/unit/test_baseline.py`, `default_green`/`head_commit_only`) was never built (grep: only the Literal in `schemas.py:79`) and no amendment records it as dropped; the Phase 5 seed design was written against the plan text, not the code.
   Owner: cicd-integration

3. [MEDIUM] scripts/seed_demo_repo.sh:249-250,291,318 -- `--force` force-pushes `main` of an existing repository and then aborts on the demo-branch push, while the header (lines 22-23) says `--force` "only re-pushes branches; it never deletes anything".
   Failure scenario: `seed_demo_repo.sh owner/harness-demo-repo --webhook https://... --force` (the exact command the script prints at line 318 for registering the webhook later) on an already-seeded repo -> `git push --force origin main` replaces `main`'s history with a fresh single commit (new shas every run: `git init` in a temp dir) -> waits for the four workflows -> `git push -u origin demo/regression` is a non-fast-forward against the existing branch -> rejected -> `set -e` exits before `demo/dependency` and before step 4, so the webhook is never registered and `main`'s prior history is gone.
   Owner: fixtures-eval

4. [MEDIUM] src/harness/orchestrator.py:346, src/integrations/cicd/remediation.py:426-430 -- `ToolError.message` (since Phase 5 `_message_of` joins every `errors[].message` of a GitHub error body into it) is written to log lines verbatim; no logging filter applies the Redactor (the only one is `escalation._ScrubUrl` on the httpx logger). PLAN "Secrets never reach the trace" mechanism 2 claims every string written to "a log line" is scrubbed.
   Failure scenario: live gateway, `HARNESS_DRY_RUN=false`, `rerun_failed_jobs` -> GitHub answers 403 whose body echoes the token (the premise `test_no_secret_leak.py:207-213` itself adopts, on the read path) -> `_rerun_failed_jobs` re-raises (no "in progress" phrase) -> `execute_plan` logs `remediation stopped at 'rerun_failed_jobs': forbidden (403): ... ghp_...` -> `remediation_suspend` escalates `tool_failure` with `failure_summary(failed)` -> `_escalate` logs `run ... escalated (tool_failure): ... ghp_...` -> the token is in stdout / the Space logs. The DB row, the served body and the notifier payload are scrubbed; the log is not. The leak test log-record scan passes only because its 403 lands on `list_workflow_run_jobs`, which never escalates.
   Owner: harness-core

5. [LOW] src/api/main.py:1119 -- `logger.info("webhook: ignored delivery (%s)", verdict.reason)` writes the `X-GitHub-Event` header value and `payload["action"]` to the log for a signed delivery; dispatch decision 6 and PLAN amendment 3 say "no header reaches a span, a log line or a problem document".
   Failure scenario: a caller holding the secret posts with `X-GitHub-Event: <8 KB of attacker text>` -> `event '<text>' is not 'workflow_run'` in the log (repr-escaped, so newlines are escaped -- injection is bounded, but the invariant as stated is false).
   Owner: api-surface

6. [LOW] src/integrations/cicd/gateway_github.py:641-656 -- `open_pull_request` POSTs the labels after the PR exists; a failure there returns `ok=False` with no PR number and nothing ever re-applies the labels.
   Failure scenario: `POST pulls` 201 -> `POST issues/{n}/labels` 5xx x4 (or 403) -> `_Failure` -> `ToolResult(ok=False, data=None)` -> `tool_failure` escalation names the PR call as failed although a draft PR exists; the result is not cached (only `ok` results are) so the next attempt (a new gateway per approval) hits the pre-check, finds the PR, and answers `already_exists=True, cached=True` with `labels` absent -> `label:agent-generated` / `label:agent-triage` obligations are never met and no result says so.
   Owner: cicd-integration

7. [LOW] src/integrations/cicd/gateway_github.py:658-672 -- `_find_marked_issue` reads one page (`per_page=100`) of `GET issues?state=open`, no pagination and no `labels` filter (dispatch decision 7 says `GET issues?state=open&labels=...`).
   Failure scenario: repository with >100 open issues where the marked issue is older than the newest 100 -> marker not found -> a second issue is filed for the same signature, contrary to Appendix C.
   Owner: cicd-integration

8. [LOW] scripts/record_fixture.py:128-154,101-117 -- `scenario_yaml` writes `commit: <head_sha>` under "determined by the recording"; `expected.commit` is `Diagnosis.suspected_commit_sha`, a label (fixtures/README.md:36-60: use `commit: null` only when null is itself the correct expectation, omit the key when the scenario has no opinion), and `scripts/eval.py:130` scores it.
   Failure scenario: record a flaky or infra failure, fill in `category`/`action` as instructed, leave the generated `commit:` -> `eval.py` reports `commit: None != <head>` and exits 1 -- the over-specified label the README warns produces a flaky gate. Second drift in the same script: the recorded `find_last_successful_run` body is the live gateway post-`before` filter (`workflow_runs` minus the failing sha, `total_count` rewritten) and the recorded log is the decoded, 2 MB tail-capped `content`, not the "raw GitHub API response ... byte for byte" README:99/154 promises; a job log over `DEFAULT_MAX_LOG_BYTES` replays with `truncated=False, total_bytes=<capped>` where the live run said `head_dropped=True`.
   Owner: fixtures-eval

9. [LOW] src/api/main.py:1122-1128, src/api/deps.py:414-421 -- `int()` on a float infinity raises `OverflowError`, which is neither in the `parse_subject` caught tuple nor in the `_delivery_key` one.
   Failure scenario: signed body `{"action":"completed","workflow_run":{"conclusion":"failure","id":1e400,"run_attempt":1},"repository":{"full_name":"..."}}` -> `json.loads` yields `inf` -> `parse_subject` raises `OverflowError` -> catch-all handler -> `500` where the route documents `400 Malformed delivery`. Reproduced in-process. Signed callers only.
   Owner: api-surface

## Contract diffs       model -> field -> plan says X, code has Y

- `EscalationRecord.reason` (A.1, contracts.py:103) -> matches A.1 as amended (`evidence_unverifiable`); code also has `run_timeout` (Phase 3 amendment, PLAN:266) which the A.1 literal block still omits -- pre-existing.
- `create_issue` args (A.4 table, catalog.py:139-151) -> plan `{title, body, labels}`, code adds optional `signature_id` -- recorded in amendment 4; the A.4 table row itself is not updated.
- `ContextManager.assemble_traced` (A.3) -> matches the amendment; `assemble` unchanged.
- `Span`, `TraceResponse`, `ToolError.kind`, `ToolResult`, `RunRequest`, `RunOutcome` -> no diffs. `ToolError.kind` gains no member for the 409 (as decided).
- `ReplayToolGateway(recorder=)`, `SqliteMemoryStore(recorder=)`, `ContextManager(recorder=)` -> additive, defaulted, recorded.
- The A.2 sample `remediation_gate` (PLAN:1501-1516) and Phase 4 prose (PLAN:1045) still escalate every `fail` as `evidence_refuted`; code (`wiring.py:176-190`) chooses by `refuted > 0`. Amendment 5 records the change; the two passages are stale.

## Unhandled failure paths   external call -> missing condition

- `POST /repos/{r}/issues/{n}/labels` (after `POST pulls` succeeded) -> failure leaves an unlabelled PR and a result without its number; no reconciliation (finding 6).
- `GET /repos/{r}/issues?state=open` -> pages beyond the first 100 never read; `labels` filter not sent (finding 7).
- `POST /webhooks/github` (inbound) -> `OverflowError` on float-infinity numeric fields -> 500 not 400 (finding 9).
- `PUT /repos/{r}/contents/{path}` -> 422 "sha was not supplied" (file created between the GET 404 and the PUT) surfaces as `ToolError(kind="unknown", http_status=422)`; loud, not clobbering -- acceptable, noted for completeness.
- Log sink -> no Redactor on `logging` (finding 4); the B.2 "token never logged" row holds only while no upstream body echoes it.
- Every other B.2 row for the four new writes routes through `_request` (timeouts x2, primary/secondary rate limit, 401/403 auth, 404 -> `not_found` handled as data on the pre-check reads and as failure on the write, 5xx x3, non-JSON -> `malformed`, 409 hard stop, 422 already-exists) -- verified present.
- B.3 fail-closed path (`retries_in_24h = 999` on `unavailable`) unchanged (`schemas.py:33,131-172`).

## Plan drift           built-but-unplanned / planned-but-missing

Built, recorded (PLAN amendment items): stage span (1), replay-mode 403 rule (3), all five writes incl. `create_issue` + `signature_id` (4), `evidence_unverifiable` (5), PEM widening + assignment pattern (6), cold_start run id (7), planted token (8), `Investigator.collect` + `fixture_slug_for` (9), Verify block re-read (10).

Built, not recorded:
- `ReplayToolGateway.invoke` opens the `gateway.invoke` span before the forbidden re-check inside `_invoke` (gateway_replay.py:175-179); dispatch decision 2 says "opened after the forbidden re-check", and `harness/gateway.py:91` says the re-check runs "before anything else". No failure results (`_persist` swallows every exception), so drift only.
- The `commit:` line of `scenario_yaml` claimed as "determined by the recording" (dispatch 11) -- it is a label (finding 8).

Planned, missing, unrecorded:
- Appendix D baseline chain: `integrations/cicd/baseline.py`, `default_green`, `head_commit_only`, `tests/unit/test_baseline.py` (PLAN:2201-2235). Only `branch_green`/`none` exist (investigator.py:443). The Phase 5 seed script and step 5 assume it (finding 2).
- No test pins the invariant `match_scenario` relies on (one Appendix C key per recorded scenario); `fixtures/README.md` states it as a checklist item only. All five keys are distinct today (verified); a sixth fixture copied from another would silently replay the alphabetically-first match.

Planned, missing, recorded: the demo repository itself (a script, user-gated -- amendment 10, verify.md step 5); the live calls of steps 1/3/5; the step 4 `observation` count read `0` on the fault-injected server (verify.md is explicit).

## Suspicions, one line each

- S1: Pre-verification ruled out (main.py:1090-1104 -- only the raw bytes and the signature header reach `verify_signature`; the 401 `detail` is a constant, the `problem()` body carries only the path). Post-verification confirmed: main.py:1119 logs the `X-GitHub-Event` value and `action` (finding 5).
- S2: Placeholder secrets are honoured; PLAN sets no entropy rule -- documented hazard, not a finding. Whitespace-only secret: main.py:1092 `not secret.strip()` warns once and webhook.py:60 returns False -> 401. Ruled out.
- S3: string `"501234891"` / float `501234891.7` / bool / absent -> `int()` coercion or a caught error -> a run, 403, or 400; `1e400` -> `OverflowError` -> 500 (finding 9). The matched directory name only reaches `RunRequest.replay_fixture`, never a response. `sorted()` order is not load-bearing: all five keys distinct (checked), though nothing tests it.
- S4: Ruled out -- main.py:899-902 serialises the `awaiting_approval` outcome through `_serialize_run_outcome`; main.py:1168-1171 mirrors the `create_run` takeover path exactly.
- S5: Ruled out -- every rendered string derives from `_serialize_run_outcome` or the scrubbed `TraceResponse`; the YAML is the loaded `policy.yaml` (not caller or model data); `run_id`/`trace_url` come from the stored model; `rule_text` answers `# no rule named ...` for an unknown id (trace_view.py:458-459), never raises; `zip(strict=True)` in evaluator.py:198 guarantees the by-index join.
- S6: (a) confirmed -- finding 6. (b) ruled out -- GitHub answers 409 for a stale sha; the 422 create/create race is a loud `unknown`. (c) ruled out in practice -- no other 422 on `git/refs` or `pulls` carries the phrase, and a false match re-GETs and re-raises when nothing is found (gateway_github.py:546-552, 643-647). (d) `signature_id` is a hash of the key (history.py:130,147), present even with memory degraded; absent only when `key is None`. (e) documented (dispatch 7); the pre-checks are reads, the Appendix E "executing none" concerns writes.
- S7: The message reaches `gateway_errors` and escalations scrubbed (store `_dump`, `_serialize_run_outcome`, notifier); it reaches two log lines unscrubbed (finding 4).
- S8: Ruled out -- `_persist` catches everything (observability.py:411); attributes are ids, counts, verdict strings and tool names; the only contention cost is up to `busy_timeout` latency in the span `finally`, outside the store write lock.
- S9: Ruled out -- `git show -w` shows only the wrapper added; `TimeoutError` is caught inside the scope so the `timeout` record is copied onto the stage span; `break`/`continue` inside `async with` exit the scope normally. (Pre-existing, not Phase 5: `span()` catches `Exception`, so a `CancelledError` from outside leaves `status=ok`.)
- S10: Confirmed -- finding 1. No harness-authored span key or value matches (`tokens.*`, `max_output_tokens`, `prompt_version`, `requested_by`, `summary` texts verified clean); log excerpts and patches are over-matched at rest and digested when served -- display only.
- S11: Consistent -- contracts, orchestrator alias, drift guard, the wiring gate; README, `scripts/eval.py`, `app.py`, the notifier and the trace CSS enumerate no reasons and special-case none. Stale PLAN prose at 1045 and 1508 (contract diffs).
- S12: Ruled out -- every remaining `501234890` belongs to `flaky_test`; the stub already answered `cold_start` as `flaky_test` by prompt-order before Phase 5, so no test changed vacuity.
- S13: Confirmed -- finding 8. `RecordingToolGateway.invoke` returns before writing for any non-read tool (record_fixture.py:101); `collect` calls no model.
- S14: Confirmed -- finding 3. The secret is passed as a `gh api -f` argument with stdout discarded and is not echoed by the script; `gh repo create` is guarded by `gh repo view`.
- S15: Ruled out -- harness additions carry only generic names (`stage`, `sections`, `budget_chars`, `signature_id`, `verdict`, `action_taken`, `outcome`); nothing CI-shaped.
- S16: All five named items are recorded (amendment 1, 3, 4, 5, 9). Unrecorded: the Appendix D chain (finding 2), the replay-gateway span order, the `commit:` claim. "Seeded with four workflows" is a script, recorded as user-gated.

## Gate observed

`uv run pytest tests/unit/test_gateway_github_writes.py tests/unit/test_webhook_signature.py tests/unit/test_trace_view.py tests/unit/test_trace_completeness.py tests/integration/test_webhook_e2e.py -q` -> 78 passed on this tree; `scripts/scrub_fixtures.py --check` -> clean.
