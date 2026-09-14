# Phase 5 — dispatch decisions (written BEFORE the build)

Written 2026-09-14 at `bb0abf3`, the tree after `phase-4-green` plus the deploy record.
Normative for this phase. Where this file and an implementer's instinct disagree, this
file wins. Settled here so the audit can check the build against a stated intent rather
than reverse-engineer one.

## How this phase is being run

**Inline by the coordinator**, as Phases 2–4 were; `phase-reviewer` is dispatched once,
after the build, with named suspicions; the fix round is coordinator-verified and
recorded as such (`backlog.md`, "Audit provenance"). Every live model call is budgeted in
`verify.md` before it is spent. **2026-09-14's Pacific quota day is spent** (handoff §1:
18 calls by 13:30 IST), so every Verify step that needs the model — step 1's replay,
steps 3/4's signed deliveries, the eval's two queued rows, and step 5 — runs after the
reset (~12:30 IST, 2026-09-15). Everything else runs today, in-process, on the stub.

## The gap the handoff did not name

Verify step 1 expects the trace's component set to be
`["agent","context_manager","evaluator","gateway","guardrails","llm","memory","orchestrator"]`.
The Space's post-deploy trace (`phase-4/verify.md` §"Deploy") carried five of the eight:
`agent, evaluator, guardrails, llm, orchestrator`. Three components have never written a
span on a replay run — the replay gateway records nothing (only `GitHubToolGateway` takes
a recorder), the memory store records nothing, and the context manager is synchronous and
has no recorder at all. PLAN's trace model ("one span per: run, stage, agent attempt, LLM
call, tool call, policy decision, memory query, evaluator check") also names a *stage*
span nothing writes. Decisions 1–4 close that; without them the view has nothing to draw
for three of the components it is supposed to make visible.

## Decisions

### 1. A `stage` span per stage, opened by the orchestrator

`Orchestrator._run` wraps each stage — gate check included — in
`span("stage", "orchestrator", stage=<name>)`, and sets `agent`, `status`, `attempts`
and `summary` when the `StageRecord` is appended (a gated stage records `status=gated`
and `gate_reason`; a timed-out one `status=timeout`). `agent.run` therefore parents to
the stage span, not to `run`; `test_replay_e2e.py::test_trace_is_persisted_and_readable`
changes accordingly (agent spans hang off stage spans, stage spans off the run span).
The waterfall's second level is the pipeline the README describes, which is the point of
a waterfall.

### 2. The replay gateway records `gateway.invoke` spans

`ReplayToolGateway(recorder=...)`, optional, defaulted `None` so every existing
construction stands; `deps.build_replay_gateway` passes the context's recorder. The span
is the same shape `GitHubToolGateway` writes (`tool`, `side_effect`, `rule_id`, `ok`,
`dry_run`, `cached`, `error_kind`), opened *after* the forbidden re-check for a refusal
(a span is a local write, not an outbound request; the refusal is still on the record).

> *Amended after the audit (review.md, plan drift):* as built, the span is opened
> *before* the re-check and wraps it, in both gateways -- the parenthetical was the
> point, the word "after" was not. The refusal still costs zero outbound requests
> (A.4 step 4, asserted at the transport), and it leaves a span, which is what a
> refusal-shaped test pins. Recorded in PLAN.md Phase 5 amendment 16; not changed.

### 3. Memory queries are spans, written by the store

`SqliteMemoryStore(recorder=...)`, optional. Four operations get a span, component
`memory`: `lookup` (`signature_id`, `found`, `occurrences`, `recent`, `actions_in_window`),
`upsert_signature` (`signature_id`, `verdict`), `record_observation`
(`observation_id`, `signature_id`, `verdict`, `action_taken`) and
`update_observation_outcome` (`observation_id`, `outcome`). The run/approval/escalation
bookkeeping (`claim_run`, `heartbeat`, `save_run`, …) is not traced: a heartbeat every
15 s is noise, and the claim and the save happen outside `run_scope` where the recorder
would drop the span anyway. Under `sqlite_locked` the span records the `MemoryStoreError`
and the run degrades as before — the trace now says *where* memory failed. The span is
persisted through `storage.connect`, not the store's faulted connection, so a locked
store still gets its error span written.

### 4. `ContextManager.assemble_traced` — A.3 amended additively

