# Phase 1 — carried backlog

Written 2026-09-10, after the urgent-bundle round (`fe7ba2d` and its follow-up).

This is the documented remainder referred to by the `phase-1-green` decision: the findings
that are known, ranked, and deliberately not fixed in Phase 1. It is not a list of things
nobody looked at. Every item below was verified still open by the re-audit against the
committed tree, with the check it was verified by recorded next to it.

The authorities remain `docs/progress/phase-1/review.md` (the original 12-finding audit)
and `docs/progress/phase-1/review-2.md` (the fix-round re-audit). This file indexes what
survived them; it does not restate their reasoning.

> **Counting note.** `review-2.md`'s own PHASE 1 verdict line says the round "adds three
> more" findings while the document lists five. Five is right — that line counts only the
> two defects newly *introduced* by the fixes. The document is committed verbatim and is
> not edited to correct it. Work from five.

## Closed by the urgent bundle

| Finding | Was | Now |
|---|---|---|
| `review-2` 1 | high | Closed for the stated cause. `MAX_RETRY_AFTER_S` 20 s, `RETRY_DELAY_BUDGET_S` bounding total sleep per `retry_structured` call, and the Investigator short-circuit means a rate-limited run is one call, ~20 s. One residual, below. |
| `review-2` 2 | medium | Closed. The whole serialised body passes through the `Redactor`, which closes the citation/observation path *and* the "next raw-content field reopens it" class. |
| `review-2` 3 | medium | Closed on both entry points — the `uvicorn` lifespan and the Space's hand-call. |
| `review-2` 5 | low | Closed. `version: 1` names one prompt again. |
| `review` 5 | medium | Closed. `RequestValidationError`, `StarletteHTTPException` and a catch-all `Exception` handler all return RFC 9457. The 422 is built from `loc`/`msg` only and never echoes the submitted input. |
| `review` 6 | medium | Closed. `problem()` scrubs its whole body through the same `Redactor` instance `_serialize_run_outcome` uses, so PLAN.md:1556 is now descriptive rather than aspirational. |

## Open — none

**Count of record: 0.** All 13 findings open entering the urgent bundle were closed, as
were the 5 the second independent audit surfaced and the 6 the third did — 24 in total
across the phase. What remains below the line are residuals and hazards, which are real
but are not findings against a stated contract, plus the audit-provenance gap above.

> Correction, in the spirit of `87cb276`: commit `830332e`'s message says "Open backlog is
> now 9: one medium and eight lows." That is wrong — it double-counted; the real number at
> that commit was 7. The commit message is left as written rather than rewritten, the same
> way `review-2.md`'s own miscounted verdict line was left and corrected here instead.

## Closed by `741a292` — the seven that would have hindered later phases

These were the standing backlog at `d5a6d21`, triaged by forward-coupling rather than by
severity: each one was closed because leaving it would have cost more in a later phase than
fixing it cost here. `d5a6d21` is the last tree on which they were open, which is why the
`phase-1-green` tag does not stay there.

| # | Source | Was | Closed by |
|---|---|---|---|
| 4 | `review.md` | medium | `_diagnosis_schema()` strips `final_confidence` / `confidence_adjustments` from the wire schema, so the model is never asked for the two fields A.11 marks harness-added. The overwrite still runs; the difference is that a second consumer can now trust `diagnosis.final_confidence` without going through it. **Phase 2 depends on this** — the policy thresholds read that field directly. |
| 4 | `review-2.md` | low | `AdditionalToolCallOutcome` + `FailureBundle.additional_tool_outcomes` make optional tool calls observable per call (`obtained` / `refused` / `failed`), rather than `executed` being populated and never read. Transcribed into Appendix A. |
| 7 | `review.md` | low | `orchestrator.py` writes `ATTR_DEGRADED_COMPONENT`, the same constant `observability.py` reads, so the run view and the trace view no longer disagree about which components degraded. |
| 8 | `review.md` | low | `minLength` / `maxLength` added to `_ALLOWED_SCHEMA_KEYS` and the copy loop, so a `Field(max_length=…)` is visible to the model instead of being enforced only by Pydantic after the fact. `format` was removed in the same pass, aligning the allow-list to PLAN.md:170. |
| 9 | `review.md` | low | The empty-response half of B.1's safety row is terminal, via the `NO_CANDIDATES` sentinel. The finding conflated two conditions that `llm.py` now distinguishes: no candidates at all (terminal) versus a candidate that stated no reason (`UNKNOWN`, still retryable — an omission is not a refusal). |
| 11 | `review.md` | low | `PriorHistory` fails closed at `999` on the degraded path per B.3. Tightened again by final-audit finding 2 above, which found the first version's escape hatch fell open on the very shape it was written for. |
| 12 | `review.md` | low | `gateway_replay.py`'s `forbidden` is a required keyword argument, so the authoritative safety re-check can no longer be skipped by omission at construction. **This is the pattern `GitHubToolGateway` must follow in Phase 2** — that gateway is the one where the re-check actually stops something. |

