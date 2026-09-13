"""Fault injection: one spelling for `HARNESS_FAULT_INJECT`, and the LLM faults.

DERIVED, NOT TRANSCRIBED. Appendix E fixes the setting (`fault_inject`, "test-only;
refused when env != dev") and the Phase 3 and Phase 4 Verify blocks fix the fault names:
`sqlite_locked` (the store's, `memory.FAULT_SQLITE_LOCKED`), `llm_bad_json:N` and
`llm_429:N` (this module's), plus whatever names an integration registers with the
composition root for its own agents. Nothing here reads the environment: the composition
root parses the setting through :func:`parse_fault`, refuses it outside `dev` and refuses
a name nobody registered, and hands each fault to the one component that knows how to
fail that way.

**The LLM faults never reach the provider.** A fault-injected run must not spend quota
(Phase 4 handoff §4), so :class:`FaultInjectingLlmClient` answers the first `count`
requests itself and only then delegates. "First `count`" is counted **per agent** -- keyed
by the request's schema, because each `LLMAgent.run` translates its output model once
and sends that same schema on every attempt of one call -- and the client is built per
run, so the count restarts for every agent of every run. That is the semantics under
which PLAN.md's step 3 reads as written: `llm_bad_json:2` gives *every* stage
`attempts == 3`, `llm_bad_json:9` exhausts a required stage, `llm_429:3` completes on
the fourth transient attempt. A process-wide counter would spend both junk responses on
the first stage's agent and leave the second's attempt count at 1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from src.harness.contracts import TokenUsage
from src.harness.errors import ConfigurationError
from src.harness.llm import LlmClient, LlmRateLimited, LlmRequest, RawLlmResponse

logger = logging.getLogger("harness.faults")

#: The first `count` responses of every agent call are not JSON.
FAULT_LLM_BAD_JSON: Final[str] = "llm_bad_json"
#: The first `count` calls of every agent call raise `LlmRateLimited` with no stated delay.
FAULT_LLM_429: Final[str] = "llm_429"

LLM_FAULTS: Final[frozenset[str]] = frozenset({FAULT_LLM_BAD_JSON, FAULT_LLM_429})

#: What `llm_bad_json` answers with. Not JSON, not a code fence, not empty: the loop
#: must classify it as a content failure and repair-retry it, exactly as it would a
#: model that answered in prose.
BAD_JSON_TEXT: Final[str] = "Sure! Here is the analysis you asked for: the failure looks"


@dataclass(frozen=True)
class Fault:
    """One parsed `HARNESS_FAULT_INJECT` value: a name, and a count where the name takes one."""

    name: str
    count: int | None = None

    @property
    def spec(self) -> str:
        return self.name if self.count is None else f"{self.name}:{self.count}"


def parse_fault(spec: str, *, counted: Sequence[str] = tuple(LLM_FAULTS)) -> Fault:
    """`"llm_bad_json:2"` -> `Fault("llm_bad_json", 2)`; `"sqlite_locked"` -> `Fault(...)`.

    Names in `counted` require a positive integer count; every other name must have none.
    Raises `ConfigurationError` on a malformed spec, so a typo fails the boot rather than
    injecting nothing silently. Whether the *name* is known is the composition root's
    check -- it holds the union of every component's names.
    """
    name, sep, raw_count = spec.strip().partition(":")
    if not name:
        raise ConfigurationError("HARNESS_FAULT_INJECT names no fault")
    if name in counted:
        if not sep or not raw_count.isdigit() or int(raw_count) < 1:
            raise ConfigurationError(
                f"fault injection {name!r} needs a positive count, as {name}:N"
            )
        return Fault(name, int(raw_count))
    if sep:
        raise ConfigurationError(f"fault injection {name!r} takes no count (got {spec!r})")
    return Fault(name)


class FaultInjectingLlmClient:
    """An `LlmClient` that answers the first `fault.count` requests per agent itself.

    Wraps the real client and delegates once the count is spent. Build one per run: the
    per-agent counters live on the instance.
    """

    def __init__(self, inner: LlmClient, fault: Fault) -> None:
        if fault.name not in LLM_FAULTS or fault.count is None:
            raise ConfigurationError(f"{fault.spec!r} is not an LLM fault")
        self._inner = inner
        self._fault = fault
        self._seen: dict[str, int] = {}

    @property
    def fault(self) -> Fault:
        return self._fault

    @staticmethod
    def _agent_key(req: LlmRequest) -> str:
        # The schema is the agent's identity for the length of one `LLMAgent.run`: built
        # once per call, identical across its attempts, different between agents.
        return json.dumps(req.schema, sort_keys=True, separators=(",", ":"), default=str)

    async def generate(self, req: LlmRequest) -> RawLlmResponse:
        key = self._agent_key(req)
        seen = self._seen.get(key, 0)
        if seen >= (self._fault.count or 0):
            return await self._inner.generate(req)
        self._seen[key] = seen + 1
        logger.info(
            "fault %s: answering attempt %d of %d for this agent without calling the provider",
            self._fault.spec, seen + 1, self._fault.count,
        )
        if self._fault.name == FAULT_LLM_429:
            raise LlmRateLimited("injected 429 (HARNESS_FAULT_INJECT)", retry_after_s=None)
        return RawLlmResponse(
            text=BAD_JSON_TEXT,
            tokens=TokenUsage(),
            finish_reason="STOP",
            model=req.model,
            latency_ms=0,
        )
