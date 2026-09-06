# Build progress

One directory per phase, written by the agents as they work:

```
docs/progress/phase-<N>/
  harness-core.md  cicd-integration.md  api-surface.md  fixtures-eval.md
  test-verifier.md      ← the gate verdict: PASS | FAIL | BLOCKED
  verify.md             ← literal expected vs actual for the plan's Verify block
  review.md             ← the audit verdict: SHIP | FIX FIRST
```

A phase is done when `test-verifier.md` says PASS and `review.md` says SHIP. Tag it
`phase-<N>-green` and move on.
