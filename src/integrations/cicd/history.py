"""Memory, as this integration uses it: keys, priors, observations, outcome resolution.

The harness's `MemoryStore` remembers opaque strings. Everything that gives those strings
meaning for a workflow failure lives here, in plain functions the three agents call:

- `signature_key_for_job` -- the `SignatureKey` a job's failure is remembered under.
- `prior_history_from` -- a `MemoryHit` rendered into the bundle's `PriorHistory`,
  including the flakiness prior's *words* (`likely_flaky` / `likely_real` / `unknown`) over
  the harness's *numbers* (`memory.dominant_verdict`), and the retry count the policy's
  `memory.retries_for_signature_24h` fact reads.
- `unavailable_history` -- the one shape a degraded read produces. `PriorHistory` itself
  forces `retries_in_24h` to the fail-closed 999 on `unavailable=True`; nothing here
  re-implements or weakens that.
- `observation_for` -- the row the Diagnostician writes after a verdict, and the Remediator
  rewrites (same deterministic id) once an action executed.
- `resolve_pending_outcomes` -- how a `pending` retry learns whether the rerun passed,
  without a webhook (dispatch decision 10).

**Memory supplies a prior, never a verdict** (PLAN.md Phase 3). Nothing here decides a
category; the prompts carry the "prior, not evidence" instruction and the policy's retry
cap is the backstop.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from src.harness.gateway import ToolCall, ToolGateway
from src.harness.guardrails import PolicyDecision
from src.harness.memory import (
    MemoryHit,
    MemoryStore,
    Observation,
    SignatureKey,
    dominant_verdict,
    has_outcome,
    observation_id_for,
    signature_id_for,
)
from src.integrations.cicd.fingerprint import signature_key_for
from src.integrations.cicd.rendering import new_call_id
from src.integrations.cicd.schemas import Diagnosis, FailureBundle, JobRef, PriorHistory

logger = logging.getLogger("harness.integrations.cicd.history")

#: The name a run's history is recorded under when the memory read failed.
DEGRADED_MEMORY: Final[str] = "memory"

#: The write tool whose executions the retry cap counts (`actions_in_window` is keyed by
#: `Observation.action_taken`, and a retry is exactly one `rerun_failed_jobs`).
RETRY_TOOL: Final[str] = "rerun_failed_jobs"

#: Verdict vocabulary the prior maps from. `Diagnosis.category` members, spelled once.
FLAKY_VERDICT: Final[str] = "flaky_test"
REAL_VERDICT: Final[str] = "real_regression"

ActionOutcome = Literal["passed_on_retry", "failed_again", "pending"]
OUTCOME_PASSED: Final[ActionOutcome] = "passed_on_retry"
OUTCOME_FAILED: Final[ActionOutcome] = "failed_again"
OUTCOME_PENDING: Final[ActionOutcome] = "pending"

#: How many recent observations `PriorHistory.sample_run_ids` names.
SAMPLE_RUN_IDS: Final[int] = 5

#: Upper bound on rerun probes per run: one read call each, and a signature that has
#: been retried more than this many times without resolution is not going to resolve now.
MAX_PENDING_PROBES: Final[int] = 3

PriorHint = Literal["likely_flaky", "likely_real", "unknown"]


def signature_key_for_job(job: JobRef, log_text: str) -> SignatureKey:
    """The key a job's failure is remembered under: repo scope, workflow|job|test, fingerprint."""
    return signature_key_for(
        repo=job.repo,
        workflow_name=job.workflow_name,
        job_name=job.job_name,
        log_text=log_text,
    )


def prior_hint_for(hit: MemoryHit) -> PriorHint:
    """PLAN.md's flakiness prior, words over the harness's numbers.

    `likely_flaky` needs the dominant verdict to be `flaky_test` *and* at least one prior
    retry to have passed -- a signature that was only ever labelled flaky, and never seen
    to pass on a rerun, stays `unknown`. `likely_real` needs a dominant `real_regression`.
    """
    record = hit.record
    dominant = (
        dominant_verdict(record.occurrences, record.verdict_counts) if record is not None else None
    )
    if dominant == FLAKY_VERDICT and has_outcome(hit.recent, OUTCOME_PASSED):
        return "likely_flaky"
    if dominant == REAL_VERDICT:
        return "likely_real"
    return "unknown"


def prior_history_from(hit: MemoryHit, key: SignatureKey) -> PriorHistory:
    """The bundle's `PriorHistory` from a successful lookup."""
    record = hit.record
    return PriorHistory(
        signature_id=signature_id_for(key),
        key=key,
        occurrences=record.occurrences if record is not None else 0,
        verdict_counts=dict(record.verdict_counts) if record is not None else {},
        last_verdict=record.last_verdict if record is not None else None,
        last_seen_at=record.last_seen_at if record is not None else None,
        prior_hint=prior_hint_for(hit),
        retries_in_24h=hit.actions_in_window.get(RETRY_TOOL, 0),
        sample_run_ids=[obs.run_id for obs in hit.recent[:SAMPLE_RUN_IDS]],
        unavailable=False,
    )


