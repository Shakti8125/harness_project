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

    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult: ...

    async def aclose(self) -> None: ...
