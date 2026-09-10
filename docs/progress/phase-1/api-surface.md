## Summary

Final Phase 1 round: closed `review.md` findings 5 and 6 together, as directed, because
6 is what keeps 5 from reopening the same class of leak it closes. `src/api/main.py` had
zero exception handlers — a malformed request got FastAPI's default 422
`application/json` with the submitted body echoed back, and any unhandled route
exception got a bare `500 text/plain "Internal Server Error"` with no problem body, no
`run_id`, nothing in the trace (finding 5). `problem()`'s `detail` was never scrubbed —
a discipline guarantee ("every call site authors a constant"), not a structural one
(finding 6). Adding the catch-all handler that finding 5 requires is exactly what makes
an exception string reach a `detail` call site for the first time, which is why the
task bundled both findings into one round rather than doing 5 first and 6 later.

Three `@app.exception_handler` registrations added: `RequestValidationError` (422,
built from `loc`/`msg` only — never `err["input"]`, which is the submitted value and
the reason FastAPI's default echoes client input), `StarletteHTTPException` (catches
FastAPI's own framework-raised 404 on an unmatched path and 405 on a matched path with
the wrong method — no route in this file raises `HTTPException` itself), and a
catch-all `Exception` handler (logs the real exception server-side via
`logger.exception`, returns a fixed generic `detail`, attaches `run_id` when one is in
scope). `problem()` now runs its whole body through
`get_app_context().recorder.redactor.scrub(...)` before returning it — the same
`Redactor` instance `_serialize_run_outcome` already uses — and its docstring is
rewritten to describe the structural guarantee rather than the discipline one it
described before.

To make `run_id` actually reach the catch-all for the two routes that mint one
(`POST /v1/runs`, `POST /v1/replay/{scenario}`), both routes now set
`request.state.run_id` immediately after minting, before doing anything that could
fail — exception handlers run outside the route function and can't see its locals, only
`request.path_params` and `request.state`. `GET /v1/runs/{run_id}` and its `/trace`
sibling already carry `run_id` as a path parameter, which the same lookup helper reads
first.

**Judgement call, as asked:** routed FastAPI's default `HTTPException` handling
(currently only reachable via framework-generated 404/405s, since no route raises it)
through `problem()` too, for consistency. Reasoning: A.12 specifies RFC 9457 for
"errors" without carving out framework-raised ones; nothing in this repo or `app.py`'s
Space UI reads FastAPI's default `{"detail": ...}` shape; and a later phase's route that
picks `raise HTTPException(409, ...)` for A.12's already-decided-approval case (or 410
for an expired one) gets the right body shape for free instead of needing to remember
`problem()` specifically. `exc.detail` is framework/route-authored, never client input,
so surfacing it as `detail` is safe — and it still passes through the same `Redactor`
scrub regardless.

## Files written

- `D:\Documents\harness_project\src\api\main.py` — the only file changed this round.
  Added: `from http import HTTPStatus`, `from fastapi.exceptions import
  RequestValidationError`, `from starlette.exceptions import HTTPException as
  StarletteHTTPException`; `_run_id_in_scope()`; three `@app.exception_handler`
  functions (`handle_validation_error`, `handle_http_exception`,
  `handle_unhandled_exception`); rewrote `problem()`'s body (added the `Redactor` scrub)
  and docstring; added `request.state.run_id = run_id` to `replay()` and `create_run()`
  right after minting.

No other file under my write territory needed a change.

## Contract deviations

None. Both findings closed as specified; the one open question the task flagged as a
judgement call is resolved and justified above, not left as a deviation.

## Endpoints now live

No new endpoints. Every existing route's error path now goes through the same RFC 9457
shape, plus three new process-wide handlers that back all of them:

- `RequestValidationError` → `422 application/problem+json` (previously
  `422 application/json` with the request body echoed).
- `StarletteHTTPException` (framework 404 on an unmatched path, 405 on a matched path
  with the wrong method) → `problem()`'s shape at the exception's own status code
  (previously `application/json {"detail": "..."}`).
- Any unhandled `Exception` on any route → `500 application/problem+json`, generic
  `detail`, `run_id` attached when in scope, real exception logged server-side
  (previously `500 text/plain "Internal Server Error"`).
