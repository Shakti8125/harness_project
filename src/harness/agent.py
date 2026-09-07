"""The unit of work the orchestrator drives.

DERIVED, NOT TRANSCRIBED. Appendix A specifies no agent abstraction anywhere. This
module is named by the repository layout (``harness/agent.py`` -- "Agent protocol,
LLMAgent base") and its shape is derived from the two things Appendix A *does* fix:
:class:`AgentResult` is generic over the payload type (A.1), and a stage's payload type
reaches the orchestrator as ``StageSpec.output_model``, supplied by the integration
(A.2). Both say the same thing -- the agent abstraction is generic over its output and
never names a payload type.

:class:`Agent` is the seam: hand it the state of a run so far, get a typed,
evidence-carrying envelope back. It is a Protocol rather than a base class, so an agent
that is deterministic Python and never calls a model satisfies it exactly as well as an
LLM-backed one, and neither has to inherit anything.

:class:`LLMAgent` is the base for the LLM-backed case. It owns the one path that would
otherwise be re-implemented, and re-broken, in every integration: render a prompt, hash
it into the trace, translate the output contract into the constrained schema dialect the
provider accepts, call the model through the retry loop, validate the returned text back
into ``output_model``, and account for attempts, latency and tokens in the returned
:class:`AgentResult`. What is left for a subclass is exactly the part that is domain
knowledge -- how to build the prompt, what evidence the output rests on, how confident
it deserves to be -- and arrives through the three hooks at the end of the class.

Nothing here knows what a stage produces, and nothing here may learn: the type parameter
is the whole point.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.contracts import AgentResult, Evidence, TokenUsage
from src.harness.llm import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_REQUEST_TIMEOUT_S,
    DEFAULT_TEMPERATURE,
    LlmClient,
    LlmRequest,
    RawLlmResponse,
    to_gemini_schema,
)
from src.harness.observability import ATTR_DEGRADED_COMPONENT, TraceRecorder
from src.harness.recovery import RetryPolicy, retry_structured

#: How a terminal `AgentError.kind` becomes an `AgentResult.status`. The two vocabularies
#: are deliberately different sizes -- the error names the condition, the status names
#: what the orchestrator should do about it -- so the mapping is written once, here,
#: rather than re-derived at each call site.
_AgentStatus = Literal["invalid_output", "tool_error", "timeout", "escalate"]

_STATUS_FOR_ERROR_KIND: dict[str, _AgentStatus] = {
    "invalid_output": "invalid_output",
    "llm_timeout": "timeout",
    "llm_rate_limited": "escalate",
    "llm_auth": "escalate",
    "llm_upstream": "escalate",
    "tool_error": "tool_error",
    "internal": "escalate",
}

if TYPE_CHECKING:
    # Imported for annotations only. `orchestrator` imports `Agent` from here under the
    # same guard, so neither module reaches the other at runtime and the cycle the two
    # halves of this seam would otherwise form never exists.
    from src.harness.orchestrator import RunState


class AgentPrompt(BaseModel):
    """What a subclass hands back from :meth:`LLMAgent.build_prompt`.

    More than a string, because the deterministic collection an agent does *before* its
    model call is itself a source of two things the envelope has to carry. Evidence
    gathered while assembling the prompt is evidence the run collected, and a read that
    failed is both a thinner prompt and a component the run must be recorded as degraded
    in. Returning all three together is what keeps either from being silently dropped on
    the way to :class:`AgentResult`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    evidence: list[Evidence] = []
    degraded: list[str] = []


class Agent[TOut: BaseModel](Protocol):
    """One stage's worth of work, generic over what it produces.

    ``key`` is the agent's identity: it is what ``StageSpec.agent_key`` selects, and it
    is written verbatim into ``AgentResult.agent`` and ``StageRecord.agent``.
    """

    key: str

    async def run(self, state: RunState) -> AgentResult[TOut]:
        """Produce this stage's output from the run so far.

        Receives the whole :class:`~src.harness.orchestrator.RunState` rather than a
        narrower argument because what one stage needs from the stages before it is a
        domain question: the state carries the opaque ``request.subject`` and the
        artifacts produced so far, and the agent decides which of them mean anything.

        Never raises for the failure of an external system. An upstream failure comes
        back as ``AgentResult(status=..., error=AgentError(...))`` so that the
        orchestrator has one uniform place to record it and the run can continue
        degraded. Only programming errors raise.
        """
        ...


