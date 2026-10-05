# Agent Harness — timeline and interview story

Source: the 50 commits on `main` (2026-09-10 → 2026-10-04), their messages, and the docs they touch
(`PLAN.md`, `README.md`, `docs/progress/**`, `docs/security/`). Numbers quoted below are the ones the
commit messages record.

> **One gap to know about before you tell this.** The first commit in the repo (`dcf480f`, 09-10) is
> already a 115-file, 24k-line snapshot, and its message refers to earlier SHAs (`fe7ba2d`, `c064e70`,
> `87cb276`, `0ffeef1`) that are not in this history. Phase 0 and the first half of Phase 1 happened
> before the history starts. Don't claim a day-by-day account of those; the `docs/progress/phase-0/` and
> `phase-1/` files are the only record of them.

---

## 1. Timeline

### Sep 10–11 — Phase 1: Investigator + Diagnostician, deployed. Three audits, then hardening
| Date | Commit | What happened |
|---|---|---|
| 09-10 | `dcf480f` | A re-audit found that prompt-template validation ran in FastAPI's lifespan, but the Hugging Face Space mounts the API as a sub-app, and Starlette `Mount` never fires lifespan events. A missing prompt file would have given a green `/healthz` and a 500 on first use. Fix: hand-call at the real entry point, comment at both sites. |
| 09-10 | `830332e` | RFC 9457 `problem+json` on every error path (zero exception handlers before). `problem()` scrubs its whole body through the Redactor, structurally rather than by call-site discipline. |
| 09-10 | `741a292` | Seven backlog findings closed because each would have hurt a later phase. Includes `PriorHistory` failing **open** (0 retries) where the spec says fail **closed** (999); fixed with a validator so no future caller can forget. |
| 09-10 | `3c3145c` | Independent audit of that fix: FIX FIRST. The round introduced a regression (405 lost its `Allow` header). |
| 09-10 | `862e8e0` | Floor-pinned mypy after a version skew (1.14.1 on PATH vs 2.3.1 locked) cost a round misdiagnosed as a cache problem, then as a code bug. |
| 09-11 | `d2f7c6e` | Third audit, six findings. One fix was **reverted** (a "terminal refusal" rule that was wrong about where `finish_reason` lives). One test had been asserting the bug. Credential-straddling-the-truncation-point bug: truncate-then-scrub leaked a token prefix; moved the cap after the scrub. |
| 09-11 | `82d79de`, `d096e1f`, `fb49e31` | Reconciled PLAN.md with the built tree (it said Fly.io 22 times; the real target was a Gradio Space). Deploy recorded with measured numbers. The gate documents contradicted the tag, so they were fixed by recording. |
| 09-11 | `85a168b` | Run-level wall clock (240 s, measured, in the orchestrator). Found that background tasks were held by weak references, with the lint warning silenced by `# noqa`. |

