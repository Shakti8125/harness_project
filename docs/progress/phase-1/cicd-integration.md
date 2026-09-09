## Summary

Comment-only fix round. The re-audit of `fe7ba2d` (the `_UPSTREAM_ERROR_KINDS`
short-circuit, aligned `prior_history_summary`, restored backticks in
`prompts/diagnostician.md`, `validate_prompt_templates()`) found the logic correct and
asked for none of it to be redone. The one open item was that the comment justifying the
early return in `Investigator.run` (and the matching paragraph in the module docstring)
claimed `evidence` "already carries everything the deterministic collection gathered"
because it "reached `notes_result` through `AgentPrompt.evidence`" — true of the
`AgentResult` object in isolation, false of anything a caller of the harness can actually
read back.

Traced the claim against the real call path: `Orchestrator.run` (`orchestrator.py:310-311`)
files `result.output` into `state.artifacts` only when `result.status == "ok"` **and**
`result.output is not None`. On the upstream-unreachable path `output=None` and `status`
is `"escalate"`/`"timeout"` (from `_STATUS_FOR_ERROR_KIND`), so that `if` is never taken —
nothing is filed. `StageRecord` (`contracts.py:82-92`) has no evidence field; its `summary`
is `result.error.message`, a one-line string. `RunOutcome` (`contracts.py:108-124`) has no
evidence field either; `final` is populated from `state.artifacts` elsewhere in the
orchestrator, which was never written to on this path. `AgentResult.evidence` itself
(`contracts.py:73`) is simply never read by anything downstream of `Investigator.run` — no
grep hit outside `AgentResult` construction sites and test fixtures. So the deterministic
collection (job ref, log digest, diff summary, dependency changes) and the
`investigator_notes` degraded-component entry genuinely do not reach the served
`RunOutcome` on a rate-limited/unreachable-provider run; only the escalation reason and the
stage summary do. This matches the measured behaviour reported in the brief (`final keys:
{}`).

Rewrote the inline comment at the early-return site and the module docstring's matching
paragraph to say this precisely: `status`/`error` are correct and preserved; `output=None`
is what ends the run; `evidence` is copied onto the returned `AgentResult` but is inert —
nothing downstream reads it, so setting it doesn't preserve anything a caller sees; the
`FailureBundle` and the `investigator_notes` degraded entry are lost. No code changed —
verified via `git diff` that the diff touches only comment/docstring lines.

The trade itself (ending the run on an unreachable provider rather than letting the
Diagnostician retry into the same wall) is correct and out of scope to revisit. The
information-loss defect this comment now names accurately is a real gap but is explicitly
out of scope for this round per the brief — it's adjacent to `review-2.md` finding 4 and is
recorded below as a finding, not built.

## Files written

- `src/integrations/cicd/agents/investigator.py` — comment-only edit, two spots:
  - Module docstring (lines ~24-35): the paragraph describing the upstream-unreachable
    early return no longer claims evidence survives; it now states what `Orchestrator.run`
    actually files (nothing, because `output=None`) and names exactly what is lost
    (`FailureBundle` fields, the `investigator_notes` degraded entry) versus what survives
    (escalation reason, `StageRecord.summary`).
  - `Investigator.run`'s early-return branch (lines ~530-546): same correction, phrased
    against the concrete call — `AgentResult.evidence` is "copied over for completeness
    but nothing downstream reads it."

No other file touched.

## Contract deviations

None. No models, field names, Literal members, or behaviour changed — comment/docstring
text only.

## Prompt changes

None. No `.md` prompt files touched this round.

## Commands run

| Command | Result |
|---|---|
| `uv run ruff check src/` | All checks passed! |
| `uv run mypy src/` | 10 pre-existing errors in 4 files, none on lines touched by this diff: `gateway_replay.py:71,235,238` (JsonValue narrowing), `investigator.py:318` / `diagnostician.py:86` (`**dict[str, float]` unpacking into `LLMAgent.__init__`'s positional/keyword params), `wiring.py:81` (missing return annotation). Confirmed via `git diff` that none of the changed lines (24-35, 530-546-ish) appear in this list — the diff is comment-only and mypy is a no-op on it. |
| `uv run pytest tests/test_layering.py -q` | 71 passed |
| `git diff -- src/integrations/cicd/agents/investigator.py` | Confirmed comment/docstring-only diff (pasted and reviewed line-by-line before finishing) |
| `git status --short` | Only `src/integrations/cicd/agents/investigator.py` modified by me; `app.py` shows modified too but I did not touch it — it belongs to a parallel agent's in-flight work in the shared tree and was left alone |

## Handoffs

- **harness-core**: none required for this round.
- **fixtures-eval**: none for this round.
- **test-verifier**: no test changes needed for a comment-only fix; nothing to hand off.

## Notes for the reviewer

- **Finding, not fixed** (explicitly out of scope per this round's brief): the information
  loss itself — a rate-limited run (the normal daily shape under a 20-request/day quota)
  serves a `RunOutcome` with `final == {}` and no trace of what was collected before the
  provider became unreachable. The comment now says this accurately, but the underlying gap
  is real: there's no field on `StageRecord` or `RunOutcome` for a required stage's partial
  work product when the stage's own output is legitimately `None`. This is a
  harness-contract question (whether `StageRecord` or `RunOutcome` should grow a field for
  it, or whether the escalation record's `message` should be enriched from the bundle
  before the `AgentResult` is built) adjacent to `review-2.md` finding 4, which the brief
  says is already open — did not duplicate it, did not build a fix for it, flagging here per
  the brief's explicit instruction to report it as a finding rather than act on it.
- Double-checked the `invalid_output` path (module docstring's first bullet, lines ~15-22)
  is unaffected by this change and remains accurate as written: that path returns a
  non-`None` `FailureBundle` with `status` still whatever `super().run` produced for a
  content-level failure (not `"escalate"`), so `output is not None` and `state.artifacts`
  *does* get written on that branch — the false-claim problem was specific to the
  upstream-unreachable branch, not this one, and I left this paragraph as-is.
