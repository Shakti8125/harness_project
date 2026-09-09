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
