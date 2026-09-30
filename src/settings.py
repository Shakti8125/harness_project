"""The ONLY module in this repository permitted to read the process environment.

`tests/test_no_env_access.py` greps the rest of the tree for `os.environ` / `os.getenv`
and fails the build if it finds any — every other module must receive configuration
through a `Settings` instance (typically via `get_settings()` below), never by reading
the environment directly.

The `Settings` class below is copied field-for-field from PLAN.md Appendix E.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError as PydanticSettingsSourceError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HARNESS_",
        extra="forbid",
        frozen=True,
    )

    # --- secrets (SecretStr; never rendered) ---
    gemini_api_key: SecretStr
    github_token: SecretStr
    github_webhook_secret: SecretStr
    escalation_webhook_url: SecretStr | None = None
    # Required by `POST /v1/runs` and `POST /v1/approvals/{id}` when `gateway == "github"`
    # (`Authorization: Bearer …`); unset there, both refuse. Unused in replay mode.
    operator_token: SecretStr | None = None

    # --- non-secret config ---
    database_path: Path = Path("./data/harness.db")
    gemini_model: str = "gemini-3.6-flash"
    model_investigator: str | None = None  # falls back to gemini_model
    model_diagnostician: str | None = None
    model_remediator: str | None = None
    escalation_threshold: float = Field(0.70, ge=0.0, le=1.0)
    log_char_budget: int = 120_000
    gemini_timeout_s: float = 60.0
    # The transient (429/503) retry policy every agent gets. The defaults are Appendix
    # B.1's, so nothing changes unless one is set; the free tier bills a `503`, so its
    # profile trades attempts for longer sleeps (`.env.example`).
    llm_transient_max_attempts: int = Field(4, ge=1, le=4)
    llm_backoff_base_s: float = Field(0.5, gt=0, le=20)
    llm_backoff_max_s: float = Field(8.0, gt=0, le=20)
    llm_backoff_jitter: Literal["full", "none"] = "full"
    github_timeout_s: float = 30.0
    github_api_base: AnyHttpUrl = "https://api.github.com"  # type: ignore[assignment]
    allowed_repos: list[str] = []  # "owner/name"; empty = replay-only
    gateway: Literal["github", "replay"] = "replay"
    dry_run: bool = True  # DEFAULTS TO TRUE — writes are opt-in
    max_concurrent_runs: int = 4
    approval_ttl_h: int = 24
    fault_inject: str | None = None  # test-only; refused when env != "dev"
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _no_unrecognised_harness_env_vars(self) -> Settings:
        """Wave-3 audit finding 6.

        `extra="forbid"` (above) is enforced by pydantic-settings only against the
        `.env` *file* source, not against the real process environment — which is the
        only source Docker and Fly ever use. A typo like `HARNESS_ESCALATION_TRESHOLD`
        would otherwise be silently ignored, leaving the operator believing a threshold
        moved when it didn't. PLAN.md:1770 promises "a startup crash rather than a
        silently ignored setting", so enforce that promise ourselves by scanning
        `os.environ` for `HARNESS_`-prefixed keys that don't correspond to any field.

        The error text below names only variable *names*, never values — see finding 5
        and `get_settings()`'s handling of the `ValidationError` this raises into.

        Assumes no non-config `HARNESS_`-prefixed variables are injected by the platform.
        This holds for this project's actual targets (Docker Compose, Fly.io) but not for
        Kubernetes: a `Service` named `harness` in the same namespace, combined with the
        default `enableServiceLinks: true`, injects `HARNESS_SERVICE_HOST` /
        `HARNESS_PORT_8000_TCP_*` into every pod, and legacy Docker `--link` with alias
        `harness` does the same — either would hard-crash the app at boot via this
        validator. K8s is not a deployment target of this plan, so this is accepted, not
        worked around.
        """
        prefix = self.model_config["env_prefix"]
        known = {f"{prefix}{name}".upper() for name in type(self).model_fields}
        unrecognised = sorted(
            key
            for key in os.environ
            if key.upper().startswith(prefix) and key.upper() not in known
        )
        if unrecognised:
            raise ValueError(
                "unrecognised environment variable name(s), values withheld: "
                + ", ".join(unrecognised)
            )
        return self


class SettingsError(RuntimeError):
    """Raised by `get_settings()` in place of pydantic's `ValidationError`.

    Note: this is a distinct class from `pydantic_settings.exceptions.SettingsError`
    (imported above as `PydanticSettingsSourceError` to keep the two apart), which is
    also caught and redacted through here — see `get_settings()`.

    Wave-3 audit finding 5: a mistyped `.env` key (e.g. `HARNESS_GITHUBTOKEN` instead of
    `HARNESS_GITHUB_TOKEN`) trips `extra="forbid"` and pydantic's `ValidationError`
    carries the offending *value* — a real secret — in its `input_value` field. Because
    `main.py` validates `Settings` at import time, an uncaught `ValidationError` there
    would print that value straight to the boot log, before the `Redactor` exists to
    scrub it. This exception type carries only field names and error kinds.
    """


# Re-audit finding 3: `err["type"] == "value_error"` is not, by itself, proof that
# `err["msg"]` is safe to print — it only proves the message came from a `ValueError`
# raised inside one of *our* validators. Today `_no_unrecognised_harness_env_vars` is the
# only such validator and its message is values-withheld by construction, but a plausible
# Phase 1 addition (e.g. a `@field_validator` that interpolates the offending value into
# its error text) would print secrets through this exact same "trusted" branch. Rather
# than trust the error *type*, allowlist the specific *message* we know is safe — pydantic
# prepends "Value error, " to every custom `ValueError` raised in a validator, so this
# checks the literal prefix of the one message we've audited. Anything else, including a
# `value_error` from a future validator, falls through to the generic type-only branch
# below and never has its `msg` printed.
_SAFE_VALUE_ERROR_MESSAGE_PREFIX = (
    "Value error, unrecognised environment variable name(s)"
)


def _redact_validation_error(exc: ValidationError) -> SettingsError:
    lines = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err["loc"]) or "<settings>"
        if err["type"] == "value_error" and str(err["msg"]).startswith(
            _SAFE_VALUE_ERROR_MESSAGE_PREFIX
        ):
            # Raised by our own `_no_unrecognised_harness_env_vars` validator: the
            # message is authored by us and already contains field/variable *names*
            # only, never raw values, so it is safe — and far more useful — to surface
            # verbatim rather than collapsing it to a bare type code.
            lines.append(f"  {loc}: {err['msg']}")
        else:
            # A pydantic-core builtin error (extra_forbidden, missing, string_type, ...).
            # `err["input"]` may hold the raw offending value (e.g. a mistyped secret) —
            # deliberately never read here. Field name + error type is enough to fix it.
            lines.append(f"  {loc}: {err['type']}")
    return SettingsError(
        "Settings failed to validate (values withheld from this message):\n"
        + "\n".join(lines)
    )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor.

    `Settings()` reads `.env` + the real environment exactly once per process; every
    caller (routes, deps.py, scripts) should go through this function rather than
    constructing `Settings` directly, so the whole app agrees on one snapshot of config.

    A `ValidationError` raised here is caught and re-raised as `SettingsError` with every
    offending value stripped (see `_redact_validation_error`); `from None` additionally
    suppresses Python's default "the above exception was the direct cause" chaining, so
    the original `ValidationError` — and any secret value it carries — never reaches a
    traceback printed to stdout/stderr.

    Re-audit finding 3: `pydantic_settings.exceptions.SettingsError` (caught below as
    `PydanticSettingsSourceError`) is a *different* class raised by pydantic-settings
    itself — not by field validation — when a complex-typed field (e.g. `allowed_repos:
    list[str]`) can't be JSON-decoded from its env-var string, which is exactly what
    happens if an operator sets `HARNESS_ALLOWED_REPOS=owner/name` instead of
    `HARNESS_ALLOWED_REPOS=["owner/name"]` (see `.env.example`). Left uncaught, it
    escapes as an uncaught third-party exception with a chained `json.JSONDecodeError`
    traceback, bypassing this whole redaction barrier. No secret field is complex-typed
    today, so no secret is actually at risk from this specific path, but the exception's
    own message names only the field and source, so it's cheap to route through the same
    fail-closed, readable-message barrier as every other boot-time config error.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise _redact_validation_error(exc) from None
    except PydanticSettingsSourceError as exc:
        raise SettingsError(
            "Settings failed to validate (values withheld from this message):\n"
            f"  {exc}"
        ) from None
