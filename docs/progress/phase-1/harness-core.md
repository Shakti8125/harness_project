# harness-core — Wave 3 fix round

Finding 2 from `review.md`, plus the user's `llm_upstream` ruling. Written up by the
orchestrating session from the agent's returned report: `docs/**` was outside the write
territory authorised for that agent this round, so it returned the record as text rather
than writing it. The other two agents wrote their own.

## What changed

| Site | Change |
|---|---|
| `src/harness/llm.py` | `MAX_RETRY_AFTER_S`, `_RETRY_DELAY_KEYS`, `_MAX_PAYLOAD_DEPTH`, `_duration_to_seconds`, `_retry_delay_from_payload`, `_retry_delay_from_headers`, public `retry_after_seconds(exc)`. The 429 and 5xx branches of `classify_provider_error` now pass `retry_after_s=retry_after_seconds(exc)`. |
| `src/harness/contracts.py:99-101` | `"llm_upstream"` added to `EscalationRecord.reason`, after `"llm_timeout"` |
| `src/harness/orchestrator.py:52-59` | same member added to the parallel `EscalationReason` alias, plus a must-not-drift comment |
| `src/harness/orchestrator.py:79` | `_OUTCOME_FOR_ERROR_KIND["llm_upstream"]`: `("escalated", "tool_failure")` → `("escalated", "llm_upstream")` |
| `PLAN.md:1017-1019` | the one authorised Appendix A `Literal` edit. Verified: 2 lines out, 2 in, entirely inside the `Literal`; B.1 line 1576 untouched |
| `src/harness/orchestrator.py:61` | no edit needed — `_ESCALATION_REASONS` derives from `get_args(EscalationReason)` and picked the member up automatically |
| `src/harness/orchestrator.py:308` | left alone. All seven `AgentError.kind` members are explicitly mapped, so the default only guards a future kind |

Nothing outside `src/harness/**` enumerates `EscalationRecord.reason`, so the widening
blocked no other agent. Checked: `src/api/main.py` serialises `RunOutcome` wholesale;
`wiring.py:91-121` produces `escalate_as` as `str` and emits only four values; `app.py:75`
is display-only; no SQL migration exists yet to carry a `CHECK` constraint.

## The `google-genai` 429 delay shape

The delay is **not** an HTTP header in the free-tier case. It is in the JSON error body, as
a `google.rpc.RetryInfo` entry in the typed `error.details` list, valued as a protobuf-JSON
duration string:

```json
{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
  {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [...]},
  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}}
```

Confirmed locally, no quota spent and no network:

1. `google/genai/errors.py` — `APIError.__init__` sets `self.details` to the whole decoded
   body. `raise_for_response` uses the full envelope (`{"error": {...}}`) for an httpx
   transport but the **inner** error object for the replay transport. Two shapes, which is
   why the implementation searches by key name rather than a fixed path. `.response` is the
   transport's response, so headers are reachable via `exc.response.headers`.
