# Fixture format

This directory is the deterministic substrate for every CI/CD demo scenario: everything a
reviewer or the eval harness sees replays through these files, so nothing depends on a real
GitHub call succeeding. It is also the eval set — `scenario.yaml` carries the ground-truth
label that `scripts/eval.py` scores the Diagnostician against (introduced in a later phase;
not present yet).

This file is the format spec. It is intentionally more detailed than any single scenario
needs, so a future agent adding scenario N+1 does not have to re-derive conventions from
reading `gateway_replay.py`.

## Layout

```
fixtures/scenarios/<name>/
  scenario.yaml       metadata + the EXPECTED diagnosis (the eval label)
  webhook.json         a real-shaped GitHub workflow_run webhook payload
  api/                 recorded tool-call responses, one file per call
  logs/job_<id>.txt     the raw job log — ANSI codes and Actions timestamp prefixes included
```

Every scenario uses this exact shape. No exceptions, no scenario-specific extra top-level
files.

## `scenario.yaml` — field by field

```yaml
name: <name>                    # must equal the directory name
description: >                  # one line (or short block), what a human triaging this
  would conclude — this is the intuition the eval label formalizes
expected:
  category: flaky_test | real_regression | dependency_break | infra_transient
           | config_issue | unknown
  min_confidence: 0.75          # scripts/eval.py asserts final_confidence >= this
  commit: <sha> | null          # expected Diagnosis.suspected_commit_sha, or null if the
                                 # scenario genuinely has no single suspect commit
                                 # (e.g. infra_timeout's empty diff)
  cites_any_of:                 # list of substrings; eval passes if >=1 Citation.quote
    - "..."                     # or Citation.note contains ANY of these (OR, not AND)
  action: retry | open_fix_pr | open_revert_pr | file_ticket | escalate
                                 # expected Diagnosis.suggested_action /
                                 # RemediationPlan.action, depending on which phase is
                                 # being scored
  effect: allow | require_approval | deny
                                 # expected PolicyDecision.effect for the matching guardrail
                                 # rule
  baseline_kind: branch_green | default_green | head_commit_only | none
                                 # expected DiffSummary.baseline_kind
  cold_start: true | false       # expected FailureBundle.cold_start
```

**Only assert what the scenario genuinely determines.** `scripts/eval.py` is a CI gate that
exits 1 on any miss (per PLAN.md Phase 3+); an over-specified label — e.g. asserting a
`commit` when the true signal is "no single commit is blameable" — produces a flaky gate.
When a field does not apply (there is no meaningful expected commit, no meaningful
`cites_any_of`, etc.), omit it rather than guessing a value to fill the slot. A missing key
means "not scored," not "must be null" — use `commit: null` only when null is itself the
correct expectation (asserting the harness must NOT name a commit), and omit the key
entirely when the scenario has no opinion.

## `webhook.json`

A real-shaped GitHub `workflow_run` webhook delivery body: top-level `action` (always
`"completed"` for these fixtures), `workflow_run` (with `id`, `run_attempt`, `head_sha`,
`head_branch`, `workflow_id`, `conclusion: "failure"`, `head_commit`, nested `repository`),
sibling top-level `workflow`, `repository`, and `sender` objects. `repository.full_name` is
what the webhook route checks against `HARNESS_ALLOWED_REPOS`; `workflow_run.id` +
`workflow_run.run_attempt` + `repository.full_name` form the idempotency key (PLAN.md
A.11 / line ~1618).

The four IDs that everything else keys off:

| id | lives in `webhook.json` at | must also appear in |
|---|---|---|
| `repository.full_name` | `repository.full_name`, `workflow_run.repository.full_name` | every `api/` filename that encodes a repo path |
| `workflow_run.id` (run_id) | `workflow_run.id` | `api/` jobs-list filename |
| `workflow_run.run_attempt` | `workflow_run.run_attempt` | `api/` jobs-list filename |
| `workflow_run.head_sha` | `workflow_run.head_sha`, `workflow_run.head_commit.id` | `scenario.yaml: expected.commit`, the compare-commits `api/` filename, `logs/job_<id>.txt` output showing that sha |
| job id (chosen by the fixture author, not present in the webhook — it's discovered by calling `list_workflow_run_jobs`) | — | the jobs-list response body, the `get_job_logs` filename, `logs/job_<id>.txt` |

## `api/` — recorded tool responses

One file per distinct gateway call, named:

```
api/METHOD_<url-path-slug>.json
```

`METHOD` is the HTTP verb GitHub's REST API uses for that call (almost always `GET` for the
read tools these fixtures exercise). `<url-path-slug>` is the request path with the leading
slash dropped and every remaining `/` replaced with `-`; a compare range's `...` is written
as a single `-` (so `compare/BASE...HEAD` becomes `compare-BASE-HEAD`) because `...` is
noisy in a filename and the two shas already make the pair unique. Query strings are not
encoded into the filename — if a scenario needs two recordings of the *same path* with
different query parameters, disambiguate with a short suffix (e.g. `-page2`) and note it in
the scenario's own comments; none of the four canned scenarios need this.

