---
name: phase-verify
description: Run the literal Verify block for a phase of PLAN.md and report actual output against the literal expected values. Use to check whether a phase is really done, or to re-check after a fix, without rebuilding anything.
---

# Verify a phase

Usage: `/phase-verify <0-6>`. Read-only with respect to source; it runs commands and reports.

Delegate to the `test-verifier` agent, or run it inline for a quick single check.

## Method

Open `PLAN.md`, find that phase's **Verify** block, and run each command **as written**.
The plan prints literal expected values — a jq object, a sqlite3 row, a test name, an HTTP
status. Compare against those, not against a general impression that things look fine.

For each command produce one row:

```
COMMAND    <exactly what was run>
EXPECTED   <the literal value from PLAN.md>
ACTUAL     <real output, pasted, not summarised>
MATCH      yes | no
```

## Rules that keep this honest

- **A skipped check is a failed check.** No Fly deploy yet, no live token, Docker not
  running — all of those make the phase **BLOCKED**, not passed.
- Never edit source to make a check pass. That is a different job, owned by the agent whose
  territory the failure is in.
- Never soften an expectation. If the plan says `conf >= 0.75` and you got 0.71, that is a
  no, and it is interesting — record the actual number, because confidence calibration is an
  open risk in the plan and the data matters.
- Paste failures in full. A truncated traceback wastes the next dispatch.

## Cross-phase checks worth re-running every time

These are cheap and catch parallel-agent drift:

```bash
uv run pytest tests/test_layering.py -q          # the layer seam
uv run mypy --strict src/harness                 # harness contract discipline
uv run pytest tests/unit/test_no_env_access.py -q
uv run python scripts/scrub_fixtures.py --check  # once fixtures exist
```

From Phase 5 on, also:

```bash
uv run pytest tests/test_no_secret_leak.py -q
```

## Verdict

Exactly one of:

- **PASS** — every command matched, nothing skipped.
- **FAIL** — at least one mismatch. Name the owning agent per failure:
  `src/harness/**` → harness-core, `src/integrations/**` → cicd-integration,
  `src/api/**`/config → api-surface, `fixtures/**`/`scripts/**` → fixtures-eval,
  `tests/**` → test-verifier.
- **BLOCKED** — a check could not run. Say what is missing and what would unblock it.

Write the result to `docs/progress/phase-<N>/verify.md`.