- All pre-existing hand-written `problem()` call sites (400/404/409-shaped/501/500 across
  `/v1/replay/{scenario}`, `POST /v1/runs`, `GET /v1/runs/{run_id}`,
  `GET /v1/runs/{run_id}/trace`) are unchanged in status code and message, now additionally
  scrubbed through the `Redactor` before leaving the process.

## Commands run

- `uv run ruff check src/api src/settings.py app.py` → `All checks passed!`
- `uv run mypy src/settings.py src/api app.py` → 11 pre-existing errors, all in
  `src/integrations/cicd/gateway_replay.py`, `agents/investigator.py`,
  `agents/diagnostician.py`, `wiring.py`, and one in `app.py:184`
  (`_zerogpu_handshake` decorator) — none in `src/api/main.py` or `src/api/deps.py`,
  none introduced or fixed by this diff (confirmed: `git diff --stat` shows only
  `src/api/main.py` changed).
- `uv run pytest -q` → **331 passed, 2 failed, 1 skipped** (was 333 passed before this
  round). Both failures are
  `tests/unit/test_startup_validates_prompt_templates_under_mount.py::
  test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500` (both mount-order
  parametrisations) — see "Note on the two failing tests" below. No other test changed
  status.
- `uv run pytest -q tests/unit/test_no_env_access.py` → 38 passed (DoD item 3, unaffected).
- Manual reproductions via `TestClient` (see "Handoffs" for the exact recipes and their
  captured output) confirming: 422 body has no `input`/submitted-value leakage; a
  framework 404 on an unmatched path returns `problem()`'s shape; a known-route 404
  carries `run_id`; a planted `ghp_`-shaped string in `detail` comes back as
  `***REDACTED***`; an unhandled `RuntimeError` with a credential-shaped message in it
  returns a generic `detail` (nothing leaked), status 500, `application/problem+json`,
  and `run_id` populated from `request.state` when the route had minted one.

## Deploy state

local only, no redeploy performed or attempted — the task is explicit that the Space
stays on pre-fix code until the tag. Nothing in this round touched `app.py`,
`Dockerfile`, `docker-compose.yml`, or deploy config.

## Note on the two failing tests (not mine to fix, flagged for test-verifier)

`test_mounted_subapp_lifespan_never_fires_healthz_then_bare_500` (both parametrisations,
`mounted_client_before` and `mounted_client_after`) pins the *exact* bare-500 shape
finding 5 requires be made structurally impossible: it deliberately breaks the prompts
directory, mounts `src.api.main.app` as a Starlette sub-app so its lifespan never runs,
and asserts `POST /v1/replay/real_regression` comes back `500 text/plain
"Internal Server Error"` with no JSON body and no `run_id` — the review-2.md finding 3
symptom, used there as a negative control proving the missing-lifespan gap.

The catch-all handler this round adds is registered on `src.api.main.app` itself, and
Starlette's exception-handling middleware belongs to the app instance whose route
matched — independent of whether that instance's *own* lifespan ever entered. So the
sub-app's `FileNotFoundError` (raised inside `Investigator.build_prompt` because the
prompts directory doesn't exist) is now caught by the mounted app's own catch-all
regardless of mount order, and the response is `500 application/problem+json` with a
generic `detail` and no `run_id` (the exception fires inside `_execute`, before
`request.state.run_id` — wait, `run_id` *is* set before `_execute` is awaited in
`replay()`, so it **is** present — verified directly, see the reproduction below).

This is finding 5 doing exactly what it was asked to do to a case its author didn't
have in view when specifying that particular test's assertions: the test's *premise*
("nothing stands between this failure and a bare 500") is precisely what this round
closes, for every route, including this one. The test needs updating, not the code —
but tests are test-verifier's territory, so I have not touched it. Exact new behaviour,
reproduced directly against the sub-app mounted the same way the test does it:

```
status: 500
content-type: application/problem+json
body: {
  "type": "about:blank",
  "title": "Internal Server Error",
  "status": 500,
  "detail": "An unexpected error occurred while processing the request.",
  "instance": "/v1/replay/real_regression",
  "run_id": "<the minted run id>"
}
```

