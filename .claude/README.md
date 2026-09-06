# Agent + skill setup for the Agent Harness build

Project-scoped, so a Claude Code session started in this folder picks them up automatically.
`PLAN.md` at the repo root is the normative reference for all of them.

## The idea

Agents are partitioned by **disjoint write territory**, not by phase. Two agents can
therefore never touch the same file, which is what makes it safe to run four of them at
once. Phases become dispatch plans over that fixed set.

The coordination mechanism is `PLAN.md` **Appendix A**, frozen in Phase 0: because every
interface is fixed in advance, an agent can code against a component another agent is
writing at the same moment.

## Agents

| Agent | Owns | Model |
|---|---|---|
| `harness-core` | `src/harness/**` — the domain-agnostic control plane | opus |
| `cicd-integration` | `src/integrations/**` — the domain layer | sonnet |
| `api-surface` | `src/api/**`, `settings.py`, packaging, deploy | sonnet |
| `fixtures-eval` | `fixtures/**`, `scripts/**` | sonnet |
| `test-verifier` | `tests/**`; the only agent that may declare a phase done | sonnet |
| `phase-reviewer` | nothing — read-only auditor | opus |

## Skills

| Skill | Use |
|---|---|
| `/phase-run <0-6>` | dispatch a phase: parallel build wave, then gate, then audit |
| `/phase-verify <0-6>` | run a phase's literal Verify block and report actual vs expected |
| `/contract-check` | diff implemented models against Appendix A, field by field |
| `/fixture-new <name>` | scaffold a scenario in the exact replay format |

## Typical session

```
/phase-run 1
# → 4 builders in parallel, then test-verifier, then phase-reviewer
/contract-check          # after any parallel wave, catches cross-agent drift
git tag phase-1-green
/phase-run 2
```

## Two failure modes to watch for

**Contract drift.** Four agents reading Appendix A independently will eventually disagree.
Run `/contract-check` after every build wave; it is much cheaper than finding the mismatch
two phases later as a runtime error.

**Territory bleed.** If an agent reports it needed to edit outside its territory, do not let
that stand — dispatch the owning agent instead. The moment two agents write the same file,
the parallelism is unsound and the next merge conflict will be silent.

Per-phase records accumulate under `docs/progress/phase-<N>/`.
