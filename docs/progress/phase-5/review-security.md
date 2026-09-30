# Stage 1e security fix round — independent audit

**Auditor:** one `phase-reviewer` dispatch, read-only, 2026-10-01. It was briefed to answer
only whether each fix closes its reproduction and what the fix broke.
**Scope:** `4e6261c..5602f47` ("Security gate for live exposure").
**Against:** the security assessment of `06c87db` (SEC-01 to SEC-09, SEC-18, SEC-19,
SEC-24) and the step-5 plan's Stage 1e, items 1-7.
**Verdict:** FIX FIRST, with four findings (1 medium, 3 low). The resolution follows the report.

## Report

The auditor's per-item lines, as returned:

| Item | Status | Evidence |
|---|---|---|
| SEC-01 | CLOSED | `deps.py:95-99`. The PEM body scan is capped at 16,384 characters and stops at the next `-----BEGIN `. Five 1 MiB adversarial shapes take 0.01-0.10 s, a real PEM block is still fully redacted, and the 120 KB `422` test passes. |
| SEC-02 | CLOSED | `main.py:1128`. `require_operator` guards `POST /v1/runs` in both modes when gateway=github. The subject's head_sha reaches only `get_commit` and `compare_commits`, both behind `url_segments.sha`. |
| SEC-03 | CLOSED | Every URL-path value in `gateway_github.py` is validated by url_segments, coerced with `int()`, or taken from GitHub's own response. One edge case remains in the sha check (finding 3). |
| SEC-04 | PARTIAL | See below. |
| SEC-05 | CLOSED | See below. |
| SEC-06 | PARTIAL | A squat on `/v1/runs` in a live deployment gets `401`. The replay-mode reproduction still works, as the plan intends. Finding 1 reopens the same harm on live through a new route. |
| SEC-07 | PARTIAL | The queue flood is closed. Admission is released exactly once on every exit traced. The anonymous quota drain remains, by the plan's "no daily guard" decision. See finding 1. |
| SEC-08 | CLOSED | See below. |
| SEC-09 | CLOSED | 2 MiB logs in ten adversarial shapes fingerprint in 0.23 s or less, and the fingerprint runs in `asyncio.to_thread`. |
| SEC-18 | CLOSED | Compose publishes `127.0.0.1:8000:8000`. |
| SEC-19 | CLOSED | See below. |
| SEC-24 | PARTIAL | The sha and path are now checked, but the other file-name parts are not (finding 2). |

**SEC-04 (PARTIAL).**
- The live tier is closed. The token is required, `decided_by` is `"operator"`, and
  `live_allowed` is checked before the single-use transition, with `gateway_for`
  re-checking.
- The replay-tier (A) reproduction still works, because the plan leaves replay unchanged.
- A stored context that fails to parse now answers 500 before the transition. The approval
  stays pending, and a reject also answers 500.
- `gateway_for`'s `PermissionError` cannot fire from `_execute`, because both routes check the
  same cached settings first. If it did fire, `_supervised` would mark the run failed.

**SEC-05 (CLOSED).**
- Branch, base and paths are sanitised on both the derived path and the model-call path. The
  gateway refuses any branch outside `agent/fix/`.
- One route to `execute_plan` skips normalisation: an approval stored before `5602f47`.
  `_execute_approved` re-decides the plan but does not re-normalise it, and the gateway does
  not refuse `.github/` itself. No such live approval exists.

**SEC-08 (CLOSED).**
- `BodySizeLimit` is in the FastAPI app's own middleware stack, so it also runs when `app.py`
  mounts the app. The auditor checked this with an outer app plus `Mount`.
- A 2 MiB chunked body gets a `413` problem document on `/webhooks/github` and
  `/v1/approvals/{id}`. A `Content-Length` over 1 MiB gets `413` on `/v1/runs`.
- A lying low `Content-Length` is framed by h11.

**SEC-19 (CLOSED).**
- The body goes through the `printf` builtin into `gh api --input -`.
- The auditor ran the script's own `json_string` on `"`, `\`, a trailing `\`, `\"`, `%s%n`
  and non-ASCII input. Every output is valid JSON with a single key.
- Control characters are refused.

### Findings

**1. [medium] Signed deliveries share the admission pool with anonymous replays.**
`src/api/main.py:1315`, with the pool defined at `deps.py:216`.
- **Defect.** Signed webhook deliveries share the admission pool with anonymous
  `/v1/replay`. On a live deployment, anyone can make a real delivery answer `429`, and GitHub
  does not redeliver on its own. That is SEC-06's harm by a new route.