The **body of the file is the raw GitHub API response**, exactly as GitHub would return it
(status-appropriate JSON), not a pre-transformed `FailureBundle`/`DiffSummary`/`JobRef`. That
transformation is the Investigator's job at replay time; recording the untransformed
response is what makes `record_fixture.py` a mechanical capture step rather than a rewrite.

Worked examples, keyed off the CI/CD tool catalog (PLAN.md lines 1170–1186), using
`real_regression`'s ids:

| Tool | GitHub call | `api/` filename |
|---|---|---|
| `list_workflow_run_jobs` | `GET /repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs` | `GET_repos-octo-org-harness-demo-repo-actions-runs-501234567-attempts-1-jobs.json` |
| `find_last_successful_run` | `GET /repos/{repo}/actions/workflows/{workflow_id}/runs?branch=...&status=success` | `GET_repos-octo-org-harness-demo-repo-actions-workflows-9001-runs.json` |
| `compare_commits` | `GET /repos/{repo}/compare/{base}...{head}` | `GET_repos-octo-org-harness-demo-repo-compare-<base_sha>-<head_sha>.json` |
| `get_commit` | `GET /repos/{repo}/commits/{sha}` | `GET_repos-<repo-slug>-commits-<sha>.json` (needed only for `baseline_kind: head_commit_only` scenarios) |
| `get_file_contents` | `GET /repos/{repo}/contents/{path}?ref={sha}` | `GET_repos-<repo-slug>-contents-<path-with-dashes>.json` (only if the Investigator's dynamic `additional_tool_calls` request it) |
| `search_workflow_runs` | `GET /repos/{repo}/actions/workflows/{workflow_id}/runs?...` | same shape as `find_last_successful_run`; disambiguate by query if a scenario needs both |
| `get_job_logs` | `GET /repos/{repo}/actions/jobs/{job_id}/logs` | **no `api/` file** — see below |

**Design decision — `get_job_logs` has no `api/` recording.** GitHub's real endpoint 302s to
a blob of plain text, not JSON, and the raw bytes already live at `logs/job_<id>.txt` per the
rule below. Duplicating a multi-thousand-line log into both `api/` (as an escaped JSON
string) and `logs/` invites the two copies to silently diverge — exactly the kind of
inconsistency this format is trying to rule out. `ReplayToolGateway.get_job_logs(job_id,
max_bytes)` is expected to read `logs/job_<id>.txt` directly, truncated to `max_bytes` if
given, and treat a missing file as the tool-error case (there is no such job log recorded),
the same way a missing `api/` file should surface as a clear replay error rather than a
silent empty result.

**Truncation direction — `max_bytes` keeps the LAST `max_bytes`, never the first.** If a
log exceeds `max_bytes`, the implementation must drop bytes off the *front*, not the back
(i.e. `content[-max_bytes:]`, not `content[:max_bytes]` / `f.read(max_bytes)`). This mirrors
PLAN.md:222's "Log download hard cap — 20 MB (keep the *last* 20 MB)": the proximate failure
is near the end of a real Actions log (setup/install noise comes first, the failing step and
its traceback come last), so keeping the head and discarding the tail throws away the one
thing every scenario exists to test. Concretely: `real_regression`'s log is grown to several
thousand lines with the `assert 91 == 90` anchor placed well before the end (per the realism
requirements below) specifically so that a `get_job_logs` implementation which truncates from
the wrong end fails this fixture loudly — `f.read(max_bytes)` on that log would return only
runner bootstrap and pip-install output, the anchor would be gone, and the Diagnostician would
have to guess `category: unknown` instead of citing real evidence. Get this backwards and the
eval failure looks like a model/prompt problem when it is actually a one-line truncation bug.

Write-tool calls (`rerun_failed_jobs`, `create_branch`, `create_or_update_file`,
`open_pull_request`, `create_issue`, `merge_pull_request`) are never pre-recorded here —
`ReplayToolGateway` synthesizes a plausible success response for those at replay time (they
have no prior state to reproduce), except where a scenario is deliberately exercising a
fault-injected failure (`HARNESS_FAULT_INJECT`, wired up starting in a later phase).

## `logs/job_<id>.txt`

The raw artifact, byte for byte what `GET .../actions/jobs/{job_id}/logs` would return:
GitHub Actions timestamp prefixes (`2026-09-05T14:03:59.1234567Z `) and ANSI color/group
codes (`\x1b[31m`, `##[group]`/`##[endgroup]`) included, not stripped. Stripping and
trimming are the Context Manager's job at replay time (PLAN.md Phase 1, `assemble()`);
a fixture that pre-cleans its log defeats the thing it's supposed to test.

Realism requirements (see `.claude/skills/fixture-new/SKILL.md` for the fuller rationale):
- A few thousand lines for scenarios meant to exercise trimming, with the true anchor buried
  well before the end, not the last line.
- Ordinary setup noise: runner bootstrap, checkout, dependency install, cache
  restore/save, matrix env setup.
- Cascading secondary errors after the true one — a scenario where the last red line is also
  the root cause tests nothing about root-cause attribution.
- The filename's `<id>` is the job id used everywhere else for this scenario (the jobs-list
  `api/` response, and — for consistency, though it is not itself parsed from the log —
  matches what a human would see in the Actions UI URL for that job).