## Closed by the final audit round (`741a292..862e8e0`, fixed at HEAD)

The third independent `phase-reviewer` pass of the phase. Six findings, all fixed; five
cleared suspicions are recorded below because a cleared one is worth as much as a finding.

| # | Was | Now |
|---|---|---|
| final 1 | medium | Closed by **reverting**, not by extending. `PROHIBITED_CONTENT` and `SPII` are out of `_TERMINAL_FINISH_REASONS` again. `finish_reason` is read off `candidates[0]`, so it says why *generation* stopped — a verdict on the sample, not on the prompt. A prompt-level block arrives as empty candidates and `NO_CANDIDATES` already covers it. The module's own admitting test says anything arguably re-samplable stays out, and these are. They were also scope creep: `review.md` finding 9 asked only for the no-candidates half. B.1 reverted to match. |
| final 2 | low-medium | Closed. `unavailable=True` now implies `retries_in_24h == 999` **unconditionally**; the `model_fields_set` escape hatch is gone, along with the four tests that pinned it. See the note below — this was a fail-open wearing a test as a disguise. |
| final 3 | low | Closed. The cap moved out of the 422 handler and into `problem()`, *after* the `Redactor` pass. Truncating first defeats scrubbing: a credential straddling the cut stops matching `SECRET_PATTERNS` and its prefix is served. |
| final 5 | low | Closed. `_MAX_DETAIL_LENGTH` is documented as characters, which is what `len()` and slicing count. |
| final 6 | low | Closed. The "... and N more error(s)" note travels as `problem(detail_suffix=...)` and is appended after the cap, so the bound cannot eat the count that announces the elision. |
| final 7 | low, latent | Closed. `problem()` drops any `Content-Type` from forwarded headers — Starlette's `init_headers` lets one displace `media_type`, which would have silently broken A.12's media type the first time Phase 2 raised an `HTTPException` with headers. |

Cleared, not findings: the cap cannot split a multi-byte sequence (it slices a `str` by code
point); no request-derived value reaches an unscrubbed header on any path in this build;
`_RUN_ID_PATTERN` rejects no legitimate id (its class is character-for-character
`orchestrator._CROCKFORD`, and 20 000 `new_run_id()` mints passed); `RETRY_DELAY_BUDGET_S`
is a true ceiling, returning before the sleep rather than overshooting by one; and
`AdditionalToolCallOutcome`, the `format` removal, `ATTR_DEGRADED_COMPONENT` and
`_diagnosis_schema` all match their contracts field for field.

> **On finding 2, because the shape is worth remembering.** The fail-open was not just in
> the code, it was pinned by a passing test —
> `test_unavailable_with_an_explicit_zero_survives_untouched` asserted that
> `PriorHistory(unavailable=True, retries_in_24h=0)` keeps the `0`. It read as
> thoughtfulness ("a caller who explicitly means zero must not be overwritten") and it was
> exactly B.3's fail-open with a docstring in front of it. A caller cannot both know the
> count and declare the history unreadable; there is no legitimate reading under which the
> supplied value wins. A green suite is not evidence when the assertion itself encodes the
> defect.

> **On the new hazard the finding-3 fix introduces.** Cutting after scrubbing means the cut
> can now land inside `***REDACTED***`, and `***RED` in a served body reads as content, not
> as an elision. `_bound_detail` drops a trailing partial marker for that reason. This is
> the same pattern as every other round in this phase: the fix is right and it moves the
> hazard rather than removing it. The trimming branch is exercised — the test sweeps the
> cut across 13 offsets and three of them split a marker.