Suggested assertion updates for that one test: status stays `500`; content-type becomes
`application/problem+json` (not `text/plain`); body is now valid JSON with
`type`/`title`/`status`/`detail`/`instance` and a `run_id` key; `detail` is the fixed
generic string, not `"Internal Server Error"` verbatim (that string is now `title`);
and the assertion `"run_id" not in replay.text` should invert, since `run_id` is now
present by design. Everything else in that file (the boot-time `OSError` test, the
positive-control real-prompts-dir test, the `app.py` hand-call test) is unaffected and
still passes.

## Handoffs

Reproduction recipes for test-verifier, none requiring code changes to reproduce —
all run directly against `from src.api.main import app` with `fastapi.testclient.TestClient`:

1. **422 is RFC 9457 and does not echo the submitted body:**
   `client.post("/v1/runs", json={"integration": "cicd", "not_a_field": "x"})` →
   assert `status_code == 422`, `content-type == "application/problem+json"`, body has
   `type`/`title`/`status`/`detail`/`instance`, and `json.dumps(body)` does **not**
   contain `"not_a_field"` or `"x"` (the two arbitrary strings that would appear if the
   old default `{"detail": [...]}` shape's `"input"` key were still being echoed).
   Reproduced output this round: `detail` was
   `"body.subject: Field required; body.idempotency_key: Field required;
   body.not_a_field: Extra inputs are not permitted"` — location and reason only.

2. **Catch-all returns a problem body with `run_id`:** register a throwaway route in a
   test module (or monkeypatch a dependency to raise) that sets
   `request.state.run_id = "<known-id>"` then raises; assert the response is
   `500`, `application/problem+json`, `detail` is the fixed generic string (not the
   exception's `str(exc)`), and `body["run_id"] == "<known-id>"`. For a route-shaped
   version with no test route needed: monkeypatch `rendering._PROMPTS_DIR` to a
   nonexistent path (the existing `broken_prompts_dir` fixture) and hit
   `POST /v1/replay/real_regression` directly (mounted or unmounted, both now behave
   the same) — see the exact captured body above.

3. **Credential-shaped string in `detail` is scrubbed:** call `problem()` directly (or
   hit any route) with a `detail` containing a GitHub-token-shaped string, e.g.
   `"upstream said: ghp_" + "a" * 36`; assert the literal token substring is absent from
   the response body and `"***REDACTED***"` (or whatever `REDACTION_PLACEHOLDER` is)
   appears in its place. Reproduced this round: input `"upstream said:
   ghp_aaaa...aaaa"` came back as `"upstream said: ***REDACTED***"`.

4. Also worth a test-verifier assertion, not previously covered: an unmatched path
   (`GET /v1/does-not-exist`) now returns `404 application/problem+json` with
   `title == "Not Found"` instead of FastAPI's default `{"detail": "Not Found"}`
   `application/json` — the `StarletteHTTPException` handler's effect.

## Notes for the reviewer

- The two test failures are the expected, intended consequence of closing finding 5 to
  its full generality, on a test that was written to pin the *absence* of exactly this
  fix as a negative control. I did not edit the test (out of territory) or work around
  it in the app code (that would mean deliberately leaving one route's exception
  unhandled, which defeats the finding). Flagged in detail above for test-verifier to
  update before `phase-1-green` is tagged.
- `run_id` attachment on the catch-all is best-effort by design, not universal: routes
  that mint a run id but hand execution to a fire-and-forget `asyncio.create_task`
  (`create_run`'s background path) will not have that task's eventual exception reach
  this handler at all — the task is never awaited by the route, so any failure inside it
  happens after the 202 response has already been sent, and this round's exception
  handlers only cover exceptions that propagate out of a route coroutine, not exceptions
  in a detached task. That gap is pre-existing (it is not new in this round, and closing
  it is a `MemoryStore.claim_run()` / background-task-supervision concern for a later
  phase, not an RFC 9457 body-shape concern) but worth naming since `request.state.run_id`
  is set on that path too, which could otherwise look like a stronger guarantee than it is.
- `HTTPStatus(exc.status_code).phrase` is used for `title` on the `HTTPException`
  handler rather than a hardcoded string, so an unusual status code still gets a
  reasonable title instead of `"Error"`; the `try/except ValueError` fallback covers any
  status code outside `HTTPStatus`'s enumerated set.
- Chose `status.HTTP_422_UNPROCESSABLE_CONTENT` over
  `status.HTTP_422_UNPROCESSABLE_ENTITY` for the validation handler — same numeric code
  (422), but the pinned FastAPI/Starlette version in this environment emits a
  `StarletteDeprecationWarning` for the `_ENTITY` name; using the non-deprecated alias
  avoids a warning that would otherwise show up on every 422 in test output.

---

## Fix round: re-audit findings 1, 3, 5 (FIX FIRST verdict)

### Summary

Three findings against the exception-handling work above, all confined to
`src/api/main.py`. Finding 1 (medium) was a live regression: `handle_http_exception`
built its `problem()` response from `exc.status_code`/`exc.detail` only and never read
`exc.headers`, so any header Starlette or a route attaches to an `HTTPException` — most
concretely `Allow` on Starlette's own 405, which RFC 9110 §15.5.6 makes a MUST — was
silently dropped once routing went through `problem()`. Findings 3 and 5 (both low) were
about `problem()`'s two echo paths: the 422 handler's docstring claimed "nothing the
client sent is reproduced" when `RunRequest`'s `extra="forbid"` puts caller-supplied JSON
*key names* into `loc` verbatim, and the same handler had no bound on `detail`'s length,
letting a large request body of nonsense keys produce a larger response body on an
unauthenticated route; `problem()` separately echoed a `run_id` path segment into the
response body without checking it was shaped like a real `RunId` first.

Fixed all three in `problem()` and its two callers (`handle_http_exception`,
`handle_validation_error`), without touching the third handler
(`handle_unhandled_exception`) or any route — none of the three findings implicated them.

### Files written

- `src/api/main.py` — the only file touched this round.

### Contract deviations

None.

### Fixes and rationale

**Finding 1 — `problem()` now takes `headers: Mapping[str, str] | None` and attaches it
to the `JSONResponse` unchanged; `handle_http_exception` passes `exc.headers` through.**
Deliberately *not* run through the `Redactor`: headers reaching `problem()` are always
framework- or route-authored (`StarletteHTTPException.headers`), never request-derived,
the same trust boundary already extended to `exc.detail` and to `title`. Scrubbing them
generically would risk corrupting a spec-shaped value (`Allow: HEAD, POST, GET`,
`Retry-After: 30`) against a threat model — this process's own code leaking a secret
through a header it authored — that the body scrub doesn't defend against either; the
call site is where that discipline belongs, same as `detail`. Documented as a deliberate
choice in `problem()`'s docstring, per the finding's explicit ask to decide and say which.

Verified by direct probe (`TestClient`, not a committed test — tests are
test-verifier's): `DELETE /v1/runs` now returns `405` with `Allow: POST` (matches
Starlette's own default-handler output for this route's registration order, confirmed by
reproducing the same result against a bare FastAPI app with no custom handlers — the
content of `Allow` is inherent to Starlette's partial-match routing, not something this
fix controls or needs to). A synthetic route raising
`HTTPException(401, headers={"WWW-Authenticate": "Bearer"})` now returns `401` with
`WWW-Authenticate: Bearer` intact (previously stripped); `HTTPException(429,
headers={"Retry-After": "30"})` now returns `429` with `Retry-After: 30` intact.

**Finding 3 — bounded `detail` and corrected the docstring.** `handle_validation_error`
now renders at most `_MAX_VALIDATION_ERRORS` (20) individual `loc: msg` entries, appends
an `"... and N more error(s)"` note for the remainder instead of silently dropping them,
and then hard-caps the assembled `detail` string at `_MAX_DETAIL_LENGTH` (2000 bytes) —
applied after joining, so it also bounds the pathological case of one single enormous
`loc` (a caller-chosen huge JSON key), not just many small ones. The docstring no longer
claims "nothing the client sent is reproduced"; it now states the narrower, true
guarantee (`err["input"]`, the submitted *value*, is never touched) and names the
`loc`-carries-key-names caveat explicitly, including that a registered secret in key
position is still caught by `problem()`'s `Redactor` pass but an arbitrary non-secret key
is not.

Verified by probe: a `POST /v1/runs` body with 500 extra unrecognized keys produces a
`422` whose `detail` is exactly 2000 bytes (previously unbounded — the finding measured
11,959→18,384 bytes on a 1-key payload; the pattern used here goes further and confirms
the cap holds under many-key amplification too, not just one long key).

**Finding 5 — `problem()` validates `run_id`'s shape before echoing it.** Added
`_RUN_ID_PATTERN`, a module-level regex mirroring `RunId`'s `StringConstraints` pattern
in `src/harness/contracts.py` (kept as a plain local regex rather than reaching into
`RunId.__metadata__` to extract it, to avoid depending on pydantic internals for
something this small). `problem()` now only sets `body["run_id"]` when the value matches;
an unvalidated path segment (`GET /v1/runs/x`) is silently omitted rather than echoed.
Fixed once, in `problem()` itself, so every call site — not just `get_run` — is covered,
per the finding's note that `_run_id_in_scope` extends the same reflection to every
handler.

Verified by probe: `GET /v1/runs/x` now returns a `404` problem body with no `run_id` key
at all (previously `{"run_id": "x"}`); `GET /v1/runs/<real-ULID-shaped-id>` for a
non-existent run still echoes that id, since it matches the pattern.

### Commands run

- `uv run ruff check src/ app.py` → clean.
- `uv run mypy src/settings.py src/api app.py` → 10 pre-existing errors, all in
  `src/integrations/**` and `app.py:184` (`_zerogpu_handshake`); identical set and count
  confirmed via `git stash`/`git stash pop` against the pre-fix tree. Zero new errors
  from this diff; `src/api/main.py` itself is clean.
- Manual `TestClient` probes (not committed — see Handoffs) exercising all three findings
  as described above.

### Deploy state

Not redeployed, per instructions. No local server was left running.

### Handoffs

For test-verifier, a recipe for each finding (none of these are committed test files):

1. **Finding 1:** `DELETE /v1/runs` (or any matched path hit with a method it doesn't
   support) returns `405` with a non-empty `Allow` header. Separately, register a
   throwaway route (or reuse an existing one if a suitable 401/429 case exists later)
   that raises `HTTPException(401, headers={"WWW-Authenticate": "Bearer"})` and assert
   the response carries that header unchanged; same for `HTTPException(429,
   headers={"Retry-After": "..."})`.
2. **Finding 3:** `POST /v1/runs` with a body containing many (order of hundreds)
   unrecognized extra keys returns `422` with `len(body["detail"]) <= 2000`. Also assert
   a normal small validation error's `detail` still reads sensibly (no regression to the
   common case).
3. **Finding 5:** `GET /v1/runs/<not-a-ulid>` returns `404` with `"run_id"` absent from
   the body; `GET /v1/runs/<valid-ULID-shaped-but-unknown-id>` returns `404` with
   `"run_id"` present and equal to the requested id.

### Notes for the reviewer

- Finding 1's fix restores the header content Starlette's own default 405 handler would
  produce for a given route registration order — that content depends on which route
  Starlette's partial-match routing picks first when several `Route` objects share a
  path (`/v1/runs` has separate `POST` and `GET` registrations, each its own `Route`), a
  pre-existing framework mechanic unrelated to this diff. It is not a merged
  "all-methods-for-this-path" `Allow`; that's what Starlette itself returns.
  `handle_http_exception` forwards whatever `exc.headers` Starlette computed rather than
  recomputing or merging it, so it is faithful to the framework's own behavior.
- Left `handle_unhandled_exception` untouched: it never received headers or reflected
  either `run_id` shape or client-supplied keys, and none of the three findings named it.
- Did not add a `headers` parameter to any of the route functions' own `problem(...)`
  calls (`replay`, `create_run`, `get_run`, `get_run_trace`) — none of those call sites
  had a header to lose before this round, and the finding's example of a Phase-2 route
  choosing `raise HTTPException(409, ...)` is exactly the shape now safe to add later
  without revisiting `problem()` again.
