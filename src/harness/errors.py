"""Exception hierarchy for the harness.

Deliberately small. PLAN.md ("errors from the gateway are returned, not raised")
makes failures of external systems *data*: a tool failure arrives as
``ToolResult(ok=False, error=ToolError(...))``, an agent failure as
``AgentResult(status=..., error=AgentError(...))``. Everything defined here is a
programming or wiring fault -- a state the process cannot reason its way out of and
should not try to.

Appendix A does not specify this hierarchy; it is derived from the repository layout
(``harness/errors.py``) and from PLAN.md's rule that only programming errors raise.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for every exception raised by the harness itself."""


class ConfigurationError(HarnessError):
    """The harness was wired or configured in a way it cannot honour.

    Raised at construction/startup time, never in response to a remote system.
    """


class PolicySpecError(ConfigurationError):
    """A policy specification failed to load or violates a hardcoded invariant."""


class SchemaTranslationError(HarnessError):
    """A contract cannot be expressed in the constrained schema dialect.

    PLAN.md requires ``to_gemini_schema`` to reject nested discriminated unions at
    import time rather than emit a schema the provider will silently ignore.
    """


class ContractViolationError(HarnessError):
    """An internal invariant between harness components was broken.

    Signals a bug in the harness or in an injected implementation of one of its
    protocols -- for example a store returning a record it was never asked for.
    """
