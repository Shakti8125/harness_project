# Phase 0 — Audit (Wave 3)

## VERDICT: **FIX FIRST**

One proven runtime contract break between two Wave-1 agents (`Adjustment`), plus three contract
ambiguities that Phase 1's four parallel agents will each resolve differently. All four fixes are
one to five lines. Everything else is low-severity and can ride along.

Gates re-run independently by the reviewer: 83 passed, ruff clean, `mypy --strict` clean.

---

## Findings

**1. [HIGH] `src/integrations/cicd/schemas.py:135` — two incompatible `Adjustment` classes; `calibrate()`'s output cannot be assigned into `Diagnosis`.**
`src/harness/confidence.py:53` defines `Adjustment` and `calibrate()` returns `list[Adjustment]` of *that* class. `schemas.py:135` redeclares a structurally identical but distinct class, and `Diagnosis.confidence_adjustments: list[Adjustment]` binds to the local one.
Failure scenario (executed, not theorised):

```
Diagnosis(..., confidence_adjustments=[harness.confidence.Adjustment(name="cold_start", delta=-0.05, reason="no baseline")])
-> ValidationError: confidence_adjustments.0
   Input should be a valid dictionary or instance of Adjustment [type=model_type]
```

The obvious workaround is worse: `Diagnosis.model_copy(update={...})` accepts the foreign instance silently, producing a `Diagnosis` whose `confidence_adjustments[0]` is `src.harness.confidence.Adjustment` — it dumps fine and fails on the next `model_validate`. mypy will not catch either path: `pyproject.toml` configures no pydantic mypy plugin, so `BaseModel.__init__` is `(**data: Any)`.
This is exactly the parallel-wave drift the audit exists for: `harness-core.md:105-106` and `:242-243` explicitly asked cicd-integration to import the harness class; the request could not arrive in time because the wave was parallel.
Owner: **cicd-integration**. Fix: delete the class body at `schemas.py:135-140` and replace with `from src.harness.confidence import Adjustment`, keeping `"Adjustment"` in `__all__` so A.11's `schemas.Adjustment` name still resolves with the identical field shape.

**2. [MEDIUM] `src/integrations/cicd/schemas.py:146-151` — `Diagnosis` emits the conclusion before the reasoning, contradicting PLAN.md:171-172.**
Field order is `category`, `summary`, `reasoning`, .... `to_gemini_schema()` derives `propertyOrdering` from Pydantic field order (PLAN.md:171), so Gemini will emit `category` — the primary conclusion — before a single reasoning token. PLAN.md:171-172 states output quality "measurably improves when the reasoning field precedes the conclusion field", and A.11:1493 annotates `reasoning` with "ordered BEFORE the conclusion via propertyOrdering" — a claim its own listing does not satisfy for `category`/`summary`.
The code is verbatim to A.11, so this is a plan-level defect, not a transcription error. Phase 0 is the freeze point; changing it after Phase 1 means re-freezing a contract four agents have coded against.
Failure scenario: `POST /v1/replay/real_regression` -> model classifies `category` with zero reasoning tokens in context -> the quality mechanism PLAN.md's structured-output decision was written to buy is inert.
Owner: **PLAN.md amendment + cicd-integration**. Move `reasoning` to the first field, or state explicitly in A.11 that "conclusion" means only `self_confidence`/`suggested_action` and that leading with `category` is deliberate.

