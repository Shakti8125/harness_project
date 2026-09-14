"""`GitHubToolGateway` — the live, GitHub-REST-backed `ToolGateway`.

Implements the CI/CD tool catalog of PLAN.md Appendix A.4 against the real API: every
read tool and, since Phase 5, every write tool the policy can allow -- `rerun_failed_jobs`,
`create_branch`, `create_or_update_file`, `open_pull_request` and `create_issue`. A catalog
write tool without a body here answers `ToolError(kind="unknown")` naming the phase that
implements it; see `catalog.IMPLEMENTED_WRITE_TOOLS`.

Three things here are load-bearing:

1. **The forbidden re-check runs first and costs zero requests** (`src/harness/gateway.py`,
   `invoke` contract, steps 1-4). `forbidden` is a required constructor argument with no
   default -- `review.md` finding 12's pattern, which matters *here* more than in replay
   because this is the gateway where a forged `allow` would otherwise reach a real repo.
2. **Appendix B.2 is implemented as one request policy**, `_request`, not as per-tool
   special cases: timeouts retry twice; a primary rate limit sleeps to the reset (capped)
   and retries once; a secondary limit honours `Retry-After`; 401 and 403 are `auth` with
   no retry; 404 is `not_found` as data; 5xx retries three times with exponential backoff;
   a non-JSON body is `malformed`. Each write tool adds Appendix C's idempotency rules on
   top: a 403 "already in progress" re-run is success and an attempt that already advanced
   is a no-op; a branch that exists is fetched and returned (`cached=True`); a file write
   passes the current blob sha and treats a 409 as a hard stop; an open PR for the same
   head is returned rather than duplicated; an issue carrying the signature marker is
   commented on rather than re-filed.
3. **`HARNESS_DRY_RUN` is honoured at the last possible moment.** A dry run performs
   every read and every pre-check for a write, then returns `ToolResult(dry_run=True)`
   instead of sending the mutating request. What would have happened is in the result.

Never logs or returns the token: it lives in the client's default headers and nothing
here formats a request for display. The redirect the log endpoint answers with points at
a different origin, and the follow-up request is sent **without** the `Authorization`
header, explicitly, rather than trusting the client to strip it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any, Final, Literal

import httpx
from pydantic import JsonValue

from src.harness.gateway import ToolCall, ToolError, ToolResult, ToolSpec
from src.harness.guardrails import PolicyDecision
from src.harness.observability import TraceRecorder
from src.integrations.cicd.catalog import (
    CATALOG,
    IMPLEMENTED_WRITE_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    not_implemented_message,
    side_effect_of,
)

logger = logging.getLogger("harness.integrations.cicd.gateway_github")

#: PLAN.md "Concrete numbers in one place".
CONNECT_TIMEOUT_S: Final[float] = 10.0
READ_TIMEOUT_S: Final[float] = 30.0
TIMEOUT_RETRIES: Final[int] = 2
SERVER_ERROR_RETRIES: Final[int] = 3
BACKOFF_BASE_S: Final[float] = 0.5
BACKOFF_CAP_S: Final[float] = 8.0
#: B.2: a primary rate limit is slept out only if the reset is within this long.
RATE_LIMIT_MAX_SLEEP_S: Final[float] = 60.0
SECONDARY_LIMIT_RETRIES: Final[int] = 2
#: PLAN.md line 222: keep the LAST 20 MB of a job log.
LOG_DOWNLOAD_CAP_BYTES: Final[int] = 20 * 1024 * 1024
#: `per_page` on the jobs listing: GitHub's maximum.
JOBS_PAGE_SIZE: Final[int] = 100
#: How many pages of open issues `create_issue` reads looking for the one carrying this
#: signature's marker before it files a new one (Phase 5 audit finding 7). Five pages of
#: 100, filtered to the labels the agent files with: a repository with more open agent
#: issues than that has a bigger problem than a duplicate.
ISSUE_SEARCH_MAX_PAGES: Final[int] = 5
#: B.2: how much of a body that failed to parse is kept as evidence.
MALFORMED_EVIDENCE_CHARS: Final[int] = 500

API_VERSION: Final[str] = "2022-11-28"
#: Phrasings of "this run is already re-running" a 403 on the re-run endpoint may carry.
_ALREADY_RUNNING_PHRASES: Final[tuple[str, ...]] = (
    "in progress", "already running", "currently running", "is running",
)
#: Phrasings of "it already exists" a 422 on a create endpoint may carry (Appendix C:
#: `Reference already exists`, `A pull request already exists`).
_ALREADY_EXISTS_PHRASES: Final[tuple[str, ...]] = ("already exists", "reference already")
#: Appendix C: the marker `create_issue` embeds so a duplicate is commented on, not filed.
ISSUE_SIGNATURE_MARKER: Final[str] = "<!-- harness:signature:{signature_id} -->"
_ZIP_MAGIC: Final[bytes] = b"PK\x03\x04"

Sleep = Callable[[float], Awaitable[None]]


class _Failure(Exception):
    """A classified request failure, raised inside `_request` and turned into a
    `ToolError` at the `invoke` boundary. Never escapes `invoke`."""

    def __init__(
        self,
        kind: Literal[
            "timeout", "rate_limited", "auth", "not_found", "malformed",
            "upstream_5xx", "invalid_args", "unknown",
        ],
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.http_status = http_status

    def as_error(self) -> ToolError:
        return ToolError(
            kind=self.kind,
            message=self.message,
            retryable=self.retryable,
            retry_after_s=self.retry_after_s,
            http_status=self.http_status,
        )


def _backoff(attempt: int) -> float:
    """Exponential from `BACKOFF_BASE_S`, capped at `BACKOFF_CAP_S`, with full jitter."""
    return random.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt)))  # noqa: S311


def _message_of(response: httpx.Response) -> str:
    """The API's own `message`, when the body is JSON and carries one -- joined with
    every `errors[].message`, because a 422's specific phrase ("A pull request already
    exists for …") lives under `errors[]` while `message` says only "Validation Failed"."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    message = body.get("message")
    if isinstance(message, str) and message:
        parts.append(message)
    errors = body.get("errors")
    if isinstance(errors, list):
        for error in errors:
            detail = error.get("message") if isinstance(error, dict) else error
            if isinstance(detail, str) and detail:
                parts.append(detail)
    return "; ".join(parts)