### Audit provenance — one gap, and one round verified by the coordinator

Everything up to `dcf480f` was audited twice by `phase-reviewer`, independently, offline.
**The findings 5 and 6 delta (`dcf480f..830332e`) was not.** That agent hit a session rate
limit with a multi-hour reset, and rather than stall the tag the coordinator ran the five
adversarial checks the reviewer had been briefed to run, and recorded the results:

- `problem()`'s new `get_app_context()` call cannot create a nested-failure path — the
  accessor is `@lru_cache`d and warmed at startup on both entry points (`main.py`'s
  lifespan and `app.py:203`), and the only construction failure available to it is invalid
  `Settings`, which `main.py:59` raises on at import.
- The catch-all masks nothing: `ServerErrorMiddleware` re-raises after the handler runs, so
  `logger.exception` is a second record rather than the only one, and `CancelledError` is
  not an `Exception` subclass.
- `request.state` is per-request (`scope.setdefault("state", {})`, and this lifespan yields
  no shared state), so no cross-request bleed; a pre-mint failure yields a body with no
  `run_id` rather than an `AttributeError` inside the handler.
- The `Redactor` pass on `problem()` carries the same accepted `MIN_REDACTABLE_SECRET_LEN`
  residual already documented for `_serialize_run_outcome` — a second surface, not a new
  class.
- `POST /v1/runs`'s fire-and-forget `_execute` task is unchanged by the delta and still
  fails invisibly after the 202. Pre-existing; no HTTP handler could have caught it.

This is a coordinator self-check, not an independent audit, and it is weaker evidence than
the three independent passes. An independent `phase-reviewer` pass over that delta is still
owed and is cheap to run; it is the first thing to spend on if anything in the error paths
misbehaves.

**The final fix round itself (`862e8e0..HEAD`) is likewise coordinator-verified, not
audited** — the same regress the phase kept hitting, stopped deliberately rather than
resolved. It is better evidenced than the earlier self-check, and the difference is worth
stating so a later reader can weigh it: each of the six fixes carries a test, and each test
was shown non-vacuous by reproducing the defect it guards against under the *old* code, not
merely by passing under the new. The straddle test was confirmed to leak
`ghp_AAAAAAAAAAAAAAAA` when the cut runs before the scrub; the marker sweep was confirmed to
split a marker at three of its thirteen offsets. That is a real check, and it is still the
author checking their own work.
### Residuals and hazards — real, but not findings against a stated contract

- **The retry bound is on sleeps, not on call durations.** `gemini_timeout_s = 60.0` with a
  hard ceiling of 9 attempts means a provider that fails *slowly* rather than fast can
  still hold a request for minutes. This was never what `review-2.md` finding 1 was about —
  a 429 returns fast, so it is not the daily end state — but the broader "a public
  unauthenticated URL can hold a connection" concern is only partly retired. The honest
  bound today is on the 429 path specifically.
- **A rate-limited run now serves `final == {}`.** Ending the run on upstream failure is
  correct and is what the high finding asked for, but the collected `FailureBundle` and the
  `investigator_notes` degraded entry no longer reach the served `RunOutcome`;
  `AgentResult.evidence` is read by nothing downstream. With a 20-request/day quota this is
  the demo's normal daily output. Adjacent to `review-2.md` finding 4; the fix is a bundle
  field, not an adjustment. The misleading comment that obscured this was corrected.
- **~~`pyproject.toml` declares `"mypy"` unpinned~~** — closed in `862e8e0` with a
  `mypy>=1.18.2` floor on the dev group. The hazard was real and cost this session an hour:
  a system mypy 1.14.1 reported an `arg-type` false positive at `orchestrator.py:312` that
  the pinned 2.3.1 does not, and the coordinator reported the gate claim as unreproducible
  before harness-core established which toolchain was authoritative. `uv.lock` remains the
  pin of record (2.3.1) and the `Dockerfile` still uses `uv sync --frozen`; the floor only
  stops an independent resolution landing below the version the gate was verified on.