**3. [MEDIUM] `src/harness/confidence.py:80` — `calibrate()` is fail-open on an unknown signal name.**
Docstring: "A name absent from `model.deltas` contributes nothing." `empty_diff_contradiction` is deliberately absent from `DEFAULT_ADJUSTMENT_DELTAS` (harness-core's item 7). If the integration forgets to add it to `ConfidenceModel.deltas`, the penalty silently evaporates.
Failure scenario: `infra_timeout` fixture, empty diff, `self_confidence = 0.80`, signal `empty_diff_contradiction` fires -> expected `final_confidence = 0.70` (at the escalation cutoff), actual `0.80` — which also clears `policy.yaml`'s `retry-suspected-flaky` bar of `gte: 0.75`. A wiring omission becomes an unauthorised retry, with nothing in the trace to explain it.
Owner: **harness-core**. Fix: `calibrate()` must fail closed — either raise `ContractViolationError` for a signal absent from `model.deltas`, or emit `Adjustment(name=..., delta=0.0, reason="no delta registered for this signal")` so the omission is visible in the trace.

**4. [MEDIUM] `src/harness/confidence.py:76-82` — clamping order is unspecified, and the two readings straddle a policy threshold.**
PLAN.md:261 is `clamp(self_confidence + sum(deltas), 0.0, 0.99)` — one clamp at the end. The docstring ("Apply every signalled adjustment to `self_confidence` and clamp the result") reads equally as per-step.
Failure scenario: `self_confidence = 0.95` with `evidence_fully_verified` (+0.05) and `evidence_refuted` (-0.15). Sum-then-clamp = **0.85** -> `policy.yaml` `open-fix-pr` (`gte: 0.85`) matches -> `require_approval`. Clamp-per-step = 0.95+0.05 -> 0.99 -> 0.99-0.15 = **0.84** -> no rule matches -> `default_effect: deny`. Identical inputs, different action.
Owner: **harness-core**. Fix: state "sum all deltas, then clamp exactly once" in the docstring before Phase 1 implements it.

**5. [MEDIUM] `src/settings.py:26` + `src/api/main.py:30` — a mistyped `.env` key prints its raw value into the boot log.**
Verified against the real class:

```
ValidationError: 1 validation error for Settings
harness_githubtoken
  Extra inputs are not permitted [type=extra_forbidden, input_value='ghp_TYPOED_SECRET_ABCDEFGH', input_type=str]
```

`main.py:30` calls `get_settings()` at import time, so uvicorn prints that traceback to stdout at boot. Appendix E's four barriers do not cover it — the value never reaches a span or an escalation, it reaches stderr before the `Redactor` exists. `test_no_secret_leak.py` (Phase 5) asserts against "captured stdout/stderr", so this is the path that will fail that gate.
Owner: **api-surface**. Fix: the composition root should catch `ValidationError` from `Settings()` and re-raise with `input_value` stripped, printing only the offending field names.

**6. [MEDIUM] `src/settings.py:26` — `extra="forbid"` does not catch a typo'd variable from the process environment, which is the only source Docker and Fly use.**
Verified: `HARNESS_GITHUBTOKEN=...` present in `os.environ` constructs `Settings` cleanly (pydantic-settings 2.15 enforces `extra` on the dotenv source, not the env source). PLAN.md:1770 claims the opposite: "`extra="forbid"` turns a typo'd env var into a startup crash rather than a silently ignored setting."
Failure scenario: `fly secrets set HARNESS_ESCALATION_TRESHOLD=0.9` -> accepted, ignored, app keeps `0.70`; the operator believes the cutoff moved. Same for `HARNESS_ALLOWED_REPOS`, whose silent emptiness makes every webhook 403.
Owner: **api-surface**. Fix: either a `model_validator` that scans for unknown `HARNESS_`-prefixed keys, or amend PLAN.md:1770 to state the guarantee only holds for `.env`.

**7. [LOW-MED] `fixtures/scenarios/real_regression/scenario.yaml:6,11` — the eval label is internally unsatisfiable.**
`min_confidence: 0.75` with `effect: require_approval`, but `policy.yaml`'s `open-fix-pr` requires `diagnosis.final_confidence: {gte: 0.85}`. In `[0.75, 0.85)` no rule matches `open_pull_request` and `default_effect: deny` applies.
Failure scenario: a run scoring `0.80` passes the `min_confidence` assertion and fails the `effect` assertion — `scripts/eval.py` exits 1 for a run that met the label's own stated bar. This is precisely the flaky gate `fixtures/README.md` warns against.
Owner: **fixtures-eval**. Fix: raise `min_confidence` to `0.85`, or drop `effect` from this scenario's label.

**8. [LOW-MED] `fixtures/README.md` (`get_job_logs` section) — the replay contract does not say which end `max_bytes` keeps.**
It says only "truncated to `max_bytes` if given". PLAN.md:222 says the log cap keeps the **last** 20 MB, because the proximate failure is near the end.
Failure scenario: `gateway_replay.py` implemented as `f.read(max_bytes)` on the Phase-1-grown multi-thousand-line log returns only runner bootstrap and pip output; the anchor `assert 91 == 90` is gone; the Diagnostician returns `category: unknown` and the `real_regression` eval fails for a reason invisible in the fixture.
Owner: **fixtures-eval** (doc) -> **cicd-integration** (Phase 1). Fix: state "keep the **last** `max_bytes`" in `fixtures/README.md`, and quote the whole `get_job_logs` contract into the Phase 1 brief — it currently lives only in `fixtures/README.md`, which is not in PLAN.md and which the agent writing `gateway_replay.py` has no reason to open.

**9. [LOW] `Dockerfile:24` — the image contains no fixtures, so PLAN.md's Phase 1 demo path cannot run in the container.**
The builder does `COPY src ./src` and the runtime copies only `/app/.venv` and `/app/src`; `docker-compose.yml` mounts only `./data`. PLAN.md:357-360 (Phase 1 Verify step 2) is `docker compose up -d --build` then `POST /v1/replay/real_regression`.
Failure scenario: that curl returns a replay error because `/app/fixtures/scenarios/real_regression/` does not exist in the image. Owner: **api-surface**.

**10. [LOW] `src/api/main.py:69,71` — `/healthz` reports the wrong degraded value and a hardcoded `status`.**
PLAN.md:1601 (B.3, disk full) specifies `healthz` reports `db: "degraded"`; the code emits `"error"` and has no `"degraded"` path at all. Separately, `status` is the literal `"ok"` regardless of the db probe.
Failure scenario: Fly volume unmounted -> sqlite unopenable -> `/healthz` returns 200 `{"status":"ok","db":"error","version":"0.1.0"}`; the Dockerfile `HEALTHCHECK` and `fly.toml`'s `[[http_service.checks]]` both assert only HTTP 200, so a machine with no database stays in rotation. Owner: **api-surface**.

**11. [LOW] The entire Phase 0 output is uncommitted.**
`git log` has one commit (`7e8f6a5 PLAN.md`); `git ls-files` lists only `PLAN.md`.
Failure scenario: PLAN.md's central claim is checked at Phase 6 with `git diff --stat -- src/harness/`. With `src/harness/**` never tracked, that command prints nothing whether or not the harness changed — the "the seam holds" claim becomes unverifiable by the mechanism designed to verify it. Also blocks the `phase-0-green` tag per `docs/progress/README.md:13`. Owner: **phase runner** (test-verifier correctly declined to commit unasked).

**12. [LOW] `tests/unit/test_no_env_access.py:24,32` — the env-access gate only scans `src/`.**
Appendix E's rule is "no other module reads `os.environ`".
Failure scenario: Phase 1's `scripts/replay.py` doing `os.getenv("HARNESS_GATEWAY")` passes the gate while violating Appendix E. Owner: **test-verifier**.

**13. [LOW] `tests/test_layering.py:46-57` — a dynamic import evades all four checks.**
Only `ast.Import`/`ast.ImportFrom` nodes are collected, and the string-literal denylist does not contain `integrations`.
Failure scenario: `importlib.import_module("src.integrations.cicd.gateway_replay")` inside a harness module passes all 53 cases. Not required by PLAN.md:66 — recording the hole, not asking for the denylist to be widened. Owner: **test-verifier**.

**14. [LOW] `120_000` has two homes.** `settings.log_char_budget` (Appendix E) and `ContextBudget.total_chars` (A.3:1089). Both plan-mandated, so not a drift — but if Phase 1's composition root builds `ContextBudget()` without threading `settings.log_char_budget`, `HARNESS_LOG_CHAR_BUDGET=40000` becomes a silent no-op. Watch item for Phase 1 wiring.

---

## Rulings on the judgement calls routed to the reviewer

**The confidence split (six harness rows + one integration row).** **Correct, keep it.** `empty_diff_contradiction`'s condition names `real_regression` and `DiffSummary.files` — both cicd-owned — and the adjustment's own name carries the domain. Putting it in the harness would put a category name in `src/harness/`. The stated cost (PLAN.md's table has no single representation in code) is real but cheap: the deltas are data in `ConfidenceModel.deltas`, so the full table exists at runtime in the composed object, which is where it matters. The split is *not* what needs fixing — finding **3** is: the split is only safe if omitting the seventh row is loud. Approve the split, fix the fail-open.

**`calibrate(self_confidence, signals: Mapping[str, str], model: ConfidenceModel) -> tuple[float, list[Adjustment]]`.** **Approve the signature as-is.** Name-to-reason keeps every condition and every piece of prose outside the harness; the rejected alternative (`Sequence[str]` with internally generated reasons) would have imported condition text into `src/harness/`, which is the right thing to have rejected. Two amendments before Phase 1 codes against it: fix the clamp ordering (finding 4) and the unknown-signal behaviour (finding 3). Note `Mapping` (not `dict`) makes the harness-side contract read-only over the caller's structure, which is correct.

**`SpanHandle` and `SecretRegistry`.** **Both approved as invented.** `SpanHandle`'s three members (`span_id`, `set_attribute`, `set_error`) are the exact minimum `Span`'s own fields imply; nothing speculative. `SecretRegistry`'s deliberate exclusions are the right ones — the field-name rule inspects `Settings`, which only the composition root can see, and the vendor regex denylist at PLAN.md:832-836 contains a literal that is itself on the layering denylist. One note for Phase 5: A.9 writes `@asynccontextmanager def span(...)`, which must become `async def` when implemented; that is a signature change from Appendix A and should be recorded rather than discovered.

**The three gaps left uninvented (`agent.py`, constructors, trace read path).** **Correct to leave unfilled — leaving them empty was the whole point of the phase — and adequately recorded in `harness-core.md:255-259`.** But the recording is not sufficient on its own: PLAN.md:306 assigns `harness/agent.py` to Phase 1, Appendix A specifies no `Agent`/`LLMAgent` anywhere, and Phase 1 dispatches four agents *simultaneously*. If those three items are not written into the Phase 1 brief with a single named owner (harness-core) and a "do not invent this yourself" instruction to the other three, you will get two conflicting `Agent` protocols in one wave. Same for the trace read path: A.12 requires `GET /v1/runs/{run_id}/trace -> TraceResponse` and nothing returns one; decide now whether it lands on `TraceRecorder` or `MemoryStore`, because api-surface and harness-core will otherwise each assume the other built it.

**`Evidence.source` retaining `"diff"`.** **Contract-mandated. Keep it; do not rename.** A.1:972 is verbatim and it is the only denylisted string literal left in the package. It is genuinely mild leakage — "diff" reads as "a difference between two states" and an incident adapter citing a deploy delta would use it naturally. The harder case in the same class is `Observation.commit_sha` (A.5:1217): a VCS concept as a first-class field on a harness memory model, which an incident adapter can only ever leave `None`. Both are Appendix A verbatim, and deviating from Appendix A is worse than carrying them. Flag both for the Phase 6 adapter review, where `Evidence.source` may need a widened `Literal` and `commit_sha` may need renaming to something like `subject_version`.

**`83 passed` vs PLAN.md's `1 passed`.** **The plan's literal expectation is superseded. This is not a miss, and test-verifier was right to refuse to invent a test.** PLAN.md:290's substantive assertion is "test_layering passes"; it does, 53/53. The reviewer verified the suite is not vacuous by copying `src/harness/**` and `tests/test_layering.py` into a scratch tree and injecting four real violations (`from src.integrations.cicd import schemas`, `import github`, the literal `"pull_request failed"`, and `workflow_run` in a comment). Result: 6 targeted failures naming the exact file and the exact token, 63 passing. A single monolithic test would have hit the printed number while naming none of them. Recommend amending PLAN.md:290 to `# expect: test_layering passes` with no count, so the Phase 1 gate does not re-litigate this.

**The denylist scoping.** **Correctly scoped — exactly PLAN.md:66's five words, word-boundary matched, case-insensitive.** harness-core's warning is sound: widening it collides with four Appendix-A-verbatim identifiers (`"diff"`, `commit_sha`, `actions_in_window`, `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN`), and renaming any of them would be a contract deviation — a strictly worse outcome than a slightly narrow test. Do not widen without a PLAN.md change. One known hole recorded as finding **13**.

**The `real_regression` fixture.** **Id consistency is sound.** Every cross-reference verified by hand: `repository.full_name`, `workflow_run.id 501234567`, `run_attempt 1`, `head_sha e2cdf1b4...` and `job_id 601234567` agree across `webhook.json`, all three `api/` filenames and bodies, and `logs/job_601234567.txt:41`; the compare recording's `base_commit.sha 8d4d0a89...` equals the green run's `head_sha` and is encoded in the compare filename; `expected.commit` equals `head_sha` and appears in `commits[].sha`; all three `cites_any_of` strings appear in the log; the cascading secondary failure (`test_checkout_total_applies_discount`) is present, so the last red line is not the root cause. No secrets anywhere in `fixtures/` (`ghp_`, `github_pat_`, `AIza`, `ghs_`, `xox*`, `BEGIN ... KEY`, bearer tokens: zero hits).
The 159-line placeholder is **adequately recorded** — `fixtures-eval.md:135-153` states the gap, why it matters, and what Phase 1 must do, and correctly distinguishes it from the separate 50,000-line synthetic log that `test_error_lines_never_trimmed` needs. Two things to carry into that padding job: the internal-consistency defect at finding **7**, and the fact that the log's timestamps end at `14:02:36` while the jobs-list claims `completed_at 14:05:44` with "Run tests" spanning `14:03:59-14:05:40` — nothing parses log timestamps today, so it is cosmetic now, but the padded log should span the real job window.

**`get_job_logs` having no `api/*.json` recording.** **Sound call, with one trap that must be closed before Phase 1 dispatches.** Not duplicating a multi-thousand-line log into an escaped JSON string is right — two copies of the same bytes diverge, and the format is explicitly trying to rule that out. The trap is not the decision, it is the contract's location and its silence on truncation direction: it lives only in `fixtures/README.md`, which is not in PLAN.md, and `gateway_replay.py` is written by a different agent in a parallel wave. See finding **8**. Quote the rule into the Phase 1 brief for cicd-integration, including "keep the **last** `max_bytes`".

---

## Contract diffs

**Appendix A.1-A.10 vs `src/harness/**`: zero drift.** Every model's fields, annotations, defaults, constraints and `model_config` dumped and compared line by line to A.1-A.10. All 42 models are `extra="forbid", frozen=True`; `RunState` alone is `extra="allow", frozen=False`, as specified. `AgentResult` is still `Generic[TOut]` with the module-level `TOut`. Every `Literal` member list, every `Field(ge=/le=/max_length=/min_length=)`, every default (`120_000/20/200/400/8_000`, `3/4/0.5/8.0/full/60.0/True`, `30.0`, `4096`, `0.0`, `30/20`, `1`, `False`) matches. `RunId`'s pattern is intact on all seven fields that carry it. `Condition.in_` carries `alias="in"` and validates `{in: [...]}`. `LlmRequest.schema` is present, required, and does not silently become optional.

**Appendix A.11 vs `src/integrations/cicd/schemas.py`: one diff.**

| model | field | plan says | code has |
|---|---|---|---|
| `Diagnosis` | `confidence_adjustments` | `list[Adjustment]` where `Adjustment` is the one shape shared across the seam (repo layout:111 puts it in `confidence.py`; A.11:1486 also lists it in `schemas.py`) | `list[schemas.Adjustment]`, a second class incompatible with `harness.confidence.Adjustment` — finding **1** |

All other 16 A.11 models are verbatim, including `Citation.note`'s `default=""`, the `max_length` list caps (`8/3/5/6`), and the mutable-default label lists.

**`policy.yaml` vs PLAN.md:404-449: byte-equivalent.** `version: 1`, `integration: cicd`, `default_effect: deny`, all six `forbidden` entries in order, all four rules in file order (`retry-suspected-flaky`, `open-fix-pr`, `file-ticket`, `read-only-always`) with every `when` key, operator and threshold, and every obligation. It validates against `PolicySpec.model_validate()`.

**Appendix E vs `src/settings.py`: field-for-field identical.** All four secrets are `SecretStr`, `escalation_webhook_url` is `SecretStr | None = None`, `dry_run: bool = True`, `env_prefix="HARNESS_"`, `extra="forbid"`, `frozen=True`. `.env` is in both `.gitignore:2` and `.dockerignore:1`; `git check-ignore -v .env` confirms it is ignored and `git ls-files` confirms it is untracked; its contents are obvious placeholders. `.env.example` is placeholders only. No secret can enter an image layer — the build context copies only `src/`. The single deviation from Appendix E's *stated behaviour* (not its field list) is finding **6**.

---

## Unhandled failure paths

Phase 0 adds exactly two external calls, both to SQLite, both in `src/api/main.py`:

| external call | condition | handling |
|---|---|---|
| `aiosqlite.connect` + `SELECT 1` (`_db_reachable`) | any exception | caught, logged, `db: "error"` — but PLAN.md:1601 specifies `db: "degraded"` for the disk-full case, and no `"degraded"` value exists anywhere. Finding **10** |
| `CREATE/INSERT/DELETE` probe (`_db_writable`) | any exception | caught, logged, `db_writable: false`, 503. Correct per PLAN.md:1601 |

Appendix B's Gemini (B.1), GitHub (B.2), remaining SQLite (B.3) and escalation-webhook (B.4) rows have no code to attach to yet — no client, no gateway, no store exists. The row to check hardest in Phase 3 is the `retries_for_signature_24h = 999` fail-closed default (PLAN.md:1598); it has no representation in code today, and unlike the other B.3 rows it is not implied by any existing signature. Recommend naming it as a required constant in the Phase 3 brief so it cannot be quietly dropped.

Guardrails invariants are correct for this phase: `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN: Final[int] = 1` at `guardrails.py:23` is a module constant with no `PolicySpec` field, no `Settings` field and no env var reaching it — genuinely unreachable from config. `ToolGateway.invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult` at `gateway.py:74` carries the `PolicyDecision` parameter that PLAN.md:454-459's second enforcement point requires, so the gateway's independent re-check is structurally possible. The re-check body itself is Phase 2 and correctly absent.

---

## Plan drift

**Built but unplanned:** nothing. Every file on disk is named in PLAN.md's repository layout or is an `__init__.py` for one of them. `confidence.py` and `errors.py` are layout-named (lines 111-112) and honestly labelled DERIVED in their own docstrings. No Phase 1+ logic leaked in: every harness concrete method is `raise NotImplementedError`, every Protocol member is `...`, every integration module below `schemas.py` is a docstring-only placeholder, `policy.yaml` is Phase 2 content but was explicitly in scope. `/readyz` hardcodes `policy_loaded = False` rather than faking readiness, which is the right call. Scope discipline is clean.

**Planned but missing:**
- `src/harness/agent.py` — layout:101. PLAN.md:306 assigns it to Phase 1, so its absence is correct; the *contract* gap (Appendix A specifies no `Agent`/`LLMAgent`) is the item that must reach the Phase 1 brief.
- `README.md` — layout:89, not in Phase 0's Built list. Not a finding.
- `scripts/`, `docs/ADAPTER_GUIDE.md`, `harness/memory/migrations/`, `api/deps.py`, `api/routes_*.py`, `api/templates/` — all later phases, all correctly absent.
- `git repo initialised with PLAN.md as the first commit` (PLAN.md:284) — satisfied, but nothing since has been committed. Finding **11**.
- PLAN.md:290's `1 passed` — superseded; recommend amending the line.

---

# Phase 0 — Re-audit (after the fix round)

## VERDICT: **SHIP**

All thirteen actionable findings closed or closed-with-caveat. Four new items below are genuine but
none blocks the tag; three are Phase 1/2 traps that belong in the next brief. Gates re-run
independently by the reviewer: `99 passed`, `ruff` clean, `mypy --strict src/harness` clean, and
`mypy --strict src` clean across all 29 files.

## Per-finding disposition

| # | Owner | Disposition | Verified how |
|---|---|---|---|
| 1 | cicd-integration | **closed** | `schemas.Adjustment is src.harness.confidence.Adjustment` → `True`. Constructed a `Diagnosis` with a `harness.confidence.Adjustment` instance: validates, `type(...).__module__ == 'src.harness.confidence'`, and `Diagnosis.model_validate(d.model_dump())` round-trips. `"Adjustment"` still in `__all__`. Zero second `class Adjustment` anywhere in the tree. |
| 2 | cicd-integration + runner | **closed** | Live `model_fields` order is `reasoning, category, summary, self_confidence, citations, suspected_commit_sha, suspected_test_ids, suspected_package, suggested_action, final_confidence, confidence_adjustments`. `git diff PLAN.md` shows the A.11 hand-edit produces the identical order. No type, constraint or default moved (full dump diffed). |
| 3 | harness-core | **closed-with-caveat** | Constants present, contract step 3 unambiguous, `len(adjustments) == len(signals)` stated as an invariant. Body still `NotImplementedError` (correct). Numeric residual ruled on below. |
| 4 | harness-core | **closed** | `confidence.py:113-122` states "Sum first, then clamp once", forbids intermediate clamping/rounding, and embeds `0.95 / +0.05 / -0.15 → 0.85` with `0.84` explicitly named wrong. |
| 5 | api-surface | **closed-with-caveat** | Both leak paths executed with real-shaped secrets. Env source → `SettingsError: unrecognised environment variable name(s), values withheld: HARNESS_GITHUBTOKEN`; **0** secret occurrences in stdout+stderr. `.env` source → `harness_githubtoken: extra_forbidden`; **0** occurrences. `from None` confirmed to suppress the chain. Caveat = new finding 3. |
| 6 | api-surface | **closed** | Validator fires correctly, names only the variable. Robustness ruled on below. |
| 7 | fixtures-eval | **closed** | Reasoning survives Open Risk 4; ruled on below. |
| 8 | fixtures-eval | **closed** | `fixtures/README.md:127-139`: "keeps the LAST `max_bytes`, never the first", `content[-max_bytes:]` vs `f.read(max_bytes)`, PLAN.md:222 cited, failure mode spelled out. |
| 9 | api-surface | **closed** | `Dockerfile:43 COPY fixtures ./fixtures` in the runtime stage. `.dockerignore` excludes only `fixtures/recorded/`. Whole `fixtures/` tree re-scanned for `ghp_`/`github_pat_`/`AIza`/`ghs_`/`xox*`/`BEGIN … KEY`/bearer/`whsec_`: **zero hits**. |
| 10 | api-surface | **closed-with-caveat** | The reported *value* is fixed. The *rotation* consequence named in the same finding is not — new finding 1. |
| 11 | phase runner | **still open, correctly** | Confirmed below; resolved by the commit that follows this report. |
| 12 | test-verifier | **closed** | Scan root is `REPO_ROOT`; excludes `tests/`, `.venv`, caches, and `src/settings.py`. 28 collected files = every `.py` outside `tests/` (counted independently). `test_scripts_dir_absence_does_not_break_the_scan` skips rather than fails once `scripts/` appears. Non-vacuity guard present. |
| 13 | test-verifier | **closed** | `test_no_dynamic_import_of_integrations` walks `ast.Call`, matching `.import_module` attribute calls, bare `import_module`, and `__import__`, checking a string-literal first arg against both `src.integrations` and `integrations`. Denylist untouched at exactly PLAN.md:66's five words. Residual (runtime-built paths) stated in the docstring. |
| 14 | — | **still the right call** | Both `120_000` homes unchanged, nothing wires them. On the carry-forward list. |

## Rulings on the three flagged items

### 1. harness-core's fail-visible-not-fail-safe residual — accept the remedy; **decline** the offer to write a harness half

The zero-delta `Adjustment` was one of the two remedies named in finding 3, so it is closed as
specified. harness-core's analysis of the residual is correct and its instinct about where the
numeric close belongs is correct — but the offer to "write the harness half" should be declined,
because **there is no harness half to write.**

`PolicyEngine.decide(ctx: ActionContext)` already consumes `facts: dict[str, JsonValue]` supplied by
the integration, and `policy.yaml` already supports `eq`/`gte`/`lt` over a flat dotted namespace. The
numerically-safe close is entirely data:

- cicd-integration's fact builder emits `diagnosis.unregistered_adjustments: int` (count of
  adjustments whose `reason == UNREGISTERED_SIGNAL_REASON`);
- `policy.yaml` adds `diagnosis.unregistered_adjustments: {eq: 0}` to the `when` block of each
  permissive rule — `retry-suspected-flaky`, `open-fix-pr`, `file-ticket`. No rule matches →
  `default_effect: deny`. `read-only-always` is left alone so reads keep working.

Adding anything to `guardrails.py` would put "a diagnosis carries adjustments" — a concept the
generic engine has no business knowing — into `src/harness/`. That is worse than the disease, by the
same argument harness-core used to keep `empty_diff_contradiction` out of the harness.

**Exposure window:** the hole cannot cause an unauthorised action before Phase 2. Phase 1 ships
Investigator + Diagnostician only; there is no `PolicyEngine`, no gateway write, no
`rerun_failed_jobs`, and `final_confidence` is a reported number rather than a gate. Deferring the
close to Phase 2 — where `policy.yaml` and the fact builder are both being written anyway — is safe.
Named as a required row in the Phase 2 brief so it cannot be quietly dropped.

harness-core's separate suggestion — an eval assertion "no adjustment carries
`UNREGISTERED_SIGNAL_REASON`" — is a good cheap second net. Keep both.

### 2. fixtures-eval's 0.85 — reasoning survives Open Risk 4; keep `effect: require_approval`

Two things could have sunk it and both hold.

**The `effect` assertion is first *scored* in Phase 4, not earlier.** `scripts/eval.py` lands in
Phase 4 (PLAN.md:682), the same phase as the Evaluator. This matters because `open-fix-pr`'s `when`
block also carries `evaluation.verdict: {eq: pass}` — unlike `retry-suspected-flaky`, it does **not**
accept `skipped`. Had the eval harness landed in Phase 2 or 3, `real_regression`'s
`effect: require_approval` would have been unsatisfiable for a second, independent reason. It
doesn't, so it isn't.

**The arithmetic clears with margin.** By Phase 4 the only adjustment that fires for this scenario is
`evidence_fully_verified` (+0.05): `cold_start` is false (`baseline_kind: branch_green`), the diff is
non-empty so no `empty_diff_contradiction`, citations exist so no `no_citations`, no gateway
degradation, no memory priors on a fresh replay. The effective bar on the model's `self_confidence`
is therefore **0.80**, against a scenario PLAN.md's own rubric puts in the 0.90–1.00 band. 0.85 is a
floor the design clears, not a bar it hopes to. And `0.85 >= 0.75` keeps PLAN.md:360's manual Phase 1
check satisfied, as claimed.

Open Risk 4 says the *deltas and the 0.70 cutoff* are asserted rather than measured; it does not
argue for loose eval labels. A label that is internally consistent and provably reachable is exactly
what you want when the numbers are unmeasured, because a failure then means something.

**Caveat to record, not to fix:** the label's two assertions are no longer independent —
`min_confidence: 0.85` and `effect: require_approval` are now the same quantity read through
`open-fix-pr`'s `gte: 0.85`. Nothing in the repo couples them. If Open Risk 4's promised
recalibration moves that policy threshold, `scenario.yaml` must move in lockstep or the eval becomes
stricter than the policy it checks.

### 3. api-surface's env validator — robust for this project's targets; one documented caveat

- **Does not leak values.** The `ValueError` text carries variable *names* only. Executed with four
  live secrets in the environment: zero occurrences of any of them in the output.
- **Case handling is correct.** `key.upper()` on both sides matches pydantic-settings'
  `case_sensitive=False` default, so a legitimately lowercase `harness_gemini_api_key` is accepted.
- **Cannot spuriously reject the stated targets.** `docker compose` injects the base
  `python:3.12-slim` env (`PATH`, `LANG`, `GPG_KEY`, `PYTHON_*`, `HOME`, `HOSTNAME`) plus `.env` plus
  `HARNESS_DATABASE_PATH` — nothing unrecognised. Fly injects `FLY_*` and `PRIMARY_REGION` — no
  `HARNESS_` prefix. `fly.toml [env]` sets five keys, all valid fields.
- **Caveat worth one comment line, not a fix:** Kubernetes with `enableServiceLinks: true` (the
  default) injects `HARNESS_SERVICE_HOST` / `HARNESS_PORT_8000_TCP_*` into every pod in a namespace
  containing a Service named `harness`; legacy Docker `--link` with alias `harness` does the same.
  Either would hard-crash the app at boot with a confusing message. K8s is not a target of this plan,
  so this is not a finding — but a sentence in the validator docstring saying "this assumes no
  non-config `HARNESS_`-prefixed variables are injected by the platform" would save someone an hour.

## New findings, most severe first

**1. [LOW-MED] `src/api/main.py:77-88` + `fly.toml` `[[http_service.checks]]` + `Dockerfile:51-52` — a machine with an unopenable database still passes both health checks.**
Finding 10's *value* fix landed; the rotation consequence named in the same finding did not.
Failure scenario: the Fly volume fails to mount → `_db_reachable` returns `"error"` → `/healthz`
returns **HTTP 200** with `{"status":"error","db":"error","version":"0.1.0"}`. `fly.toml`'s check
(`path = "/healthz"`) and the Dockerfile `HEALTHCHECK` both assert only HTTP 200, so the machine
stays in rotation and 500s every `POST /v1/replay/...`.
This cannot be fixed in Phase 0 the obvious way: repointing the checks at `/readyz` would fail
permanently, because `readyz` hardcodes `policy_loaded = False` until Phase 2. The other option is a
2-line change — return 503 from `/healthz` when `db == "error"`, keeping 200 for `"ok"` and
`"degraded"` per B.3 — which leaves the Phase 0 DoD body byte-identical and is compatible with A.12
(that row specifies the happy path, not a prohibition). **Phase 1 is when `fly deploy` actually
happens (PLAN.md:372), so this must be in the Phase 1 brief either way.** Owner: **api-surface**.

**2. [LOW-MED] `src/integrations/cicd/schemas.py:189-196` and PLAN.md:1517-1522 — `RemediationPlan` still emits the conclusion before the reasoning.**
Field order is `action`, then `rationale`. This is the identical defect the fix round just corrected
in `Diagnosis`, in the other model marked "# the Remediator's LLM output" — so it also goes through
`to_gemini_schema()`'s `propertyOrdering`.
Failure scenario: Phase 2's Remediator emits `action: "open_fix_pr"` before a single rationale token,
so PLAN.md:171-172's quality mechanism is inert for the one agent that proposes side-effecting
actions. Moving `rationale` above `action` is a no-op for every consumer except `propertyOrdering`,
and Phase 0 is the freeze point — after Phase 2 codes against it, this is a re-freeze. The fix round
set a precedent by amending A.11 for `Diagnosis`; it was applied to one of the two models with this
shape. Owner: **cicd-integration + PLAN.md amendment**.

**3. [LOW] `src/settings.py:120-137` — `get_settings()` is the only leak-safe way to construct `Settings`, and nothing enforces that.**
The redaction barrier is one function deep. With three valid secrets set and one required field
missing:

```
1 validation error for Settings
github_webhook_secret
  Field required [type=missing, input_value={'gemini_api_key': 'AIzaS... 'ghp_REALTOKEN_ABCDEF'}, input_type=dict]
```

`str(ValidationError)` prints a whole token verbatim, because pydantic elides the *middle* of the
input dict. The finding-6 validator adds a second trigger for the same thing (its
`errors()[0]['input']` is the full plaintext settings dict). Through `get_settings()` this is
invisible — verified, zero occurrences — but nothing stops Phase 1's `src/api/deps.py` (the
composition root, PLAN.md:306) or a future `scripts/replay.py` from calling `Settings()` directly, or
from writing `except ValidationError as e: logger.error(e)`. Appendix E's `test_no_secret_leak.py`
asserts against captured stdout/stderr and is a merge gate before the repo goes public.
Two adjacent holes in the same barrier:
- `_redact_validation_error` surfaces `err['msg']` verbatim for any `value_error`. Safe today
  because our validator is the only producer — but a plausible Phase 1 addition like a
  `@field_validator("gemini_api_key")` raising `ValueError(f"key must start with AIza, got {v}")`
  would print the key.