def unavailable_history(key: SignatureKey | None) -> PriorHistory:
    """The degraded shape. `retries_in_24h` becomes 999 by `PriorHistory`'s own rule."""
    return PriorHistory(
        signature_id=signature_id_for(key) if key is not None else None,
        key=key,
        unavailable=True,
    )


def memory_agrees(prior: PriorHistory, category: str) -> bool:
    """PLAN.md's `memory_agreement` row: a readable prior whose dominant verdict is `category`.

    Never true on a degraded read -- "the `memory_agreement` confidence bonus is not
    applied" is half of what the soft-dependency decision promises.
    """
    if prior.unavailable:
        return False
    return dominant_verdict(prior.occurrences, prior.verdict_counts) == category


def observation_for(
    *,
    key: SignatureKey,
    run_id: str,
    diagnosis: Diagnosis,
    job: JobRef,
    occurred_at: datetime | None = None,
    action_taken: str | None = None,
    action_outcome: ActionOutcome | None = None,
) -> Observation:
    """One observation per (signature, run): the verdict, and later the action taken."""
    signature_id = signature_id_for(key)
    return Observation(
        observation_id=observation_id_for(signature_id, run_id),
        signature_id=signature_id,
        run_id=run_id,
        occurred_at=occurred_at or datetime.now(UTC),
        verdict=diagnosis.category,
        confidence=diagnosis.final_confidence,
        action_taken=action_taken,
        action_outcome=action_outcome,
        commit_sha=job.head_sha,
    )


def terminal_write_tool(executed_tools: Sequence[str]) -> str | None:
    """The tool that names what the harness did: the last write that ran.

    A retry is one `rerun_failed_jobs`; a ticket one `create_issue`; a PR is branch, files,
    then `open_pull_request`, and the last of those is the effect a reader cares about.
    """
    return executed_tools[-1] if executed_tools else None


async def resolve_pending_outcomes(
    memory: MemoryStore,
    gateway: ToolGateway,
    hit: MemoryHit,
    *,
    decision: PolicyDecision,
) -> MemoryHit:
    """Turn `pending` retry observations into `passed_on_retry` / `failed_again` where possible.

    For each recent observation still pending on a `rerun_failed_jobs`, read the run it
    came from back out of the store, find the workflow run and attempt it retried, and ask
    the gateway for the jobs of the *next* attempt. Every job `success` → passed; any
    `failure` → failed again; anything else (404 because the rerun never happened or is
    still running, a missing bundle) leaves it pending. Returns the hit with the resolved
    outcomes substituted, so the prior is computed over what is now known.

    A probe that fails is data, not degradation: it says nothing about this run's evidence
    and must not turn `unavailable` on.
    """
    pending = [
        obs for obs in hit.recent
        if obs.action_outcome == OUTCOME_PENDING and obs.action_taken == RETRY_TOOL
    ][:MAX_PENDING_PROBES]
    if not pending:
        return hit

    resolved: dict[str, ActionOutcome] = {}
    for obs in pending:
        outcome = await _probe_rerun(memory, gateway, obs, decision)
        if outcome is None:
            continue
        try:
            await memory.update_observation_outcome(obs.observation_id, outcome)
        except Exception:  # noqa: BLE001 - the write is a courtesy; the read already succeeded
            logger.warning("history: could not record %s for %s", outcome, obs.observation_id)
            continue
        resolved[obs.observation_id] = outcome

    if not resolved:
        return hit
    recent = [
        obs.model_copy(update={"action_outcome": resolved[obs.observation_id]})
        if obs.observation_id in resolved else obs
        for obs in hit.recent
    ]
    return hit.model_copy(update={"recent": recent})


async def _probe_rerun(
    memory: MemoryStore, gateway: ToolGateway, obs: Observation, decision: PolicyDecision
) -> ActionOutcome | None:
    outcome = await memory.get_run(obs.run_id)
    if outcome is None:
        return None
    raw_bundle = outcome.final.get("bundle")
    if not isinstance(raw_bundle, dict):
        return None
    try:
        job = FailureBundle.model_validate(raw_bundle).job
    except Exception:  # noqa: BLE001 - an older or foreign bundle shape is not a fault
        return None
    result = await gateway.invoke(
        ToolCall(
            call_id=new_call_id(),
            tool="list_workflow_run_jobs",
            args={"run_id": job.run_id, "attempt": job.run_attempt + 1},
        ),
        decision,
    )
    if not result.ok or result.data is None:
        return None
    jobs: Any = result.data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return None
    conclusions = {
        str(item.get("conclusion")) for item in jobs if isinstance(item, dict)
    }
    if "failure" in conclusions:
        return OUTCOME_FAILED
    if conclusions == {"success"}:
        return OUTCOME_PASSED
    return None
