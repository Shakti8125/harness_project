# Phase 0 — Verify (re-run after Wave-3 audit fix round)

Environment: `uv 0.12.10`, venv Python `3.12.14`. `export PATH="/c/Users/Shakti/.local/bin:$PATH"`
run before every `uv` invocation (not on inherited PATH in this shell). All commands below were
run from `D:\Documents\harness_project`, against the code as landed by all four builder agents'
Wave-3 fixes plus this agent's own fixes to findings 12 and 13.

This supersedes the first `verify.md`. PLAN.md has been amended by the phase runner:
line 290 now reads `uv run pytest -q  # expect: test_layering passes (count grows as later
phases add tests)` — no literal count pinned. The `1 passed` vs `83 passed` discrepancy
flagged in the previous run is resolved by that amendment, not by this agent softening
anything.

## Literal Verify block (PLAN.md Phase 0)

```
COMMAND    uv sync
EXPECTED   (no literal value given; "syncs deps")
ACTUAL     Resolved 58 packages in 0.80ms
           Checked 56 packages in 1ms
MATCH      yes
```

```
COMMAND    uv run ruff check .
EXPECTED   (no literal value given; "clean")
ACTUAL     All checks passed!
MATCH      yes
```

```
COMMAND    uv run mypy src/harness
EXPECTED   mypy clean
ACTUAL     Success: no issues found in 13 source files
MATCH      yes — the previous run's informational note
           ("unused section(s): module = ['harness.*']") is gone; api-surface dropped the
           dead pyproject.toml override as reported in the Wave-3 fix round. Confirmed by
           reading pyproject.toml: only the effective `src.harness.*` override remains.
```

```
COMMAND    uv run pytest -q
EXPECTED   test_layering passes (PLAN.md line 290, post-amendment: no literal count pinned)
ACTUAL     ........................................................................ [ 72%]
           ...........................                                              [100%]
           99 passed in 0.46s
MATCH      yes. 99 = 66 (tests/test_layering.py, up from 53 — see fix for finding 13 below)
           + 33 (tests/unit/test_no_env_access.py, up from 30 — see fix for finding 12
           below). The previous run's "no, superseded" verdict no longer applies: PLAN.md's
           Verify block itself no longer pins a count, so there is nothing to mismatch.
```

```
COMMAND    docker compose up -d --build
EXPECTED   container builds and starts
ACTUAL     ... (full build log; final lines)
            Image harness:local Built
            Container harness_project-app-1 Recreate
            Container harness_project-app-1 Recreated
            Container harness_project-app-1 Starting
            Container harness_project-app-1 Started
MATCH      yes. Also confirms Wave-3 finding 9's fix: build log shows a new
           `[runtime 6/7] COPY fixtures ./fixtures` layer, and
           `docker exec harness_project-app-1 ls /app/fixtures/scenarios/real_regression`
           lists `api/ logs/ scenario.yaml webhook.json` inside the running container.
```

```
COMMAND    curl.exe -s localhost:8000/healthz
EXPECTED   {"status":"ok","db":"ok","version":"0.1.0"}
ACTUAL     {"status":"ok","db":"ok","version":"0.1.0"}
MATCH      yes — byte-for-byte identical
```

## Cross-phase checks (phase-verify skill)

```
COMMAND    uv run pytest tests/test_layering.py -q
EXPECTED   passes ("the layer seam")
ACTUAL     ..................................................................       [100%]
           66 passed in 0.30s
MATCH      yes. 66 = 13 modules x 5 checks (was 4; this agent added
           test_no_dynamic_import_of_integrations to close finding 13) + 1 collection guard.
           Verified live against an injected violation in a scratch tree (not committed,
           not part of the suite): appending
           `importlib.import_module("src.integrations.cicd.gateway_replay")` to a copy of
           `src/harness/gateway.py` and re-running `pytest -k dynamic` produced exactly one
           new targeted failure naming the file and the resolved target string
           ('src.integrations.cicd.gateway_replay'), confirming the new check is not
           vacuous. Scratch tree deleted after the check; nothing under this repo's
           tests/** or src/** was used to fabricate the pass.
```