`assemble` is synchronous by contract (A.3) and stays so. `ContextManager(recorder=...)`
gains an optional recorder and an `async def assemble_traced(req)` that opens one
`context.assemble` span (component `context_manager`: `sections`, `budget_chars`,
`estimated_tokens`, and every section's `TruncationReport` under `truncation`) around
`assemble`. The three agents call `assemble_traced`; `assemble` is unchanged for every
other caller and test. The harness owns the attribute vocabulary because it owns the
`Component` literal; nothing domain-shaped enters `src/harness/`.

### 5. The trace view is rendered from the bodies the JSON routes serve

`GET /runs/{run_id}/view` builds its context in `src/api/trace_view.py` from
`_serialize_run_outcome(outcome)` (digested, scrubbed — exactly `GET /v1/runs/{id}`'s
body) and the scrubbed `TraceResponse`, and renders `src/api/templates/trace.html`
(Jinja2, autoescape on, ~100 lines of CSS, no JS). A secret that cannot reach the JSON
cannot reach the HTML, by construction rather than by a second scrub path.

What the page shows: a header (run id, status, integration, created at, latency, tokens
by kind, estimated cost — "unpriced" when 0, degraded components, the trace URL); the
escalation, with `reason`, `message`, `channels`, `delivered_at` and `delivery_error`;
the stage table; a waterfall of every span (depth from `parent_span_id`, bar offset and
width as percentages of the run span's duration, computed server-side; name, component,
status, duration, and the attributes that identify the span — agent, tool, rule/effect,
claim result, attempt, tokens); and per-stage cards. The diagnose card itemises
`self_confidence` → `final_confidence` with every `confidence_adjustments` row and lists
each citation beside its verdict, joined **by index** with `evaluation.verdicts` (the
evaluate agent builds one claim per citation, in order — `claim_id = cl_<n>`). The
evaluate card shows `verdict`, the three counts, `confidence_delta` and the report's
`reason` verbatim, so a share-rule `fail` reads as "0 of 1 verified", never as "refuted".
The remediate card shows the plan, every decision with `downgraded_from` when set, and
**the matching rule quoted from `policy.yaml`** — read off the loaded
`PolicyEngine.rule(rule_id)` and rendered as YAML, so the text on the page is the rule
the engine enforced, not a copy. An unknown run answers `404 application/problem+json`
like every other route; a run whose trace is missing still renders (the cards do not
need spans).

### 6. `POST /webhooks/github` — signature first, then mode-dependent acceptance

Pure helpers live in `src/api/webhook.py` (`verify_signature`, `classify_event`); the
route in `main.py` reuses `_claim_or_degrade`, `_claim_response`, `_execute` and
`_spawn_run` unchanged — it is signature verification and event filtering in front of
the same path `POST /v1/runs` takes (handoff §5).

Order, as PLAN states it: (a) `X-Hub-Signature-256` is verified with
`hmac.compare_digest` over the **raw request bytes** against
`HARNESS_GITHUB_WEBHOOK_SECRET`; missing, malformed or wrong → `401` with a
`WWW-Authenticate` challenge, and nothing after this line runs for an unsigned caller —
the body is not even parsed. A blank secret verifies nothing (every delivery `401`, a
warning logged once per process): a replay-only deployment must still boot without a
webhook secret, and an unverifiable delivery must never be accepted. (b) The body must be
JSON (`400`), the event `workflow_run` with `action == "completed"` and
`workflow_run.conclusion == "failure"` — anything else (`ping`, `check_run`, a success,
`requested`) → `204`. (c) Acceptance depends on the deployment's gateway:
**live** (`HARNESS_GATEWAY=github`): `repository.full_name` must be in
`HARNESS_ALLOWED_REPOS`, else `403`; **replay**: the delivery must match a recorded
scenario — same repo, `workflow_run.id` and `run_attempt` as that scenario's
`webhook.json`, i.e. the same Appendix C key — else `403` saying the deployment is
replay-only. The recorded scenarios *are* a replay deployment's allowlist; this is what
lets Verify steps 3–4 post `flaky_test`'s webhook at a replay-mode server and get a real
run, and what keeps a real repository's delivery to the Space (today replay, no
allowlist) a visible `403` rather than a silent nothing. (d) Appendix C's key is
computed, the claim taken, `_claim_response` answers a redelivery (`200 deduplicated`,
`200` the pending approval, `202 in_progress`), and an acquired claim spawns `_execute`
in the background and answers `202 {run_id, status}` — the claim is one SQL statement,
well inside GitHub's 10 s.

`X-GitHub-Delivery` is not part of the key (Appendix C) but is worth having on the trace:
when it is GUID-shaped it rides in `requested_by` as `webhook:github:<guid>`, which the
`run` span already records; any other value is dropped, not reflected. No header reaches
a span, a log line or a problem document; the signature least of all.

### 7. The write tools, in both gateways

`GitHubToolGateway` implements `create_branch`, `create_or_update_file`,
`open_pull_request` and `create_issue` (the catalog's own not-implemented message
promised "the PR-writing and issue tools" for this phase; leaving one out would mean
rewriting the promise). Each follows Appendix C, with the pre-check *before* the dry-run
exit so a dry run reports what it found:

