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
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import SecretStr

from src.harness.context_manager import ContextBudget, ContextManager
from src.harness.contracts import RunId
from src.harness.errors import ConfigurationError
from src.harness.gateway import ToolGateway
from src.harness.guardrails import PolicyEngine
from src.harness.llm import GeminiClient, LlmClient
from src.harness.memory import FAULT_SQLITE_LOCKED, MemoryStore, SqliteMemoryStore
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder
from src.harness.orchestrator import Orchestrator, new_run_id
from src.harness.storage import apply_migrations
from src.integrations.cicd.gateway_github import GitHubToolGateway
from src.integrations.cicd.gateway_replay import ReplayToolGateway
from src.integrations.cicd.wiring import build_orchestrator, load_policy_spec
from src.settings import Settings, get_settings

logger = logging.getLogger("harness.api.deps")

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
FIXTURES_ROOT: Final[Path] = REPO_ROOT / "fixtures" / "scenarios"

#: Credential shapes scrubbed from every trace, on top of the exact values registered
#: from config. These name specific vendors' token formats, which is domain knowledge —
#: PLAN.md keeps it out of `harness/observability.py` deliberately, and the composition
#: root is where it belongs. The fuller list arrives with the observability phase.
SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
)


#: The escalation channels a run built here delivers on. `db` is real since Phase 3:
#: `MemoryStore.save_run` files the record on the `escalation` table (dispatch decision 13).
ESCALATION_CHANNELS: Final[tuple[str, ...]] = ("log", "db")


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

    def __post_init__(self) -> None:
        if self.memory is None:
            object.__setattr__(
                self, "memory", build_memory_store(self.settings)
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
        )

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
        """The gateway a `RunContext` describes -- the one the run used, rebuilt."""
        if run_context.mode == "live":
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
        """
        orchestrator = build_orchestrator(
            gateway=gateway,
            context_manager=self.context_manager,
            llm=self.llm,
            recorder=self.recorder,
            engine=self.engine,
            escalation_threshold=self.settings.escalation_threshold,
            investigator_model=self.settings.model_investigator or self.settings.gemini_model,
            diagnostician_model=(
                self.settings.model_diagnostician or self.settings.gemini_model
            ),
            remediator_model=self.settings.model_remediator or self.settings.gemini_model,
            timeout_s=self.settings.gemini_timeout_s,
            approval_ttl_h=self.settings.approval_ttl_h,
            escalation_channels=ESCALATION_CHANNELS,
            memory=self.memory,
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


def build_memory_store(settings: Settings) -> SqliteMemoryStore:
    """The one `MemoryStore`, over the same file the recorder writes.

    `HARNESS_FAULT_INJECT=sqlite_locked` makes every store operation fail as if the file
    were locked -- PLAN.md Phase 3 Verify step 4 -- and is refused outside `env=dev`
    (Appendix E: "test-only; refused when env != dev"), at construction, so a production
    process with the variable set fails to boot rather than serving degraded runs.
    """
    fault = settings.fault_inject
    if fault is not None and settings.env != "dev":
        raise ConfigurationError(
            "HARNESS_FAULT_INJECT is a development-only setting and is refused when "
            f"HARNESS_ENV={settings.env!r}"
        )
    if fault is not None and fault != FAULT_SQLITE_LOCKED:
        raise ConfigurationError(f"unknown fault injection {fault!r}")
    return SqliteMemoryStore(settings.database_path, fault_inject=fault)


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
    redactor = Redactor(build_secret_registry(settings), SECRET_PATTERNS)
    recorder = TraceRecorder(db_path=settings.database_path, redactor=redactor)

    context_manager = ContextManager(
        # PLAN.md's log context budget has two homes and this is the wire between them.
        # A bare `ContextBudget()` here would make `HARNESS_LOG_CHAR_BUDGET` a silent
        # no-op, which is the failure this line exists to prevent.
        default_budget=ContextBudget(total_chars=settings.log_char_budget)
    )

    llm: LlmClient = GeminiClient(
        api_key=settings.gemini_api_key.get_secret_value(),
        default_timeout_s=settings.gemini_timeout_s,
    )

    return AppContext(
        settings=settings,
        recorder=recorder,
        context_manager=context_manager,
        llm=llm,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
        memory=build_memory_store(settings),
        engine=PolicyEngine(load_policy_spec()),
    )


def mint_run_id() -> RunId:
    """Re-exported so routes do not reach into the orchestrator module for it."""
    return new_run_id()