```
COMMAND    uv run mypy --strict src/harness
EXPECTED   clean ("harness contract discipline")
ACTUAL     Success: no issues found in 13 source files
MATCH      yes (no informational note this time — see above)
```

```
COMMAND    uv run pytest tests/unit/test_no_env_access.py -q
EXPECTED   passes
ACTUAL     .................................                                        [100%]
           33 passed in 0.30s
MATCH      yes. 33 = 28 source files outside tests/ (unchanged set: 15 under src/harness/+
           src/api/, 13 now also scanned under src/integrations/cicd/**, which the old
           src/-only scan already covered) + 5 non-parametrized checks (was 2: this agent
           added test_scripts_dir_absence_does_not_break_the_scan,
           test_tests_dir_itself_is_excluded_from_the_scan, and
           test_harness_source_files_were_actually_collected to close finding 12 and make
           the widened scope's boundaries explicit and tested). The scan root changed from
           `src/` to `REPO_ROOT` (excluding `tests/`, `.venv`, and cache/VCS dirs), so it
           now automatically covers a future `scripts/` the moment Phase 1 creates it — see
           "Gaps and environment notes" below for exactly what is and isn't covered.
```

```
COMMAND    uv run python scripts/scrub_fixtures.py --check
EXPECTED   runs "once fixtures exist"
ACTUAL     can't open file 'D:\Documents\harness_project\scripts\scrub_fixtures.py':
           [Errno 2] No such file or directory (exit=2)
MATCH      not applicable this phase — unchanged from the previous run. scripts/ still does
           not exist; fixtures-eval's handoff still defers scripts/scrub_fixtures.py to a
           later phase. Not a regression, not a new finding.
```

## Fixes verified live (not just by reading diffs)

- **Finding 1 (Adjustment duplication), cicd-integration:** `src/integrations/cicd/schemas.py`
  now has `from src.harness.confidence import Adjustment` (no local redeclaration);
  `"Adjustment"` remains in `__all__`. Confirmed by grep — zero occurrences of a second
  `class Adjustment` definition anywhere in the tree.
- **Finding 2 (Diagnosis field order), cicd-integration + PLAN.md amendment:** `Diagnosis`'s
  first field is now `reasoning: str = Field(max_length=1200)`, with an inline comment
  "ordered first, via propertyOrdering, so the model reasons before concluding". PLAN.md's
  Appendix A.11 was amended to match per the phase runner's summary.
- **Finding 3 (fail-open calibrate), harness-core:** `src/harness/confidence.py` now defines
  `UNREGISTERED_SIGNAL_DELTA: Final[float] = 0.0` and
  `UNREGISTERED_SIGNAL_REASON: Final[str] = "no delta registered for this signal"`, and the
  docstring's contract step 3 requires an unregistered signal to still emit a loud
  `Adjustment` rather than silently contributing nothing. `calibrate()`'s body remains
  `raise NotImplementedError` — correct for a Phase 0 contract freeze; this is a docstring/
  signature fix, not an implementation, and Phase 1's implementer is the one who must
  satisfy it (flagged for the Phase 1 gate to actually exercise, since it cannot be tested
  against a `NotImplementedError` body today).
- **Finding 4 (clamp order), harness-core:** the same docstring now states "Sum first, then
  clamp once" explicitly, with the worked example from the audit (`0.95 +0.05 -0.15` sums to
  `0.85` and clamps to `0.85`, not `0.84`) copied in verbatim.
- **Finding 5/6 (secret leak on typo, unrecognised env var), api-surface:** `src/settings.py`
  now has `_no_unrecognised_harness_env_vars` (a `model_validator(mode="after")`) that scans
  `os.environ` for `HARNESS_`-prefixed keys not in `model_fields`, and `get_settings()`
  catches `ValidationError` and re-raises `SettingsError` with values stripped via
  `_redact_validation_error`. Verified live: `.env` currently contains 8 `HARNESS_*` keys,
  all valid field names (`GEMINI_API_KEY`, `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`,
  `DATABASE_PATH`, `GATEWAY`, `DRY_RUN`, `ENV`, `LOG_LEVEL`) — container boots clean, no
  ValidationError in `docker logs`.