- **Reproduction.** Twelve anonymous `POST /v1/replay/flaky_test?sync=false` requests, then
  the signed delivery, which answers `429 "Too many runs"`.
- **Suggested fix.** Exempt HMAC-verified deliveries from admission, or give them reserved
  capacity.

**2. [low] The replay gateway's other file-name parts can still traverse.**
`src/integrations/cicd/gateway_replay.py:75,89,96-98`.
- **Defect.** The SEC-24 file-name traversal is still open through repo, branch, run_id,
  attempt and workflow_id. Only `/` is replaced, so `\` survives. This works on Windows only.
- **Reproduction.** Both calls return `ok=True` with the other scenario's data on this host:
  - `find_last_successful_run{workflow_id: 9001, branch: "x\..\..\..\real_regression\webhook"}`
    returns another scenario's `webhook.json`.
  - A string `run_id` of the same shape on `list_workflow_run_jobs` returns another
    scenario's jobs.
- **Suggested fix.**
  - Coerce the numeric ids with `int()`.
  - Pass the branch through `ref_name`.
  - Refuse `\` in repo and branch.

**3. [low] `_SHA` accepts a trailing newline.**
`src/integrations/cicd/url_segments.py:23`.
- **Defect.** `_SHA` ends in `$`, which also matches before a trailing newline. The live gateway
  then raises `httpx.InvalidURL` out of `invoke` instead of answering `invalid_args`. Nothing
  leaves the repository, and 0 requests are sent.
- **Reproduction.** `get_commit{sha: "abcdef1\n"}`.
- **Suggested fix.** Use `fullmatch`.

**4. [low] A non-string path becomes the literal path `"None"`.**
`src/integrations/cicd/remediation.py:310-312`.
- **Defect.** A model's `create_or_update_file` call with no `path` (the `pr_draft=None` path)
  gets `path "None"`, because `file_path(None)` validates `str(None)`. A non-string path
  does the same. Before `5602f47`, a missing path was a `KeyError`, then `invalid_args`.
- **Suggested fix.** Refuse non-str values.

## Resolution (2026-10-01; coordinator-verified, not re-audited)

All four findings are fixed in the commit after `5602f47`. Each has a test that fails on
`5602f47` with the reproduction above and passes after the fix. Under the standing
one-audit-per-phase rule, the round was not re-audited.

1. **Fixed.** A verified delivery is admitted with `RunAdmission.admit_trusted`: counted, so
   anonymous callers see it in the total, but never refused.
   - Test: `test_security_gate.py::test_a_signed_delivery_is_admitted_when_anonymous_runs_fill_the_pool`.
   - On `5602f47` the delivery answered `429 "Too many runs"`.
2. **Fixed.** `fixture_slug_for` now holds every part of the file name to the gateway rules:
   `repo_name`, `int()` ids, `ref_name` for the branch, `sha` and `file_path`. Because it
   raises `ValueError`, `record_fixture.py` gets the same rule.
   - Test: `test_url_segments.py::test_the_replay_gateway_refuses_every_file_name_part_that_walks_the_tree`.
   - On `5602f47` the branch reproduction returned `ok=True`.
   - A guard test confirms the scenario's own recordings still resolve.
3. **Fixed.** Every `url_segments` pattern is matched with `fullmatch`.
   - Test: `test_url_segments.py::test_a_sha_with_a_trailing_newline_is_invalid_args_not_a_crash`.
   - On `5602f47` it raised `httpx.InvalidURL`.
4. **Fixed.** `url_segments` refuses any value that is not a `str`, so `refused_path` drops the call.
   - Test: `test_plan_branch_and_paths.py::test_a_file_write_without_a_string_path_is_dropped_not_written_as_none`.
   - On `5602f47` the call was kept with path `"None"`.

**Accepted as the plan decided, and not changed:**
- **The replay tier of SEC-04, SEC-06 and SEC-07.** `/v1/replay`, and `/v1/runs` in replay
  mode, stay public by PLAN's choice. The plan's "no daily guard" decision stands. Gating
  `/v1/replay` with the operator token on a live deployment is an option for the user; it
  would also disable the Space UI's replay button while live.
- **Approvals stored before `5602f47` are not re-normalised** at execution. None exists on a
  live deployment. The Space's database is wiped on every restart, and switching to live mode
  restarts it.
- **A malformed stored context answers `500` before the single-use transition.** The approval
  stays pending, where before it was used up on the way to the same `500`.
- **Anchor changes on unrealistic lines only.** The auditor found them only for a type
  preceded by a digit or a dot, or a tab-separated section header. The five scenarios'
  fingerprints are pinned.