- **One test skips because `gradio` is deliberately absent from the lockfile.**
  `test_app_py_main_hand_calls_validate_prompt_templates_before_launch` is the only thing
  pinning that `app.py`'s `main()` performs the template hand-call before `demo.launch()` —
  i.e. the assertion that closes the mount-lifespan finding. It is guarded by
  `pytest.importorskip("gradio")` and was verified non-vacuous under an ephemeral
  `uv run --with gradio` overlay, which did not touch the project venv or lockfile. Running
  it unconditionally means adding `gradio`/`spaces` to the dev group, which trades away the
  isolation `requirements.txt` argues for ("gradio … is absent on purpose — the Space
  installs it itself"). A real trade, owned by whoever owns `pyproject.toml`; deliberately
  not decided inside a bug-fix round.
- **The 20 s wall-clock test** in `tests/unit/test_retry_delay_budget.py` takes the suite
  from ~6 s to ~28 s. It buys real-time proof against the shipped constants, which the
  mocked tests beside it cannot. The same property survives scaling both constants to 0.5 s
  via monkeypatch — the shipped values stay pinned by an assertion the test already carries.
  Revisit the first time someone skips the suite because of it.

## Settled — do not reopen

Carried from `handoff-fixround2.md` §6, still binding:

- The `prompt.render` span ships as-is; `AgentPrompt.prompt_version` is harness-core's call,
  queued behind Phase 2. Clean-run span count is **7**, not 5.
- The dormant prompt clauses land with Phase 3, worded around a real prior, together with
  `review.md` finding 11.
- A refused write-tool request carries no confidence signal in either direction. No PLAN.md
  adjustment row covers it; inventing one would be scope creep.
- Header-before-body precedence for the retry delay is correct as built; the duplicated
  `EscalationReason` lists stay duplicated with their drift-guard test;
  `retry_after_seconds` stays public as a test seam.
- Verify step 4 (escalation-threshold override) was substituted with offline coverage and
  does not need re-running before the phase closes.

## Deploy state — closed

The Space was held on pre-fix code for most of this phase because it served an unredacted
~40 KB job log on a public unauthenticated URL. **That hold is lifted.**
<https://shakti-agent-harness.hf.space> now serves `82d79de` (the `phase-1-green` tree),
pushed as a clean fast-forward of 17 commits.

Verified live rather than assumed: `healthz` ok; the `real_regression` replay returns
`completed` / `real_regression` / `open_fix_pr` at confidence 0.98 with 4 citations and
nothing degraded; the trace returns 8 spans, which is the assertion that catches a mounted
sub-app silently losing its lifespan; a malformed body returns `422` as
`application/problem+json`. **The served body is 5,830 bytes against ~40 KB before,** its
largest single string is 626 characters of model prose, and it carries no credential-shaped
match. The eighth span is a retried `llm.attempt` — Recovery on the live target, not a
structural change to the clean-run count of 7.

What is *not* closed, and is deliberately a standing decision rather than a finding: there
is no authentication on any endpoint, and the free tier's 20 requests/day can be exhausted
by a stranger in about ten requests. `HARNESS_DRY_RUN=true` and `HARNESS_GATEWAY=replay`
keep the blast radius to quota.

## Constraints that outlive this phase

- **Live Gemini quota is 20 requests/day, free tier.** One replay = 2. Both the fix-round
  re-audit and the urgent-bundle re-audit reached their verdicts spending none.
- **`git add` new files explicitly and check `git diff --cached --name-status`** before
  committing anything that moves a module to a data file. A `git commit -a` nearly shipped
  a broken Space when the `prompts/*.md` files were untracked beside deleted `.py` ones.
- **`app.py` must not read `os.environ`** — `tests/unit/test_no_env_access.py` scans it.
- **A mounted sub-app gets no lifespan events.** `demo.app.mount("/", api)` means anything
  that must happen at Space startup needs a hand-call in `app.py:main()`, not just a
  lifespan hook. This has now caught two separate pieces of startup work; assume it will
  catch the third.
- **The Space is pinned to `zero-a10g`** and cannot leave it. See `docs/deploy-huggingface.md` §2.
- **Never print or pass the HF token on a command line.**
