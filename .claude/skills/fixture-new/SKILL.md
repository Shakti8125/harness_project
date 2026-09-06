---
name: fixture-new
description: Scaffold a new canned failure scenario under fixtures/scenarios/ in the exact replay format — scenario.yaml ground truth, webhook payload, recorded API responses, and a realistic job log. Use when adding a demo scenario or a regression case.
---

# New scenario fixture

Usage: `/fixture-new <name> [taxonomy-category]`. Delegate to the `fixtures-eval` agent,
which owns `fixtures/**`.

Scenarios are the deterministic substrate of the whole demo — nothing a reviewer sees
depends on something breaking live — and they double as the eval set that scores the
Diagnostician. A sloppy fixture silently weakens both.

## Layout

```
fixtures/scenarios/<name>/
  scenario.yaml
  webhook.json
  api/METHOD_<url-path-slug>.json
  logs/job_<id>.txt
```

## scenario.yaml — this is the eval label

```yaml
name: <name>
description: one line, what a human triaging this would conclude
expected:
  category: flaky_test | real_regression | dependency_break | infra_transient | config_issue
  min_confidence: 0.75
  commit: <sha or null>
  cites_any_of: ["substring that must appear in some citation"]
  action: retry | open_fix_pr | open_revert_pr | file_ticket | escalate
  effect: allow | require_approval | deny
  baseline_kind: branch_green | default_green | head_commit_only | none
  cold_start: false
```

`scripts/eval.py` scores against this and exits 1 on any miss, so only assert what the
scenario genuinely determines. An over-specified label produces a flaky CI gate, which is a
particularly embarrassing failure mode for a project about flaky tests.

## Making the log realistic

The most common mistake is a short, clean log. It makes the Context Manager untested and the
Diagnostician's job artificially easy.

- A few thousand lines, with the real anchor buried in the middle.
- Keep the Actions timestamp prefixes and ANSI colour codes — the trimmer strips them, so
  the fixture must contain them.
- Include **cascading secondary errors after the true one**. This is what distinguishes a
  system that finds the root cause from one that quotes the last red line.
- Include ordinary setup noise: dependency resolution, cache restore, matrix setup.
- Whatever the log ends up asserting, `logs/job_<id>.txt` must be the *raw* artifact; all
  trimming is the harness's job, never the fixture's.

## Consistency requirements

- `webhook.json` must be a real-shaped `workflow_run` payload and its `workflow_run.id`,
  `run_attempt`, `head_sha`, `repository.full_name` must match every file in `api/` and the
  ids in `logs/`. Mismatches surface as a confusing idempotency or baseline bug later.
- Every gateway call the Investigator makes for this scenario needs a file in `api/`. A
  missing one should surface as a clear replay error, not a silent empty result.
- To exercise cold start, give `api/` a `find_last_successful_run` response with an empty
  list rather than deleting the file.
- Run `uv run python scripts/scrub_fixtures.py --check` before committing. If the fixture
  was recorded from a real repo with `record_fixture.py`, this is mandatory, not optional.

## After scaffolding

```bash
curl.exe -s -X POST "localhost:8000/v1/replay/<name>" | jq '{cat:.final.diagnosis.category, conf:.final.diagnosis.final_confidence, effect:.final.remediation.decisions[0].effect}'
uv run python scripts/eval.py --scenario <name> --runs 3
```

Three runs, not one: a scenario that only classifies correctly sometimes is worth knowing
about before it becomes a CI gate.