class LLMAgent[TOut: BaseModel]:
    """Base for an agent whose output is one structured model call.

    Satisfies :class:`Agent` structurally; subclasses inherit from this, not from the
    Protocol. Construction is keyword-only and every dependency is passed in -- there is
    no lookup, no registry and no ambient default -- because the composition root is the
    only place that may know which model, which client and which retry policy this
    particular agent gets.
    """

    key: str
    output_model: type[TOut]
    llm: LlmClient
    model: str
    recorder: TraceRecorder
    retry_policy: RetryPolicy
    schema_translator: Callable[[type[BaseModel]], dict[str, JsonValue]]
    temperature: float
    max_output_tokens: int
    thinking_budget: int | None
    timeout_s: float

    def __init__(
        self,
        *,
        key: str,
        output_model: type[TOut],
        llm: LlmClient,
        model: str,
        recorder: TraceRecorder,
        retry_policy: RetryPolicy | None = None,
        schema_translator: Callable[[type[BaseModel]], dict[str, JsonValue]] = to_gemini_schema,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        thinking_budget: int | None = None,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        """Bind this agent to its model, client and policy.

        ``model`` is a plain string rather than a lookup key so that a per-agent model
        override is a wiring decision, not a branch inside the harness.

        ``schema_translator`` is injected, defaulting to the one translator that exists,
        because the schema dialect belongs to whichever ``LlmClient`` is being used;
        pinning it inside this class would weld the base to a single provider and undo
        the Protocol.

        ``retry_policy=None`` means the default :class:`RetryPolicy`, whose attempt
        counts and backoff bounds come from PLAN.md's numbers table.
        """
        self.key = key
        self.output_model = output_model
        self.llm = llm
        self.model = model
        self.recorder = recorder
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self.schema_translator = schema_translator
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.thinking_budget = thinking_budget
        self.timeout_s = timeout_s

    async def run(self, state: RunState) -> AgentResult[TOut]:
        """Build the prompt, call the model, validate the output, envelope the result.

        The fixed path, which no subclass overrides:

        1. ``await self.build_prompt(state)`` -> :class:`AgentPrompt`.
        2. Record the SHA-256 of ``prompt.text`` as ``AgentResult.prompt_sha256`` and as
           a span attribute, so an output can be attributed to the exact prompt that
           produced it across edits.
        3. Translate ``self.output_model`` through ``self.schema_translator`` and build
           the :class:`~src.harness.llm.LlmRequest` from the knobs held on this agent.
        4. Drive the call through
           :func:`~src.harness.recovery.retry_structured`, which is what turns a schema
           violation, a transient upstream failure or a timeout into attempts and
           finally into an ``AgentError`` rather than an exception.
        5. On success, ask the two hooks below for the output's evidence and confidence;
           merge that evidence with the evidence the prompt already carried.
        6. Return the envelope: summed tokens, total latency, attempt count, the model
           name, and ``status`` derived from the error kind when there is one.

        ``prompt.degraded`` is appended to ``state.degraded`` either way, including on
        the failure path -- a degraded read is a fact about the run, not about whether
        the model then answered.
        """
        started = time.monotonic()
        async with self.recorder.span("agent.run", "agent", agent=self.key) as span:
            prompt = await self.build_prompt(state)

            # Recorded before the model call, so a degraded read is on the record whether
            # or not the call that follows it succeeds.
            for component in prompt.degraded:
                if component not in state.degraded:
                    state.degraded.append(component)
            if prompt.degraded:
                span.set_attribute(ATTR_DEGRADED_COMPONENT, list(prompt.degraded))

            prompt_sha256 = hashlib.sha256(prompt.text.encode("utf-8")).hexdigest()
            span.set_attribute("prompt_sha256", prompt_sha256)
            span.set_attribute("prompt_chars", len(prompt.text))

            schema = self.schema_translator(self.output_model)

            async def call(text: str) -> RawLlmResponse:
                return await self.llm.generate(
                    LlmRequest(
                        model=self.model,
                        prompt=text,
                        schema=schema,
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                        thinking_budget=self.thinking_budget,
                        timeout_s=self.timeout_s,
                    )
                )

            output, attempts, error = await retry_structured(
                call, prompt.text, self.output_model, self.retry_policy, self.recorder
            )

            tokens = TokenUsage(
                prompt=sum(record.tokens.prompt for record in attempts),
                completion=sum(record.tokens.completion for record in attempts),
                thinking=sum(record.tokens.thinking for record in attempts),
                total=sum(record.tokens.total for record in attempts),
                estimated_cost_usd=sum(record.tokens.estimated_cost_usd for record in attempts),
            )
            span.set_tokens(tokens)

            evidence = list(prompt.evidence)
            confidence: float | None = None
            if output is not None:
                evidence.extend(self.evidence_for(output, state))
                confidence = self.confidence_for(output, state)

            status: Literal["ok", "invalid_output", "tool_error", "timeout", "escalate"] = (
                "ok" if error is None else _STATUS_FOR_ERROR_KIND.get(error.kind, "escalate")
            )
            span.set_attribute("status", status)

            return AgentResult[TOut](
                agent=self.key,
                status=status,
                output=output,
                confidence=confidence,
                evidence=evidence,
                attempts=max(1, len(attempts)),
                latency_ms=int((time.monotonic() - started) * 1000),
                tokens=tokens,
                prompt_sha256=prompt_sha256,
                model=self.model,
                error=error,
            )

    async def build_prompt(self, state: RunState) -> AgentPrompt:
        """Assemble this agent's prompt. The one hook a subclass must implement.

        Async because for most agents this is where the deterministic work happens:
        calling read tools through the gateway, budgeting the result through the
        :class:`~src.harness.context_manager.ContextManager`, and rendering it into a
        template. That work is domain knowledge in full, which is why it lives here and
        not in :meth:`run`.
        """
        raise NotImplementedError

    def evidence_for(self, output: TOut, state: RunState) -> list[Evidence]:
        """Evidence the validated output rests on. Defaults to none.

        Separate from ``AgentPrompt.evidence`` because these two answer different
        questions: what the agent gathered, versus what the model actually leant on.
        Only the integration can read a payload well enough to say the second.
        """
        return []

    def confidence_for(self, output: TOut, state: RunState) -> float | None:
        """Post-calibration confidence for the validated output. Defaults to none.

        Returns the number that goes into ``AgentResult.confidence``, so an
        implementation that scores itself is expected to have already passed its
        self-reported figure through :func:`~src.harness.confidence.calibrate`. The
        signals that feed that call are domain readings of the run's artifacts, so
        deciding them here -- outside the harness -- is deliberate.
        """
        return None