- `pydantic_settings.exceptions.SettingsError` (a different class from `src.settings.SettingsError`)
  escapes the handler entirely. `HARNESS_ALLOWED_REPOS=octo-org/harness-demo-repo` — the way an
  operator would naturally spell it, since the field is `list[str]` and `.env.example:24` only shows
  `[]` — crashes at boot with an uncaught third-party exception and a chained `JSONDecodeError`. No
  secret leaks (no secret field is complex), and it fails closed, but it bypasses the redaction path.
Guard that costs one test: a grep gate alongside `test_no_env_access.py` asserting `Settings(`
appears nowhere outside `src/settings.py`. Owner: **api-surface** (barrier) / **test-verifier** (gate).

**4. [LOW] `src/harness/gateway.py:74` — the `ToolGateway.invoke` Protocol carries `decision` but states no re-check obligation.**
PLAN.md:454-459: "`ToolGateway.invoke()` requires a `PolicyDecision` argument and **re-checks the
tool name against `forbidden` itself**. The gateway is authoritative." The signature carries the
argument; nothing in the contract text tells the implementer to use it. A Phase 2 agent writing
`gateway_github.py` from `src/harness/gateway.py` alone has no instruction to verify anything.
Failure scenario: `GitHubToolGateway.invoke(call, decision)` accepts `decision` and ignores it → the
second of PLAN.md's two independent enforcement points is structurally present and behaviourally
absent, which is worse than one honest check because the trace will show a `PolicyDecision` was
consulted. Same class of defect as finding 4, except it guards a security invariant rather than a
number, and the remedy is the same three lines of docstring harness-core just wrote for
`calibrate()`. Owner: **harness-core**.