2. The SDK's own `tests/errors/test_api_error.py:31-100` asserts `error.details == {"error":
   {...}}`, pinning the envelope shape.
3. `google/genai/_gaos/utils/retries.py:144-183` ships parsers for both `retry-after`
   (delta-seconds and HTTP-date) and `retry-after-ms` — direct evidence those headers occur
   on Google endpoints. Different stack from `models.generate_content`, so it does not cover
   our case, but it settled the header spellings.
4. This repo's own evidence that the 429 body carries a typed `details` list:
   `verify.md`'s recorded refusal quotes `quotaId` / `limit` / `model`, which is
   `QuotaFailure.violations[]` — a sibling of `RetryInfo` in the same list. Span
   `sp_8e99c0a70409e31d` in `data/harness.db` is the real 429 the reviewer describes.
5. A real `google.genai.errors.ClientError(429, body, httpx.Response(...))` was then built
   from that body and run against the new code: `41.0` for both shapes.

Deliberately **not** honoured: `retry-after-ms`, because B.1 does not name it and the SDK's
own usages carry sub-second values (their tests use `"1"`, i.e. 1 ms) — obeying it would
reproduce the very "four attempts in under four seconds" defect this fixes. Also not
traversed: proto / `proto-plus` message objects, which a gRPC or Vertex client would deliver
instead of a dict. The composition root uses the REST `GeminiClient`; that is a stated limit
rather than a guess coded to an unverified shape.

## The bound

`MAX_RETRY_AFTER_S: Final[float] = 60.0`, applied inside `_duration_to_seconds` so every
path — header, body, HTTP-date — is bounded at one point.

- **Why 60**: it mirrors the only cap PLAN.md states anywhere for honouring a
  provider-supplied delay, Appendix B.2's "sleep to `x-ratelimit-reset`, capped at 60 s",
  and matches the per-call patience the numbers table already grants one request.
- **Clamped, not rejected.** Asked for an hour, we wait a minute. Discarding an over-large
  value would fall back to a `[0, 0.5]` jitter draw, which is strictly worse.
- **Rejected outright (→ `None` → jittered backoff):** non-numeric strings, wrong types,
  `bool` (an `int` subclass — `True` would otherwise parse as one second), negatives, zero
  (a zero sleep after being told to slow down is how a budget disappears in milliseconds),
  and non-finite values (`"nans"` / `"infs"` parse as float but fail `math.isfinite`).
  Payload recursion is depth-capped at 6 so a cyclic body terminates. The entry point is
  wrapped in `except Exception` returning `None`: raising while classifying an exception
  would turn a recoverable rate limit into a crash.

## Open questions the agent raised rather than deciding

1. **Honouring a delay makes a rate-limited run wall-clock slow, and nothing bounds it.**
   See the standalone note in `review.md`'s successor round — this is the significant one.
2. **Header-before-body precedence is a judgement call.** B.1 names `Retry-After`, so an
   explicit header wins and the body is the fallback (and is what actually answers today).
   If the two ever disagree, we take the header rather than the larger.
3. **The clamp is silent.** `"3600s"` becomes a 60 s sleep with no log. `classify_provider_error`
   is a pure function on the hot error path, so no warning was added; a one-line
   `logger.info` on clamp would make "we ignored the provider" visible in a postmortem.
4. **`retry_after_seconds` is public on purpose** — a seam for `test-verifier` that avoids
   constructing a full transport error path. Not part of Appendix A; free to rename now.
5. **The two `EscalationReason` lists remain duplicated.** The orchestrator alias must stay a
   static `Literal` for mypy to check `_OUTCOME_FOR_ERROR_KIND`; deriving it at runtime from
   the model annotation would launder the types through `Any`. The dangerous drift direction
   (alias wider than contract) is a `ValidationError` at escalation time, which is why the
   drift guard test matters.

## Verification

`uv run ruff check src/harness` clean; `uv run mypy --strict src/harness` clean;
`tests/test_layering.py` 71 passed. Two offline reproduction scripts, all-PASS, zero live
requests: 24 assertions for the retry-delay extraction, 7 for `llm_upstream`. Recipes handed
to `test-verifier` rather than written as tests.

---

# harness-core — Phase 1 backlog round

Recorded by the coordinator, not by the agent: `harness-core`'s exclusive write glob is
`src/harness/**`, so it returned this as text rather than writing outside its territory.
That was the correct call and the record belongs here rather than being lost.

Closes `review.md` findings 7, 8 and 9, plus one coordinator ruling.

## What changed

| Finding | Change | File |
|---|---|---|
| 7 | The run span writes `ATTR_DEGRADED_COMPONENT` instead of the literal `"degraded"`, so `GET /v1/runs/{id}` and `.../trace` read the same key. | `orchestrator.py` |
| 8 | `minLength`/`maxLength` added to `_ALLOWED_SCHEMA_KEYS` and the copy loop, so a `Field(max_length=…)` is a rule the model is shown, not only one it is graded against. | `llm.py` |
| 9 | `NO_CANDIDATE_FINISH_REASON` (`"NO_CANDIDATES"`) and `UNSTATED_FINISH_REASON` (`"UNKNOWN"`) split; only the former joins `_TERMINAL_FINISH_REASONS`. | `llm.py`, `recovery.py` |
| — | `format` removed from `_ALLOWED_SCHEMA_KEYS` and the copy loop, aligning code to PLAN.md:170. | `llm.py` |

## Why `degraded_component` won rather than `degraded`

Nothing anywhere read the `"degraded"` spelling. `ATTR_DEGRADED_COMPONENT` already had one
reader (`observability.py:457`) and one established writer (`agent.py:213`); the literal had
one stray writer. Changing the reader would have meant editing a constant plus a working
writer to accommodate the stray. Double counting was checked: the run span's list is a
superset of the per-stage writes, and `read_trace` de-duplicates, so a component reported by
both appears once — unlike token totals, which are filtered to `component == "llm"`
precisely because they *would* double.

## Why the finish-reason sentinel was split rather than broadened

`"UNKNOWN"` was overloaded: it meant both "no candidate at all" and "a candidate that stated
no finish reason". Only the first is the deterministic, non-retryable condition Appendix B.1's
safety row describes. Making the whole bucket terminal would have turned a genuinely transient
condition non-retryable — a worse bug than the one being fixed. An empty response now costs 1
provider call instead of 3, against a 20-requests/day quota; a candidate that omits the field
still gets its 3 attempts.

`"NO_CANDIDATES"` is our word, not the provider's. If a future SDK introduces a real finish
reason with that spelling it would inherit terminal treatment — which is the behaviour we want
anyway, so the collision is benign. Recorded because it is a coincidence rather than a design.

## The `format` ruling — coordinator's call, not the agent's

The agent surfaced a PLAN.md divergence rather than resolving it, per the standing instruction
added in `0ffeef1`: PLAN.md:170 lists `format` in the strip list, but `_ALLOWED_SCHEMA_KEYS`
had carried it since the original build.

Ruled: strip it. No current schema emits `format` (`to_gemini_schema` over `Diagnosis` and
`InvestigationNotes` yields zero occurrences), so the change is zero-risk today and closes a
latent trap — the first Phase 2 field typed `datetime`, `UUID` or `HttpUrl` would otherwise
pass a Pydantic-emitted `format` through to the provider, producing exactly the opaque 400 the
set's own docstring warns about. Code catching up to the plan, so no PLAN.md amendment was
owed.

## One consequence worth knowing before Phase 2

`HttpUrl` carries `minLength: 1, maxLength: 2083` in Pydantic's JSON schema. With finding 8's
change, a Phase 2 URL field now ships those bounds to the provider where the node used to be
bare. That is correct — they are genuine string bounds — but the blast radius is wider than
the hand-written `Field(max_length=…)` declarations that motivated the fix. Nothing in the
current tree is affected.

## Verification

`uv run ruff check src/` clean. `uv run mypy --strict src/harness` clean on both the pinned
2.3.1 and bare-PATH 1.14.1. The dialect's support for the string bounds was confirmed against
the pinned SDK (`google_genai 2.22.0`, `google/genai/types.py:2959` and `:2975`) rather than
from memory. Reproduction recipes were handed to `test-verifier`, which wrote the durable
tests; no tests were written by this agent.

---

# harness-core — re-audit finding 2 (terminal finish reasons)

Recorded by the coordinator. The agent flagged that three instructions in its brief conflict
on whether it should write this file: its territory rule (`src/harness/**` only), its
standing instruction to return findings as text, and the report-back section asking for a
docs file. **The territory rule wins and the coordinator records it** — that is the standing
resolution, so the conflict does not need re-litigating each round.

## What changed

`_TERMINAL_FINISH_REASONS` (`src/harness/recovery.py`) now holds `SAFETY`, `RECITATION`,
`BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII` and the `NO_CANDIDATES` sentinel.

## The finding's arithmetic was wrong, and the fix is better for it

The audit said "4 of the pinned SDK's 8 deterministic-refusal reasons". The `FinishReason`
enum in `google_genai 2.22.0` (`types.py:488-529`) has **18** members, and the audit's
candidate list omitted seven that a consistency argument reaches — `LANGUAGE`,
`UNEXPECTED_TOOL_CALL`, `TOO_MANY_TOOL_CALLS`, `IMAGE_PROHIBITED_CONTENT`, `IMAGE_RECITATION`,
`IMAGE_OTHER`, `NO_IMAGE`. The agent read the enum rather than the finding and ruled on all
18, which is what makes the set defensibly *closed* rather than merely larger.

## The admitting test

*Is the refusal a function of the content we sent, such that asking again cannot change the
answer?*

**Admitted:** `PROHIBITED_CONTENT` and `SPII` — both are verdicts on the input, and the repair
instruction this loop appends cannot un-say what the evidence already contains, so all three
attempts draw the identical refusal. `SPII` is the realistic daily case for this application:
the input is job logs, and a log carrying an email address or a pasted credential is ordinary.
One call instead of three, against a 20/day quota.

**Declined, with reasons:** `MALFORMED_FUNCTION_CALL` and the tool-call family describe a
generated artefact that came out wrong, not a refusal to generate — a re-sample is the remedy
and is already what the loop applies. The image family is unreachable (this loop asks for text
validating against a JSON schema and never requests an image modality) and would be
re-samplable anyway. `LANGUAGE` is ambiguous in the SDK's own gloss, and ambiguity resolves
towards retrying. `OTHER` and `FINISH_REASON_UNSPECIFIED` are catch-alls carrying no claim
about reproducibility.

## Where the agent said it is least certain

`MALFORMED_FUNCTION_CALL`. At `temperature=0.0` the same prompt plausibly produces the same
malformed call, which would make three attempts a waste. It came down on retrying because the
failure is a defective sample rather than a refusal, and because being wrong in that direction
costs three attempts while being wrong in the other costs a failed run. The client declares no
tools, so it should be unreachable today. Re-litigate with real observations if that changes.

## Recorded, not changed

The terminal branch's message is the generic "provider returned no usable candidate" for all
six reasons. It reads oddly for `SPII` — there *was* a candidate and it was refused — but the
specific reason is preserved in `detail.finish_reason`, which is what B.1 requires be recorded
and what escalation reads. Changing the message text is contract-adjacent and was left to the
coordinator.

## Plan amendment made

PLAN.md B.1's safety row named only `SAFETY`. The implemented set was already wider than that
row *before* this round, so the row is now updated to name the five provider values and to
point at `recovery._TERMINAL_FINISH_REASONS` for the admitting test and the exclusions —
pointing at the constant rather than restating the list, so the row does not go stale the next
time the SDK pin moves.

## Verification

`uv run ruff check src/` clean; `uv run mypy --strict src/harness` clean; `tests/test_layering.py`
71 passed; full suite 361 passed, 1 skipped at the time of the change. Recipes handed to
test-verifier, which owns the durable tests — including the load-bearing half, which pins the
*exclusions* so a future widening cannot quietly make a re-samplable condition terminal.