- `create_branch` — `GET git/ref/heads/{name}` first; found → the existing ref,
  `cached=True`, no write. Else `POST git/refs`; a `422` whose message says "already
  exists" → fetch and return the ref, `cached=True`.
- `create_or_update_file` — `GET contents/{path}?ref={branch}` for the current blob
  `sha` (`404` → a new file, no sha) unless the call names one; `PUT contents/{path}`
  with that sha. `409` → `ToolError(kind="unknown", http_status=409, retryable=False)`,
  "changed since it was read; not clobbering" — a hard stop, no new error kind (A.4's
  list has none for it, and the kind is not what any caller branches on).
- `open_pull_request` — `GET pulls?head={owner}:{branch}&state=open` first; one open →
  returned, `cached=True`. Else `POST pulls` (draft, then `POST issues/{n}/labels` when
  labels are given); `422` "already exists" → the `GET` again.
- `create_issue` — `GET issues?state=open&labels=…` scanned for the marker
  `<!-- harness:signature:{signature_id} -->` in the body; found → `POST
  issues/{n}/comments`, `cached=True`. The marker is appended to the body by the gateway
  when the call carries `signature_id`; without one there is nothing to match and a new
  issue is filed.

Dry run returns `ToolResult(ok=True, dry_run=True, data={"would_have": <args minus
file content>, …pre-check findings})` (Appendix E); `content_b64` is never echoed into
`data` — the drafted file is already in the plan, and a second copy in `executed[]` is a
second thing for the digest pass to remember. `IMPLEMENTED_WRITE_TOOLS` grows to all
five; `not_implemented_message` stays for a future catalog entry. `ReplayToolGateway`
synthesises the same shapes with deterministic identifiers (a PR number and a commit sha
derived from a hash of the args), so a replayed approval reads the same way twice.
`test_approve_re_evaluates_and_executes_through_the_gateway` changes from "the first
call fails honestly" to "three calls execute, dry-run, and the run completes".

### 8. Secrets: the pattern set PLAN names, and the test PLAN names

`deps.SECRET_PATTERNS` gains the two shapes the handoff lists as missing: the PEM header
— widened to swallow the whole block when its `END` line is present, so a pasted key is
not left with only its first line redacted — and
`(?i)(api[_-]?key|token|password)\s*[:=]\s*\S{8,}`. The rest of PLAN's list is already
covered (`gh[pousr]_`, `github_pat_`, `AIza`, bearer).

`tests/test_no_secret_leak.py` sets the two sentinels PLAN spells, runs every recorded
scenario through the API (the stub model), one escalated run, one webhook rejected with a
forged signature, and one live-gateway call whose mocked GitHub error body echoes the
token, then asserts zero occurrences of each sentinel — and of the token pasted into a
fixture log — in every `trace_span` row, every `escalation` row, captured stdout/stderr
and log output, the raw bytes of the SQLite file, and every JSON and HTML body served.

**The pasted token lives in `real_regression`'s log**, a `curl -H "Authorization: token
ghp_…"` line in the setup noise — the hand-written fixture, not one `gen_fixture_log.py`
regenerates byte for byte. It matches no anchor pattern, so the scenario's fingerprint
and every recorded expectation stand. `scripts/scrub_fixtures.py` lists that value as a
known sentinel and leaves it alone; every other credential shape in `fixtures/` is
rewritten (or reported, under `--check`).

### 9. `EscalationRecord.reason` gains `evidence_unverifiable` — A.1 amended additively

The Phase 4 audit's S2 and the backlog ask for it here. A `fail` report with
`refuted == 0` (the share rule: too few verified, nothing shown false) escalates
`evidence_unverifiable`; a report with any `refuted` still escalates `evidence_refuted`.
`contracts.py`, `orchestrator.EscalationReason` and the drift-guard test move together;
the gate in `wiring.make_remediation_gate` chooses between the two off the report's
counts. The view renders the report's `reason` beside the enum either way.

### 10. `scripts/replay.py --post-signed` is the webhook client the Verify block uses

`--post-signed <webhook.json> [--url http://127.0.0.1:8000] [--wait]` signs the file's
bytes with the secret from settings, posts them with the three GitHub headers (`X-GitHub-
Event: workflow_run`, a fresh `X-GitHub-Delivery` GUID, `X-Hub-Signature-256`), and
prints the status and the body's `run_id`/`status`/`original_run_id`; `--wait` polls
`GET /v1/runs/{id}` until the run leaves `in_progress`. A second post of the same file
prints the `deduplicated` line PLAN's step 4 expects.

