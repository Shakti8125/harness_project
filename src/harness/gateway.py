"""Tool sandboxing seam.

Frozen transcription of PLAN.md Appendix A.4. The harness knows that tools have a
name, a JSON Schema for their arguments and a side-effect class; it never knows what
any concrete tool does. The catalog is supplied by the integration that implements
:class:`ToolGateway`.

Per PLAN.md, ``invoke`` never raises for a remote failure: the failure comes back as
``ToolResult(ok=False, error=ToolError(...))`` so it becomes evidence an agent can
reason about and the harness gets one uniform place to classify and trace it.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from src.harness.guardrails import PolicyDecision


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: dict[str, JsonValue]                     # JSON Schema
    side_effect: Literal["read", "write", "destructive"]
    idempotent: bool
    timeout_s: float = 30.0


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str                                           # "tc_" + 12 hex
    tool: str
    args: dict[str, JsonValue]
    idempotency_key: str | None = None                     # required when side_effect != "read"


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["timeout", "rate_limited", "auth", "not_found", "malformed",
                  "forbidden_by_policy", "upstream_5xx", "invalid_args", "unknown"]
    message: str                                           # redacted
    retryable: bool
    retry_after_s: float | None = None
    http_status: int | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    tool: str
    ok: bool
    data: dict[str, JsonValue] | None = None
    error: ToolError | None = None
    latency_ms: int
    attempts: int = 1
    cached: bool = False
    dry_run: bool = False


class ToolGateway(Protocol):
    """A sandboxed set of callable tools, implemented by an integration."""

    integration: str

    def catalog(self) -> list[ToolSpec]: ...

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        """Run one tool call -- after independently re-checking that the tool is permitted.

        ``decision`` is the :class:`PolicyDecision` the caller already obtained from the
        policy engine. Requiring it as an argument makes it impossible to reach a tool
        without having consulted the engine, and hands the implementation the exact
        decision to record beside the result. It is passed in to be *recorded*, not to be
        *believed*.

        PLAN.md places two independent enforcement points on this path: the caller
        consults the engine before planning, and the gateway checks again here. **The
        gateway is the authoritative one** -- it is the last code that runs before an
        external system is touched, and the failure mode both checks exist for is a call
        that never went through the planner at all.

        The contract, in the order it must be implemented:

        1. **Re-check ``call.tool`` against the forbidden set first, before anything
           else.** The forbidden set comes from the same ``PolicySpec.forbidden`` the
           engine was built from and is held by the implementation itself. It is never
           read out of ``decision``, and no field of ``decision`` may skip, shorten or
           satisfy this step.
        2. **A forbidden tool is refused even when ``decision.effect == "allow"``.** That
           pairing is not a contradiction to be resolved in the decision's favour: it is
           the precise input this step exists to stop, because a hand-forged decision is
           indistinguishable by inspection from an engine-issued one, and a model that
           can invent a tool name can invent the paperwork for it too. "The decision said
           allow, so the call proceeds" is the wrong answer. Getting this wrong is worse
           than having no second check at all, because the trace then shows a decision
           was consulted next to a forbidden call that ran -- it reads as though
           something verified it.
        3. **The refusal is returned, not raised**: ``ToolResult(call_id=call.call_id,
           tool=call.tool, ok=False, error=ToolError(kind="forbidden_by_policy",
           retryable=False, ...))``. Only programming errors raise (see this module's
           docstring); a refusal is evidence, so it comes back as data an agent can
           reason about and the trace can show. No retry can change the outcome, hence
           ``retryable=False``.
        4. **The refusal costs zero outbound requests.** Because step 1 runs first, a
           forbidden tool opens no connection and sends no request to the external
           system -- nothing whatsoever reaches the wire. This is observable at the
           transport layer, and is meant to be asserted there.

        A permitted tool that then fails remotely also comes back as
        ``ToolResult(ok=False, error=ToolError(...))``, with the ``kind`` that classifies
        the remote failure.
        """
        ...

    async def aclose(self) -> None: ...