- **Finding 7 (unsatisfiable eval label), fixtures-eval:** `scenario.yaml`'s
  `min_confidence` is now `0.85` with a long inline comment explaining the exact
  `policy.yaml` interaction and citing "Wave 3 audit finding 7" by name.
  `effect: require_approval` is retained (not dropped), consistent with 0.85 now clearing
  the `open-fix-pr` rule's `gte: 0.85` bound.
- **Finding 8 (truncation direction), fixtures-eval:** `fixtures/README.md` now states
  "Truncation direction — `max_bytes` keeps the LAST `max_bytes`, never the first" with the
  `content[-max_bytes:]` vs `f.read(max_bytes)` contrast spelled out.
- **Finding 9 (fixtures missing from image), api-surface:** confirmed above — new
  `COPY fixtures ./fixtures` layer in the Dockerfile, verified present inside the running
  container.
- **Finding 10 (healthz degraded/status), api-surface:** `_db_reachable` now returns
  `"degraded"` for a `disk i/o error`-shaped `OperationalError` distinctly from `"error"`
  for anything else, and `healthz()`'s `"status"` field now mirrors `db_status` rather than
  being hardcoded `"ok"`. Not exercised live this run (would require actually breaking the
  sqlite file to trigger the degraded path) — noted as a coverage gap below, same as before.
- **Finding 12 (env-access scan scoped to src/ only), this agent:** fixed — see
  `tests/unit/test_no_env_access.py` rewrite. Scan root moved from `src/` to `REPO_ROOT`,
  excluding `tests/`, `.venv`, and cache/VCS directories. `scripts/` does not exist yet;
  the moment Phase 1 creates it, it is covered automatically with zero test changes — a
  dedicated test (`test_scripts_dir_absence_does_not_break_the_scan`) pins this transition
  down explicitly rather than leaving it implicit.
- **Finding 13 (dynamic import evades layering check), this agent:** fixed — see
  `tests/test_layering.py`'s new `test_no_dynamic_import_of_integrations`, verified against
  a real injected violation in a scratch tree (see cross-phase check above). The string-
  literal denylist itself was deliberately NOT widened, per the reviewer's explicit ruling.

## Gaps and environment notes for the reviewer

- **`jq` is still not installed** on this machine. Phase 0's verify block does not use it.
  Restated (not new) as a **Phase 1 blocker**: PLAN.md's Phase 1 Verify block uses `jq`
  heavily (e.g. a cold-start check piping into `jq '{...}'`). Whoever runs the Phase 1 gate
  needs `jq` installed first or those specific commands are BLOCKED, not passed.
- `data/harness.db` exists on disk. `tests/conftest.py`'s autouse
  `_guard_real_db_untouched` fixture asserts its mtime is unchanged across the whole
  99-test run; it was.
- Fly.io: not deployed this phase, consistent with PLAN.md (deploy is explicitly Phase 1).
  Not a Phase 0 gate item; not marked BLOCKED because it isn't in scope.
- `/healthz`'s new `"degraded"` path (Finding 10's fix) has no live exercise in this gate —
  simulating a disk-full sqlite condition is out of scope for a half-day Phase 0 gate.
  Recorded as a coverage gap in `test-verifier.md`, owned by whichever phase first wires a
  real failure-injection test for it.
- **Finding 11 (uncommitted tree) is still open.** `git log --oneline` still shows exactly
  one commit (`7e8f6a5 PLAN.md: agent harness build plan`); `git status` shows only
  `PLAN.md` (this phase runner's Appendix amendment) as a tracked, modified file — every
  other file in the repo, including all of this agent's own test fixes, remains untracked.
  This is explicitly not test-verifier's territory to fix (committing is the phase runner's
  call), so it is restated here rather than silently resolved.
