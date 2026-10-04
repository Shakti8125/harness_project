# Plan: finish Phase 5 Verify step 5 on a real GitHub repo, with the Gemini quota under control

> **Status: FINAL, 2026-09-30; decisions confirmed by the user 2026-10-01** (see "Confirmed
> with the user" below). Nothing below has been executed. Start at Session A,
> Stage 0, and read the whole file first: it is the plan for both sessions.
>
> **What finalisation changed** (drafted earlier on 2026-09-30, finalised the same day, after
> a four-slice security audit and a line-by-line check of the draft against `06c87db`):
> - **New Stage 1e, the security gate.** Stage 5 must not run until it is done. The audit is
>   `docs/security/assessment-2026-09-30.md`, **uncommitted, and not for the public repo
>   until its fixes are deployed**. In the step-5 live configuration the Space would let any
>   anonymous caller start live runs (SEC-02) and decide approvals (SEC-04). The GitHub
>   gateway would send model-supplied paths unencoded, and `../` escapes the repo (SEC-03).
>   Separately, one request can stall today's Space (SEC-01).
> - **Quota maths corrected.** The free-tier profile caps only *transient* retries. A run
>   can still cost up to 7 requests per agent, because schema retries (3) and timeout
>   retries (2) are separate budgets. Schema failures are common: 16 of the 63 local
>   `llm.attempt` spans are `invalid_output`.
> - **Ledger spec corrected against real data.** `started_at` is stored as `…+00:00`, not
>   `Z`. Span status is only `ok`/`error`; the cause is in `error.type`. No span records a
>   model name. `GET /v1/runs` is paginated as `{items, next_cursor}`.
> - **Stage 5 step 7's `jq` fixed.** The old `.[0].final…` could never have worked: the list
>   is `items` and carries no `final`.
> - **Secret handling tightened.** The PAT permissions are now explicit (never Workflows or
>   Administration). The webhook secret is entered with `read -rs`, never inline (SEC-19).
>   `docker compose` is not used for Stage 4, because it binds every interface (SEC-18).

## Context

The harness was closed at Phase 5 on 2026-09-17. `master` = `phase-5-green` = `06c87db`, and
the Space (https://shakti-agent-harness.hf.space) serves that commit in replay mode with dry
run on. Verify steps 1–4 and the eval's last two rows ran live and matched
(`docs/progress/phase-5/verify.md`, "Live — 2026-09-17"). **Step 5 never ran.** Step 5 is a
real repository driving the harness: create the demo repo, mint a PAT, switch the Space to
`HARNESS_GATEWAY=github`, fire a failure, check the delivery is `202` and that a Redeliver
creates no second run.

Since 2026-09-30 the code is also on GitHub. `origin` is
`https://github.com/Shakti8125/harness_project` (**public**), and local `master` tracks
`origin/main`. The six `phase-N-green` tags are pushed. The Space remote is still `space`.
The demo repo is separate and does not exist yet.

The live steps kept slipping for one reason: Gemini's free-tier quota. The cap is **20
`generate_content` requests per model per Pacific day**. It resets at midnight PT, which is
**12:30 IST until 2026-11-01 and 13:30 IST after DST ends**. **A `503` (overloaded) counts
against the 20.**

Today the client retries a 429/503 four times with sub-second full-jitter backoff
(`src/harness/recovery.py:58-60`: `TRANSIENT_MAX_ATTEMPTS=4`, `BACKOFF_BASE_S=0.5`,
`BACKOFF_MAX_S=8.0`). On 2026-09-15, five replays during an overload window produced 18
`503`s and 1 success, then a `429`, and the whole day was gone. The Space shares the same
key, and therefore the same quota pool. Its `/v1/replay` is public and unauthenticated, so
anyone can spend the quota (SEC-07).

Decisions already taken with the user:
- **Gemini:** retry hardening through settings, plus a committed probe script and a quota
  ledger. Free, and `src/harness/` stays untouched. No billing, no model split, no daily guard.
- **Staging:** run local live mode against the real repo first, then the Space.

**Confirmed with the user on 2026-10-01** ("I will go with your recommendations"). Do not
re-ask these:
- **Stage 1e (security gate) runs before Stage 5.** It is code in `src/api/` and
  `src/integrations/` only, so the harness gate still holds.
- **All three optional items in Stage 1e item 7 are in:** SEC-07 queue cap, SEC-18 compose
  bound to `127.0.0.1`, and SEC-19 `gh api --input -`.
- **One independent `phase-reviewer` pass over Stage 1e's diff.** This follows the standing
  "one audit per phase" rule; a security fix round is exactly where it has paid every time.
- **Early replay-mode deploy** once that audit is clean: push the Session A commits to the
  Space with the Space still in replay mode, so SEC-01 and SEC-08 close now rather than on
  the live day. Push to `origin` in the same sitting. **Still ask before each push**; the
  standing rule is one ask per change, and this confirmation does not replace it.
- Other subagents: none. Work inline; the audit is the only dispatch.

The plan spans two sessions:
- **Session A:** code, tests and GitHub prep. It costs ≤1 model request and can run on any day.
- **Session B:** the live day, ~10–13 requests nominal, starting after the Pacific reset.

Standing rules still apply:
- Ask before every deploy or exposure change, one ask per change. A push to `origin`
  (public GitHub) counts as exposure too.
- Count the Pacific day's requests, failed ones included.
- Write patch scripts to the scratchpad and run them with `python <file>`. No Bash heredocs.
- Never `git checkout --` a dirty file.
- Prove new tests against the pre-fix tree (stash `src/` first).
- `git add` new files explicitly.
- Leave `.archify/` and `docs/PROJECT_DEEP_DIVE.*` alone. They are untracked and not part of
  this work.
- **New:** do not commit `docs/security/assessment-2026-09-30.md` until SEC-01 through
  SEC-09 are deployed. When the Stage 1e commit goes out, deploy it to the Space and push it
  to `origin` in the same sitting, because a public fix diff describes the hole it closes.

---

## Session A — Stage 0: pre-flight (0 requests)

1. Read these first:
   - `docs/progress/phase-6/handoff.md` §1–2
   - `docs/progress/phase-5/verify.md`: Step 5 and the Live sections
   - `docs/progress/phase-5/backlog.md`: "A 503 storm burns the day"
   - `docs/security/assessment-2026-09-30.md` §1, §5 SEC-01 to SEC-09, and §9
2. Run `docker compose down`, because a stale container may hold port 8000. Always address the
   server as `127.0.0.1`.
3. Run the gate on HEAD:
   - `uv run ruff check .`
   - `uv run mypy --strict src/harness`
   - `uv run pytest -q`, expected 898 passed / 2 skipped (`verify.md`, the `2c79035` record)
   - `uv run python scripts/eval.py --runs 1 --llm stub` (never quote this as accuracy)
4. Check the Space with free checks only: `uv run python scripts/check_space.py`, **without**
   `--replay`.
5. `git status`: nothing staged. `git remote -v` shows `origin` (GitHub) and `space`.

## Session A — Stage 1: Gemini hardening (0 requests; 1 to smoke the probe)

### 1a. Make the transient retry policy configurable
The code path already exists. `build_orchestrator`/`build_agents` take `retry_policy`
(`src/integrations/cicd/wiring.py:254,327`). `AppContext.build_orchestrator_for`
(`src/api/deps.py:376-422`) never passes one, so every agent gets `RetryPolicy()` defaults
(`src/harness/agent.py:172`).

- `src/settings.py`: add four non-secret fields. The defaults equal today's constants, so
  behaviour is unchanged unless a value is set.
  - `llm_transient_max_attempts: int = Field(4, ge=1, le=4)`
  - `llm_backoff_base_s: float = Field(0.5, gt=0, le=20)`
  - `llm_backoff_max_s: float = Field(8.0, gt=0, le=20)`
  - `llm_backoff_jitter: Literal["full", "none"] = "full"`
- `src/api/deps.py` `build_orchestrator_for`: pass
  `retry_policy=RetryPolicy(transient_max_attempts=…, backoff_base_s=…, backoff_max_s=…, jitter=…)`
  from settings. Import `RetryPolicy` from `src.harness.recovery`. Leave `max_attempts`
  (schema retries, 3) and `timeout_s` at their defaults. Agents read the timeout from their
  own `timeout_s`, which `deps.py:413` already feeds from `gemini_timeout_s`, not from the
  policy.
- **Free-tier profile.** Put it in `.env` locally and in Space Variables, and document it
  commented out in `.env.example`:
  - `HARNESS_LLM_TRANSIENT_MAX_ATTEMPTS=2`
  - `HARNESS_LLM_BACKOFF_BASE_S=5`
  - `HARNESS_LLM_BACKOFF_MAX_S=15`
  - `HARNESS_LLM_BACKOFF_JITTER=none`

  One overloaded agent then costs 2 requests instead of 4. The single sleep is
  `backoff_delay(1) = min(15, 5) = 5 s`, which fits the per-call sleep budget
  `RETRY_DELAY_BUDGET_S=20` (`recovery.py:93`). A provider-stated `Retry-After` is still
  honoured and clamped at 20 s (`llm.py:397`, `MAX_RETRY_AFTER_S`).

  **What the profile does not cap.** The loop's hard ceiling is
  `max_attempts + transient_max_attempts + TIMEOUT_MAX_ATTEMPTS` (`recovery.py:312`), which
  is 3 + 2 + 2 = **7 attempts per agent** under the profile (9 with the defaults).
  Schema-invalid replies (`invalid_output`) are billed; they make up 16 of the 63 local
  attempt spans to date. Reducing `max_attempts` was considered and rejected: it would turn
  those recoverable replies into failed runs. The stop rules in Session B are the guard.
- `PLAN.md`:
  - Add the four fields to Appendix E's `Settings` block.
  - Add an amendment note under Appendix B.1's 429/503 row: the defaults are B.1's, and the
    free-tier profile trades attempts for longer sleeps because a `503` is billed.
  - Record this in the Phase 5 backlog entry, closing its "back off in seconds, cap attempts"
    item.
- **Tests**, in a new `tests/unit/test_retry_settings.py`:
  1. Defaults: `build_orchestrator_for` yields agents whose `retry_policy` equals
     `RetryPolicy()`.
  2. The env profile reaches every agent's policy.
  3. An integration test with a scripted client that raises a 503-classified error on every
     call (reuse the shapes in `tests/integration/test_llm_upstream_escalation.py`). Under
     `transient_max_attempts=2`, the trace holds exactly 2 `llm.attempt` spans for the
     Investigator and the run escalates `llm_upstream`. Monkeypatch `asyncio.sleep` so the
     test does not sleep.

  Prove tests 2 and 3 fail on the pre-fix tree by stashing `src/` first.
- **Gate:** `git diff --stat -- src/harness/` must be empty.

### 1b. `scripts/probe_gemini.py`, committed (1 request per use)
The scratchpad copy is gone. Its body is recoverable from the session transcript
`~/.claude/projects/D--Documents-harness-project/c4ff3201-….jsonl` (search
`Reply with the single word: ok`).

What the script does:
- Reads the key and model through `get_settings()`. Only `settings.py` may read the
  environment; `tests/unit/test_no_env_access.py` greps for violations.
- Accepts `--model` to override the model.
- Calls `genai.Client(api_key=…).aio.models.generate_content(model, "Reply with the single word: ok")`.
- Prints `OK: <text> | usage` or `ERROR: <type> | <message with the key scrubbed>`. Scrub
  by the *stripped* registered value **and** by shape. The deployed key is `AQ.`-shaped,
  which no current pattern covers (SEC-13), so a regex on `AIza` alone would miss it.
- Exits 0 on OK, 1 otherwise.
- Its docstring states "costs one request".

### 1c. `scripts/quota_ledger.py` (0 requests)
The script answers one question: how many requests has today's Pacific day spent?

- **Pacific-day start.** Compute it without `tzdata`: `zoneinfo` has no database on
  Windows and `tzdata` is not in `uv.lock` (checked: absent). Do not add it. Hand-code the
  US rule instead. PDT runs from the 2nd Sunday of March 02:00 to the 1st Sunday of
  November 02:00, so the day starts at 07:00 UTC under PDT and 08:00 UTC under PST. Pin the
  rule with a unit test at the 2026-03-08 and 2026-11-01 transitions. `--since-utc`
  overrides the computed start.
- **Local count.** `--db PATH`, repeatable, defaults to `./data/harness.db`.
  - Open the database read-only (`file:…?mode=ro`, `uri=True`).
  - Count `trace_span` rows with `name='llm.attempt' AND started_at >= :start`. The schema
    is `src/harness/migrations/001_init.sql:46`.
  - **`started_at` is stored as `2026-09-17T18:10:14.704366+00:00`.** Bind `:start` in the
    same `YYYY-MM-DDTHH:MM:SS+00:00` form. A `Z` suffix sorts after `.` and `+`, so it
    would drop rows inside the boundary second.
  - `status` is only `ok` or `error`. Break errors down by
    `json_extract(error_json, '$.type')`. Seen to date: `LlmUpstreamError` (503),
    `LlmRateLimited` (429) and `invalid_output` (schema). **All of them count as spent.**
- **Per model.** No span carries a model name (checked: no span in the local DB has a
  `model` attribute), and adding one would touch `src/harness/`. The ledger therefore
  reports one pool, named by `get_settings().gemini_model`. It prints a warning if any
  `HARNESS_MODEL_*` override differs from `gemini_model`, because then the single-pool
  count is wrong. Keep the per-agent overrides unset for this plan (no model split). The
  `schema` attribute (`InvestigationNotes`, `Diagnosis`, `RemediationPlan`, which are the
  only three in the local DB) gives a per-agent breakdown for free; print it.
- **Space count.** `--space URL`:
  - Page `GET /v1/runs?limit=200` by `next_cursor`. The response is
    `{"items": [{run_id, status, created_at, …}], "next_cursor": …}`, newest first.
  - Stop paging at the first item with `created_at < start`.
  - For each run started today, `GET /v1/runs/{id}/trace` (`{run_id, spans: [...], …}`),
    and count spans with `name == "llm.attempt"` and `started_at >= start` the same way,
    `error.type` included.
  - Anonymous callers' runs count too, and should be listed (SEC-07). The Space's database
    is ephemeral and not reachable any other way.
- **Output:** total, ok, errors by type, the per-schema split, and `remaining = 20 - total`.
  Print a warning line at ≥ 16.
- **Cross-check** against Google AI Studio's usage/rate-limit page if it is available. Never
  print the key.

### 1d. Record and commit (Stage 1)
- Add a README note and a short `docs/progress/phase-5/backlog.md` entry for 1a–1c.
- Commit on `master`, e.g. `Gemini free tier: configurable transient retry policy, probe and
  quota ledger`, with the Co-Authored-By trailer. Stage the new files explicitly.
- **Do not push to the Space yet.** Stage 1 and Stage 1e go out together in the early
  replay-mode deploy at the end of Stage 1e (item 8).
- Smoke the probe once, which costs 1 request. Run it only if the day's ledger allows it.

## Session A — Stage 1e: security gate before any live exposure (0 requests)

Source: `docs/security/assessment-2026-09-30.md`. Every fix below is in `src/api/`,
`src/integrations/` or `scripts/`, so **`git diff --stat -- src/harness/` must still be
empty**. SEC-12 is the one audit item inside the harness; it stays in the backlog. Prove
each new test against the pre-fix tree.

1. **SEC-01: PEM redaction is quadratic** (`src/api/deps.py:88-90`). Replace the body
   with a scan that stops at the next `BEGIN` and is bounded:
   `-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:(?:(?!-----BEGIN )[\s\S]){0,16384}?-----END [A-Z ]{0,40}PRIVATE KEY-----)?`,
   or replace it with a `str.find` loop.
   - Test: 1 MB of repeated `-----BEGIN RSA PRIVATE KEY-----` scrubs in < 0.5 s.
   - Test: a real two-line PEM block is still fully redacted.
   - Test: the `422` route with a 120 KB adversarial key answers in < 1 s.
2. **SEC-08: bound request bodies.** A small ASGI middleware, or a check in the three
   `POST` routes:
   - `Content-Length > 1 MiB` → `413`.
   - A body without a length is counted while streaming and refused past 1 MiB.
   - Tests: 413 on `/webhooks/github`, `/v1/runs` and `/v1/approvals/{id}`, and a real
     `workflow_run` fixture still `202`s.
3. **SEC-02, SEC-04, SEC-06: operator token on live deployments.**
   - Add `operator_token: SecretStr | None = None` to `Settings` (`HARNESS_OPERATOR_TOKEN`).
     `SecretStr` fields are registered with the Redactor by type (`deps.py` `for name in
     type(settings).model_fields`), so no extra wiring is needed; confirm with a test.
   - When `settings.gateway == "github"`, `POST /v1/runs` (both modes) and
     `POST /v1/approvals/{id}` require `Authorization: Bearer <token>`, compared with
     `hmac.compare_digest`. An unset token → `403` (fail closed). A missing or wrong header →
     `401` with a problem body that names no config.
   - When `gateway == "replay"`, behaviour is unchanged. That keeps today's Space, the Verify
     blocks and the ten test files that post `cicd:` keys to `/v1/runs` working.
   - `decided_by` comes from the authenticated route (`"operator"`), not from the body's
     `actor`, when the token is in force.
   - Re-check `live_allowed(record.context.repo)` before `_execute_approved` runs a live
     plan.
   - The signed webhook stays the only anonymous way into a live deployment.
   - Tests:
     - gateway=github without a token: 403 on both routes.
     - With the token but a wrong header: 401.
     - With the right header: 202/200.
     - gateway=replay: unchanged (existing suites green).
     - The SEC-06 squatting reproduction: a `cicd:` key pre-claimed on `/v1/runs` without
       the token is refused, and the signed delivery then answers `202`, not
       `deduplicated`.
   - `PLAN.md` A.12 and Appendix E get an amendment note.
4. **SEC-03, SEC-24: validate every URL segment in the gateways**
   (`src/integrations/cicd/gateway_github.py`, `gateway_replay.py`). One helper module in
   `src/integrations/cicd/`:
   - SHA: `^[0-9a-f]{7,40}$`.
   - Branch/ref: git's ref-name rules (no `..`, no leading or trailing `/`, no `//`, no
     `.lock` suffix, no control characters or `~^:?*[\`).
   - File path: non-empty `/`-separated segments, none `.` or `..`, no `\ ? # %` or control
     characters, each segment through `urllib.parse.quote(seg, safe="")`.

   Apply it to:
   - `_current_blob_sha`, `_create_or_update_file`, `get_file_contents`
   - `get_commit`, `compare_commits`, `_get_ref`
   - the replay gateway's SHA lookups

   A violation raises `ToolError(kind="invalid_args")` and sends nothing. That matters in
   dry-run too: the pre-check `GET`s carry the PAT.

   Tests: `contents/../../../user`, `x?ref=evil` and `a/../../b` are refused before
   any request (use a transport that fails on any call). The SEC-02 reproduction
   `head_sha="../../../victim/private/commits/main"` is refused. Legitimate paths such as
   `src/app/pricing.py` still resolve, and a path with a space is percent-encoded.
5. **SEC-05: the plan's branch, base and paths are derived, not trusted**
   (`src/integrations/cicd/remediation.py`, `normalize_plan`, applied before approval):
   - Branch: always `agent/fix/<signature_id[:8]>`, overwriting whatever the model wrote.
     This makes the comment at `schemas.py:297` true.
   - Base: the failing run's branch from the bundle.
   - Paths under `.github/` or failing item 4's rules are dropped, and the drop is recorded
     in the existing `dropped` list.
   - In `_create_or_update_file`, refuse a branch without the `agent/fix/` prefix.
   - Tests: a model draft with `branch: "main"` and a `.github/workflows/x.yml` file
     normalises to the agent branch and drops the workflow file. The replay fixtures'
     expected plans still match (update their expected branch name if it differs, and say
     so in the commit).
6. **SEC-09: linear fingerprinting** (`src/integrations/cicd/fingerprint.py:87-96`):
   - Add a `(?<![\w.])` lookbehind to `_EXCEPTION`.
   - Header pattern: drop the nested `\s+`, for example
     `^_{3,}[ ]+(?P<title>\S.*?\S|\S)[ ]+_{3,}$`. The timing test below decides, not the
     pattern's looks.
   - Cap each cleaned line at 2,000 characters before matching.
   - Call the fingerprint via `asyncio.to_thread` in the Investigator.
   - Tests: a 2 MiB adversarial log (`"Error: " + "a"*16000` lines, and a header line with
     800 spaces) fingerprints in < 1 s. All five scenarios keep their `signature_id`s: pin
     the current values first.
7. **Confirmed 2026-10-01; do all three:**
   - **SEC-07:** refuse with `429` when runs queued behind the semaphore exceed
     `2 × max_concurrent_runs`. This is admission control, not the declined daily guard.
   - **SEC-18:** `docker-compose.yml` → `"127.0.0.1:8000:8000"`.
   - **SEC-19:** `seed_demo_repo.sh` sends the hook body through `gh api --input -`, so the
     secret leaves argv.
8. **Record and commit.**
   - Add an amendments paragraph to `docs/progress/phase-5/backlog.md`, naming the SEC ids
     closed and the ones carried.
   - Commit as `Security gate for live exposure: operator token, body cap, URL segment
     validation, linear redaction and fingerprinting` with the trailer. Stage files
     explicitly. **Do not stage `docs/security/`.**
   - **Independent audit (confirmed):** one `phase-reviewer` dispatch over
     `<stage-1 commit>..HEAD`. The brief names SEC-01 to SEC-09 and SEC-07, SEC-18, SEC-19,
     and asks only whether each fix closes its reproduction and what it broke. Specify a
     terse report format. Findings go in `docs/progress/phase-5/review-security.md`. Fix
     anything it finds, and prove each fix's test against the pre-fix tree.
   - **Early replay-mode deploy (confirmed; ask before each push):** once the audit is
     clean, run the Stage 0 gate again, then:
     1. `git push space master:main`, with the Space still `HARNESS_GATEWAY=replay`.
     2. `scripts/check_space.py --wait-for-new-build`.
     3. Check SEC-01 closed, free: a `POST /v1/runs` with a 120 KB PEM-marker key answers
        `422` in well under a second. SEC-08's `413` is checked the same way.
     4. `git push origin master:main`.

     Record the deploy in `backlog.md`. Session B then needs no code deploy, only the Space
     settings change; Stage 5 step 2 applies only if commits were added after this push.

## Session A — Stage 2: GitHub prep (0 requests; each item is the user's action or needs their OK)

1. **Webhook secret.** Generate one without echoing it into history:
   `python -c "import secrets; print(secrets.token_hex(32))"`, then paste it into the local
   `.env` (`HARNESS_GITHUB_WEBHOOK_SECRET`) with an editor. The same value goes into three
   places: `.env`, the Space secret and the GitHub hook. Never use the `.env.example`
   placeholder: it is honoured (backlog), so a placeholder secret would quietly accept
   signatures. Strip trailing whitespace when pasting it into the Space UI (SEC-13: the
   registry holds the raw value).
2. **Operator token.** Generate a second random value the same way. Put it in `.env` as
   `HARNESS_OPERATOR_TOKEN`. It is also needed as a Space secret in Stage 5.
3. **Seed the demo repo.** Run in Git Bash as the owner:
   `scripts/seed_demo_repo.sh Shakti8125/harness-demo-repo`. Confirm the owner name with the
   user before running.
   - **Pre-check `python3` in Git Bash first:** `bash -c 'command -v python3 && python3 -V'`.
     Line 327 runs `python3 - <<'EOF'` *after* `main` is pushed. A missing `python3`, or
     the Windows Store alias, stops the script halfway through. If that happens, fix
     `python3` and re-run with `--force`, which fast-forwards `main` and never force-pushes
     it.
   - **`--force` only against `Shakti8125/harness-demo-repo`, re-typed and checked.** On
     any other repo it replaces the whole tree of `main` (SEC-20).
   - **Run it WITHOUT `--webhook`.** The `demo/regression` and `demo/dependency` pushes fire
     two failing runs. A hook registered now would deliver both, which is up to 6 unplanned
     requests. Those failed runs become Stage 4a's input.
   - When it finishes, note the failed run ids:
     `gh run list -R <repo> --branch demo/regression --json databaseId,conclusion`.
4. **Fine-grained PAT**, scoped to the demo repo only, with a 7–30 day expiry.
   - `HARNESS_DRY_RUN` stays `true` throughout this test, so **read-only suffices**:
     Actions, Contents, Metadata, Pull requests and Issues, all read. Dry-run write tools
     still run their GET pre-checks.
   - **Never grant Workflows or Administration**, now or later. The PAT's scope is the last
     boundary behind SEC-03 and SEC-05.
   - Appendix E's write scopes are needed only if `dry_run=false` is ever chosen. That is not
     in this plan, and it additionally requires SEC-10 and SEC-11 to be fixed.
   - Put the PAT in the local `.env` as `HARNESS_GITHUB_TOKEN`, replacing the placeholder.
5. **Optional:** `gh extension install cli/gh-webhook`, for Stage 4b.

---

## Session B — the live day

### Budget and stop rules

| Step | Requests (nominal) | Transient worst case (profile: 2/agent) | Hard ceiling (7/agent) |
|---|---|---|---|
| Probe | 1 | 1 | 1 |
| 4a local `replay.py --live`, regression | 3 | 6 | 21 |
| 4b local webhook, flaky (optional) | 3 | 6 | 21 |
| 5 Space webhook, flaky; Redeliver is free | 3 | 6 | 21 |
| 5+ Space `gh run rerun` on regression (optional) | 3 | 6 | 21 |

The hard-ceiling column is there to be honest, not to plan against. One run *can* spend the
day. The ledger after every step is what keeps that from happening twice.

- **When to start:** begin after the reset: 12:30 IST, or 13:30 IST after 2026-11-01. Aim to
  finish before ~20:00 IST; the observed overload window is around 21:00 IST.
- **Check the day first:** run `quota_ledger.py` against local and `--space`. Its run list
  shows strangers' replays on the Space.
- **Probe once.** On `ERROR 503`, wait 30–60 minutes and probe again, at most 3 probes. On
  `429`, the day is gone.
- **After every step:** run the ledger. If the total is ≥ 16, stop and skip the optional
  rows. **If one run spent ≥ 7 requests, stop too.** That is schema retries stacking, and
  the next run will likely do the same.
- **Stop on any `llm_upstream` or `rate_limited` escalation.** Do not retry it the same day.
  Record it, because it is a result.

### Stage 4 — local live (no Space change)
Put these in `.env`:
- `HARNESS_GATEWAY=github`
- `HARNESS_ALLOWED_REPOS=["Shakti8125/harness-demo-repo"]` (the JSON list form; a bare string
  crashes boot with a values-withheld `SettingsError`)
- `HARNESS_DRY_RUN=true`
- `HARNESS_OPERATOR_TOKEN=…` (from Stage 2)
- the free-tier profile

Run the server with bare `uvicorn` on `127.0.0.1`. Do not use `docker compose` here: it
publishes port 8000 on every interface (SEC-18).

- **4a.** `uv run python scripts/replay.py --live --repo Shakti8125/harness-demo-repo --run-id <failed demo/regression run>`
  - This runs in-process (`replay.py:209-248`). It uses neither `/v1/runs` nor the
    operator token.
  - Expect the baseline `default_green` (Phase 5 amendment 12) and category
    `real_regression`.
  - Expect `suspected_commit_sha` to be the "Round discounts up" commit and the status
    `awaiting_approval` with the fix PR held (`open-fix-pr`).
  - **New after 1e:** the held plan's branch is `agent/fix/<8 hex>` and touches no
    `.github/` path.
  - Expect every gateway span to be a read or `dry_run=True`.
  - Do not paste `--json` output anywhere public.
- **4b (optional).**
  1. Start `uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000`.
  2. **Free check (0 requests):** `POST /v1/runs` with `mode=live` and no `Authorization`
     header → `403`/`401`. That is SEC-02 closed on the real app.
  3. Start `gh webhook forward --repo Shakti8125/harness-demo-repo --events workflow_run --url http://127.0.0.1:8000/webhooks/github --secret <secret>`.
     Check the flag names with `--help` first. If the extension takes the secret on argv,
     accept that for a local-only session, or skip 4b.
  4. Fire `gh workflow run flaky.yml -R Shakti8125/harness-demo-repo`. `load=high` fails about
     80% of the time. A green run gets a `204` and costs nothing; re-dispatch it if needed.
  5. Expect a `202`, then `completed` with `flaky_test` and `rerun_failed_jobs` executed
     dry-run.
  6. Check dedupe for free by posting the same delivery again. Two ways: capture the
     forwarded body and use `scripts/replay.py --post-signed <file>`, or read it from
     GitHub's delivery API. Expect `deduplicated`, and one `run` row via the Verify step 4
     `sqlite3` queries.
  7. Stop the forwarder.
- **Optional, 0 model requests:** record the real failure as a replayable scenario with
  `scripts/record_fixture.py --repo … --run-id … --name live_flaky`. Then run
  `scripts/scrub_fixtures.py --check` **with the default root**. A mistyped `--root` passes
  vacuously (SEC-21). Also confirm by eye that the new directory is under
  `fixtures/scenarios/`. Commit it only after a manual read for secrets.

### Stage 5 — the Space (the literal step 5; ask before each sub-step)
**Precondition:** Stage 1e is committed, and its tests and (if agreed) its independent
audit are green. Without it, stop here.

1. **Space settings, user action in the Space UI.** Setting these restarts the Space and
   wipes its DB.
   - Secrets:
     - `HARNESS_GITHUB_TOKEN` (the PAT)
     - `HARNESS_GITHUB_WEBHOOK_SECRET`
     - `HARNESS_OPERATOR_TOKEN`

     Paste each one without a trailing newline.
   - Variables:
     - `HARNESS_GATEWAY=github`
     - `HARNESS_ALLOWED_REPOS=["Shakti8125/harness-demo-repo"]`
     - the four free-tier `HARNESS_LLM_*` values
   - Leave `HARNESS_DRY_RUN` unset; it defaults to true.
2. **Deploy, only if needed.** Session A's commits normally went out in Stage 1e item 8's
   early replay-mode deploy. Check first: `git log --oneline space/main..master` is empty
   when there is nothing new. If commits were added since, run `git push space master:main`
   and then `git push origin master:main`, asking before each.

   Either way, after step 1's settings restart, run `scripts/check_space.py
   --wait-for-new-build`. Poll for something only the live configuration does (handoff §2),
   not for a `200`. The discriminator: `POST /v1/runs` with `mode=live` and no token now
   answers `403`/`401`, where replay mode answered `501`. That proves the restart applied
   `HARNESS_GATEWAY=github` and that SEC-02 is closed, for 0 requests.
3. **Wake the Space right before firing** by running `check_space.py` again. A cold Space
   overruns GitHub's 10 s delivery timeout (Open Risk 11). If the first delivery times out,
   Redeliver.
4. **Register the hook.** Enter the secret without putting it in shell history or argv
   (SEC-19):
   ```bash
   read -rs HARNESS_GITHUB_WEBHOOK_SECRET && export HARNESS_GITHUB_WEBHOOK_SECRET
   scripts/seed_demo_repo.sh Shakti8125/harness-demo-repo --webhook-only --webhook https://shakti-agent-harness.hf.space/webhooks/github
   unset HARNESS_GITHUB_WEBHOOK_SECRET
   ```
   It touches no ref. Unless 1e item 7's `--input -` change landed, the secret is briefly in
   `gh`'s argv; on a single-user machine that is accepted.
5. **Fire** `gh workflow run flaky.yml -R Shakti8125/harness-demo-repo`.
   - Find the delivery with `gh api repos/<repo>/hooks` (hook id), then
     `gh api repos/<repo>/hooks/<id>/deliveries`. Expect `status_code` **202**. The UI's
     "Recent Deliveries" shows the same.
6. **Redeliver**, either from the UI or with
   `gh api -X POST repos/<repo>/hooks/<id>/deliveries/<delivery_id>/attempts`.
   - PLAN's literal says "202". The implemented route answers a duplicate of a *finished* run
     `200 {"status":"deduplicated",…}` (verify.md step 4). Record the observed code against
     the literal.
   - The invariant being tested is **no second run**: `GET /v1/runs` lists one run for that
     delivery.
7. **Read the result.** The list carries no `final`, so take the run id from it and then
   read the run:
   ```bash
   curl.exe -s "https://shakti-agent-harness.hf.space/v1/runs?limit=5" | jq '.items[] | {run_id, status, created_at}'
   curl.exe -s "https://shakti-agent-harness.hf.space/v1/runs/<run_id>" | jq '{status, category: .final.diagnosis.category}'
   ```
   Pick the run whose `created_at` matches the delivery, not blindly `.items[0]`: strangers'
   replays share the list. Expect `completed` and `flaky_test`. Open `/runs/<id>/view` and
   save what it shows.
8. **Optional, if the ledger allows:** `gh run rerun <failed demo/regression run id>`. The new
   `run_attempt` is a new Appendix C key and gets a new delivery. Expect `real_regression`
   and `awaiting_approval` on the Space. Do **not** approve it, since approvals now need the
   operator token and there is nothing to learn from a dry-run approval here. Let it expire.
9. **Check for anonymous activity (0 requests):** list today's runs with the ledger's
   `--space` and account for every one. Any run you did not start is a finding to record,
   not to ignore.

### Stage 6 — record and roll back
- **Records:**
  - `docs/progress/phase-5/verify.md`: add a section "Step 5 — Live — <date>" with each
    command, the literal expected and the actual result, the request count, and the
    202-vs-200 note.
  - `PLAN.md` Execution-order row 5: step 5 run.
  - README: a line for the live number.
  - `docs/security/assessment-2026-09-30.md`: add a "Status" line per SEC id (fixed in
    `<sha>` / carried). Once the fixes are deployed, commit it (ask first; the repo is
    public).
  - Memory: update `phase-5-live-steps-user-gated.md` and `gemini-quota-day-is-pacific.md`,
    adding the DST shift and the profile.
- **Tag:** do not move `phase-5-green`. Ask whether to add a new tag, e.g.
  `phase-5-step5-live`, and whether to push it to `origin`.
- **Exposure rollback. Ask the user; the recommended default is roll back:**
  1. Deactivate or delete the hook: `gh api -X DELETE repos/<repo>/hooks/<id>`.
  2. Set the Space back to `HARNESS_GATEWAY=replay` and remove `HARNESS_ALLOWED_REPOS`.
  3. Revoke the PAT, or let it expire. Remove it from the Space secrets.

  A live hook left on a public Space spends the shared quota on every red run. Keep the
  free-tier profile variables either way. `HARNESS_OPERATOR_TOKEN` can stay; in replay mode
  it gates nothing.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Delivery `401` | Secret mismatch between the hook and the Space, or a trailing newline pasted into the Space secret | Re-set the one side that is wrong. Blank secret means every delivery `401`s. |
| Delivery `403` | Repo not in the allowlist, or the Space is still in replay mode | Check the JSON list form and `HARNESS_GATEWAY` |
| Delivery `204` | Not `completed`+`failure` (a green flaky run) | Re-dispatch |
| Delivery `413` | Body over the 1 MiB cap (1e item 2) | Should not happen for `workflow_run`; check what is posting |
| GitHub "timed out" | Cold Space | Wake it, then Redeliver |
| `POST /v1/runs` → `403` on the Space | Expected after 1e: live deployments need `Authorization: Bearer $HARNESS_OPERATOR_TOKEN` | Send the header, or use the webhook |
| Boot crash naming a variable | A typo'd `HARNESS_*` name (`extra=forbid` plus the env scan) | Fix the name |
| Run escalated `llm_upstream`/`rate_limited` | Overload or quota | Stop for the day, record it, probe tomorrow |
| Run escalated `tool_failure` with a 403 | The PAT is missing a read scope | Add the read scope; nothing was written (dry run) |
| Run escalated `tool_failure`, `invalid_args` naming a path or ref | 1e item 4 refused a model-supplied segment | Record it: that is the guard working. Read the trace for what the model tried. |
| Ledger total jumps with runs you did not start | Anonymous `/v1/replay` calls (SEC-07) | Record them; if the day is threatened, stop and consider 1e item 7 |

## Verification of this plan's code changes (Session A)
- The new tests fail on the pre-fix tree and pass after the change: Stage 1 (retry settings,
  ledger DST rule) and Stage 1e (items 1–6, each with its reproduction).
- Full `pytest` green, `ruff` clean, `mypy --strict src/harness` clean.
- `git diff --stat -- src/harness/` is empty **across both commits**.
- The stub eval still gives 5/5, with the same `signature_id`s as before 1e item 6.
- `quota_ledger.py --db ./data/harness.db --since-utc 2026-09-17T07:00:00+00:00` prints
  **7**: 6 `ok` plus 1 `LlmUpstreamError`. Checked against the local DB on 2026-09-30. That
  is step 1's 4 attempts (one `503`) plus step 3's 3, as `verify.md` records. The eval used
  temporary DBs, the Space its own DB, and the probe none, so the ledger will not show the
  day's full 21. That gap is exactly why `--space` and the by-hand rule exist.
- `probe_gemini.py` prints `OK` (1 request).
- The independent audit of Stage 1e, if agreed, reports no open finding against SEC-01 to
  SEC-09.