### 11. `scripts/record_fixture.py` records what the Investigator reads, without a model call

`--repo <owner/name> --run-id <workflow_run id> --name <scenario>` fetches the run in
webhook shape (as `replay.py --live` does), wraps the live gateway in a recording
gateway that writes every read tool's response to `api/GET_<slug>.json` (the slug rule
`fixtures/README.md` states; job logs to `logs/job_<id>.txt`, never to `api/`), and
drives **`Investigator.build_prompt`** over it — the deterministic collection, every
read call the replay will need, and no `generate`. It then scrubs the directory the way
`scrub_fixtures.py` does and writes a `scenario.yaml` with only the keys the recording
determines (`name`, `expected.commit`, `baseline_kind`, `cold_start`) and a commented
block naming the label keys a person fills in (`category`, `min_confidence`,
`cites_any_of`, `action`, `effect`). A scenario recorded this way replays and scores;
the model's optional `additional_tool_calls` at replay time meet a missing `api/` file
as `not_found`, which the Investigator already treats as data.

### 12. `scripts/seed_demo_repo.sh` seeds a repository the four scenarios can fire from

`gh`-driven, refuses to touch a repository that already exists unless `--force`. Seeds
`main` with the pricing package and tests the fixtures describe, `requirements.txt`, and
four `workflow_dispatch` workflows: `flaky.yml` (a deadline test that fails under an
input-controlled load), `infra.yml` (`pip install` against an unreachable index),
`regression.yml` and `dependency.yml` (which run the test suite; their failures come from
the `demo/regression` and `demo/dependency` branches the script pushes — one commit each
over `main`, so `find_last_successful_run` finds `main`'s green run as `default_green`
and the compare is exactly the offending change). It prints the three things the user
then does by hand: the fine-grained PAT (Appendix E's scopes), the webhook
(`/webhooks/github`, `workflow_run`, the secret), and the Space's variables. Creating the
repository is an outward-facing action; the script is written and tested for syntax,
and run only on the user's say-so.

### 13. The Verify block, as this phase can honestly meet it — PLAN amended

- **Step 1** — the view and the component list, once the run exists. The in-process
  equivalent (`tests/integration/test_trace_view.py`) pins ≥ 12 spans, the eight
  components, `open-fix-pr` / `require_approval` on the page, and every adjustment name;
  the literal step needs three live calls.
- **Step 2** — `tests/test_no_secret_leak.py`, free, decision 8.
- **Step 3** — `401` is free; the signed `flaky_test` delivery is three live calls.
- **Step 4** — the second delivery is free (`deduplicated`, no agents run); the
  `sqlite3` lines read `1` and `1`.
- **Step 5** — needs the demo repository, a PAT, the webhook, and a Space redeploy with
  `HARNESS_GATEWAY=github` and an allowlist: four user-gated steps and three live calls.
  The Space's exposure changes with it (a real repository, a real token, writes still
  dry-run); asked for, not assumed.

## Territory map for this phase (coordinator writes all of it)

| Area | Files |
|---|---|
| harness | `orchestrator.py` (stage span, the reason), `memory.py` (recorder, spans), `context_manager.py` (recorder, `assemble_traced`), `contracts.py` (`evidence_unverifiable`) |
| integration | `gateway_github.py` (four write tools, 409/422), `gateway_replay.py` (recorder, synthesised writes), `catalog.py`, `agents/{investigator,diagnostician,remediator}.py` (`assemble_traced`), `wiring.py` (the gate's second reason) |
| api | `deps.py` (recorders, patterns), `webhook.py` (new), `trace_view.py` (new), `templates/trace.html` (new), `main.py` (two routes) |
| fixtures / scripts | `real_regression/logs/job_601234567.txt` (the pasted token), `replay.py` (`--post-signed`), `record_fixture.py` (new), `scrub_fixtures.py` (new), `seed_demo_repo.sh` (new) |
| tests | `test_no_secret_leak.py`, `unit/test_webhook_signature.py`, `unit/test_trace_view.py`, `unit/test_gateway_github_writes.py`, `unit/test_gateway_replay_writes.py`, `unit/test_trace_completeness.py`, `integration/test_webhook_e2e.py`, `integration/test_idempotency.py`, `integration/test_trace_view.py`, `integration/test_record_fixture.py`, `integration/test_scrub_fixtures.py`; updates to `test_replay_e2e.py`, `test_approvals_e2e.py`, `test_escalation_reason_drift_guard.py`, `test_evaluator.py`/`test_evaluator_e2e.py` where the reason changes |
| docs | this file, `verify.md`, `test-verifier.md`, `review.md` (reviewer), `backlog.md`, PLAN.md amendments, README, the Phase 6 handoff |
