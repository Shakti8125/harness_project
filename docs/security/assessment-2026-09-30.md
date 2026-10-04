# Security assessment — 2026-09-30

> **Handling.** This document contains working exploit detail for a public deployment
> (<https://shakti-agent-harness.hf.space>). It was held out of the public repository until
> SEC-01 through SEC-09 were fixed (`5602f47`, `a0a64a2`) and deployed (`a0a64a2`,
> 2026-10-01). It is now the record of what was found and fixed. Each finding carries a
> **Status** line. Of the carried findings, SEC-10 (medium while the Space is in live mode)
> and SEC-11 must be fixed before anyone sets `dry_run=false`; SEC-07's anonymous quota
> drain was accepted by the step-5 plan; the rest are low or info.

| | |
|---|---|
| Tree audited | `master` = `phase-5-green` = `06c87db` (the commit the Space serves and GitHub `main` holds) |
| Method | Four read-only auditors in parallel, each owning one slice (below), then a coordinator verification pass over every high and medium finding |
| Network | None to Gemini, GitHub or the Space. The dependency audit queried the public vulnerability database only. |
| Model requests spent | 0 |
| Plan this feeds | `docs/progress/phase-5/step5-live-plan.md` (finalised the same day; its Stage 1e is this document's remediation list) |

## 1. Summary

Nothing in the audit shows a secret has leaked. The git history, the Docker image, the
fixtures, the local databases and every served JSON and HTML route were checked, and the real
`.env` values appear nowhere (§6). The HMAC check on the webhook is correct. SQL is
parameterised throughout. The trace view escapes everything it renders. Dry run holds on every
write path.

The problems are about **who can make the harness act**, and **what a model's output can
steer once it acts**:

1. **Today's Space can be stalled by one anonymous request (SEC-01).** The redaction
   pattern for PEM keys is quadratic, and a 422 response scrubs caller-shaped text with it
   before bounding. On this machine, 62 KB of repeated `-----BEGIN RSA PRIVATE KEY-----` takes
   1.07 s to scrub, and the time quadruples every time the size doubles. The whole app runs on
   one event loop, so `/healthz` stalls with it.
2. **The Step-5 live configuration hands every anonymous caller the live gateway (SEC-02,
   SEC-04).** `POST /v1/runs` accepts `mode=live` with no credential, and `POST
   /v1/approvals/{id}` accepts any decision from anyone. Both are harmless in today's replay
   deployment and become the main exposure the moment the Space holds a PAT.
3. **The GitHub gateway builds API URLs from values it does not encode (SEC-03, SEC-05).**
   File paths, SHAs and branch names come from the model or the caller and are interpolated
   raw. httpx resolves `..` before sending, so a value like `../../../x` leaves the repo.
   `contents/../../../user` goes out as `GET https://api.github.com/repos/user`. The fix
   plan's branch name is constrained only by a comment. Under dry run, only the GET
   pre-checks go out. With dry run off, a model-chosen branch of `main` would commit straight
   to the default branch.

Scale:

| Severity (worst configuration) | Count | IDs |
|---|---|---|
| Critical, but only with `dry_run=false` | 3 | SEC-02, SEC-03, SEC-04 |
| High | 2 | SEC-01 (today), SEC-05 (only with `dry_run=false`) |
| Medium | 6 | SEC-06 – SEC-11 |
| Low | 13 | SEC-12 – SEC-24 |
| Info | 1 | SEC-25 |

**For today's deployment (A below)** the top items are SEC-01 (high), plus SEC-04, SEC-07
and SEC-08 (medium). **Before the Step-5 live day (B)**, SEC-01 through SEC-09 must be
fixed. The plan's Stage 1e lists them with tests; all of the fixes land in `src/api/` and
`src/integrations/`, and none in `src/harness/`. **Before anyone sets `dry_run=false` (C)**,
SEC-10 and SEC-11 must also be addressed, and the PAT must never carry the Workflows or
Administration permissions.

## 2. Configurations assessed

Every finding is rated separately for each configuration, because the same line of code is
harmless in one and critical in another.

| | Gateway | Credentials on the host | Dry run | Allowlist |
|---|---|---|---|---|
| **A — today** | `replay` | Gemini key only (the `HARNESS_GITHUB_TOKEN` on the Space is unused) | on | empty |
| **B — Step-5 live** (the plan) | `github` | Gemini key, a fine-grained PAT (demo repo only, read-only), webhook secret | on | `["Shakti8125/harness-demo-repo"]` |
| **C — writes on** (not planned) | `github` | as B, with a write-capable PAT | **off** | as B |

## 3. Threat model

**Assets.**
- The Gemini free-tier quota: 20 requests per model per Pacific day, and a `503` counts.
  The Space and local dev share one key.
- The Gemini key, the PAT, the webhook secret.
- The integrity of the demo repository.
- The Space's availability.
- The integrity of the Step-5 result itself: a spoofed or suppressed run is a false record.

**Actors.**
1. Anyone on the internet: every route except the webhook is unauthenticated, by PLAN's
   stated choice for a portfolio demo.
2. Anyone who can make CI print text in the monitored repo: a pusher, or the author of a
   fork PR. That text reaches three LLM prompts, and the Remediator's output becomes GitHub
   API calls.
3. A peer on the operator's LAN, when the app runs under `docker compose`.
4. The operator, by mistake: a mistyped repo passed to `--force`, a secret in shell history,
   a typo in the fixture root.

**Trust boundaries.**
- The signed webhook versus the unsigned JSON routes.
- CI content versus model output versus a tool call.
- `dry_run` at the gateway.
- The PAT's scope at GitHub, which is the last boundary and the one SEC-03 falls back on.

## 4. Method

| Slice | Files | Auditor |
|---|---|---|
| Ingress and access control | `src/api/main.py`, `webhook.py`, `deps.py` (request paths) | `phase-reviewer`, briefed for security |
| Untrusted CI content → repo writes | `src/integrations/cicd/agents/*`, `remediation.py`, `gateway_github.py`, `catalog.py`, `policy.yaml`, `src/harness/guardrails.py` | `phase-reviewer`; **its report arrived truncated after finding 1**, and the coordinator finished the slice by reading the code (§5 marks which items) |
| Secrets, redaction, rendering | `settings.py`, `observability.py`, the Redactor wiring, `trace_view.py`, `trace.html`, `rendering.py`, the leak test, Docker and git hygiene | `phase-reviewer` |
| Persistence, scripts, container, dependencies | `storage.py`, `memory.py`, migrations, `fingerprint.py`, `history.py`, `scripts/*`, `Dockerfile`, `uv.lock` | `phase-reviewer` |

Evidence labels:
- **Verified (coordinator).** The coordinator re-read the code path or re-ran the
  reproduction independently of the auditor.
- **Confirmed (auditor).** The auditor traced the path end to end or reproduced it
  in-process with dummy settings and the network patched out.
- **Plausible.** The code path exists, but part of the exploit depends on something not
  exercised, such as GitHub-side behaviour or a model actually following an injection.

## 5. Findings

### SEC-01 — PEM redaction is quadratic; one anonymous request stalls the Space
**A: High · B: High · C: High** · Verified (coordinator)

> **Status (2026-10-04):** **Fixed** in `5602f47` (bounded PEM body that stops at the next `BEGIN`). Deployed `a0a64a2` 2026-10-01; on the Space the 120 KB reproduction answers `422` in 1.47 s, the time a benign 120 KB body takes (was 14.5 s).

- **Where:** `src/api/deps.py:88-90`
  (`-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?`). It
  is reached from `problem()` at `src/api/main.py:320`, which scrubs *before* bounding the
  detail at `:322`, and from the at-rest scrub at `src/harness/memory.py:366`.
- **Issue:** For every `BEGIN` with no matching `END`, the lazy body scans to the end of the
  input, so the cost is O(n²). `Redactor.scrub` is synchronous CPU work inside async
  handlers.
- **Evidence:** Coordinator timing of the pattern alone: 15.5 KB takes 0.07 s, 31 KB takes
  0.27 s, 62 KB takes 1.07 s. The auditor drove the whole route: a `POST /v1/runs` carrying
  one extra JSON key made of the BEGIN marker repeated to 120 KB answered `422` after
  14.5 s. A benign 120 KB key answered in 0.01 s.
- **Exploit:** Extrapolated, a ~1 MB key blocks the event loop for several minutes. Every
  route stalls, `/healthz` included. It is repeatable and needs no credential. In B and C a
  CI log or diff can carry the same text into `save_run`.
- **Fix:** Stop the body scan at the next `BEGIN` and bound it:
  `-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:(?:(?!-----BEGIN )[\s\S]){0,16384}?-----END [A-Z ]{0,40}PRIVATE KEY-----)?`,
  or replace the regex with a `str.find` scan. Add a timing test: 1 MB of adversarial input
  scrubs in under 0.5 s. SEC-08's body cap bounds the input as well. `SECRET_PATTERNS` lives
  in `src/api/`, so the harness is untouched.

### SEC-02 — `POST /v1/runs` accepts `mode=live` from anyone; the caller's SHA escapes the allowlist
**A: n/a (answers 501) · B: High · C: Critical** · Verified (coordinator: code); traversal Confirmed (auditor)

> **Status (2026-10-04):** **Fixed** in `5602f47`: `POST /v1/runs` needs `Authorization: Bearer <HARNESS_OPERATOR_TOKEN>` when `HARNESS_GATEWAY=github`, and every sha goes through `url_segments`. Deployed `a0a64a2` 2026-10-01; checked live 2026-10-03/04: `401` without the token.

- **Where:** `src/api/main.py:1013-1033` (the live branch checks only
  `settings.gateway` and `live_allowed(parsed["repo"])`), `:1050-1052`. The caller's
  `head_sha` reaches `f"/repos/{repo}/commits/{sha}"` at
  `src/integrations/cicd/gateway_github.py:817-820`.
- **Issue:** In live mode the only gate is that the subject *names* the allowlisted repo.
  The caller also chooses `run_id`, `run_attempt`, `head_sha`, the workflow name and the
  idempotency key, and there is no HMAC on this route.
- **Exploit (B):** Anyone can start live runs against the demo repo on demand. Each run
  spends Gemini quota (nominal 3 requests; see SEC-07) and PAT rate limit. The auditor
  reproduced `head_sha="../../../victim/private/commits/main"` producing
  `GET https://api.github.com/repos/victim/private/commits/main` with the PAT attached.
  Whatever the PAT can read comes back through `citations[].quote` in `GET /v1/runs/{id}`.
  With the plan's single-repo read-only PAT, that means the demo repo (public anyway) and
  public GitHub data.
- **Exploit (C):** As B, plus `create_issue` (allowed at confidence ≥ 0.0) and
  `rerun_failed_jobs` run under the PAT with no approval.
- **Fix:** When `HARNESS_GATEWAY=github`, require an operator credential on `POST /v1/runs`
  (a new `HARNESS_OPERATOR_TOKEN`, `SecretStr`, compared with `hmac.compare_digest`; unset
  means refuse). The signed webhook stays the only anonymous way to start a live run.
  Validate `head_sha` as `^[0-9a-f]{7,40}$` (SEC-03).

### SEC-03 — Model- and caller-supplied path, SHA and ref segments are interpolated unencoded into GitHub API URLs
**A: n/a · B: Medium · C: Critical** · Verified (coordinator: httpx normalisation reproduced); GitHub-side acceptance Plausible

> **Status (2026-10-04):** **Fixed** in `5602f47` (`url_segments` validates and encodes every sha, ref and path in the GitHub gateway); `a0a64a2` matches with `fullmatch` and refuses non-strings. Deployed `a0a64a2` 2026-10-01.

- **Where:** `gateway_github.py:580` (`_current_blob_sha`, the GET pre-check), `:591`/`:604`
  (the `PUT contents` write), `:823` (`get_file_contents`), `:817-820` (`compare_commits`,
  `get_commit`), and the ref lookup at `:537`, which puts a branch name in the URL. `FilePatch.path`
  (`schemas.py:289`) and the catalog's `sha`/`base`/`head` are bare `str`, with no
  validation anywhere.
- **Evidence (coordinator, httpx 0.28.1):**
  `build_request("PUT", "/repos/o/r/contents/../../../user")` sends to
  `https://api.github.com/repos/user`. A `?ref=…` inside a path becomes a real query
  parameter. `%2f` is not decoded, so only literal `../` escapes.
- **Exploit (B, dry run on):** The write methods return before the network write, but the
  pre-check `GET`s run first and carry the PAT. A prompt-injected Remediator path such as
  `../../../<owner>/<repo>/contents/x` reads outside the demo repo, as far as the PAT
  allows.
- **Exploit (C):** `PUT /repos/<other>/<repo>/contents/<x>` writes to another repository if
  the PAT covers it. A fine-grained single-repo PAT makes GitHub refuse it, and that scope is
  the only thing that does.
- **Fix:** In `gateway_github.py`, validate before building any URL, and answer a violation
  with `ToolError(kind="invalid_args")` without sending a request. SHA:
  `^[0-9a-f]{7,40}$`. Branch and ref names: git's ref-name rules (no `..`, no leading or
  trailing `/`, no `//`, no `.lock` suffix, no control characters or `~^:?*[\`). File paths:
  non-empty `/`-separated segments, none of them `.` or `..`, no `\ ? # %` or control
  characters, and each segment passed through `urllib.parse.quote(seg, safe="")`. Add the
  same SHA check to the replay gateway (SEC-24).

### SEC-04 — Anyone can approve or reject a held fix plan, under any name
**A: Medium · B: Medium · C: Critical** · Verified (coordinator)

> **Status (2026-10-04):** **Fixed for live deployments** in `5602f47`: the operator token guards `/v1/approvals`, `decided_by` is `operator`, and `live_allowed` is re-checked before execution. Deployed `a0a64a2` 2026-10-01; `401` without the token, checked live. **Accepted:** replay deployments stay public by PLAN's choice.

- **Where:** `src/api/main.py:1460-1540` (no credential; `actor` is free text). The approval
  id is served publicly at `templates/trace.html:305`, in the `approval_id` attribute of the
  plan span (`agents/remediator.py:335`), and in `final.remediation.pending_approval` from
  `GET /v1/runs/{id}`. The execution path does not re-check `live_allowed` or
  `settings.gateway` (`deps.py:368-371`).
- **Exploit (A/B):** A stranger lists `/v1/runs`, reads the approval id and decides it as
  "shakti". The owner then gets `409`. A dry-run action observation is written to memory, and
  in B the PAT-backed GET pre-checks run. The real harm is a falsified demo record.
- **Exploit (C):** An anonymous approval creates a branch, commits the model-drafted files
  and opens a PR in the demo repo. Combined with SEC-05, the files can land on `main`.
- **Fix:** When `HARNESS_GATEWAY=github`, require the SEC-02 operator token on
  `/v1/approvals/{id}`, record `decided_by` from the credential rather than the body, and
  re-check `live_allowed(context.repo)` before executing.

### SEC-05 — The fix plan's branch, base and file paths are whatever the model wrote
**A/B: Info (dry run) · C: High** · Verified (coordinator)

> **Status (2026-10-04):** **Fixed** in `5602f47`: `normalize_plan` derives the branch `agent/fix/<signature_id[:8]>` and the base, and drops `.github/` paths; the gateway refuses a write outside `agent/fix/`. Deployed `a0a64a2` 2026-10-01; the live run of 2026-10-04 held `agent/fix/6349b35d`. Residual: approvals stored before `5602f47` are not re-normalised (none exist).

- **Where:** `schemas.py:297`: `branch: str  # deterministic: "agent/fix/{signature_id[:8]}"`.
  That comment is the only constraint. No code sets or checks it (`grep agent/fix src` finds
  only the comment). `remediation.py:210-240` passes `draft.branch`, `draft.base` and
  `patch.path` straight into `create_branch`, `create_or_update_file` and
  `open_pull_request`. `gateway_github.py:548-555`: `_create_branch` on an *existing* ref
  returns `already_exists` and does not refuse.
- **Exploit (C):** Log text persuades the Remediator to use `"branch": "main"`.
  `create_branch` sees the ref exists and returns `already_exists`, then
  `create_or_update_file` commits to `main` directly, and `open_pull_request(head=main,
  base=main)` fails *after* the commit. Paths under `.github/workflows/` are refused by GitHub
  only if the PAT lacks the Workflows permission. Otherwise that is code execution with the
  repo's Actions secrets. The approval screen shows the plan, but see SEC-04.
- **Fix:** In `normalize_plan`, which the harness applies before approval: derive the
  branch name as `agent/fix/<signature_id[:8]>` rather than trusting the model; set `base`
  to the failing run's branch (or the repo's default branch) from the bundle; refuse any
  path under `.github/` and any path failing SEC-03's rules. In the gateway: refuse to write
  to a branch that existed *before* this run's `create_branch`, unless it carries the
  `agent/fix/` prefix. Operationally: never grant the PAT Workflows or Administration.

### SEC-06 — Idempotency-key squatting suppresses triage of a real delivery
**A: Low · B: Medium** · Confirmed (auditor, reproduced); key derivation Verified (coordinator)

> **Status (2026-10-04):** **Fixed for live deployments** in `5602f47` (operator token on `/v1/runs`); `a0a64a2` admits signed deliveries even when anonymous replays fill the pool. Deployed `a0a64a2` 2026-10-01. **Accepted:** the replay-tier reproduction, by PLAN's choice.

- **Where:** `contracts.py:24` (any 8–128-character key), `main.py:1050-1052` (the caller's
  key is claimed as-is), `main.py:653-664` (the webhook key is `cicd:` +
  `sha256(repo|run_id|run_attempt)[:32]`, which is public and deterministic).
- **Exploit:** The auditor posted `/v1/runs` in replay mode with the key the
  `real_regression` delivery would compute. The genuinely signed delivery then answered
  `200 deduplicated` and served the squatter's `failed` run. In B, an observer of the public
  demo repo pre-claims `(repo, N, 1)` as soon as run N starts. The real failure is never
  triaged, Redeliver deduplicates too, and **Step 5 records a false result**.
- **Fix:** This is covered in B by SEC-02's operator token on *all* of `POST /v1/runs` when
  `HARNESS_GATEWAY=github`. Do not simply reserve the `cicd:` prefix: ten test files post
  `/v1/runs` with `cicd:` keys (e.g. `tests/integration/test_replay_e2e.py`), and Verify
  queries by key. A later clean-up can namespace replay-mode keys.

### SEC-07 — No admission control; anonymous callers can drain the quota
**A: Medium · B: Medium** · Confirmed (auditor)

> **Status (2026-10-04):** **Partly fixed** in `5602f47`: admission control answers `429` beyond `3 × max_concurrent_runs`. Deployed `a0a64a2` 2026-10-01. **Carried:** the anonymous quota drain through `/v1/replay`, by the step-5 plan's "no daily guard" decision.

- **Where:** `main.py:786-787`, `:983-988` and `:1062` spawn a task and a 15 s heartbeat per
  accepted request. The semaphore (`max_concurrent_runs`) limits execution, not
  acceptance. `recovery.py:312` sets each agent's hard ceiling at
  `max_attempts + transient_max_attempts + 2`, which is 9 with the defaults and 7 under the
  plan's free-tier profile.
- **Exploit:** A loop of `POST /v1/replay/<scenario>?sync=false` queues thousands of runs.
  The first seven or so spend the day's 20 requests, and the rest keep the loop and SQLite
  busy for as long as the queue lasts.
- **Fix (the user's call; the plan decided "no daily guard"):** Refuse with `429` when the
  number of runs *queued behind* the semaphore exceeds a small N (e.g.
  `2 × max_concurrent_runs`). This is admission control, not a daily quota guard. Until
  then, the plan's quota ledger is the only defence, and it only detects.

### SEC-08 — Request bodies are read without a size bound
**A/B/C: Medium** (the Hugging Face proxy's own cap is unknown) · Confirmed (auditor)

> **Status (2026-10-04):** **Fixed** in `5602f47` (1 MiB cap, declared or streamed). Deployed `a0a64a2` 2026-10-01; on the Space a declared and a chunked oversized body both answer `413`.

- **Where:** `main.py:1090` (`await request.body()` before the HMAC check), plus FastAPI's
  JSON parsing on `/v1/runs` and `/v1/approvals`. No limit is set in the app or in uvicorn
  (`Dockerfile:59`).
- **Exploit:** A multi-GB anonymous POST to `/webhooks/github` is buffered in memory. It
  also feeds SEC-01.
- **Fix:** Refuse `Content-Length` over 1 MiB with `413` (a `workflow_run` delivery is tens
  of KB), and stream-count bodies that carry no length.

### SEC-09 — The fingerprint regexes are super-linear on CI log lines
**B/C: Medium** (live mode only; logs come from GitHub) · Verified (coordinator: timing)

> **Status (2026-10-04):** **Fixed** in `5602f47` (linear patterns, a 2,000-character line cap, the fingerprint in `asyncio.to_thread`). Deployed `a0a64a2` 2026-10-01.

- **Where:** `src/integrations/cicd/fingerprint.py:87-92` (`_EXCEPTION`, used with
  `.search`) and `:96` (`_SECTION_HEADER`). They run synchronously on the raw log, up to 2
  MiB (`investigator.py:110,355`). `asyncio.to_thread` is not used anywhere in `src`.
- **Evidence (coordinator):** `_EXCEPTION.search("Error: " + "a"*n)` takes 0.094 s at
  n=2,000, 0.392 s at 4,000 and 1.585 s at 8,000, which is quadratic. The auditor measured
  24 s at 16,000, and 4.2 s on an 800-space section header.
- **Exploit:** A commit or fork PR whose CI prints a long identifier-like line stalls the
  process for as long as that line takes to fingerprint.
- **Fix:** Add a `(?<![\w.])` lookbehind to `_EXCEPTION`, so the search does not restart
  inside identifiers. Replace `\s+(.+?)\s+` in the header pattern with `[ ]+(\S.*?\S)[ ]+`.
  Cap each cleaned line at 2,000 characters before matching. Run the fingerprint in
  `asyncio.to_thread`. Add a test: a 2 MiB adversarial log fingerprints in under 1 s.

### SEC-10 — Memory history can be poisoned: signatures are forgeable and carry no provenance
**B/C: Medium** · Plausible (auditor)

> **Status (2026-10-04):** **Carried.** Must be fixed before anyone sets `dry_run=false`.

- **Where:** `fingerprint.py:242-261` (the key is repo + workflow name + job name + first
  `FAILED` node id + message; everything except the repo comes from the payload or the log),
  `investigator.py:215-235,394`, `webhook.py:113-116`.
- **Exploit:** A fork PR's run prints main's flaky test id and message, and so takes over
  main's flaky signature. Its verdicts move `prior_hint` and the memory-agreement bonus
  (`diagnostician.py:229`). Its reruns count against `retries_for_signature_24h < 2`
  (`policy.yaml:26`), so the real flaky test escalates instead of retrying. Runs posted to
  the unauthenticated `/v1/runs` are recorded the same way.
- **Fix:** Put `event` and `head_repository` into the signature scope. Record observations
  only from signed-webhook runs on trusted events (`push`, `workflow_dispatch`, same-repo
  `pull_request`). This needs to happen before C, not before B.

### SEC-11 — Issues are filed without approval, and their bodies carry attacker-influenced text
**A/B: Info (dry run) · C: Medium** · Verified (coordinator: policy); impact Plausible

> **Status (2026-10-04):** **Carried.** Must be fixed before anyone sets `dry_run=false`.

- **Where:** `policy.yaml` rule `file-ticket`: `create_issue` is `allow` at
  `final_confidence >= 0.0`, with no approval. The body is model-written
  (`remediation.py:196-205`) from prompts that include CI text.
- **Exploit (C):** Injected log text makes the drafted issue @-mention arbitrary users
  (notification spam from the PAT owner's identity) or carry phishing links, on every red
  run. SEC-02 lets an anonymous caller trigger that at will.
- **Fix:** Before C, strip or escape `@` mentions and raw HTML in agent-authored bodies, and
  raise the rule's threshold or make it `require_approval`.

### SEC-12 — LIKE wildcards in `claim_run` turn one request into a full-table read
**Low** · Verified (coordinator: code); Confirmed (auditor: 3,001 rows, 150 MB in one `fetchall`)

> **Status (2026-10-04):** **Carried** to the backlog (the fix is in `src/harness/`).

- **Where:** `src/harness/memory.py:633-637`,
  `SELECT * … WHERE idempotency_key = ? OR idempotency_key LIKE ?` with `f"{key}#%"`
  unescaped. It runs under the process-wide write lock.
- **Exploit:** A second `POST /v1/runs` with `idempotency_key: "%%%%%%%%"` fetches every
  `#fresh:` replay row, `outcome_json` included.
- **Fix:** `LIKE ? ESCAPE '\'` with `%`, `_` and `\` escaped, and select only the four
  columns the chain check reads. **This touches `src/harness/`**, so it stays in the
  backlog; the step-5 plan keeps the harness untouched. SEC-02's token closes the route in B.

### SEC-13 — Credential shapes the Redactor misses
**Low** · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `deps.py:82-100`, `:488`. Covered today: `gh[pousr]_`, `github_pat_`, `AIza`,
  `xox*`, `bearer`, PEM, `token:`/`api_key=`, and the registered values.
- **Gaps:**
  - `Authorization: Basic <b64>` inside a line. `curl -v -u x:$PAT` in a CI step prints the
    PAT in base64, which can be quoted into `citations[].quote` and served.
  - Userinfo URLs (`https://user:pass@host`).
  - Tokens cut short (`hp_…`, `ghp_` plus 35 characters).
  - The deployed Gemini key is `AQ.`-shaped (53 characters), which no pattern covers, so the
    literal registry value is its only protection.
  - The registry stores the *unstripped* environment value while the SDK strips it. A
    Space secret pasted with a trailing newline would never match.
- **Fix:** Register `value.strip()`. Add `(?i)\bbasic\s+[A-Za-z0-9+/=]{16,}`,
  `://[^/\s:@]+:[^@\s]+@` and `AQ\.[A-Za-z0-9_\-]{40,}`.

### SEC-14 — Raw CI log and diff text go into the model prompt unscrubbed
**Low** · Plausible (auditor)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `investigator.py:433-444`, `rendering.py:396-415`. Only the sinks are scrubbed.
- **Exploit:** A secret in a log plus an instruction such as "spell it with spaces" comes
  back in the model's output as `g h p _ …`, which no literal or shape match recognises.
- **Fix:** Scrub `log_text` and `diff_text` before assembling the context. That is one call,
  in `src/integrations/`.

### SEC-15 — The leak test does not test the production redactor, or transformed leaks
**Low** · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `tests/test_no_secret_leak.py:43-46`, `:77`, `:101-102`. It builds its own
  `Redactor(registry, SECRET_PATTERNS)`, which leaves out the heuristic tier and
  `build_redactor`'s wiring. It asserts an exact `str.count` of each sentinel, and never
  drives `app.py`.
- **Fix:** Build the context through `build_redactor`/`get_app_context`. Add sentinel
  variants that are prefix-cut, Basic-encoded, percent-encoded and padded with whitespace.

### SEC-16 — Background-run exception text is served publicly; a malformed subject returns 500
**Low** · Confirmed (auditor, reproduced)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `main.py:746` (`mark_failed(str(exc))` goes into `escalation.payload.detail`,
  which `/v1/runs/{id}` and `/v1/escalations` serve), `:703`, `:1013` (`parse_subject`
  outside any `try`).
- **Evidence:** `subject={"repository":{"full_name":123}}` produced the served detail
  `"'int' object has no attribute 'replace'"`, plus an unretrieved-task traceback.
- **Fix:** Store a fixed message plus the exception class, and validate the subject's shape
  into a `422`.

### SEC-17 — A captured signed delivery can be replayed after a restart
**B: Low** · Plausible (auditor)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `main.py:653-664`, `:813-828`. Deduplication rests on the `run` row alone, with
  no timestamp window. The Space's database is ephemeral (`app.py`: "no persistent disk").
- **Exploit:** Anyone holding a delivery body and its signature (a repo admin, via "Recent
  Deliveries") can start a fresh live run after every Space restart.
- **Fix:** Accept the risk for the demo and record it, or persist delivery GUIDs with a TTL.

### SEC-18 — `docker compose` publishes the unauthenticated API on every interface
**Low** · Verified (coordinator)

> **Status (2026-10-04):** **Fixed** in `5602f47` (compose publishes `127.0.0.1:8000:8000`).

- **Where:** `docker-compose.yml:7-8` (`"8000:8000"`).
- **Exploit:** A LAN peer reaches `/v1/runs` and `/v1/approvals` (SEC-02, SEC-04) and spends
  the operator's quota.
- **Fix:** `"127.0.0.1:8000:8000"`. The plan's Stage 4b uses a bare `uvicorn --host
  127.0.0.1` and is unaffected.

### SEC-19 — The webhook secret reaches process arguments and shell history
**Low** · Confirmed (auditor)

> **Status (2026-10-04):** **Fixed** in `5602f47` (the hook body goes to `gh api --input -` on stdin); used to register the demo repo's hook on 2026-10-03.

- **Where:**
  - `scripts/seed_demo_repo.sh:56`: `-f config[secret]="$HARNESS_GITHUB_WEBHOOK_SECRET"` is
    `gh`'s argv, and `GH_DEBUG=api` would log it.
  - `seed_demo_repo.sh:355` and the step-5 plan's Stage 5 step 4 both tell the operator to
    set the secret inline on the command line.
- **Fix:** Read the secret with `read -rs` and send the hook body through
  `gh api --input -`. The plan now tells the operator to use `read -rs` (Stage 5 step 4).

### SEC-20 — `seed_demo_repo.sh --force` on the wrong repo empties its `main`
**Low** · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (low); operator rule: `--force` only against the demo repo, re-typed and checked.

- **Where:** `seed_demo_repo.sh:276-283`. `reset --soft FETCH_HEAD` keeps an index holding
  only the demo tree, and the only repo check is the `*/*` shape at `:36`.
- **Exploit:** A mistyped `--force owner/real-repo` pushes a fast-forward commit that
  deletes every other file and adds four workflows. It is revertible, but live immediately.
- **Fix:** Refuse `--force` unless the target carries a marker file (or repo description)
  that identifies it as the demo repo.

### SEC-21 — `scrub_fixtures.py --check` can pass without checking anything
**Low** · Confirmed (auditor, reproduced)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `scripts/scrub_fixtures.py:49,74,95-97`. `rglob` on a missing root yields
  nothing, and the script prints "clean" with exit code 0. The extensions `.patch`, `.diff`,
  `.jsonl`, `.TXT` and extensionless files are never scanned.
- **Fix:** Exit non-zero on a missing root. Scan every file, and fail on any file that
  cannot be decoded.

### SEC-22 — Unpinned build inputs
**Low** · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (low).

- **Where:** `Dockerfile:5,7,24` (`python:3.12-slim` by tag, `uv:latest`), and
  `requirements.txt:23-30` (the Space's direct dependencies, unpinned and without hashes). The
  `uv.lock` audit in §7 therefore does not describe exactly what the Space runs.
- **Fix:** Pin the images by digest, and export hashed pins that are compatible with gradio.

### SEC-23 — Expression injection in the seeded `infra.yml`; no `permissions:` blocks
**Low** (dispatch needs write access) · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (low; in the seeded demo repository).

- **Where:** `seed_demo_repo.sh:225`:
  `run: pip install --index-url "${{ inputs.index_url }}"`. None of the seeded workflows
  declares `permissions:`.
- **Fix:** Pass the input through `env:` and quote it in shell. Add
  `permissions: contents: read` to every seeded workflow.

### SEC-24 — The replay gateway puts a model-supplied SHA into a filesystem path
**Low** (on Windows; on Linux a missing path component blocks it) · Confirmed (auditor, reproduced on this host)

> **Status (2026-10-04):** **Fixed** in `5602f47` (sha and path) and `a0a64a2` (every replay file-name part). Deployed `a0a64a2` 2026-10-01.

- **Where:** `gateway_replay.py:99-101,154,263-269`.
  `get_commit{sha: "x/../../../real_regression/webhook"}` read another scenario's JSON.
- **Fix:** The same `^[0-9a-f]{7,40}$` check as SEC-03.

### SEC-25 — No security headers; refusal details describe the configuration
**Info** · Confirmed (auditor)

> **Status (2026-10-04):** **Carried** (info).

- **Where:** `main.py:1256`: the HTML view sets no CSP, `nosniff` or `frame-ancestors`.
  `main.py:1022-1031`: the 501 detail names `HARNESS_GATEWAY=replay`.
  `main.py:1147-1152`: `403` versus `202` tells a caller whether a repo is allowlisted.
- **Fix:** Add `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
  frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` to the view. Use generic
  refusal details. Autoescaping already holds, so the headers are defence in depth.

## 6. Checked and clean

**Webhook.**
- The HMAC is computed over the raw body bytes and compared with `hmac.compare_digest`.
- The header must match `^sha256=[0-9a-fA-F]{64}$`, and a blank secret refuses everything
  (`webhook.py:60-66`).
- Verification runs before JSON parsing, the allowlist, the scenario match and the claim
  (`main.py:1097-1107`).
- A duplicated header yields its first value, which still has to verify.
- `X-GitHub-Delivery` is never part of the deduplication key (`webhook.py:119-124`).
- Missing, empty, `sha1=`, wrong-digest and upper-case-prefix headers all answered `401`
  (reproduced).

**Allowlist.**
- It is an exact, case-sensitive match that fails closed (`deps.py:366`); `Demo/Repo` got
  `403`.
- The gateway is bound to `self.repo` at construction (`gateway_github.py:768`), so a
  model-supplied `repo` argument cannot redirect it. Only SEC-03's traversal leaves the
  repo.

**Dry run.**
- Every POST and PUT in `gateway_github.py` is preceded by a `self.dry_run` return:
  `rerun_failed_jobs` `:494`, `create_branch` `:555`, `create_or_update_file` `:598`,
  `open_pull_request` `:650/:664`, `create_issue` `:742/:749`.
- The flag comes only from `settings.dry_run` (`deps.py:323,358`). No argument can flip it.

**Forbidden tools.**
- `merge_pull_request`, `force_push`, `delete_branch`, `delete_workflow_run`,
  `create_deployment` and `update_branch_protection` are refused twice: by the policy
  engine first (`guardrails.py:246`) and again at the gateway before any span
  (`gateway_github.py:867`).
- The default effect is `deny`.

**Issue deduplication marker.**
- `_find_marked_issue` filters by the agent's labels (`gateway_github.py:705-732`).
  Outsiders cannot label issues in a public repo, so they cannot spoof the marker.

**Scenario paths.**
- Paths are resolved and checked for containment (`deps.py:308-311`). `%2e%2e` got `400`;
  `..` and `%2F` got `404`.
- `record_fixture.py --name` is restricted (`:241`).

**SQL and deserialisation.**
- Every value is a bound parameter. The only f-strings interpolate module constants.
- List limits are bounded to 1..200.
- YAML is read only with `safe_load`, and there is no pickle or `eval` anywhere.

**Secrets at rest and in transit.**
- `.env` is gitignored and dockerignored, and has never been committed in any ref.
- The Dockerfile copies only `pyproject.toml`, `uv.lock`, `src` and `fixtures`.
- A scan of every commit for `ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `AIza`, `AQ.`,
  `hf_`, `xox` and PEM shapes found only fake test tokens (`ghp_FIXTURELEAKS…`,
  `ghp_RECORDEDLEAK…`, alphabet runs, a stub PEM). The coordinator re-ran this before the
  2026-09-30 GitHub push.
- The real `.env` values (raw, base64, URL-encoded, last 16 characters) appear 0 times in
  git history, `data/*.db`, `eval_report.json`, `docs/`, `fixtures/`, `src/`, `scripts/`
  or `tests/`.

**Credentials in use.**
- The Gemini key travels only in the `x-goog-api-key` header, never in a URL.
  `classify_provider_error` emits fixed strings.
- The PAT is sent only as `Authorization`, and log blobs are fetched without it.
- Transport errors are reduced to class names.

**Settings.**
- All four secrets are `SecretStr` and registered by type.
- The `ValidationError` barrier withholds values (`settings.py:129-186`).

**Served content.**
- `/v1/runs/{id}` replaces log excerpts and patches with a digest, then scrubs.
- Escalations, approvals and problem bodies are scrubbed.
- Spans are scrubbed at write time, and memory JSON columns at rest.
- The log record factory scrubs `msg` and `args`.

**Trace view.**
- Autoescape is on (`trace_view.py:55`), and there is no `|safe`, `Markup` or markdown.
- The only `href` is the server-built `trace_url`.
- A hostile payload in every field rendered escaped: 47 escaped instances, 0 raw.

**Container.**
- It runs as non-root UID 1000.
- There is no `COPY . .`.

**Seed script.**
- It sets `set -euo pipefail`, quotes its expansions and heredocs, never puts a token in a
  remote URL, and never force-pushes `main`.

**Other scripts.**
- The token goes only to `github_api_base`, after the allowlist check.
- `--post-signed` sends an HMAC, never the secret.

## 7. Dependencies

The locked direct dependencies are:
- pydantic 2.13.5
- pydantic-settings 2.15.0
- fastapi 0.141.1 (starlette 1.6.0)
- uvicorn 0.52.4
- httpx 0.28.1
- aiosqlite 0.22.1
- google-genai 2.22.0
- jinja2 3.1.6
- pyyaml 6.0.3

`uvx pip-audit` found **no known vulnerabilities** in any of these:
- the hashed runtime export of `uv.lock`
- the dev group
- `gradio==6.26.0` (the Space's `sdk_version`, which is not in the lock)

The Space's own unpinned resolution cannot be audited (SEC-22).

## 8. Already known and re-confirmed

These are recorded in `docs/progress/phase-5/backlog.md` or PLAN.md and were not re-rated:
- `/v1/replay` is public and unauthenticated (PLAN's choice).
- A placeholder webhook secret is honoured.
- The retry cap is check-then-act.
- The heuristic redaction tier does not look through base64.
- `exc_info` tracebacks are not scrubbed. Note that `raise … from exc` at `llm.py:628`
  keeps the raw SDK error as `__cause__`.
- A write without an `idempotency_key` runs uncached.
- `_says_exists` is a phrase match.
- An approved plan that was altered at rest is refused with `invalid_args`.
- `create_issue` can file a duplicate after a label is removed.
- `seed_demo_repo.sh` has never run against GitHub.

## 9. Remediation order

| When | Items | Where the code lives |
|---|---|---|
| **Now: reachable on today's Space** | SEC-01, SEC-08 | `src/api/` |
| **Before Step 5 exposes the Space in live mode** (plan Stage 1e) | SEC-02, SEC-04, SEC-06 (one operator-token change), SEC-03, SEC-05, SEC-24 (URL and plan validation), SEC-09 | `src/api/`, `src/integrations/` |
| **The user's call, recommended before Step 5** | SEC-07 (queue cap), SEC-19 (secret entry), SEC-18 | `src/api/`, scripts, compose |
| **Before anyone sets `dry_run=false`** | SEC-10, SEC-11; a PAT with no Workflows or Administration permission | `src/integrations/`, `policy.yaml` |
| **Backlog** | SEC-12 (touches `src/harness/`), SEC-13 – SEC-17, SEC-20 – SEC-23, SEC-25 | various |

The "one independent audit per phase" rule applies here too. The Stage 1e diff is a
security fix round, and it is where an independent `phase-reviewer` pass has earned its
cost every time on this project. Spend one, narrowly briefed, before Stage 5.

## 10. Limits of this assessment

- **Nothing was tested against the live Space, Gemini or GitHub.** GitHub's behaviour on a
  traversed path (SEC-03) is inferred from the URL the client sends, not observed.
- **The Hugging Face proxy's body-size and timeout limits are unknown.** They may soften
  SEC-01 and SEC-08, but they are not something to rely on.
- **The model's susceptibility to the injections is not measured.** SEC-05, SEC-10, SEC-11
  and SEC-14 describe what the code *permits* a model to do, not what Gemini did.
- **The second slice's auditor report was truncated after its first finding.** The
  coordinator re-derived that slice by reading the code: the dry-run map, repo binding,
  forbidden set, policy, branch and path handling, and the issue marker. Its remaining
  points may not have been re-derived in the auditor's exact wording.