## Contract diffs

**Zero.** Every model's fields, annotations, defaults, constraints and `model_config` re-dumped from
the live classes and diffed against Appendix A.1–A.11.

- A.1–A.10 vs `src/harness/**`: **zero drift**, unchanged from the prior pass. File mtimes confirm no
  Appendix-A harness module was touched in the fix round. All 42 models still `extra="forbid",
  frozen=True`; `RunState` alone `extra="allow", frozen=False`. `RunId` pattern intact on all seven
  fields. `Condition.in_` still carries `alias="in"`. `LlmRequest.schema` still present and required.
- A.11 vs `src/integrations/cicd/schemas.py`: **the single diff from the prior pass is gone.**
  `Diagnosis.confidence_adjustments` is now `list[src.harness.confidence.Adjustment]`. All 17 models
  match field-for-field. The `reasoning`-first reorder is present in both code and plan, identically.
- `confidence.py`: not an Appendix A module. Two new `Final` constants, both DERIVED and documented.
  `Adjustment`'s shape is unchanged and still matches A.11's listing, which is what keeps the
  re-export honest.
- `policy.yaml` vs PLAN.md:404-449: **still byte-equivalent**; not touched in the fix round.
- Appendix E vs `src/settings.py`: **field-for-field identical**. Two additions
  (`_no_unrecognised_harness_env_vars`, `SettingsError`/`_redact_validation_error`) add behaviour
  Appendix E promises; neither adds or changes a field.