### Sep 11 — Phase 2: Remediator + guardrails (same day)
`074936d` PolicyEngine (forbidden → invariants → first match → default; a missing fact never satisfies a
clause), approval state machine, 13-tool catalog, GitHub gateway with dry-run. `039cca5` a 90-case deny
matrix and a forged-allow test with zero outbound calls recorded. `fc8209b` the **first live run** found
what stubs couldn't: the provider counts thinking tokens against `max_output_tokens`, so the Remediator
hit MAX_TOKENS and returned an empty tool call. `17fd4b5` / `88afccb` failed executions escalate; base64
file bodies (which the Redactor can't see through) are digested; audit found six issues, fixed.

### Sep 13 — Phase 3: Memory
`66208d0` `SqliteMemoryStore`, signature fingerprints, priors, idempotent run claims; 44 files, +7.9k.
`de1365d` the store redacts every JSON column at write time. `a644c90` audit returned ten findings, nine
closed (each with a test that fails on the previous commit), one carried open with reasoning. Suite:
**603 passed**.

### Sep 14 — Phase 4: the Evaluator
`dbc63ab` a fourth stage that turns every citation into a claim and verifies it **deterministically**
(`quote_exists` with a 0.92 fuzzy threshold, `file_in_diff`, `dependency_bump`, `test_in_log`,
`commit_in_range`). A refuted claim escalates and the Remediator never runs. Missing artifact =
*unverifiable*, never *refuted*. Also: escalation webhook, fault injection, output-budget growth.
`e580aad` audit: five findings, including httpx logging the webhook URL at INFO. `bb0abf3` the live
eval: **3/3**.

### Sep 14–17 — Phase 5: observability, real webhook, and the first contact with reality
| Date | Commit | What happened |
|---|---|---|
| 09-14 | `66e2448` | `/runs/{id}/view` trace page, HMAC-verified `POST /webhooks/github`, PR-writing tools, `tests/test_no_secret_leak.py` as a gate. 854 passed. |
| 09-14 | `9e3b4df` | Audit: nine findings, **two HIGH**. (1) The secret scrubber, run through base64 at rest, *rewrote source lines inside a stored approval*, and the approval route would have committed the corrupted file. (2) Appendix D's cold-start baseline chain wasn't implemented. |
| 09-15 | `2c79035` | The first live verify found a regression the 898-test suite had missed: my log redaction cleared `record.args`, and uvicorn's access formatter unpacks them, so every request threw a logging error. No test drove a formatter that reads args. |
| 09-15 | `3a0b1f6` | Live attempt #1 failed honestly: Gemini returned 503 on 18 of 19 attempts, **Google bills 503s against the 20/day free quota**, and my retry policy amplified it. Verified steps deferred to the next quota day. |
| 09-17 | `06c87db` | Phase 5 closed: live steps matched (real_regression 0.99 with 5 verified citations, fix PR held; flaky_test 0.97, retry executed dry-run; duplicate delivery → 1 run, no model call). Phase 6 explicitly **not started**. |

### Sep 30 – Oct 1 — Security assessment, before exposing anything live
A 4-auditor parallel security review (`docs/security/assessment-2026-09-30.md`): 25 findings, 0 secrets
leaked, but 3 critical-if-`dry_run=false`.
| Commit | What it closed |
|---|---|
| `4e6261c` | Configurable retry policy for the free tier (a 503 is billed like a success), probe script, quota ledger. `src/harness/` untouched. |
| `5602f47` | SEC-01..09: quadratic PEM regex (one anonymous 62 KB request stalled the whole event loop), operator token on write routes, 1 MiB body cap, URL-segment validation (`../../` escape from the repo in a GitHub API URL), model can only write to `agent/fix/*`, bounded fingerprinting, admission control. |
| `a0a64a2` | Audit of *that* fix, four findings: anonymous replays could starve signed webhook deliveries (429); path-traversal-ish bugs in the replay gateway; `$` vs `fullmatch` trailing newline. |

### Oct 3–4 — Going live, and closing out
`1f32fd7` live runs never carried `default_branch`, so the baseline chain silently degraded; tests had
masked it with a too-generous mock. `430c627` **Step 5 passed on the Space**: a real failing GitHub
Actions run → GitHub webhook → 202 in 0.06 s → `flaky_test` 0.99, 3/3 citations verified, retry executed
dry-run; redelivery deduped. Ran on `gemini-3.5-flash-lite` because `gemini-3.6-flash` 503'd on 5 of 6
runs. `90cbb0e` every one of the 25 security findings gets a status. `6df5b9a` usage guide + re-pinned
architecture diagram.

**Scale at the end:** ~15.5k lines in `src/`, ~17.5k lines in `tests/`, 976 tests, 249 tracked files, six
planned phases, five completed.

---

## 2. The interview story

### The 30-second version
> I built a multi-agent control plane — orchestration, context budgeting, policy, memory, claim
> verification, tracing — as a **domain-agnostic library**, then plugged CI/CD failure triage into it as
> the first integration. A failed GitHub Actions run goes in; a diagnosis comes out where every citation
> has been *machine-verified* against the real log and diff, and the only actions it can take are ones a
> YAML policy allows. It's live on a Hugging Face Space. What I'd actually want you to take from it
> isn't the bot, it's the discipline: I built it in phases with a gate, an independent audit and a
> recorded verdict on each, and the audits kept finding real bugs, including in my own fixes.

### The two-minute version (STAR)

**Situation.** Everyone has a "LLM reads my logs" demo. The thing that's hard to demonstrate is the seam
between a reusable agent harness and a domain. I wanted that seam to be a *checkable claim*: a test
walks the AST of every file under `src/harness/` and fails the build if a CI-specific word appears.

**Task.** Ship something deployed and demoable, where the safety properties — no fabricated evidence, no
unauthorised write, no leaked secret — are enforced by code and tests rather than by prompting.

**Action.** Six vertical slices, each runnable on its own, each ended by the same gate: a test verifier
runs the plan's literal Verify block, an independent reviewer agent audits against the plan, and the
verdict is written to the repo. I played architect/coordinator over specialist agents with disjoint write
territories (harness core, CI/CD integration, API surface, fixtures/eval, test verifier, reviewer). The
design decisions I'd defend:
1. *Verify, don't trust:* citations are claims checked by deterministic code, not a second LLM.
2. *Confidence the model doesn't grade:* the model self-reports, code applies adjustments and clamps once.
3. *Policy is data, enforced twice:* YAML rules quoted into the trace, plus the gateway re-checks the
   forbidden set independently.
4. *Fail closed:* a missing fact never satisfies a policy clause; an unreadable history means "retried 999
   times".
5. *Context budgeting that can't eat the error:* anchors are inviolable, and a 50,000-line-log test pins it.

**Result.** Phase 5 shipped and is live: 5/5 category accuracy on the live model (one pass per scenario,
over two days, constrained by a 20-requests/day free tier), 0 refuted citations, 0 forbidden actions
executed, a real GitHub webhook handled end to end in 0.06 s with dedup, and a 976-test suite.

### Stories to have ready (pick the one the question calls for)

**"Tell me about a time a passing test suite lied to you."** Three of them, all in the log:
- `2c79035`: 898 green tests, and every live request still threw a logging error. My log scrubber
  cleared `record.args`; uvicorn's access formatter unpacks args positionally. No test drove a formatter
  that reads them. It took the first *live* run to find it.
- `d2f7c6e`: a test named `…explicit_zero_survives_untouched` was asserting a fail-open bug. It *read*
  as thoughtfulness. A passing assertion encoding the defect is not evidence.
- `1f32fd7`: a recorder test mocked the run endpoint with the *webhook's* richer payload, hiding that
  live subjects had no `default_branch`, so the baseline chain silently degraded.

**"A time your fix made things worse."** Every fix round in Phase 1 introduced a defect caught only by
the next independent audit: the `Allow` header regression, a "terminal finish reasons" fix that I then
*reverted* because it was wrong about the SDK's semantics, the base64 scrubber that would have committed
corrupted code. My process answer: each fix carries a test shown to fail on the old code, and the
stopping rule for auditing is argued (smaller rounds, better-evidenced), not assumed.

**"A security story."** I ran a 4-auditor parallel assessment *before* flipping the Space to live mode.
Found a quadratic regex that let one anonymous request stall the event loop, URL interpolation that could
turn `contents/../../../user` into `GET /repos/user`, and a write path where a model-chosen branch of
`main` would commit straight to the default branch under `dry_run=false`. I held the report out of the
public repo until fixed and deployed, then published it with a status per finding. Then I audited the fix
too, and it found that anonymous traffic could starve signed webhook deliveries.

**"Working against a real constraint."** The free Gemini tier is 20 requests/day and *counts 503s*. My
retry policy had been hammering it (4 retries in a second), burning the quota on a single failing replay.
Fix was a configurable retry profile plus a quota-ledger script reading span data; the live demo was
planned around it, with a stop rule that ended a whole day at 5 of 20 requests.

**"Plans vs. reality."** PLAN.md named Fly.io 22 times; Fly started requiring payment info, so the target
became a Gradio Space. That wasn't cosmetic: a Space has no persistent disk, which inverted a risk-register
entry and made Phase 3's "4th flaky run shows occurrences=3" unreachable on the live target. I amended the
plan, not just the vocabulary.

### Honest caveats (say them before they're asked)
- **AI-assisted build.** Commits are co-authored by Claude and agents had write territories. Your claim is
  architecture, specification, gating, review decisions, and verification, not typing every line. Be ready
  to explain any module cold; the audits are your evidence you understood the failure modes.
- **Phase 6 (second adapter) was never started.** That's the one that would make "the seam holds"
  falsifiable *by diff* rather than by an AST lint. Say so, and say it's the first thing you'd do next.
- **Eval is small.** 5 scenarios, one model pass each, over two days. The stub run (25/25) tests plumbing,
  not the model. One run of `dependency_break` filed a ticket where the label expected a held PR (right
  category, label miss, reported not hidden). Don't call it "accuracy at scale".
- **Live mode is demo-scoped:** one allowlisted repo, `dry_run=true`, fine-grained PAT, and two carried
  findings (SEC-10/11) that must be fixed before anyone turns dry run off.
- **Some audits were coordinator-verified, not independent** (e.g. the findings 5/6 delta in Phase 1,
  the Phase 2 and Stage 1e fix rounds). The repo records that explicitly; so should you.

### Likely follow-ups, with the short answer
- *Why not use an LLM to verify the LLM?* It moves the credulity one step; deterministic checks against the
  artifacts the harness collected are cheap, reproducible, and testable.
- *Why SQLite?* One file, WAL, migrations, redaction at write time; the Space has no persistent disk, so
  memory resets on restart (a documented limitation, not a surprise).
- *How do you know the harness is really domain-agnostic?* An AST test today; a second adapter (Phase 6)
  is the proper proof and is not built.
- *What would you change?* Make the audits cheaper so they run per PR; add a second adapter; fix SEC-10/11
  and add a real eval set with repeated sampling.