def _tail(buffer: bytes, cap: int) -> bytes:
    return buffer[-cap:] if len(buffer) > cap else buffer


def _says_exists(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in _ALREADY_EXISTS_PHRASES)


def _obj(body: dict[str, Any], key: str) -> dict[str, Any]:
    """`body[key]` when it is an object, else `{}` -- GitHub nests the parts that matter."""
    value = body.get(key)
    return value if isinstance(value, dict) else {}


def _as_str_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _pull_summary(pull: dict[str, Any]) -> dict[str, JsonValue]:
    head = _obj(pull, "head")
    base = _obj(pull, "base")
    raw_labels = pull.get("labels")
    labels: list[JsonValue] = [
        str(label["name"])
        for label in (raw_labels if isinstance(raw_labels, list) else [])
        if isinstance(label, dict) and label.get("name")
    ]
    return {
        "number": int(pull.get("number", 0)),
        "html_url": str(pull.get("html_url", "")),
        "state": str(pull.get("state", "")),
        "draft": bool(pull.get("draft", False)),
        "head": str(head.get("ref", "")),
        "base": str(base.get("ref", "")),
        "labels": labels,
    }


class GitHubToolGateway:
    """`ToolGateway` over the GitHub REST API for one repository."""

    integration = "cicd"

    def __init__(
        self,
        *,
        repo: str,
        token: str,
        forbidden: tuple[str, ...],
        dry_run: bool = True,
        api_base: str = "https://api.github.com",
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        read_timeout_s: float = READ_TIMEOUT_S,
        recorder: TraceRecorder | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Bind the gateway to one repository and one credential.

        `forbidden` is required with no default, for the reason the module docstring
        gives. `client` and `sleep` are injectable for tests; a caller that supplies a
        client owns its lifetime (`aclose` leaves it open).
        """
        self.repo = repo
        self.forbidden = frozenset(forbidden)
        self.dry_run = dry_run
        self.recorder = recorder
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=api_base.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "agent-harness",
            },
            timeout=httpx.Timeout(
                connect=connect_timeout_s,
                read=read_timeout_s,
                write=read_timeout_s,
                pool=connect_timeout_s,
            ),
            follow_redirects=False,
        )
        # Appendix C: completed write calls, by idempotency key, for this gateway's life.
        self._completed_writes: dict[str, ToolResult] = {}

    # -- catalog ------------------------------------------------------------------
    def catalog(self) -> list[ToolSpec]:
        return list(CATALOG)

    # -- the request policy (Appendix B.2) ---------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """One API call under B.2's retry rules. Raises `_Failure` when they run out."""
        timeouts = 0
        server_errors = 0
        secondary = 0
        primary_slept = False
        while True:
            try:
                response = await self._client.request(
                    method, path, params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                timeouts += 1
                if timeouts > TIMEOUT_RETRIES:
                    raise _Failure(
                        "timeout",
                        f"{method} {path} timed out after {timeouts} attempt(s)",
                        retryable=True,
                    ) from exc
                await self._sleep(_backoff(timeouts - 1))
                continue
            except httpx.HTTPError as exc:
                raise _Failure(
                    "unknown", f"{method} {path} failed at the transport: {type(exc).__name__}",
                    retryable=True,
                ) from exc

            status = response.status_code
            if status < 400:
                return response

            if status == 401:
                raise _Failure(
                    "auth", "bad credentials (401); the token was rejected",
                    http_status=401,
                )

            if status == 403:
                remaining = response.headers.get("x-ratelimit-remaining")
                retry_after = response.headers.get("retry-after")
                if remaining == "0":
                    reset = response.headers.get("x-ratelimit-reset")
                    try:
                        wait = (
                            max(0.0, float(reset) - time.time())
                            if reset else RATE_LIMIT_MAX_SLEEP_S
                        )
                    except ValueError:
                        # A malformed reset header is still a rate limit, not a bad
                        # argument (review finding 4); wait the cap, as for a missing one.
                        wait = RATE_LIMIT_MAX_SLEEP_S
                    if wait <= RATE_LIMIT_MAX_SLEEP_S and not primary_slept:
                        primary_slept = True
                        await self._sleep(wait)
                        continue
                    raise _Failure(
                        "rate_limited",
                        f"primary rate limit exhausted; resets in {wait:.0f}s",
                        retryable=True,
                        retry_after_s=wait,
                        http_status=403,
                    )
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = RATE_LIMIT_MAX_SLEEP_S
                    secondary += 1
                    if secondary <= SECONDARY_LIMIT_RETRIES and wait <= RATE_LIMIT_MAX_SLEEP_S:
                        await self._sleep(wait)
                        continue
                    raise _Failure(
                        "rate_limited",
                        f"secondary rate limit; retry after {wait:.0f}s",
                        retryable=True,
                        retry_after_s=wait,
                        http_status=403,
                    )
                scopes = response.headers.get("x-accepted-oauth-scopes", "")
                detail = _message_of(response)
                raise _Failure(
                    "auth",
                    "forbidden (403): "
                    + (detail or "insufficient permissions")
                    + (f"; accepted scopes: {scopes}" if scopes else ""),
                    http_status=403,
                )

            if status == 404:
                raise _Failure(
                    "not_found", f"{method} {path} returned 404", http_status=404
                )

            if status >= 500:
                server_errors += 1
                if server_errors > SERVER_ERROR_RETRIES:
                    raise _Failure(
                        "upstream_5xx",
                        f"{method} {path} returned {status} after {server_errors} attempt(s)",
                        retryable=True,
                        http_status=status,
                    )
                await self._sleep(_backoff(server_errors - 1))
                continue

            # 409 / 422 are answered here as `unknown` with the status attached; the write
            # tools below catch the ones Appendix C gives a meaning to (a 422 "already
            # exists" is success, a 409 on a file write is a hard stop) and let the rest
            # surface as the tool failure they are.
            raise _Failure(
                "unknown",
                f"{method} {path} returned {status}: {_message_of(response) or 'no message'}",
                http_status=status,
            )

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise _Failure(
                "malformed",
                "non-JSON body from GET "
                f"{path}: {response.text[:MALFORMED_EVIDENCE_CHARS]!r}",
                http_status=response.status_code,
            ) from exc

    async def _send_json(
        self, method: str, path: str, json_body: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._request(method, path, json_body=json_body)
        try:
            body = response.json()
        except ValueError as exc:
            raise _Failure(
                "malformed",
                f"non-JSON body from {method} {path}: "
                f"{response.text[:MALFORMED_EVIDENCE_CHARS]!r}",
                http_status=response.status_code,
            ) from exc
        return body if isinstance(body, dict) else {"items": body}

    async def _post_json(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        return await self._send_json("POST", path, json_body)

    async def _put_json(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        return await self._send_json("PUT", path, json_body)

    # -- the log endpoint ---------------------------------------------------------
    async def _download_log(self, job_id: int, cap: int) -> tuple[bytes, bool, int]:
        """Follow the one redirect, stream, keep the LAST `cap` bytes.

        Returns `(kept, head_dropped, total_bytes)`. The blob host is not the API host, so
        the follow-up is sent through a bare client with no `Authorization` header.
        """
        first = await self._request("GET", f"/repos/{self.repo}/actions/jobs/{job_id}/logs")
        if first.status_code in (301, 302, 303, 307, 308):
            location = first.headers.get("location")
            if not location:
                raise _Failure("malformed", "log redirect carried no Location header")
            async with httpx.AsyncClient(
                timeout=self._client.timeout, follow_redirects=False
            ) as anonymous:
                try:
                    async with anonymous.stream("GET", location) as blob:
                        if blob.status_code >= 400:
                            raise _Failure(
                                "upstream_5xx" if blob.status_code >= 500 else "not_found",
                                f"log blob returned {blob.status_code}",
                                retryable=blob.status_code >= 500,
                                http_status=blob.status_code,
                            )
                        # A bytearray trimmed only once it doubles the cap: constant
                        # memory (< 2 x cap) without re-copying the whole tail on
                        # every chunk, which for a 20 MB log would be quadratic.
                        window = bytearray()
                        total = 0
                        async for chunk in blob.aiter_bytes():
                            total += len(chunk)
                            window += chunk
                            if len(window) > 2 * cap:
                                del window[:-cap]
                        buffer = _tail(bytes(window), cap)
                except httpx.TimeoutException as exc:
                    raise _Failure(
                        "timeout", "log download timed out", retryable=True
                    ) from exc
                except httpx.HTTPError as exc:
                    # The same transport-error classification the API-host path
                    # applies: `invoke` never raises for a remote failure, and the
                    # blob host is a remote (review finding 1).
                    raise _Failure(
                        "unknown",
                        f"log download failed at the transport: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
        else:
            buffer = first.content
            total = len(buffer)
            buffer = _tail(buffer, cap)

        if buffer.startswith(_ZIP_MAGIC):
            try:
                with zipfile.ZipFile(io.BytesIO(buffer)) as archive:
                    parts = [
                        archive.read(name) for name in sorted(archive.namelist())
                        if not name.endswith("/")
                    ]
            except zipfile.BadZipFile as exc:
                raise _Failure("malformed", "log archive is not a valid zip") from exc
            joined = b"\n".join(parts)
            total = len(joined)
            buffer = _tail(joined, cap)
        return buffer, total > len(buffer), total

    # -- the write tool -----------------------------------------------------------
    async def _rerun_failed_jobs(self, args: dict[str, Any]) -> dict[str, JsonValue]:
        run_id = int(args["run_id"])
        attempt = int(args["attempt"]) if args.get("attempt") is not None else None

        # Appendix C: if the attempt has already advanced past the one the bundle saw,
        # someone (or a previous run of this harness) already re-ran it.
        current = await self._get_json(f"/repos/{self.repo}/actions/runs/{run_id}")
        current_attempt = int(current.get("run_attempt", 0)) if isinstance(current, dict) else 0
        if attempt is not None and current_attempt > attempt:
            return {
                "run_id": run_id,
                "rerun_requested": False,
                "already_advanced": True,
                "current_attempt": current_attempt,
                "dry_run": self.dry_run,
            }
        if self.dry_run:
            return {
                "run_id": run_id,
                "rerun_requested": False,
                "dry_run": True,
                "current_attempt": current_attempt,
                "note": "dry run: the re-run request was not sent",
            }
        try:
            await self._request(
                "POST", f"/repos/{self.repo}/actions/runs/{run_id}/rerun-failed-jobs"
            )
        except _Failure as failure:
            # Appendix C: "cannot re-run; the run is already in progress" is success.
            # Matched on a few phrasings because the exact text is PLAN's paraphrase
            # and the real message is unverified offline (review note); a phrasing
            # this misses degrades to `auth` -> `tool_failure`, which is loud, not
            # silent.
            lowered = failure.message.lower()
            if failure.http_status == 403 and any(
                phrase in lowered for phrase in _ALREADY_RUNNING_PHRASES
            ):
                return {
                    "run_id": run_id,
                    "rerun_requested": False,
                    "already_in_progress": True,
                    "dry_run": False,
                }
            raise
        return {"run_id": run_id, "rerun_requested": True, "dry_run": False}

    # -- the PR-writing and issue tools (Phase 5, Appendix C) -----------------------
    def _would_have(self, args: dict[str, Any], **found: JsonValue) -> dict[str, JsonValue]:
        """Appendix E's dry-run answer: what the write *would* have done, plus what the
        pre-checks found. `content_b64` is never echoed -- the drafted file is already in
        the plan, and a second copy in `executed[]` is a second thing to digest."""
        shown = {k: v for k, v in args.items() if k != "content_b64"}
        if "content_b64" in args:
            shown["content_bytes"] = len(str(args["content_b64"]))
        return {"would_have": shown, "dry_run": True, **found}

    async def _get_ref(self, name: str) -> dict[str, JsonValue] | None:
        """The branch ref, or `None` when it does not exist."""
        try:
            body = await self._get_json(f"/repos/{self.repo}/git/ref/heads/{name}")
        except _Failure as failure:
            if failure.kind == "not_found":
                return None
            raise
        if not isinstance(body, dict):
            raise _Failure("malformed", "ref response was not an object")
        obj = _obj(body, "object")
        return {"ref": str(body.get("ref", f"refs/heads/{name}")), "sha": str(obj.get("sha", ""))}

    async def _create_branch(self, args: dict[str, Any]) -> dict[str, JsonValue]:
        name = str(args["name"])
        from_sha = str(args["from_sha"])
        existing = await self._get_ref(name)
        if existing is not None:
            # Appendix C: an existing branch is returned as-is, and the result says so.
            return {**existing, "created": False, "already_exists": True, "dry_run": self.dry_run}
        if self.dry_run:
            return self._would_have(args, existed=False)
        try:
            body = await self._post_json(
                f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{name}", "sha": from_sha}
            )
        except _Failure as failure:
            if failure.http_status == 422 and _says_exists(failure.message):
                existing = await self._get_ref(name)
                if existing is not None:
                    return {
                        **existing, "created": False, "already_exists": True, "dry_run": False,
                    }
            raise
        obj = _obj(body, "object")
        return {
            "ref": str(body.get("ref", f"refs/heads/{name}")),
            "sha": str(obj.get("sha", from_sha)),
            "created": True,
            "dry_run": False,
        }

    async def _current_blob_sha(self, path: str, branch: str) -> str | None:
        try:
            body = await self._get_json(
                f"/repos/{self.repo}/contents/{path.lstrip('/')}", {"ref": branch}
            )
        except _Failure as failure:
            if failure.kind == "not_found":
                return None  # a new file
            raise
        if isinstance(body, dict) and isinstance(body.get("sha"), str):
            return str(body["sha"])
        raise _Failure("malformed", f"contents response for {path!r} carried no sha")

    async def _create_or_update_file(self, args: dict[str, Any]) -> dict[str, JsonValue]:
        path = str(args["path"]).lstrip("/")
        branch = str(args["branch"])
        content_b64 = str(args["content_b64"])
        message = str(args["message"])
        # Appendix C: pass the current blob sha, so a write that raced another writer
        # answers 409 instead of clobbering. The caller may supply it; else it is read.
        sha = str(args["sha"]) if args.get("sha") else await self._current_blob_sha(path, branch)
        if self.dry_run:
            return self._would_have(args, current_sha=sha, path=path, branch=branch)
        payload: dict[str, Any] = {"message": message, "content": content_b64, "branch": branch}
        if sha is not None:
            payload["sha"] = sha
        try:
            body = await self._put_json(f"/repos/{self.repo}/contents/{path}", payload)
        except _Failure as failure:
            if failure.http_status == 409:
                # Appendix C: someone else wrote it since it was read. A hard stop, not a
                # retry -- overwriting would discard their change.
                raise _Failure(
                    "unknown",
                    f"{path!r} changed on {branch!r} since it was read (409); not clobbering",
                    http_status=409,
                ) from failure
            raise
        commit = _obj(body, "commit")
        content = _obj(body, "content")
        return {
            "path": path,
            "branch": branch,
            "commit_sha": str(commit.get("sha", "")),
            "content_sha": str(content.get("sha", "")),
            "written": True,
            "dry_run": False,
        }

    async def _find_open_pull(self, head: str, base: str | None) -> dict[str, JsonValue] | None:
        owner = self.repo.split("/", 1)[0]
        params: dict[str, Any] = {"head": f"{owner}:{head}", "state": "open", "per_page": 5}
        if base:
            params["base"] = base
        body = await self._get_json(f"/repos/{self.repo}/pulls", params)
        pulls = body if isinstance(body, list) else []
        for pull in pulls:
            if isinstance(pull, dict) and pull.get("number") is not None:
                return _pull_summary(pull)
        return None

    async def _open_pull_request(self, args: dict[str, Any]) -> dict[str, JsonValue]:
        head = str(args["head"])
        base = str(args["base"])
        labels: list[JsonValue] = [str(label) for label in args.get("labels") or []]
        existing = await self._find_open_pull(head, base)
        if existing is not None:
            # Appendix C: never two PRs for one signature. The labels are reconciled
            # rather than assumed: a label call that failed after the PR was created
            # (below) leaves an unlabelled PR, and this pre-check is the retry's only
            # path to it (Phase 5 audit finding 6).
            have = [str(label) for label in _as_str_list(existing.get("labels"))]
            missing: list[JsonValue] = [label for label in labels if label not in have]
            if self.dry_run:
                return {
                    **existing, "opened": False, "already_exists": True,
                    "labels_missing": missing, "dry_run": True,
                }
            if missing:
                await self._post_json(
                    f"/repos/{self.repo}/issues/{existing['number']}/labels",
                    {"labels": missing},
                )
            return {
                **existing, "labels": [*have, *missing], "labels_applied": missing,
                "opened": False, "already_exists": True, "dry_run": False,
            }
        if self.dry_run:
            return self._would_have(args, existed=False)
        payload = {
            "title": str(args["title"]),
            "body": str(args["body"]),
            "head": head,
            "base": base,
            # PLAN's obligation `draft_only`: the gateway never opens a ready-for-review PR.
            "draft": True,
        }
        try:
            body = await self._post_json(f"/repos/{self.repo}/pulls", payload)
        except _Failure as failure:
            if failure.http_status == 422 and _says_exists(failure.message):
                existing = await self._find_open_pull(head, base)
                if existing is not None:
                    return {**existing, "opened": False, "already_exists": True, "dry_run": False}
            raise
        summary = _pull_summary(body)
        if labels:
            # Labels ride on the issue side of a PR. A failure here is reported as the
            # PR's failure: `no_auto_merge`/`label:agent-generated` are obligations, and
            # an unlabelled agent PR is not the artifact the policy approved. The error
            # names the PR that now exists, so a person can find it and the next
            # attempt's pre-check applies the labels it lacks.
            try:
                await self._post_json(
                    f"/repos/{self.repo}/issues/{summary['number']}/labels", {"labels": labels}
                )
            except _Failure as failure:
                raise _Failure(
                    failure.kind,
                    f"draft pull request #{summary['number']} was opened "
                    f"({summary['html_url']}) but labels {labels!r} could not be applied: "
                    f"{failure.message}",
                    retryable=failure.retryable,
                    retry_after_s=failure.retry_after_s,
                    http_status=failure.http_status,
                ) from failure
        return {**summary, "labels": labels, "opened": True, "dry_run": False}

    async def _find_marked_issue(
        self, marker: str, labels: list[JsonValue]
    ) -> dict[str, JsonValue] | None:
        """The open issue whose body carries `marker`, filtered to the labels the agent
        files with (dispatch decision 7) and paged past the first hundred (Phase 5 audit
        finding 7). A label removed by hand from the marked issue hides it from this
        filter and a second issue is filed; the marker in its body still says why."""
        params: dict[str, Any] = {"state": "open", "per_page": JOBS_PAGE_SIZE}
        if labels:
            params["labels"] = ",".join(str(label) for label in labels)
        for page in range(1, ISSUE_SEARCH_MAX_PAGES + 1):
            body = await self._get_json(
                f"/repos/{self.repo}/issues", {**params, "page": page}
            )
            issues = body if isinstance(body, list) else []
            for issue in issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                if marker in str(issue.get("body") or ""):
                    return {
                        "number": int(issue.get("number", 0)),
                        "html_url": str(issue.get("html_url", "")),
                        "title": str(issue.get("title", "")),
                    }
            if len(issues) < JOBS_PAGE_SIZE:
                break
        return None

    async def _create_issue(self, args: dict[str, Any]) -> dict[str, JsonValue]:
        title = str(args["title"])
        text = str(args["body"])
        labels: list[JsonValue] = [str(label) for label in args.get("labels") or []]
        signature_id = str(args["signature_id"]) if args.get("signature_id") else None
        marker = ISSUE_SIGNATURE_MARKER.format(signature_id=signature_id) if signature_id else None
        existing = await self._find_marked_issue(marker, labels) if marker else None
        if existing is not None:
            # Appendix C: comment on the open issue for this signature instead of a second one.
            if self.dry_run:
                return self._would_have(args, commented_on=existing["number"], filed=False)
            await self._post_json(
                f"/repos/{self.repo}/issues/{existing['number']}/comments",
                {"body": f"{text}\n\n{marker}"},
            )
            return {**existing, "filed": False, "commented": True, "dry_run": False}
        if self.dry_run:
            return self._would_have(args, existed=False)
        payload: dict[str, Any] = {
            "title": title,
            "body": f"{text}\n\n{marker}" if marker else text,
            "labels": labels,
        }
        body = await self._post_json(f"/repos/{self.repo}/issues", payload)
        return {
            "number": int(body.get("number", 0)),
            "html_url": str(body.get("html_url", "")),
            "title": title,
            "labels": labels,
            "filed": True,
            "dry_run": False,
        }

    # -- dispatch -----------------------------------------------------------------
    async def _dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, JsonValue]:
        repo = self.repo
        if tool == "list_workflow_run_jobs":
            # GitHub's largest page. The rerun probe (`history._probe_rerun`) says "every
            # job passed" only when `total_count` equals the jobs it saw, so a run with
            # more jobs than this stays `pending` rather than being judged on one page
            # (Phase 3 audit finding 5).
            body = await self._get_json(
                f"/repos/{repo}/actions/runs/{int(args['run_id'])}"
                f"/attempts/{int(args.get('attempt', 1))}/jobs",
                params={"per_page": JOBS_PAGE_SIZE},
            )
        elif tool == "get_job_logs":
            raw_cap = args.get("max_bytes")
            cap = min(int(raw_cap), LOG_DOWNLOAD_CAP_BYTES) if raw_cap else LOG_DOWNLOAD_CAP_BYTES
            kept, head_dropped, total = await self._download_log(int(args["job_id"]), cap)
            return {
                "job_id": int(args["job_id"]),
                "content": kept.decode("utf-8", errors="replace"),
                "truncated": head_dropped,
                "head_dropped": head_dropped,
                "total_bytes": total,
            }
        elif tool in ("find_last_successful_run", "search_workflow_runs"):
            params: dict[str, Any] = {"per_page": int(args.get("per_page", 20))}
            if args.get("branch"):
                params["branch"] = str(args["branch"])
            params["status"] = (
                "success" if tool == "find_last_successful_run" else str(args.get("status", ""))
            ) or None
            body = await self._get_json(
                f"/repos/{repo}/actions/workflows/{int(args['workflow_id'])}/runs",
                {k: v for k, v in params.items() if v is not None},
            )
            before = args.get("before")
            if tool == "find_last_successful_run" and before and isinstance(body, dict):
                # The API lists newest first. `before` is the failing head sha, so the
                # failing run itself and anything on that same commit is not a baseline.
                # Runs created *after* the failing one on a different commit cannot be
                # told apart here without the failing run's timestamp; documented
                # approximation, exact in the push-to-branch case that is the norm.
                runs = body.get("workflow_runs")
                if isinstance(runs, list):
                    body["workflow_runs"] = [
                        run for run in runs
                        if not (isinstance(run, dict) and run.get("head_sha") == before)
                    ]
                    body["total_count"] = len(body["workflow_runs"])
        elif tool == "compare_commits":
            body = await self._get_json(
                f"/repos/{repo}/compare/{args['base']}...{args['head']}"
            )
        elif tool == "get_commit":
            body = await self._get_json(f"/repos/{repo}/commits/{args['sha']}")
        elif tool == "get_file_contents":
            body = await self._get_json(
                f"/repos/{repo}/contents/{str(args['path']).lstrip('/')}",
                {"ref": args["ref"]} if args.get("ref") else None,
            )
        elif tool == "rerun_failed_jobs":
            return await self._rerun_failed_jobs(args)
        elif tool == "create_branch":
            return await self._create_branch(args)
        elif tool == "create_or_update_file":
            return await self._create_or_update_file(args)
        elif tool == "open_pull_request":
            return await self._open_pull_request(args)
        elif tool == "create_issue":
            return await self._create_issue(args)
        else:  # pragma: no cover - guarded by the catalog checks in `invoke`
            raise _Failure("invalid_args", f"no handler for {tool!r}")
        return body if isinstance(body, dict) else {"items": body}

    # -- invoke -------------------------------------------------------------------
    async def invoke(self, call: ToolCall, decision: PolicyDecision) -> ToolResult:
        started = time.monotonic()
        side_effect = side_effect_of(call.tool)

        def finish(
            *,
            ok: bool,
            data: dict[str, JsonValue] | None = None,
            error: ToolError | None = None,
            cached: bool = False,
            dry_run: bool = False,
        ) -> ToolResult:
            return ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                ok=ok,
                data=data,
                error=error,
                latency_ms=int((time.monotonic() - started) * 1000),
                cached=cached,
                dry_run=dry_run,
            )

        # Steps 1-2 of the `invoke` contract: the forbidden set first, before any span, any
        # cache lookup, or anything that could open a connection. Step 4 -- zero outbound
        # requests -- follows from the position of this block.
        if call.tool in self.forbidden:
            logger.warning(
                "refused forbidden tool %r (decision %r said %r)",
                call.tool, decision.rule_id, decision.effect,
            )
            refusal = finish(
                ok=False,
                error=ToolError(
                    kind="forbidden_by_policy",
                    message=(
                        f"{call.tool!r} is in the forbidden set and is refused regardless "
                        "of the decision presented with it"
                    ),
                    retryable=False,
                ),
            )
            if self.recorder is not None:
                # Recorded after the refusal is decided and before returning: a span is a
                # local write, not an outbound request, and the trace should show the
                # refusal happened.
                async with self.recorder.span(
                    "gateway.invoke", "gateway", tool=call.tool, side_effect=side_effect,
                    rule_id=decision.rule_id, ok=False, error_kind="forbidden_by_policy",
                ):
                    pass
            return refusal

        async def run() -> ToolResult:
            if call.tool not in READ_TOOLS and call.tool not in WRITE_TOOLS:
                return finish(
                    ok=False,
                    error=ToolError(
                        kind="invalid_args", message=f"unknown tool {call.tool!r}", retryable=False
                    ),
                )
            if call.tool in WRITE_TOOLS:
                if call.idempotency_key is not None:
                    cached = self._completed_writes.get(call.idempotency_key)
                    if cached is not None:
                        return cached.model_copy(update={"call_id": call.call_id, "cached": True})
                if call.tool not in IMPLEMENTED_WRITE_TOOLS:
                    return finish(
                        ok=False,
                        error=ToolError(
                            kind="unknown",
                            message=not_implemented_message(call.tool),
                            retryable=False,
                        ),
                    )
            try:
                data = await self._dispatch(call.tool, dict(call.args))
            except KeyError as exc:
                return finish(
                    ok=False,
                    error=ToolError(
                        kind="invalid_args",
                        message=f"missing required argument {exc}",
                        retryable=False,
                    ),
                )
            except (TypeError, ValueError) as exc:
                return finish(
                    ok=False,
                    error=ToolError(
                        kind="invalid_args", message=f"bad argument: {exc}", retryable=False
                    ),
                )
            except _Failure as failure:
                return finish(ok=False, error=failure.as_error())

            is_dry = bool(data.get("dry_run")) if side_effect != "read" else False
            result = finish(
                ok=True,
                data=data,
                cached=bool(
                    data.get("already_in_progress")
                    or data.get("already_advanced")
                    or data.get("already_exists")
                    or data.get("commented")
                ),
                dry_run=is_dry,
            )
            if call.tool in WRITE_TOOLS and call.idempotency_key is not None:
                self._completed_writes[call.idempotency_key] = result
            return result

        if self.recorder is None:
            return await run()
        async with self.recorder.span(
            "gateway.invoke", "gateway", tool=call.tool, side_effect=side_effect,
            rule_id=decision.rule_id,
        ) as span:
            result = await run()
            span.set_attribute("ok", result.ok)
            span.set_attribute("dry_run", result.dry_run)
            span.set_attribute("cached", result.cached)
            if result.error is not None:
                span.set_attribute("error_kind", result.error.kind)
                span.set_attribute("http_status", result.error.http_status)
            return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
