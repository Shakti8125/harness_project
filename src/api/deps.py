"""The composition root.

Every dependency in this application is constructed here, once, and passed explicitly
into whatever needs it. There is no container, no registry and no plugin discovery:
PLAN.md's "the harness is a library, not a framework" reduces, in practice, to the rule
that if you want to know what an object was built with, you read this file.

Two rules this module exists to honour, both from the Phase 0 handoff:

1. **`get_settings()`, never `Settings()`.** A raw `ValidationError` from `Settings()`
   carries the offending *value* — a real secret — and would print it verbatim into the
   boot log. `get_settings()` re-raises it stripped. This is enforced by
   `tests/unit/test_settings_construction_gate.py`, so a bypass fails the build.
2. **`settings.log_char_budget` reaches `ContextBudget.total_chars`.** It is threaded
   into the one `ContextManager` every agent shares, so `HARNESS_LOG_CHAR_BUDGET` is a
   real knob rather than a value nothing reads.

And one from Phase 3: **`AppContext.initialize()` is the single startup routine.** It
applies the schema migrations through both objects that share the SQLite file. The API
lifespan calls it under Docker; `app.py` hand-calls it on the Space, because a mounted
sub-app receives no lifespan events (Phase 2 handoff §10). Anything that must happen at
startup goes in there, so there is one list to keep and two callers of it.

And one from Phase 4: **`build_fault` is the one reader of `HARNESS_FAULT_INJECT`.** It
refuses the setting outside `dev` and refuses a name no component registered, and each
fault reaches exactly the component that knows how to fail that way -- the store, the
per-run LLM client wrapper, or the Diagnostician. A fault the composition root does not
route is a fault nothing injects, which is why the known-names check lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import SecretStr

from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import EscalationRecord, RunId
from src.harness.errors import ConfigurationError
from src.harness.escalation import EscalationNotifier, WebhookNotifier
from src.harness.faults import LLM_FAULTS, Fault, FaultInjectingLlmClient, parse_fault
from src.harness.gateway import ToolGateway
from src.harness.guardrails import PolicyEngine
from src.harness.llm import GeminiClient, LlmClient
from src.harness.memory import FAULT_SQLITE_LOCKED, MemoryStore, SqliteMemoryStore
from src.harness.observability import (
    Redactor,
    SecretRegistry,
    TraceRecorder,
    install_log_redaction,
)
from src.harness.orchestrator import HEARTBEAT_INTERVAL_S, Orchestrator, new_run_id
from src.harness.recovery import RetryPolicy
from src.harness.storage import apply_migrations
from src.integrations.cicd.gateway_github import GitHubToolGateway
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import (
    FAULT_FABRICATE_CITATION,
    build_orchestrator,
    load_policy_spec,
)
from src.settings import Settings, get_settings

logger = logging.getLogger("harness.api.deps")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
FIXTURES_ROOT: Final[Path] = REPO_ROOT / "fixtures" / "scenarios"

#: Credential shapes scrubbed from every trace, on top of the exact values registered
#: from config. These name specific vendors' token formats, which is domain knowledge —
#: PLAN.md keeps it out of `harness/observability.py` deliberately, and the composition
#: root is where it belongs. Phase 5 completes PLAN's list ("Secrets never reach the
#: trace", mechanism 3): the PEM header -- widened to take the whole block when its END
#: line is present, so a pasted key is not left with only its first line redacted.
#: Precise enough to apply everywhere, a base64-encoded file body included.
#:
#: The PEM body scan is bounded (SEC-01): it stops at the next `-----BEGIN ` and after
#: `PEM_BODY_MAX_CHARS`, so input made of unterminated BEGIN lines costs linear time. The
#: unbounded lazy body scanned to the end of the input once per BEGIN, which made 120 KB
#: of caller text in a `422` take 14 s on the one event loop.
PEM_BODY_MAX_CHARS: Final[int] = 16_384
SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
        rf"(?:(?:(?!-----BEGIN )[\s\S]){{0,{PEM_BODY_MAX_CHARS}}}?"
        r"-----END [A-Z ]{0,40}PRIVATE KEY-----)?"
    ),
)

#: The `api_key=` / `token:` / `password=` assignment shapes: what a careless workflow
#: echoes into its own log. They also match the ordinary text of a source file, so the
#: `Redactor` applies them to plain text -- log lines, span attributes, served bodies --
#: and never through a base64 encoding, where the string may be a drafted file that an
#: approval later commits (Phase 5 audit finding 1; PLAN.md Phase 5 amendment 11).
HEURISTIC_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S{8,}"),
)


def build_redactor(settings: Settings) -> Redactor:
    """The one `Redactor` shape every composition site uses: the registered secrets, the
    credential shapes everywhere, the assignment heuristics on plain text only."""
    return Redactor(
        build_secret_registry(settings), SECRET_PATTERNS,
        heuristic_patterns=HEURISTIC_SECRET_PATTERNS,
    )


#: The escalation channels every run built here delivers on. `db` is real since Phase 3:
#: `MemoryStore.save_run` files the record on the `escalation` table (dispatch decision
#: 13). `webhook` (Phase 4) is added by `escalation_channels_for` when a URL is set.
ESCALATION_CHANNELS: Final[tuple[str, ...]] = ("log", "db")

#: Every `HARNESS_FAULT_INJECT` name some component knows how to inject: the store's,
#: the LLM client wrapper's, and the CI/CD integration's. Anything else is refused at
#: boot -- a misspelt fault that injects nothing would make a Verify step pass vacuously.
KNOWN_FAULTS: Final[frozenset[str]] = frozenset(
    {FAULT_SQLITE_LOCKED, *LLM_FAULTS, FAULT_FABRICATE_CITATION}
)


def webhook_url(settings: Settings) -> str | None:
    """`HARNESS_ESCALATION_WEBHOOK_URL`, or `None` when unset or blank -- an empty value
    from a `.env` line with nothing after the `=` is "no webhook", not a webhook at ""."""
    if settings.escalation_webhook_url is None:
        return None
    url = settings.escalation_webhook_url.get_secret_value().strip()
    return url or None


def escalation_channels_for(settings: Settings) -> tuple[str, ...]:
    """`log` and `db` always; `webhook` when `HARNESS_ESCALATION_WEBHOOK_URL` is set."""
    if webhook_url(settings) is not None:
        return (*ESCALATION_CHANNELS, "webhook")
    return ESCALATION_CHANNELS


def retry_policy_for(settings: Settings) -> RetryPolicy:
    """The transient (429/503) half of every agent's `RetryPolicy`, from settings.

    Only the four `HARNESS_LLM_*` knobs are set here. `max_attempts` (the schema retries)
    and `timeout_s` keep their defaults: the agents take their timeout from their own
    `timeout_s`, which `build_orchestrator_for` feeds from `gemini_timeout_s`. With every
    knob unset this equals `RetryPolicy()`.
    """
    return RetryPolicy(
        transient_max_attempts=settings.llm_transient_max_attempts,
        backoff_base_s=settings.llm_backoff_base_s,
        backoff_max_s=settings.llm_backoff_max_s,
        jitter=settings.llm_backoff_jitter,
    )


def build_fault(settings: Settings) -> Fault | None:
    """Parse `HARNESS_FAULT_INJECT`, or refuse it.

    Refused outside `env=dev` (Appendix E: "test-only; refused when env != dev") and for
    any name outside `KNOWN_FAULTS`, both at construction, so a production process with
    the variable set fails to boot rather than serving degraded runs -- and a typo fails
    the boot rather than injecting nothing.
    """
    spec = settings.fault_inject
    if spec is None or not spec.strip():
        # Unset, or set to nothing (`HARNESS_FAULT_INJECT=` in a `.env`, or compose
        # forwarding a variable the shell never exported): no fault.
        return None
    if settings.env != "dev":
        raise ConfigurationError(
            "HARNESS_FAULT_INJECT is a development-only setting and is refused when "
            f"HARNESS_ENV={settings.env!r}"
        )
    fault = parse_fault(spec)
    if fault.name not in KNOWN_FAULTS:
        raise ConfigurationError(f"unknown fault injection {fault.name!r}")
    return fault


def build_notifier(settings: Settings, redactor: Redactor) -> EscalationNotifier | None:
    """The outbound escalation channel (Appendix B.4), or `None` when no URL is set.

    The URL is a `SecretStr` and therefore already in the redactor's registry; the
    notifier scrubs every body through that same redactor before it leaves.
    """
    url = webhook_url(settings)
    if url is None:
        return None
    return WebhookNotifier(url, redactor=redactor)


class RunAdmission:
    """How many runs may be in flight at once: admission control (SEC-07).

    The semaphore limits how many runs *execute*; before this nothing limited how many
    were *accepted*, and every accepted run is a task, a heartbeat and a claim row. A run
    is admitted by the route that starts it and released when it finishes, and at most
    `max_concurrent_runs` run while up to twice that many wait -- the next request is
    refused with `429`. Not a daily quota guard: a slow trickle of runs is never refused.

    Check and increment happen with no `await` between them, so on one event loop two
    requests cannot both take the last place.
    """

    def __init__(self, max_concurrent_runs: int) -> None:
        self.capacity = max_concurrent_runs * 3
        self.in_flight = 0

    def try_admit(self) -> bool:
        if self.in_flight >= self.capacity:
            return False
        self.in_flight += 1
        return True

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


@dataclass(frozen=True)
class RunContext:
    """What it takes to build the same gateway a run used.

    Stored beside a pending approval (as opaque JSON on the `approval` row) so the deciding
    request can execute the plan through the gateway the run would have used. An
    API-layer notion: the store never reads it.
    """

    mode: Literal["live", "replay"]
    repo: str
    scenario_dir: Path | None

    def to_json(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "repo": self.repo,
            "scenario_dir": str(self.scenario_dir) if self.scenario_dir is not None else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> RunContext:
        mode = data.get("mode")
        if mode not in ("live", "replay"):
            raise ValueError(f"approval context names no run mode: {mode!r}")
        scenario_dir = data.get("scenario_dir")
        return cls(
            mode=mode,
            repo=str(data.get("repo", "")),
            scenario_dir=Path(str(scenario_dir)) if scenario_dir else None,
        )


@dataclass(frozen=True)
class AppContext:
    """Everything the routes need, built once at startup."""

    settings: Settings
    recorder: TraceRecorder
    context_manager: ContextManager
    llm: LlmClient
    run_semaphore: asyncio.Semaphore
    #: Phase 3. The one durable store, shared with the recorder's file. Defaulted from
    #: `settings.database_path` in `__post_init__` so a context assembled by hand (the
    #: test suite) gets a real store on the same temp file its recorder uses, without
    #: every fixture having to build one.
    memory: MemoryStore | None = None
    #: Built once from `policy.yaml` at startup. A malformed policy raises here, so the
    #: process fails to boot rather than serving runs a gateway would refuse nothing for;
    #: `readyz` reports `policy_loaded` off this same object. The default factory loads
    #: the same file, so a context assembled by hand (the test suite does this) enforces
    #: the same policy the composition root does.
    engine: PolicyEngine = field(default_factory=lambda: PolicyEngine(load_policy_spec()))
    #: Phase 3 fix round (audit finding 2): how often a claimed run's row is heartbeated
    #: while it waits for a concurrency slot, before the orchestrator's own heartbeat
    #: takes over. The orchestrator's constant; a knob only so a test can shrink it.
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    #: Phase 4. The parsed `HARNESS_FAULT_INJECT`, routed by `build_orchestrator_for`
    #: (LLM faults, the citation fault) and by the default memory store below (the store
    #: fault). Defaulted from the settings in `__post_init__` so a hand-built context is
    #: guarded exactly like the real one.
    fault: Fault | None = None
    #: Phase 4. The outbound escalation channel; `None` when no webhook URL is set.
    #: Defaulted from the settings in `__post_init__`.
    notifier: EscalationNotifier | None = None
    #: SEC-07. Admission control over the runs the routes accept; defaulted from
    #: `max_concurrent_runs` in `__post_init__`.
    admission: RunAdmission | None = None

    def __post_init__(self) -> None:
        # Log lines are a sink like the rows and the bodies: the recorder's Redactor is
        # installed at the log record factory, once, for every logger in the process
        # (Phase 5 audit finding 4). A hand-assembled context gets it too.
        install_log_redaction(self.recorder.redactor)
        if self.fault is None:
            object.__setattr__(self, "fault", build_fault(self.settings))
        if self.memory is None:
            object.__setattr__(
                self,
                "memory",
                build_memory_store(
                    self.settings, self.recorder.redactor, fault=self.fault,
                    recorder=self.recorder,
                ),
            )
        if self.notifier is None:
            object.__setattr__(
                self, "notifier", build_notifier(self.settings, self.recorder.redactor)
            )
        if self.admission is None:
            object.__setattr__(
                self, "admission", RunAdmission(self.settings.max_concurrent_runs)
            )

    @property
    def runs_admitted(self) -> RunAdmission:
        """`admission`, narrowed: `__post_init__` guarantees it is set."""
        assert self.admission is not None
        return self.admission

    @property
    def escalation_channels(self) -> tuple[str, ...]:
        return escalation_channels_for(self.settings)

    async def deliver_escalation(
        self, run_id: RunId, record: EscalationRecord
    ) -> EscalationRecord:
        """Deliver an escalation raised outside a run (the approval route) on the same
        outbound channel a run's own escalations use. Never raises (Appendix B.4)."""
        if self.notifier is None:
            return record
        try:
            return await self.notifier.deliver(run_id, record)
        except Exception:  # noqa: BLE001 - a delivery failure never fails the request
            logger.warning("run %s: escalation notifier raised", run_id, exc_info=True)
            return record.model_copy(
                update={"delivered_at": None, "delivery_error": "notifier raised"}
            )

    @property
    def store(self) -> MemoryStore:
        """`memory`, narrowed: `__post_init__` guarantees it is set."""
        assert self.memory is not None
        return self.memory

    async def initialize(self) -> None:
        """Everything that must happen once at startup, in order.

        Migrations first, on the file the settings name and on the recorder's (the same
        file in every real deployment; a hand-built context may split them, and the
        runner is idempotent). A migration failure raises out of here and the process
        exits non-zero -- Appendix B.3, fail fast rather than serve a half-migrated
        schema.
        """
        await apply_migrations(self.settings.database_path)
        await self.recorder.initialize()

    def scenario_dir(self, scenario: str) -> Path:
        """Resolve a replay scenario directory, refusing anything outside the root.

        The scenario name arrives from a URL path segment. Resolving it and then checking
        containment is what stops `../../etc` from reading files the app can open but has
        no business serving.
        """
        candidate = (FIXTURES_ROOT / scenario).resolve()
        if not candidate.is_relative_to(FIXTURES_ROOT.resolve()):
            raise ValueError(f"scenario {scenario!r} resolves outside the fixtures root")
        return candidate

    @property
    def forbidden(self) -> tuple[str, ...]:
        """The gateway's own copy of the forbidden set, from the same loaded policy."""
        return tuple(self.engine.spec.forbidden)

    def build_replay_gateway(self, scenario_dir: Path, repo: str) -> ReplayToolGateway:
        return ReplayToolGateway(
            scenario_dir=scenario_dir,
            repo=repo,
            forbidden=self.forbidden,
            dry_run=self.settings.dry_run,
            recorder=self.recorder,
        )

    def match_scenario(self, subject: dict[str, object]) -> tuple[str, Path] | None:
        """The recorded scenario a webhook delivery is a replay of, if any (Phase 5).

        A replay-only deployment has no repository allowlist; its recorded scenarios are
        what it can run, so a delivery is accepted when its Appendix C key -- repository,
        `workflow_run.id`, `run_attempt` -- equals that of some scenario's `webhook.json`.
        Read from disk on every call: five small files, and no cache to invalidate when a
        fixture is recorded while the process runs.
        """
        wanted = _delivery_key(subject)
        if wanted is None:
            return None
        for webhook in sorted(FIXTURES_ROOT.glob("*/webhook.json")):
            try:
                recorded = json.loads(webhook.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(recorded, dict) and _delivery_key(recorded) == wanted:
                return webhook.parent.name, webhook.parent
        return None

    def build_live_gateway(self, repo: str) -> GitHubToolGateway:
        """The real gateway, for one repository. Never built unless `gateway == "github"`.

        `dry_run` comes straight from settings and defaults to true, so the first live run
        anyone starts reads everything and writes nothing.
        """
        return GitHubToolGateway(
            repo=repo,
            token=self.settings.github_token.get_secret_value(),
            forbidden=self.forbidden,
            dry_run=self.settings.dry_run,
            api_base=str(self.settings.github_api_base),
            read_timeout_s=self.settings.github_timeout_s,
            recorder=self.recorder,
        )

    def live_allowed(self, repo: str) -> bool:
        """Live mode is opt-in twice: the gateway setting, and the repo allowlist."""
        return self.settings.gateway == "github" and repo in self.settings.allowed_repos

    def gateway_for(self, run_context: RunContext) -> ToolGateway:
        """The gateway a `RunContext` describes -- the one the run used, rebuilt.

        A live context is rebuilt only while this deployment still serves its repository
        live (SEC-04): an approval stored under a live configuration and decided after the
        deployment went back to replay, or dropped the repository, must not reach GitHub.
        """
        if run_context.mode == "live":
            if not self.live_allowed(run_context.repo):
                raise PermissionError("this deployment does not serve that repository live")
            return self.build_live_gateway(run_context.repo)
        if run_context.scenario_dir is None:  # pragma: no cover - replay always names one
            raise ValueError("a replay run must name a scenario directory")
        return self.build_replay_gateway(run_context.scenario_dir, run_context.repo)

    def build_orchestrator_for(
        self, gateway: ToolGateway, run_id: RunId | None = None
    ) -> Orchestrator:
        """An orchestrator over an already-built gateway.

        Built per request rather than once at startup because the gateway is bound to a
        specific scenario directory or repository. Construction touches no I/O — it is a
        handful of object references — so the cost is a rounding error next to the model
        call, and the alternative (one mutable gateway re-pointed per request) would be
        shared state between concurrent runs.

        ``run_id`` fixes the id the run will be recorded under, so a route that must
        answer with an id *before* the run finishes can mint one and still have the
        orchestrator agree with it.

        Phase 4: an LLM fault wraps the client *per run*, so its per-agent counters start
        fresh for every run (`faults.FaultInjectingLlmClient`); the citation fault reaches
        the Diagnostician through the wiring.

        The transient retry policy comes from the `HARNESS_LLM_*` settings (`retry_policy_for`);
        the schema retries and the per-call timeout keep their own sources.
        """
        llm = self.llm
        fabricate_citation = False
        if self.fault is not None and self.fault.name in LLM_FAULTS:
            llm = FaultInjectingLlmClient(self.llm, self.fault)
        elif self.fault is not None and self.fault.name == FAULT_FABRICATE_CITATION:
            fabricate_citation = True
        orchestrator = build_orchestrator(
            gateway=gateway,
            context_manager=self.context_manager,
            llm=llm,
            recorder=self.recorder,
            engine=self.engine,
            escalation_threshold=self.settings.escalation_threshold,
            investigator_model=self.settings.model_investigator or self.settings.gemini_model,
            diagnostician_model=(
                self.settings.model_diagnostician or self.settings.gemini_model
            ),
            remediator_model=self.settings.model_remediator or self.settings.gemini_model,
            retry_policy=retry_policy_for(self.settings),
            timeout_s=self.settings.gemini_timeout_s,
            approval_ttl_h=self.settings.approval_ttl_h,
            escalation_channels=self.escalation_channels,
            memory=self.memory,
            notifier=self.notifier,
            fabricate_citation=fabricate_citation,
        )
        if run_id is not None:
            orchestrator.run_id_factory = lambda: run_id
        return orchestrator

    def build_replay_orchestrator(
        self, scenario_dir: Path, repo: str, run_id: RunId | None = None
    ) -> Orchestrator:
        """An orchestrator wired to one recorded scenario."""
        return self.build_orchestrator_for(
            self.build_replay_gateway(scenario_dir, repo), run_id=run_id
        )


def _delivery_key(subject: dict[str, object]) -> tuple[str, int, int] | None:
    """Appendix C's three identifiers off a `workflow_run` delivery, or `None` when the
    body is not shaped like one."""
    run = subject.get("workflow_run")
    repository = subject.get("repository")
    if not isinstance(run, dict) or not isinstance(repository, dict):
        return None
    try:
        return (
            str(repository.get("full_name", "")),
            int(run.get("id", 0)),
            int(run.get("run_attempt", 1)),
        )
    except (TypeError, ValueError, OverflowError):
        # `OverflowError`: `1e400` parses as infinity (Phase 5 audit finding 9).
        return None


def build_memory_store(
    settings: Settings,
    redactor: Redactor | None = None,
    *,
    fault: Fault | None = None,
    recorder: TraceRecorder | None = None,
) -> SqliteMemoryStore:
    """The one `MemoryStore`, over the same file the recorder writes, scrubbing what it
    stores through the same `Redactor` the recorder uses (or one built here when a
    hand-assembled context did not pass its own).

    `fault` is the already-guarded `HARNESS_FAULT_INJECT` (`build_fault`); only
    `sqlite_locked` is the store's -- it makes every store operation fail as if the file
    were locked (PLAN.md Phase 3 Verify step 4) -- and any other fault leaves the store
    alone. `recorder` (Phase 5) is what the store writes its `memory.*` spans through.
    """
    store_fault = fault.name if fault is not None and fault.name == FAULT_SQLITE_LOCKED else None
    return SqliteMemoryStore(
        settings.database_path,
        fault_inject=store_fault,
        redactor=redactor or build_redactor(settings),
        recorder=recorder,
    )


def build_secret_registry(settings: Settings) -> SecretRegistry:
    """Register the resolved value of every `SecretStr` field on `Settings`.

    Driven off the field *types* rather than a hand-written list of names: a secret added
    to `Settings` in a later phase is redacted from the trace the moment it exists, with
    nothing here to remember to update. That is the whole reason the fields are typed
    `SecretStr` in the first place.
    """
    registry = SecretRegistry()
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            registry.register(name, value.get_secret_value())
    return registry


@lru_cache
def get_app_context() -> AppContext:
    """Build (once) everything the application runs on.

    Cached, so the routes and the startup hook see the same objects. Note the settings
    accessor: `get_settings()`, never `Settings()` — see this module's docstring.
    """
    settings = get_settings()
    redactor = build_redactor(settings)
    recorder = TraceRecorder(db_path=settings.database_path, redactor=redactor)

    context_manager = ContextManager(
        # PLAN.md's log context budget has two homes and this is the wire between them.
        # A bare `ContextBudget()` here would make `HARNESS_LOG_CHAR_BUDGET` a silent
        # no-op, which is the failure this line exists to prevent.
        default_budget=ContextBudget(total_chars=settings.log_char_budget),
        # Phase 5: the `context.assemble` spans.
        recorder=recorder,
    )

    llm: LlmClient = GeminiClient(
        api_key=settings.gemini_api_key.get_secret_value(),
        default_timeout_s=settings.gemini_timeout_s,
    )
    fault = build_fault(settings)

    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=context_manager,
        llm=llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
        memory=build_memory_store(settings, redactor, fault=fault, recorder=recorder),
        engine=PolicyEngine(load_policy_spec()),
        fault=fault,
        notifier=build_notifier(settings, redactor),
    )


def mint_run_id() -> RunId:
    """Re-exported so routes do not reach into the orchestrator module for it."""
    return new_run_id()