## Unhandled failure paths

| external call | condition | handling |
|---|---|---|
| `aiosqlite.connect` + `SELECT 1` | `disk I/O error` | **now handled** — `"degraded"`, matching B.3 (PLAN.md:1601). String-matches the exception message; the only option before Phase 3's fault injection. |
| same | anything else | `"error"`, logged, 200. **Consequence unhandled** — new finding 1. |
| same | `db_path.parent.mkdir` failing (read-only/full filesystem) | raises `OSError`/`PermissionError`, not `sqlite3.OperationalError` → generic handler → `"error"` not `"degraded"`. Same disk-full condition, wrong bucket. Not ranked: the observable difference is one string on a path that already reaches finding 1's real problem. |
| `CREATE/INSERT/DELETE` probe | any | caught, `db_writable: false`, 503. Correct per B.3. |

Appendix B's 29 rows still have no code to attach to. Three easiest to skip, named in their phase
briefs below: `retries_for_signature_24h = 999` fail-closed (B.3, Phase 3), 404-on-a-read-tool
returning `ToolError(kind="not_found")` **as data** (B.2, Phase 2), and 409/422 "already exists"
treated as success returning the existing resource (B.2, Phase 2/3).

Guardrails invariants re-checked and unchanged. `MAX_SIDE_EFFECTING_ACTIONS_PER_RUN: Final[int] = 1`
is a module constant with no `PolicySpec` field, no `Settings` field, and no env var reaching it.
`forbidden` evaluation order is stated in the module docstring. The gateway's independent re-check is
structurally possible and contractually unstated — new finding 4.

