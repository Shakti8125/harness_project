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

The write half of the catalog is synthesized, per `fixtures/README.md`: write calls have
no prior state to reproduce, so every implemented write answers a plausible success in the
shape the live gateway returns, carrying `dry_run` as configured, with identifiers derived
deterministically from the call's arguments (a replayed approval reads the same way twice).
A catalog write tool no gateway implements answers the same `ToolError(kind="unknown")` the
live gateway does -- the catalog entry is real, the policy decision about it is real, and
the trace shows both. Every write call's `idempotency_key` is remembered per gateway
instance (Appendix C), so a retry inside one run returns the first result with
`cached=True` rather than "firing" twice.

Phase 5: with a `recorder`, every `invoke` is a `gateway.invoke` span in the same shape
`GitHubToolGateway` writes, so a replayed trace carries the `gateway` component the live
one does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from pydantic import JsonValue

from src.harness.gateway import ToolCall, ToolError, ToolResult, ToolSpec
from src.harness.guardrails import PolicyDecision
from src.harness.observability import TraceRecorder
from src.integrations.cicd import url_segments
from src.integrations.cicd.catalog import (
    CATALOG,
    IMPLEMENTED_WRITE_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    not_implemented_message,
    side_effect_of,
)

logger = logging.getLogger("harness.integrations.cicd.gateway_replay")

#: PLAN.md line 222: keep the last 20 MB of a job log.
LOG_DOWNLOAD_CAP_BYTES: Final[int] = 20 * 1024 * 1024

__all__ = [
    "LOG_DOWNLOAD_CAP_BYTES", "READ_TOOLS", "ReplayToolGateway", "fixture_slug_for", "repo_slug",
]


def repo_slug(repo: str) -> str:
    """`octo-org/harness-demo-repo` -> `octo-org-harness-demo-repo`.

    The `api/` filename convention of `fixtures/README.md`: the request path with the
    leading slash dropped and every remaining `/` replaced by `-`.
    """
    return repo.replace("/", "-")


