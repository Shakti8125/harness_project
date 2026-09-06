## VERDICT           PASS

## Gate results

Each row: command run exactly as PLAN.md/skill prescribes → actual output → match?

| Command | Expected (literal) | Actual | Match |
|---|---|---|---|
| `uv sync` | (no literal value; must succeed) | `Resolved 58 packages in 0.80ms` / `Checked 56 packages in 1ms` | yes |
| `uv run ruff check .` | (no literal value; must be clean) | `All checks passed!` | yes |
| `uv run mypy src/harness` | mypy clean | `Success: no issues found in 13 source files` (no informational note this time — api-surface dropped the dead `harness.*` override) | yes |
| `uv run pytest -q` | PLAN.md line 290 post-amendment: `test_layering passes (count grows as later phases add tests)` — no literal count | `99 passed in 0.46s` | yes — nothing to mismatch; PLAN.md no longer pins a number |
| `docker compose up -d --build` | container builds and starts | `Image harness:local Built` / `Container harness_project-app-1 Started`, plus new `COPY fixtures ./fixtures` layer confirmed present inside the running container | yes |
| `curl.exe -s localhost:8000/healthz` | `{"status":"ok","db":"ok","version":"0.1.0"}` | `{"status":"ok","db":"ok","version":"0.1.0"}` | yes, byte-for-byte |
| `uv run pytest tests/test_layering.py -q` (cross-phase) | passes | `66 passed in 0.30s` | yes |
| `uv run mypy --strict src/harness` (cross-phase) | clean | `Success: no issues found in 13 source files` | yes |
| `uv run pytest tests/unit/test_no_env_access.py -q` (cross-phase) | passes | `33 passed in 0.30s` | yes |
| `uv run python scripts/scrub_fixtures.py --check` (cross-phase) | runs once fixtures exist | `scripts/` still does not exist — not applicable this phase, unchanged from prior run | not a failure; deferred |

Full command-by-command detail with pasted raw output lives in
`docs/progress/phase-0/verify.md`.

## Tests written / fixed this round

- **`tests/unit/test_no_env_access.py` (Finding 12 fix).** Widened the scan root from
  `src/` to `REPO_ROOT`, excluding: `tests/**` (test code legitimately manipulates env —
  see `tests/conftest.py`'s `isolated_settings`), `.venv/`, `__pycache__/`, and dot-dirs
  (`.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`), plus the one permitted exception
  `src/settings.py` itself.
  **Covers:** all of `src/**` (unchanged: `src/harness/**`, `src/api/**`, and now also
  explicitly re-verified `src/integrations/cicd/**`, which the old `src/`-scoped scan
  already reached but this walk makes structurally general rather than `src/`-specific),
  plus `scripts/**` automatically the moment Phase 1 creates it (verified: `scripts/` does
  not exist yet; a dedicated test,
  `test_scripts_dir_absence_does_not_break_the_scan`, pins that this is a deliberate,
  tested transition rather than an accident of `rglob` on a missing path).
  **Does NOT cover:** `tests/**` (deliberately excluded, stated above), non-`.py` files
  (`pyproject.toml`, `Dockerfile`, `fly.toml`, `docker-compose.yml` — a different grep
  mechanism would be needed and no Appendix E failure scenario names them), and anything
  under `.venv` (third-party code, out of repo control).
  Added three new non-parametrized tests
  (`test_scripts_dir_absence_does_not_break_the_scan`,
  `test_tests_dir_itself_is_excluded_from_the_scan`,
  `test_harness_source_files_were_actually_collected`) so the widened boundary is itself
  under test, not just documented. 33 cases total (28 files + 5 checks), up from 30.

- **`tests/test_layering.py` (Finding 13 fix).** Added `_is_dynamic_import_call`,
  `_dynamic_import_targets`, and `test_no_dynamic_import_of_integrations`: walks every
  `ast.Call` node in each `src/harness/**` module, recognises
  `importlib.import_module(...)`, a bare `import_module(...)`, and `__import__(...)`, and
  when the first positional argument is a plain string constant, asserts it does not
  resolve into `src.integrations`/`integrations`. Chose to close the hole with a dedicated
  AST check rather than record-only, per the task's "either is acceptable" — cheap (roughly
  20 lines) and it exercises the exact failure scenario the audit named
  (`importlib.import_module("src.integrations.cicd.gateway_replay")`).
  **Deliberately did not widen the string-literal denylist** — the reviewer was explicit
  that doing so would collide with four Appendix-A-verbatim identifiers (`"diff"`,
  `commit_sha`, `actions_in_window`, `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN`) and force a
  contract deviation; this check is keyed on "does this string resolve into
  src.integrations", a structurally different question from "is this one of the five
  denylisted domain words".
  **Residual limitation, stated in the module docstring:** only a plain `ast.Constant`
  first argument is recognised. A module path built at runtime via string concatenation,
  f-strings, or a variable cannot be resolved statically and is out of scope by
  construction — recorded, not silently pretended away.
  Verified against a real injected violation in a scratch tree (deleted after use, not
  part of the committed suite): copying `src/harness/gateway.py` and appending
  `importlib.import_module("src.integrations.cicd.gateway_replay")` produced exactly one
  new failing case, naming the file (`src\harness\gateway.py`) and the resolved target
  string verbatim. 66 cases total (13 modules × 5 checks + 1 guard), up from 53.