**Secrets sweep:** all four `Settings` secrets are `SecretStr`. `get_secret_value()` is called in
exactly one place, `main.py:96`, and its result is consumed only by `bool()`. `SecretRegistry` is
unchanged and correctly unpopulated. No span attribute, log line, RFC 9457 detail or escalation
payload exists yet to leak into. `.env` is in `.gitignore:2` and `.dockerignore:1`; `git
check-ignore` confirms both `.env` and `data/harness.db` are ignored. The build context now includes
`fixtures/`, re-scanned and clean.

## Plan drift

**Built but unplanned:** nothing new. The three behavioural additions from the fix round
(`_no_unrecognised_harness_env_vars`, `_redact_validation_error`, the `"degraded"` branch) each
implement a sentence PLAN.md already promises (1770, Appendix E's secrets paragraph, 1601) inside a
Phase 0 deliverable. `calibrate()`'s body is still `raise NotImplementedError` — the fix was
docstring-only, so no Phase 1 logic was smuggled in as a fix.

**PLAN.md hand-edits:** two, both verified. A.11's `Diagnosis` reorder matches the code exactly. Line
290's `1 passed` → "count grows as later phases add tests" is the recommended amendment; nothing else
in the Verify block moved.

**Finding 11 — committing now captures the right thing.** `git add -A -n` stages **72 files**: 30
under `src/` (29 `.py` + `policy.yaml`), **13 under `src/harness/`**, 7 tests, 7 fixtures, 8 docs, 11
`.claude/`, `uv.lock`, and the six root config files, plus the modified `PLAN.md`. Zero `.env`, zero
`*.db`, zero `.venv`, zero caches. `src/harness/**` becomes tracked, which is the whole point —
`git diff --stat -- src/harness/` at Phase 6 will have a baseline to diff against.