def fixture_slug_for(repo: str, tool: str, args: dict[str, Any]) -> str | None:
    """The `api/GET_<slug>.json` slug a read tool's recording lives at, per
    `fixtures/README.md`; `None` for `get_job_logs` (which has no `api/` file -- the raw
    text is `logs/job_<id>.txt`) and for any tool that is not a read.

    Module-level so `scripts/record_fixture.py` writes the file the replay gateway will
    look for: one rule, two callers.

    Every part of the slug is held to the live gateway's rules first (SEC-24): the slug is
    a file name under the scenario directory, and a `\\` in any caller- or model-supplied
    part walked out of it on Windows. The ids are integers; a bad part raises `ValueError`.
    """
    slug = repo_slug(url_segments.repo_name(repo))
    if tool == "list_workflow_run_jobs":
        return (
            f"repos-{slug}-actions-runs-{int(args['run_id'])}"
            f"-attempts-{int(args.get('attempt', 1))}-jobs"
        )
    if tool in ("find_last_successful_run", "search_workflow_runs"):
        # Appendix D's chain asks the same path twice with a different `branch=` query
        # (the failing branch, then the default branch), and the README's rule for two
        # recordings of one path is a suffix: `-branch-<name>`, slashes as dashes.
        base = f"repos-{slug}-actions-workflows-{int(args['workflow_id'])}-runs"
        raw_branch = args.get("branch")
        if not raw_branch:
            return base
        branch = url_segments.ref_name(raw_branch, name="branch")
        return f"{base}-branch-{branch.replace('/', '-')}"
    if tool == "compare_commits":
        base_sha = url_segments.sha(args["base"], name="base")
        head_sha = url_segments.sha(args["head"], name="head")
        return f"repos-{slug}-compare-{base_sha}-{head_sha}"
    if tool == "get_commit":
        return f"repos-{slug}-commits-{url_segments.sha(args['sha'])}"
    if tool == "get_file_contents":
        return f"repos-{slug}-contents-{url_segments.file_path(args['path']).replace('/', '-')}"
    return None


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


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
        recorder: TraceRecorder | None = None,
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
        self.recorder = recorder
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
        return fixture_slug_for(self.repo, tool, args)

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
        """`_invoke`, under one `gateway.invoke` span when a recorder is bound.

        The forbidden re-check inside `_invoke` still runs before anything else -- the
        span is a local write, not an outbound request, and recording a refusal is what
        makes the refusal visible in the trace (the live gateway does the same).
        """
        if self.recorder is None:
            return await self._invoke(call, decision)
        async with self.recorder.span(
            "gateway.invoke", "gateway", tool=call.tool, side_effect=side_effect_of(call.tool),
            rule_id=decision.rule_id,
        ) as span:
            result = await self._invoke(call, decision)
            span.set_attribute("ok", result.ok)
            span.set_attribute("dry_run", result.dry_run)
            span.set_attribute("cached", result.cached)
            if result.error is not None:
                span.set_attribute("error_kind", result.error.kind)
            return result

    async def _invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
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

            # SEC-24: `fixture_slug_for` holds every part of the file name to the live
            # gateway's rules and raises `ValueError` for one that fails them.
            slug = self._fixture_slug(call.tool, args)
        except KeyError as exc:
            return failure("invalid_args", f"missing required argument {exc}")
        except ValueError as exc:
            return failure("invalid_args", str(exc))

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

        try:
            data = self._synthesize_write(call)
        except KeyError as exc:
            return failure("invalid_args", f"missing required argument {exc}")
        except (TypeError, ValueError) as exc:
            return failure("invalid_args", f"bad argument: {exc}")
        result = success(data)
        if call.idempotency_key is not None:
            self._completed_writes[call.idempotency_key] = result
        return result

    def _synthesize_write(self, call: ToolCall) -> dict[str, JsonValue]:
        """The live gateway's response shape for a write, without a repository behind it.

        Replay has no run to re-run, no branch to create, no PR to open, so each answer
        is the plausible success the live tool would return: identifiers are derived from
        a hash of the arguments (stable across replays), `dry_run` says whether anything
        *would* have been touched, and a `note` says the response was synthesized.
        """
        args = dict(call.args)
        digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        # A deterministic small integer for a PR / issue number, and a sha-shaped id.
        number = int(digest[:6], 16) % 9000 + 1000
        note = "replay gateway: response synthesized; nothing was written"
        repo_url = f"https://github.com/{self.repo}"
        if call.tool == "rerun_failed_jobs":
            raw_run_id = args.get("run_id")
            if not isinstance(raw_run_id, int) or isinstance(raw_run_id, bool):
                raise ValueError("rerun_failed_jobs requires an integer run_id")
            return {
                "run_id": raw_run_id,
                "rerun_requested": not self.dry_run,
                "dry_run": self.dry_run,
                "note": "replay gateway: no workflow run exists to re-run; response synthesized",
            }
        if call.tool == "create_branch":
            name = str(args["name"])
            return {
                "ref": f"refs/heads/{name}",
                "sha": str(args["from_sha"]),
                "created": not self.dry_run,
                "dry_run": self.dry_run,
                "note": note,
            }
        if call.tool == "create_or_update_file":
            return {
                "path": str(args["path"]),
                "branch": str(args["branch"]),
                "commit_sha": digest[:40],
                "content_sha": digest[40:64] + digest[:16],
                "written": not self.dry_run,
                "dry_run": self.dry_run,
                "note": note,
            }
        if call.tool == "open_pull_request":
            return {
                "number": number,
                "html_url": f"{repo_url}/pull/{number}",
                "head": str(args["head"]),
                "base": str(args["base"]),
                "draft": bool(args.get("draft", True)),
                "labels": [str(label) for label in _as_list(args.get("labels"))],
                "opened": not self.dry_run,
                "dry_run": self.dry_run,
                "note": note,
            }
        if call.tool == "create_issue":
            return {
                "number": number,
                "html_url": f"{repo_url}/issues/{number}",
                "title": str(args["title"]),
                "labels": [str(label) for label in _as_list(args.get("labels"))],
                "filed": not self.dry_run,
                "dry_run": self.dry_run,
                "note": note,
            }
        raise ValueError(f"no synthesized response for {call.tool!r}")

    async def aclose(self) -> None:
        """Nothing to close: this gateway holds no client and no connection."""
        return None