## Failures

None. All 99 tests in the suite pass against the code as landed by harness-core,
cicd-integration, api-surface, and fixtures-eval after their Wave-3 fixes, plus this
agent's own fixes to findings 12 and 13. No source was edited to make anything pass.

## Coverage gaps

- **`/healthz`'s new `"degraded"` path** (api-surface's Finding 10 fix) has no test
  exercising it — simulating a disk-full/`OperationalError` condition to actually reach
  that branch is not attempted this phase. Owner of a future test: whoever owns
  failure-injection fixtures (likely fixtures-eval or api-surface, depending on how
  Phase 1+ structures fault injection — `settings.fault_inject` exists as a field already).
- **`calibrate()`'s fail-closed contract (Finding 3's fix)** is a docstring/constant change
  only — the function body is still `raise NotImplementedError` (correct for a Phase 0
  freeze). No test can exercise `UNREGISTERED_SIGNAL_REASON` against a real implementation
  yet. This must become a named unit test the moment Phase 1 implements `calibrate()` —
  flagging it now so it isn't forgotten once the body exists.
- **`scripts/scrub_fixtures.py --check`** — still no test/gate behind it; the script itself
  still does not exist. Not a Phase 0 gap.
- **`jq` is not installed** on this machine. Not used by Phase 0's Verify block. Restated
  (still true, not new) as a **Phase 1 blocker** — Phase 1's Verify block uses `jq` heavily;
  whoever runs that gate needs it installed first or those specific commands are BLOCKED.
- **Fly.io deploy / live webhook / live GitHub token** — out of scope for Phase 0. Not
  marked BLOCKED; will be a real BLOCKED-if-missing item starting Phase 1's gate.
- **Finding 11 (uncommitted tree) remains open** — restated, not test-verifier's territory.
  `git log` still shows one commit; everything except `PLAN.md`'s amendment is untracked.

## Notes for the reviewer

- **PLAN.md line 290's amendment resolves the prior "1 passed" vs "83 passed" discrepancy
  cleanly.** It now reads "expect: test_layering passes (count grows as later phases add
  tests)" with no literal number. The count is now 99 (up from 83, because this agent's
  own two fixes each added parametrized cases: +13 in `test_layering.py` for the new
  dynamic-import check across 13 modules, +3 non-parametrized cases in
  `test_no_env_access.py`). Nothing was softened to hit a number; the number simply isn't
  pinned anymore, by the phase runner's own amendment.
- **On finding 13, I chose "close it" over "record it as a known limitation."** The task
  brief said either was acceptable. A ~20-line AST check that catches the exact named
  failure scenario, verified live against an injected violation, is cheap enough that
  leaving it as a comment-only limitation would have been the weaker choice for a "central
  claim" test. The remaining residual gap (runtime-constructed module paths) is itself
  recorded in the docstring rather than left implicit — I did not claim to close 100% of
  all conceivable dynamic-import evasions, only the one named in the finding.
- **On finding 12, the new scan's boundary (`tests/` excluded, everything else under
  `REPO_ROOT` included) is itself pinned by dedicated tests**, not just prose in a
  docstring, per the instruction to say exactly what is and isn't covered.
- **`calibrate()`'s docstring changes (findings 3 and 4) cannot be gated by a test yet** —
  the function still raises `NotImplementedError`, correctly, per the Phase 0 freeze. I did
  not write a test that mocks around the `NotImplementedError` to pretend the contract is
  exercised; that would be manufacturing a green result rather than reporting an honest gap.
  This is flagged above as a coverage gap for Phase 1's gate to pick up.
- I did not touch anything under `src/**`, `fixtures/**`, or `pyproject.toml`. I did not
  commit anything to git — the repo still has one tracked commit plus one modified tracked
  file (`PLAN.md`, the phase runner's own amendment); everything else, including both of
  my own test-file changes, remains untracked. Staging/committing is not my call absent an
  explicit instruction to do so.