## Consistency requirements (checklist for a new or edited scenario)

- [ ] `webhook.json`'s `repository.full_name`, `workflow_run.id`, `workflow_run.run_attempt`,
      `workflow_run.head_sha` match every `api/` filename and every occurrence in `logs/`.
- [ ] Every gateway call the Investigator's deterministic collector makes for this scenario's
      `baseline_kind` has a corresponding `api/` file (a missing one is a fixture bug, not a
      "cold start" — cold start is expressed by an *empty list* in the
      `find_last_successful_run`/`search_workflow_runs` response body, never by deleting the
      file — see PLAN.md Appendix D).
- [ ] `scenario.yaml: name` equals the directory name.
- [ ] `scenario.yaml: expected.commit` (when set) equals `webhook.json:
      workflow_run.head_sha` and the sha appears in the `compare_commits` recording's
      `commits[].sha`.
- [ ] No secrets: run `uv run python scripts/scrub_fixtures.py --check` before committing
      (mandatory, not optional, if the fixture was captured with `record_fixture.py` from a
      real repo — see PLAN.md lines ~1820).

## The six scenarios

| Scenario | Shape | What it proves | Status |
|---|---|---|---|
| `real_regression` | off-by-one in `discount()`, `assert 91 == 90`, diff contains exactly that one-line change | correct blame + `require_approval` | **complete** (Phase 1) |
| `flaky_test` | wall-clock deadline test slips on a shared runner; diff touches only formatting code | the retry rule -- denied by the fail-closed cap in Phase 2, allowed once memory is real (Phase 3) | **complete** (Phase 2) |
| `dependency_break` | `requirements.txt` bumps pydantic 1.10.13 -> 2.9.2, import-time failure | dependency_bump claim checking | not yet built |
| `infra_timeout` | pypi.org read timeout during install, cascading collection errors, **empty diff** (an empty re-trigger commit) | the empty-diff contradiction penalty; the retry rule for `infra_transient` | **complete** (Phase 2) |
| `cold_start` | copy of `flaky_test` with `find_last_successful_run` returning an empty list | `baseline_kind: none`, auto-retry disabled | not yet built |
| `hallucination` | fixture whose log deliberately lacks the line the Diagnostician is steered to cite | the Evaluator refuting a fabricated citation | not yet built |

`infra_timeout`'s empty diff is deliberate and must never be "fixed" by adding files — it is
the only scenario that exercises `empty_diff_contradiction`.

The `flaky_test` and `infra_timeout` logs were synthesised by `scripts/gen_fixture_log.py`,
deterministically: re-running it for a scenario reproduces the committed file byte for byte,
so a regenerated log never silently changes a fixture. The generator is the cheap way to
give the next scenario the few thousand lines of realistic noise this format asks for.
