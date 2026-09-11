"""`ReplayToolGateway` — a deterministic, fixture-backed `ToolGateway`.

Used for offline demo and eval replay of the scenarios under `fixtures/scenarios/`.
Nothing here reaches the network: every read tool resolves to a file recorded by
`fixtures/README.md`'s naming rules, and a missing file is a loud replay error rather
than a silent empty result.

Two things in here are load-bearing and easy to get subtly wrong:

1. **The forbidden re-check runs first, before anything else** (`src/harness/gateway.py`'s
   `invoke` contract, steps 1-4). A forbidden tool is refused even when
   `decision.effect == "allow"`, the refusal is returned rather than raised, and because
   the check precedes every file open the refusal touches nothing at all.
2. **`get_job_logs` keeps the LAST `max_bytes`, never the first.** See
   `fixtures/README.md`: the proximate failure sits near the end of a job log, so
   `content[-max_bytes:]` is the whole point and `f.read(max_bytes)` would silently hand
   the Diagnostician runner bootstrap noise and nothing else.

The write half of the catalog (Phase 2) is synthesized, per `fixtures/README.md`: write
calls have no prior state to reproduce, so `rerun_failed_jobs` answers a plausible success
carrying `dry_run` as configured, and the tools no phase has implemented yet answer the
same `ToolError(kind="unknown")` the live gateway does -- the catalog entry is real, the
policy decision about it is real, and the trace shows both. Every write call's
`idempotency_key` is remembered per gateway instance (Appendix C), so a retry inside one
run returns the first result with `cached=True` rather than "firing" twice.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from pydantic import JsonValue

from src.harness.gateway import ToolCall, ToolError, ToolResult, ToolSpec
from src.harness.guardrails import PolicyDecision
from src.integrations.cicd.catalog import (
    CATALOG,
    IMPLEMENTED_WRITE_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    not_implemented_message,
)

logger = logging.getLogger("harness.integrations.cicd.gateway_replay")

#: PLAN.md line 222: keep the last 20 MB of a job log.
LOG_DOWNLOAD_CAP_BYTES: Final[int] = 20 * 1024 * 1024

__all__ = ["LOG_DOWNLOAD_CAP_BYTES", "READ_TOOLS", "ReplayToolGateway", "repo_slug"]


def repo_slug(repo: str) -> str:
    """`octo-org/harness-demo-repo` -> `octo-org-harness-demo-repo`.

    The `api/` filename convention of `fixtures/README.md`: the request path with the
    leading slash dropped and every remaining `/` replaced by `-`.
    """
    return repo.replace("/", "-")


class ReplayToolGateway:
    """`ToolGateway` backed by a recorded scenario directory."""

    integration = "cicd"

    def __init__(
        self,
        *,
        scenario_dir: Path,
        repo: str,
        forbidden: tuple[str, ...],
        dry_run: bool = True,
    ) -> None:
        """Bind the gateway to one scenario directory.

        `forbidden` is the gateway's **own** copy of `PolicySpec.forbidden`, wired at the
        composition root because the `invoke` signature does not supply it. It is
        deliberately not read out of the `PolicyDecision` argument: the failure mode the
        re-check exists for is a decision that never came from the engine at all, and a
        hand-forged decision would carry a hand-forged forbidden list with it.

        Required, with no default: this is the gateway's own copy of the authoritative
        safety re-check (see the module docstring), and a caller that forgets to pass it
        must get a `TypeError` at construction, not a gateway that silently refuses
        nothing. Pass `forbidden=()` explicitly if a caller genuinely wants an empty set
        -- that is a stated decision, not an accident.
        """
        self.scenario_dir = scenario_dir
        self.repo = repo
        self.forbidden = frozenset(forbidden)
        self.dry_run = dry_run
        # Appendix C: completed write calls, by idempotency key, for the life of this
        # gateway -- which is one run, since the composition root builds one per request.
        self._completed_writes: dict[str, ToolResult] = {}

    # -- catalog ------------------------------------------------------------------
    def catalog(self) -> list[ToolSpec]:
        return list(CATALOG)

    # -- fixture resolution -------------------------------------------------------
    def _api_path(self, slug: str) -> Path:
        return self.scenario_dir / "api" / f"GET_{slug}.json"

    def _fixture_slug(self, tool: str, args: dict[str, Any]) -> str | None:
        repo = repo_slug(self.repo)
        if tool == "list_workflow_run_jobs":
            return (
                f"repos-{repo}-actions-runs-{args['run_id']}"
                f"-attempts-{args.get('attempt', 1)}-jobs"
            )
        if tool in ("find_last_successful_run", "search_workflow_runs"):
            return f"repos-{repo}-actions-workflows-{args['workflow_id']}-runs"
        if tool == "compare_commits":
            return f"repos-{repo}-compare-{args['base']}-{args['head']}"
        if tool == "get_commit":
            return f"repos-{repo}-commits-{args['sha']}"
        if tool == "get_file_contents":
            return f"repos-{repo}-contents-{str(args['path']).replace('/', '-')}"
        return None

    def _read_job_log(self, job_id: int, max_bytes: int | None) -> tuple[str, bool, int] | None:
        path = self.scenario_dir / "logs" / f"job_{job_id}.txt"
        if not path.is_file():
            return None
        raw = path.read_bytes()
        cap = max_bytes if max_bytes is not None else LOG_DOWNLOAD_CAP_BYTES
        truncated = len(raw) > cap
        # THE line this class exists to get right: the tail, not the head.
        kept = raw[-cap:] if truncated else raw
        return kept.decode("utf-8", errors="replace"), truncated, len(raw)

    # -- invoke -------------------------------------------------------------------
    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        started = time.monotonic()

        def failure(kind: str, message: str, *, retryable: bool = False) -> ToolResult:
            return ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                ok=False,
                error=ToolError(kind=kind, message=message, retryable=retryable),  # type: ignore[arg-type]
                latency_ms=int((time.monotonic() - started) * 1000),
                dry_run=self.dry_run,
            )

        def success(data: dict[str, JsonValue]) -> ToolResult:
            return ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                ok=True,
                data=data,
                latency_ms=int((time.monotonic() - started) * 1000),
                dry_run=self.dry_run,
            )

        # Step 1 and 2 of the `invoke` contract: the forbidden set is checked before
        # anything else and wins over `decision.effect == "allow"`. Step 4 follows from
        # the position of this block -- nothing has been opened or fetched yet.
        if call.tool in self.forbidden:
            logger.warning(
                "refused forbidden tool %r (decision %r said %r)",
                call.tool, decision.rule_id, decision.effect,
            )
            return failure(
                "forbidden_by_policy",
                f"{call.tool!r} is in the forbidden set and is refused regardless of the "
                "decision presented with it",
            )

        if call.tool in WRITE_TOOLS:
            return self._invoke_write(call, failure, success)

        if call.tool not in READ_TOOLS:
            return failure("invalid_args", f"unknown tool {call.tool!r} for replay gateway")

        args = dict(call.args)
        try:
            if call.tool == "get_job_logs":
                job_id = int(args["job_id"])
                raw_max = args.get("max_bytes")
                result = self._read_job_log(
                    job_id, int(raw_max) if raw_max is not None else None
                )
                if result is None:
                    return failure("not_found", f"no recorded log for job {job_id}")
                content, truncated, total_bytes = result
                return success(
                    {
                        "job_id": job_id,
                        "content": content,
                        "truncated": truncated,
                        "head_dropped": truncated,
                        "total_bytes": total_bytes,
                    }
                )

            slug = self._fixture_slug(call.tool, args)
        except KeyError as exc:
            return failure("invalid_args", f"missing required argument {exc}")

        if slug is None:
            return failure("invalid_args", f"no fixture mapping for tool {call.tool!r}")

        path = self._api_path(slug)
        if not path.is_file():
            # `fixtures/README.md`: a missing `api/` file is a fixture bug, not a cold
            # start. Cold start is an empty list in a recorded response body.
            return failure("not_found", f"no recorded response at api/{path.name}")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return failure("malformed", f"recorded response api/{path.name} is not JSON: {exc}")

        return success(body if isinstance(body, dict) else {"items": body})

    def _invoke_write(
        self,
        call: ToolCall,
        failure: Callable[..., ToolResult],
        success: Callable[[dict[str, JsonValue]], ToolResult],
    ) -> ToolResult:
        """The synthesized write half. Reached only after the forbidden re-check."""
        if call.idempotency_key is not None:
            cached = self._completed_writes.get(call.idempotency_key)
            if cached is not None:
                return cached.model_copy(update={"call_id": call.call_id, "cached": True})

        if call.tool not in IMPLEMENTED_WRITE_TOOLS:
            return failure("unknown", not_implemented_message(call.tool))

        # `rerun_failed_jobs`: the one write this phase can take. Replay has no run to
        # re-run, so the response is the shape the live gateway returns, with `dry_run`
        # saying whether anything *would* have been touched.
        raw_run_id = call.args.get("run_id")
        if not isinstance(raw_run_id, int) or isinstance(raw_run_id, bool):
            return failure("invalid_args", "rerun_failed_jobs requires an integer run_id")
        result = success(
            {
                "run_id": raw_run_id,
                "rerun_requested": not self.dry_run,
                "dry_run": self.dry_run,
                "note": (
                    "replay gateway: no workflow run exists to re-run; response synthesized"
                ),
            }
        )
        if call.idempotency_key is not None:
            self._completed_writes[call.idempotency_key] = result
        return result

    async def aclose(self) -> None:
        """Nothing to close: this gateway holds no client and no connection."""
        return None
