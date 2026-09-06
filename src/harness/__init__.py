"""Domain-agnostic agent control plane.

This package is the portfolio artifact: orchestration, context budgeting, tool
sandboxing, memory, claim verification, policy, tracing, recovery and calibration,
with no knowledge of any particular problem domain. Domain payloads, tool catalogs,
claim checkers and policy files live in an integration package and are injected at a
composition root.

Nothing is re-exported here on purpose: importing a submodule by name keeps the
dependency edges between harness components visible in every call site.
"""

from __future__ import annotations

__all__: list[str] = []
