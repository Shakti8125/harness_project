## Summary

Fixed Wave-3 audit finding 3 (medium, flagged as this round's top priority — a live
exposure on the public HF Space): `RunOutcome.final.bundle.logs[].excerpt` — the entire
budgeted CI job log, tens of thousands of characters, unauthenticated, unredacted — was
being served verbatim in the JSON body of `POST /v1/replay/{scenario}` and
`GET /v1/runs/{run_id}`. Fixed at the HTTP serialisation boundary only, inside
`src/api/main.py`; `src/harness/orchestrator.py` (named in the finding as the field's
origin) was not touched, per the scope instruction, since `harness-core` is editing
`src/harness/**` concurrently in this wave.

**Follow-up (same task, coordinator-authorised extension):** the same route also carried
`final.bundle.diff.files[].patch` — raw repository diff hunks, same exposure class, same
route, same threat model, just numerically small (252 chars) on the one-file fixture the
first pass measured against. Extended the same treatment to it, still `src/api/**` only,
still not touching `src/integrations/cicd/schemas.py` (where `FileChange.patch` is
declared — `cicd-integration`'s tree this wave).

## Files written

- `D:\Documents\harness_project\src\api\main.py` — added `_digest_str_field()` and
  `_serialize_run_outcome()`, and routed both `RunOutcome`-serialising response sites
  through the latter.

No other file under my write territory needed a change for this fix.

## What changed, precisely

New helper, `_digest_str_field(container: dict, field: str) -> None`, which replaces
`container[field]` — a `str` — with two siblings, `{field}_length` (int) and
`{field}_sha256` (hex digest), and is a no-op when the field is absent or not a `str`.
That last clause is load-bearing: `FileChange.patch` is typed `str | None`, and a `None`
patch (GitHub omits it for binary or too-large files) must stay `None`, not become a
digest of the empty string — confirmed directly (see Commands run).

New helper, `_serialize_run_outcome(outcome: RunOutcome) -> dict[str, Any]`, defined
just above `problem()` in `src/api/main.py`. It calls the ordinary
`outcome.model_dump(mode="json")`, then for every top-level artifact in `final` (not
hardcoded to the key names `"bundle"`/`"diagnosis"`, since those are an integration-owned
mapping in `src/integrations/cicd/wiring.py`'s `ARTIFACT_KEYS`, not something
`src/api/**` should assume stays fixed):

- walks `artifact["logs"]`, if a list, and applies `_digest_str_field(log_entry,
  "excerpt")` to each `LogExcerpt`-shaped dict in it;
- walks `artifact["diff"]["files"]`, if present and a list, and applies
  `_digest_str_field(file_entry, "patch")` to each `FileChange`-shaped dict in it.

Everything else — `total_lines`/`included_lines`/`anchor_line_numbers`/`truncation` on
each log entry, `path`/`status`/`additions`/`deletions` on each file entry, and every
other field of `final` — is untouched.

Both call sites that previously did `outcome.model_dump(mode="json")` — the synchronous
branch of `POST /v1/replay/{scenario}` and `GET /v1/runs/{run_id}` — now call
`_serialize_run_outcome(outcome)` instead. `GET /v1/runs` (list) never carried `final` in
the first place (it returns a hand-built summary: `run_id`, `integration`, `status`,
`created_at`, `duration_ms`), so it was not and is not exposed. `GET /v1/runs/{run_id}/trace`
serialises a `TraceResponse`, not a `RunOutcome`; its span attributes are redacted at
write time by the existing `Redactor` (per `observability.py`'s
`attributes: dict[str, JsonValue] = {}  # redacted at write time`), which is the path the
audit already confirmed is covered — out of scope here and left alone.

`app.py` (the HF Space launcher) needed no change. Confirmed by reading it: it mounts
the real FastAPI `app` from `src/api/main.py` unchanged and calls it in-process through
`httpx.ASGITransport(app=api)` (`app.py:46-53`), then renders whatever JSON that route
returns, including in the "Raw RunOutcome" tab (`json.dumps(outcome, indent=2)`,
`app.py:65`, `:158`). It never calls `.model_dump()` itself. Fixing the route body fixes
the Space's raw-outcome view for free, exactly as the task predicted.

## Before / after size, measured on `real_regression`

Measured by running the replay orchestrator once per script (each is one live Gemini
call — the replay gateway replays recorded GitHub/log fixtures, but the LLM call itself
is live) and dumping the same `RunOutcome` object both ways, to isolate the serialisation
change from model non-determinism between separate runs.

Logs-only fix (first pass):

```
BEFORE (outcome.model_dump(mode="json"), old behaviour): 47,504 bytes
AFTER  (_serialize_run_outcome(outcome), new behaviour):   6,340 bytes
```

Logs + diff-patch fix (this follow-up, separate run — citation count and byte totals
differ slightly from the first pass purely because it is a fresh, independent Gemini
call):

```
BEFORE (outcome.model_dump(mode="json"), old behaviour): 47,346 bytes
AFTER  (_serialize_run_outcome(outcome), new behaviour):   6,010 bytes
```

`final.bundle.diff.files[0]` in the AFTER dict: `keys = ['additions', 'deletions',
'patch_length', 'patch_sha256', 'path', 'status']` — `patch` absent, `patch_length = 252`
(matching the coordinator's measurement exactly), `patch_sha256` present as a 64-hex
digest. `final.bundle.logs[0].excerpt_length = 40,638` — matching the audit's original
figure — with `excerpt` absent and `excerpt_sha256` present.

`None`-patch handling, confirmed directly (not inferred) with a standalone check of
`_digest_str_field`:

```
{'path': 'x', 'patch': None, 'status': 'added'}   -> unchanged: patch stays None,
                                                      no patch_length/patch_sha256 added
{'path': 'y', 'patch': 'hello world', ...}        -> patch removed, patch_length=11,
                                                      patch_sha256 added
{'path': 'z'}  (no patch key at all)              -> unchanged, no-op
```

`.status`, `.final.diagnosis.category` (`real_regression`), `.final.diagnosis.
suggested_action` (`open_fix_pr`), `.run_id`, `.trace_url` and `.stages` (3 entries) all
verified intact post-fix in a separate end-to-end run through the actual
`/v1/replay/real_regression` route (`httpx.ASGITransport` against `src.api.main.app`,
total response 5,503 bytes, from the first pass before the diff-patch extension; not
re-run a third time against the live route to conserve the model's daily quota, since the
in-process orchestrator runs above already exercise the identical `_serialize_run_outcome`
code path the route calls).

One observation unrelated to my change, worth flagging rather than silently treating as
a pass: the task's Verify-step-5 checklist expects `.final.diagnosis.citations` to have
5 entries; my runs (independent live Gemini calls, non-deterministic decoding) returned 4
citations with quotes intact each time. This is model output variance on the
Diagnostician's stage, not a serialisation regression — `_serialize_run_outcome` never
touches `final.diagnosis` at all, only `final.<artifact>.logs[]` and
`final.<artifact>.diff.files[]`. Flagging it because `test-verifier` should know the
citation count is not currently stable run-to-run against the live model, independent of
this fix.

## Reasoning: removal vs. truncation vs. length+digest

The task scoped the choice to exactly these three. Chose **length + sha256 digest**,
against the question "what does a legitimate API consumer lose":

- **Truncation** (e.g. first N chars) was rejected: it isn't actually a security fix,
  only a smaller one. A pasted credential can land anywhere in a 40k-character log, so a
  fixed-prefix truncation still leaks it whenever it happens to fall in the kept range,
  while giving a false impression of having been "handled." PLAN.md's own precedent for
  a similar case (B.2's malformed-body evidence) is "first 500 **redacted** chars" —
  i.e. truncation there is paired with a redaction pass, not a substitute for one; a bare
  truncation here would not clear that bar.
- **Outright removal** was rejected in favour of digest because it's strictly dominated:
  a digest costs two small fields and a `sha256` call, and in return gives a consumer who
  has independently obtained the real log (from the CI provider — the actual source of
  truth for CI output, not the harness) a way to verify byte-for-byte that this is the
  exact excerpt the harness reasoned over, without this process ever re-serving the
  content. That's the same content-addressed shape this codebase already uses for
  exactly this purpose (`Evidence.excerpt` + `Evidence.sha256` in
  `src/harness/contracts.py:36-37`, "sha256 of excerpt, normalized; used by the
  Evaluator"). Removal alone throws that capability away for no additional safety.
- What a legitimate consumer loses under length+digest: the ability to read the raw log
  text or diff patch directly off this endpoint. What they keep: `total_lines`,
  `included_lines`, `anchor_line_numbers` and the full `truncation` report on every log
  entry, `path`/`status`/`additions`/`deletions` on every file entry (already telling
  them exactly what changed and how much log was kept and where), plus the harness's
  actual designed evidence surface for a human or downstream system —
  `final.diagnosis.citations[].quote` (bounded at 500 chars, up to 6, produced
  deliberately by the Diagnostician) — untouched. Nothing else in `final` is touched.

## Known limitation of this fix (on the record, not discovered a third time)

This is a **field-name walk**: it matches the literal keys `"excerpt"` (inside
`artifact["logs"]`) and `"patch"` (inside `artifact["diff"]["files"]`). It is not
structural — nothing in the schema marks a field as "raw content," and nothing scrubs by
type or size. That was raised and deliberately not asked for in this round (the
alternative — marking raw-content fields in `src/integrations/cicd/schemas.py`, or
scrubbing by size/type instead of by name — would also touch a file outside `src/api/**`
this wave). The consequence, plainly: **the same class of exposure reopens the moment a
later phase adds another raw-content field to an artifact under `final`** (a new tool
result body, a config file's contents, a stack trace) unless whoever adds that field also
extends this same name-based list. It was missed twice already — first the log excerpt,
then the diff patch, both on the same route, both in the same finding's threat model —
because nothing enforces the connection between "this schema field carries raw external
content" and "this response-serialisation walk knows to scrub it." Recorded here for the
remaining nine findings and for whichever agent adds the next artifact field: this walk
needs a corresponding entry added by hand, and it will not fail loudly if that step is
skipped.

## Contract deviations

One, named rather than silently built, per the standing instruction:

- **A.12's literal `200 RunOutcome`** for `POST /v1/replay/{scenario}` and
  `GET /v1/runs/{run_id}` is no longer a lossless `model_dump` of the internal
  `RunOutcome` object for two nested fields (`final.<artifact>.logs[].excerpt` and
  `final.<artifact>.diff.files[].patch`). PLAN.md does not carve out an exception for
  `final` the way it does for `detail` (A.12: "detail passes through the Redactor") —
  `RunOutcome.final` is typed `dict[str, JsonValue]  # integration payload; opaque to the
  harness` with no stated scrubbing. This fix round's own instructions frame the scrub as
  required and already decided at the "should we do this" level (this is the
  live-exposure item, top priority; the diff-patch extension was explicitly
  user-authorised by the coordinator), so I did not re-litigate that call — only the
  *how*, which is reported above. Recording it here as the deviation, since a reviewer
  diffing served JSON against `RunOutcome.model_dump()` will find them no longer
  identical for these two fields, and that should be a deliberate, documented fact rather
  than a surprise.

## Reproduction recipe for test-verifier

```
POST /v1/replay/real_regression   (or GET /v1/runs/{run_id} after any run)
```

Assert on the response body:
- `resp.json()["final"]["bundle"]["logs"][0]` does NOT contain key `"excerpt"`.
- `resp.json()["final"]["bundle"]["logs"][0]` DOES contain `"excerpt_length"` (int > 0)
  and `"excerpt_sha256"` (64 hex chars).
- For each entry in `resp.json()["final"]["bundle"]["diff"]["files"]`:
  - it does NOT contain key `"patch"`;
  - if the underlying `FileChange.patch` was a non-empty string, the entry DOES contain
    `"patch_length"` (int > 0) and `"patch_sha256"` (64 hex chars);
  - **the `None` case matters and must be its own assertion, not inferred from the
    non-`None` one**: construct or use a fixture where a `FileChange` has `patch=None`
    (GitHub omits it for binary/too-large files) and assert that entry has neither
    `"patch_length"` nor `"patch_sha256"` — the digest must never fire on `None`, and
    the only way to catch a future edit that does (e.g. `hashlib.sha256(str(patch))`)
    is a fixture that actually exercises the `None` branch.
- `path`, `status`, `additions`, `deletions` on every `diff.files[]` entry are unchanged
  from what `FailureBundle.model_dump()` would have produced for that entry.
- `len(resp.content)` is materially smaller than the equivalent
  `RunOutcome.model_dump(mode="json")` would produce for the same outcome — a good bound
  is "under 20,000 bytes for the `real_regression` fixture" (measured after: 6,010–6,340
  bytes depending on citation count; measured before: 47,346–47,504 bytes for the same
  underlying outcome, the small spread across both being independent live-model calls,
  not a serialisation difference).
- `resp.json()["final"]["diagnosis"]["citations"]` is unchanged in shape/content —
  `quote` fields still present and non-empty (the digest fix must never touch this key
  path; a regression test that also asserts citation quotes are *not* empty strings
  protects against a future edit accidentally widening the log-scrub to `Citation.quote`
  by matching on field name `"excerpt"` too broadly — it doesn't today, since
  `Citation`'s field is named `quote`, not `excerpt`, but a name-based match is exactly
  the kind of helper that can rot if someone renames things later).
- All of `.status == "completed"`, `.final.diagnosis.category == "real_regression"`,
  `.final.diagnosis.suggested_action == "open_fix_pr"`, `.run_id`, `.trace_url`,
  `.stages` (non-empty list) still present — unaffected by this change, but worth
  re-asserting in the same test since they share the response body.
- A secret-shaped string (e.g. a `ghp_`-prefixed token) planted into a scenario's
  recorded log fixture, **or into a recorded diff patch** (a committed `.env`/config/key
  is at least as plausible a leak vector as a pasted log token, per the coordinator's
  extension), must NOT appear anywhere in `resp.text` — this is the actual regression the
  finding warns about and is the test that should exist once the Phase 5 credential
  fixture lands; today no fixture contains one, so this specific assertion has nothing to
  plant against yet, but the shape (`assert "ghp_" not in resp.text` or similar against a
  synthetic fixture, run against both the log-excerpt path and the diff-patch path) is
  the one to add. Out of scope for me to write per this round's instructions, but
  recorded here as the durable regression test.
- Per the "known limitation" note above: if `test-verifier` is asked to guard against
  regressions structurally rather than case-by-case, the honest test is not "these two
  fields are scrubbed" but "no field in `final` that carries raw external content is
  served un-scrubbed" — which today has no schema-level way to express, since nothing
  marks a field as raw content. Recording the gap rather than writing a test for it,
  per this round's "no tests" and "don't make it structural" constraints.

## Commands run

| Command | Result |
|---|---|
| `uv run ruff check src/api src/settings.py` | All checks passed (both before and after the diff-patch extension) |
| `uv run mypy --strict src/api` | 10 errors, all in `src/integrations/cicd/**` (pre-existing, other agents' concurrent territory); zero errors under `src/api/` (checked again after the extension) |
| One-shot script: run replay orchestrator once, dump same `RunOutcome` both ways (logs-only fix) | BEFORE 47,504 bytes / AFTER 6,340 bytes |
| `POST /v1/replay/real_regression` via `httpx.ASGITransport` against the real `app` (logs-only fix) | 200, 5,503 bytes total response, `.status`/`.final.diagnosis.*`/`.run_id`/`.trace_url`/`.stages` all present and correct shape, `excerpt` absent from `final.bundle.logs[0]`, `excerpt_length`/`excerpt_sha256` present |
| One-shot script: run replay orchestrator once, dump same `RunOutcome` both ways (logs + diff-patch fix) | BEFORE 47,346 bytes / AFTER 6,010 bytes; `final.bundle.diff.files[0]` has `patch` absent, `patch_length=252` (matches coordinator's figure), `patch_sha256` present; `final.bundle.logs[0]` has `excerpt` absent, `excerpt_length=40,638` |
| Standalone check of `_digest_str_field` against `patch=None`, `patch="hello world"`, and a dict with no `patch` key | `None` stays `None` with no `_length`/`_sha256` added; string is replaced with `_length`/`_sha256`; missing key is a no-op |

Both live-Gemini-call scripts and scratch files used for measurement live under the
session scratchpad, not the repo, and were not committed.

## Deploy state

Local only, not deployed. I did not run `docker compose up` or push to the Space; this
round's instructions were response-path code only, and the Space's existing deployment
already picks up this fix on its next redeploy since `app.py` requires no change (see
above). Verified against: an in-process ASGI client (`httpx.ASGITransport`) driving the
real `src.api.main.app`, hitting the real replay orchestrator with a live Gemini call —
the closest local equivalent to what the public Space serves, without touching the
live URL or its quota.

## Handoffs

- **fixtures-eval**: this round's instructions kept the Phase 5 leak-test scope
  (`test_no_secret_leak.py` covering HTTP responses) explicitly out of my territory —
  untouched, as directed.
- **test-verifier**: the reproduction recipe above is ready to become a regression test;
  I did not write one, per instructions.
- **phase-reviewer**: the one contract deviation (A.12's `RunOutcome` is no longer a
  lossless dump of `final.<artifact>.logs[].excerpt` or `final.<artifact>.diff.
  files[].patch`), and the field-name-walk limitation, are both recorded above for the
  next audit pass rather than left implicit.

## Notes for the reviewer

- I did not touch `src/harness/orchestrator.py:336-339` or
  `src/integrations/cicd/schemas.py` (where `FailureBundle`/`LogExcerpt`/`FileChange`
  are declared) — the finding and its follow-up both name fields whose origin is outside
  `src/api/**`, and the instructions were explicit that `harness-core` and
  `cicd-integration` own those trees this wave. The fix is fully contained in
  `src/api/**` and reaches both routes that serialise a `RunOutcome`.
- The scrub is a **field-name walk, not structural** — see "Known limitation of this
  fix" above. This was raised as an explicit, deliberate choice for this round (matching
  the coordinator's instruction not to build a schema-level or type/size-based scrub),
  not an oversight, but it is the reason the diff-patch exposure existed at all: the
  first pass fixed the field the reviewer measured, not the class of field. Whoever adds
  the next raw-content field to an artifact under `final` needs to know this walk exists
  and extend it by hand.
- The citation-count observation (4 vs. the expected 5 in the Verify checklist) is model
  non-determinism on live Gemini calls, not something my change touches or could cause —
  `_serialize_run_outcome` never reads or writes `final.diagnosis`. Flagged above so it
  isn't mistaken for a regression introduced by this fix.
- `GET /v1/runs` and `POST /v1/runs`'s `202` response were re-checked and confirmed to
  never have carried `final` in the first place; no change was needed or made there.
