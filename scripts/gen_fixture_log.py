"""Synthesise a realistic GitHub Actions job log for a canned scenario.

`fixtures/README.md` asks for logs of a few thousand lines with the true anchor buried
well before the end, ordinary setup noise, and cascading secondary errors after the root
cause -- so that the Context Manager's trimming is actually exercised and a system that
quotes the last red line is distinguishable from one that finds the cause. Writing that by
hand does not scale to six scenarios; this produces it deterministically from a short
description of the failure.

Usage:
    uv run python scripts/gen_fixture_log.py flaky_test
    uv run python scripts/gen_fixture_log.py infra_timeout
    uv run python scripts/gen_fixture_log.py dependency_break

Writes `fixtures/scenarios/<name>/logs/job_<id>.txt`. Deterministic: the same name
produces byte-identical output, so a regenerated log never silently changes a fixture.
"""

# ruff: noqa: E501  -- log lines are reproduced at their real width
from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESC = "\x1b"
CYAN, RED, GREEN, YELLOW, BOLD, RESET = (
    f"{ESC}[36m", f"{ESC}[31m", f"{ESC}[32m", f"{ESC}[33m", f"{ESC}[1m", f"{ESC}[0m",
)

MODULES = [
    "tests/test_pricing.py", "tests/test_checkout.py", "tests/test_cart.py",
    "tests/test_inventory.py", "tests/test_scheduler.py", "tests/test_reports.py",
    "tests/test_api_orders.py", "tests/test_api_customers.py", "tests/test_auth.py",
    "tests/test_webhooks.py", "tests/test_currency.py", "tests/test_tax.py",
    "tests/test_shipping.py", "tests/test_notifications.py", "tests/test_search.py",
]
PACKAGES = [
    ("pydantic", "2.9.2"), ("fastapi", "0.115.0"), ("httpx", "0.27.2"),
    ("sqlalchemy", "2.0.35"), ("alembic", "1.13.3"), ("pytest", "8.3.3"),
    ("pytest-asyncio", "0.24.0"), ("pytest-cov", "5.0.0"), ("coverage", "7.6.1"),
    ("ruff", "0.6.8"), ("mypy", "1.11.2"), ("uvicorn", "0.30.6"), ("anyio", "4.6.0"),
    ("idna", "3.10"), ("sniffio", "1.3.1"), ("certifi", "2024.8.30"), ("h11", "0.14.0"),
    ("typing-extensions", "4.12.2"), ("annotated-types", "0.7.0"), ("pydantic-core", "2.23.4"),
    ("starlette", "0.38.6"), ("click", "8.1.7"), ("packaging", "24.1"), ("pluggy", "1.5.0"),
    ("iniconfig", "2.0.0"), ("mako", "1.3.5"), ("markupsafe", "2.1.5"),
]


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.rng = random.Random(0)

    def tick(self, ms: float | None = None) -> str:
        step = ms if ms is not None else self.rng.uniform(15, 140)
        self.now += timedelta(milliseconds=step)
        # Actions prints seven fractional digits.
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.now.microsecond * 10:07d}Z"


class Log:
    def __init__(self, start: datetime, seed: int) -> None:
        self.clock = Clock(start)
        self.rng = random.Random(seed)
        self.lines: list[str] = []

    def line(self, text: str, ms: float | None = None) -> None:
        self.lines.append(f"{self.clock.tick(ms)} {text}")

    def group(self, title: str) -> None:
        self.line(f"{CYAN}##[group]{title}{RESET}")

    def endgroup(self) -> None:
        self.line(f"{CYAN}##[endgroup]{RESET}")


def bootstrap(log: Log, repo: str, head_sha: str, job_name: str, runner: int) -> None:
    log.line("Current runner version: '2.320.0'")
    log.line(f"Runner name: 'gh-actions-runner-ubuntu-{runner}'")
    log.line("Runner group name: 'GitHub Actions'")
    log.line(f"Machine name: 'fv-az{1000 + runner}-{runner * 37 % 900}'")
    log.group("GITHUB_TOKEN Permissions")
    for perm in ("Contents: read", "Metadata: read", "Packages: read"):
        log.line(perm)
    log.endgroup()
    log.line("Secret source: Actions")
    log.line("Prepare workflow directory")
    log.line("Prepare all required actions")
    log.line("Getting action download info")
    log.line("Download action repository 'actions/checkout@v4' (SHA:11bd71901bbe5b1630ceea73d27597364c9af683)")
    log.line("Download action repository 'actions/setup-python@v5' (SHA:0a5c61591373683505ea898e09a3ea4f39ef2b9c)")
    log.line("Download action repository 'actions/cache@v4' (SHA:0c907a75c2c80ebcb7f088228285e798b750cf8f)")
    log.line(f"Complete job name: {job_name}")
    log.group("Run actions/checkout@v4")
    log.line("with:")
    log.line(f"  repository: {repo}")
    log.line("  token: ***")
    log.line("  ssh-strict: true")
    log.line("  persist-credentials: true")
    log.line("  clean: true")
    log.line("  fetch-depth: 1")
    log.endgroup()
    log.line(f"Syncing repository: {repo}")
    log.line("Getting Git version info")
    log.line("/usr/bin/git version")
    log.line("git version 2.46.0")
    log.line(f"Deleting the contents of '/home/runner/work/{repo.split('/')[1]}/{repo.split('/')[1]}'")
    log.line("Initializing the repository")
    log.line("Disabling automatic garbage collection")
    log.line("Setting up auth")
    log.line("Fetching the repository")
    log.line("remote: Enumerating objects: 418, done.")
    log.line("remote: Counting objects: 100% (418/418), done.")
    log.line("remote: Compressing objects: 100% (301/301), done.")
    log.line("remote: Total 418 (delta 160), reused 384 (delta 142), pack-reused 0")
    log.line("Receiving objects: 100% (418/418), 1.44 MiB | 9.02 MiB/s, done.")
    log.line("Resolving deltas: 100% (160/160), done.")
    log.line(f"From https://github.com/{repo}")
    log.line(f" * [new ref]         {head_sha} -> origin/main")
    log.line("Determining the checkout info")
    log.line("Checking out the ref")
    log.line("/usr/bin/git checkout --progress --force -B main refs/remotes/origin/main")
    log.line("Switched to a new branch 'main'")
    log.line("branch 'main' set up to track 'origin/main'.")
    log.line("/usr/bin/git log -1 --format='%H'")
    log.line(f"'{head_sha}'")
    log.group("Run actions/setup-python@v5")
    log.line("with:")
    log.line("  python-version: 3.12")
    log.line("  cache: pip")
    log.endgroup()
    log.line("Installed versions")
    log.line("  Successfully set up CPython (3.12.6)")
    log.line("Received 0 of 31457280 (0.0%), 0.0 MBs/sec")
    log.line("Received 31457280 of 31457280 (100.0%), 28.4 MBs/sec")
    log.line("Cache Size: ~30 MB (31457280 B)")
    log.line("/usr/bin/tar -xf /home/runner/work/_temp/cache.tzst -P -C /home/runner/work --use-compress-program unzstd")
    log.line("Cache restored successfully")
    log.line("Cache restored from key: setup-python-Linux-x64-python-3.12.6-pip-8c1f")


def pip_install(log: Log, *, fail_on: str | None = None, verbose: bool = False) -> None:
    flag = " -v" if verbose else ""
    log.group(f"Run pip install{flag} -r requirements.txt -r requirements-dev.txt")
    log.line("shell: /usr/bin/bash -e {0}")
    log.line("env:")
    log.line("  pythonLocation: /opt/hostedtoolcache/Python/3.12.6/x64")
    log.line("  PIP_DISABLE_PIP_VERSION_CHECK: 1")
    log.endgroup()
    if verbose:
        log.line("Using pip 24.2 from /opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/site-packages/pip (python 3.12)")
        log.line("Looking in indexes: https://pypi.org/simple")
    for package, version in PACKAGES:
        if verbose and fail_on != package:
            log.line("  Found index url https://pypi.org/simple/")
            log.line(f"  Fetching project page and analyzing links: https://pypi.org/simple/{package}/")
            log.line(f"  Getting page https://pypi.org/simple/{package}/")
            log.line(f"  Found index url https://pypi.org/simple/ (from https://pypi.org/simple/{package}/)")
            log.line(f"  Looking up \"https://pypi.org/simple/{package}/\" in the cache")
            log.line("  Request header has \"max_age\" as 0, cache bypassed")
            log.line("  No cache entry available")
            log.line("  Starting new HTTPS connection (1): pypi.org:443")
            log.line(f"  https://pypi.org:443 \"GET /simple/{package}/ HTTP/1.1\" 200 {log.rng.randint(4000, 90000)}")
            log.line(f"  Updating cache with response from \"https://pypi.org/simple/{package}/\"")
            for minor in range(log.rng.randint(4, 14)):
                skipped = f"{version.split('.')[0]}.{max(0, int(version.split('.')[1]) - minor - 1)}.{log.rng.randint(0, 9)}"
                log.line(f"  Skipping link: none of the wheel's tags ({package.replace('-', '_')}-{skipped}-cp311-cp311-manylinux_2_17_x86_64) are compatible (run pip debug --verbose to show compatible tags)")
            log.line(f"  Found link https://files.pythonhosted.org/packages/{log.rng.randbytes(2).hex()}/{log.rng.randbytes(16).hex()}/{package.replace('-', '_')}-{version}-py3-none-any.whl (from https://pypi.org/simple/{package}/), version: {version}")
        if fail_on == package:
            log.line(f"Collecting {package}=={version}")
            for attempt in range(1, 6):
                log.line(
                    f"  WARNING: Retrying (Retry(total={5 - attempt}, connect=None, read=None, "
                    f"redirect=None, status=None)) after connection broken by "
                    f"'ReadTimeoutError(\"HTTPSConnectionPool(host='pypi.org', port=443): "
                    f"Read timed out. (read timeout=15)\")': /simple/{package}/",
                    ms=15_000,
                )
            log.line(
                f"{RED}ERROR: Could not find a version that satisfies the requirement "
                f"{package}=={version} (from versions: none){RESET}",
                ms=15_000,
            )
            log.line(
                f"{RED}ERROR: No matching distribution found for {package}=={version}{RESET}"
            )
            log.line(
                f"{RED}ERROR: Exception: ReadTimeoutError: HTTPSConnectionPool(host='pypi.org', "
                f"port=443): Read timed out. (read timeout=15){RESET}"
            )
            log.line(f"{RED}##[error]Process completed with exit code 1.{RESET}")
            return
        log.line(f"Collecting {package}=={version}")
        log.line(
            f"  Downloading {package.replace('-', '_')}-{version}-py3-none-any.whl "
            f"({log.rng.randint(40, 2400)} kB)"
        )
        log.line(
            f"     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ {log.rng.randint(40, 2400) / 1000:.1f}/"
            f"{log.rng.randint(40, 2400) / 1000:.1f} MB {log.rng.randint(5, 60)}.{log.rng.randint(0, 9)} MB/s eta 0:00:00"
        )
    log.line("Installing collected packages: " + ", ".join(p for p, _ in PACKAGES))
    log.line("Successfully installed " + " ".join(f"{p}-{v}" for p, v in PACKAGES))


def lint(log: Log, *, mypy: bool = True) -> None:
    log.group("Run ruff check . && mypy src" if mypy else "Run ruff check .")
    log.line("shell: /usr/bin/bash -e {0}")
    log.endgroup()
    log.line("All checks passed!")
    if mypy:
        log.line("Success: no issues found in 148 source files")


def pytest_session(
    log: Log,
    *,
    total: int,
    failing: dict[str, list[str]],
    duration_s: float,
    trailer: list[str],
) -> None:
    """A verbose session: `total` tests across MODULES, the given ones failing."""
    log.group("Run pytest -v --cov=src --cov-report=term-missing -p no:cacheprovider")
    log.line("shell: /usr/bin/bash -e {0}")
    log.line("env:")
    log.line("  PYTHONHASHSEED: 0")
    log.endgroup()
    log.line("============================= test session starts ==============================")
    log.line("platform linux -- Python 3.12.6, pytest-8.3.3, pluggy-1.5.0 -- /opt/hostedtoolcache/Python/3.12.6/x64/bin/python")
    log.line("cachedir: .pytest_cache")
    log.line("rootdir: /home/runner/work/harness-demo-repo/harness-demo-repo")
    log.line("configfile: pyproject.toml")
    log.line("plugins: asyncio-0.24.0, cov-5.0.0, anyio-4.6.0")
    log.line("asyncio: mode=Mode.AUTO, default_loop_scope=None")
    log.line(f"collected {total} items")
    log.line("")
    per_module = total // len(MODULES)
    failed_lines: list[str] = []
    index = 0
    for module in MODULES:
        for n in range(per_module):
            index += 1
            name = f"{module}::test_{module.split('_', 1)[1][:-3]}_case_{n:04d}"
            pct = int(index * 100 / total)
            if module in failing and n == per_module // 3:
                for test_name in failing[module]:
                    log.line(f"{module}::{test_name} {RED}FAILED{RESET}{' ' * 20}[{pct:3d}%]")
                    failed_lines.append(f"{module}::{test_name}")
            log.line(f"{name} {GREEN}PASSED{RESET}{' ' * 20}[{pct:3d}%]", ms=log.rng.uniform(5, 45))
    log.line("")
    for block in trailer:
        for text in block.split("\n"):
            log.line(text)
    log.line("---------- coverage: platform linux, python 3.12.6-final-0 -----------")
    log.line("Name                                           Stmts   Miss  Cover   Missing")
    log.line("----------------------------------------------------------------------------")
    for i, module in enumerate(MODULES):
        src = "src/" + module.split("test_", 1)[1]
        log.line(f"{src:46} {log.rng.randint(80, 600):5d} {log.rng.randint(0, 40):6d} {log.rng.randint(88, 100):5d}%   {110 + i * 7}-{113 + i * 7}")
    log.line("----------------------------------------------------------------------------")
    log.line("TOTAL                                           6210    260    96%")
    log.line("")
    log.line("=========================== short test summary info ============================")
    for test in failed_lines:
        log.line(f"{RED}FAILED {test}{RESET}")
    minutes, seconds = divmod(duration_s, 60)
    log.line(
        f"{RED}{BOLD}================== {len(failed_lines)} failed, {total - len(failed_lines)} passed "
        f"in {duration_s:.2f}s ({int(minutes)}:{int(seconds):02d}) =================={RESET}"
    )


def post_steps(log: Log, *, upload_warning: bool = True) -> None:
    log.group("Post Run actions/cache@v4")
    log.line("Post job cleanup.")
    log.line("Cache hit occurred on the primary key, not saving cache.")
    log.endgroup()
    log.group("Post Run actions/setup-python@v5")
    log.line("Post job cleanup.")
    log.endgroup()
    log.group("Post Run actions/checkout@v4")
    log.line("Post job cleanup.")
    log.line("/usr/bin/git version")
    log.line("git version 2.46.0")
    log.line("Temporarily overriding HOME='/home/runner/work/_temp/...' before making global config changes")
    log.line("Adding repository directory to the temporary git global config as a safe directory")
    log.line("/usr/bin/git config --global --add safe.directory /home/runner/work/harness-demo-repo/harness-demo-repo")
    log.line("/usr/bin/git config --local --name-only --get-regexp core\\.sshCommand")
    log.line("/usr/bin/git submodule foreach --recursive sh -c \"git config --local --name-only --get-regexp 'http\\.https\\:\\/\\/github\\.com\\/\\.extraheader' && git config --local --unset-all 'http.https://github.com/.extraheader' || :\"")
    log.endgroup()
    log.line(f"{RED}##[error]Process completed with exit code 1.{RESET}")
    log.group("Complete job")
    log.line("Cleaning up orphan processes")
    log.line("Uploading runner diagnostic logs")
    if upload_warning:
        log.line(f"{YELLOW}Warning: Failed to upload test-results artifact: request timed out after 30000ms{RESET}")
    log.line("Job completed with conclusion 'failure'")
    log.endgroup()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

FLAKY_TRACEBACK = f"""=================================== FAILURES ===================================
{RED}{BOLD}________________ test_job_runs_within_deadline _________________{RESET}

    def test_job_runs_within_deadline() -> None:
        scheduler = Scheduler(workers=2)
        started = time.perf_counter()
        scheduler.submit(sleep_job, seconds=0.4)
        scheduler.submit(sleep_job, seconds=0.4)
        scheduler.join()
        elapsed = time.perf_counter() - started
>       assert elapsed < 1.0, f"job took {{elapsed:.3f}}s, expected < 1.0s"
E       AssertionError: job took 1.207s, expected < 1.0s
E       assert 1.2070312290000001 < 1.0

tests/test_scheduler.py:41: AssertionError
----------------------------- Captured stderr call -----------------------------
/opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/threading.py:1073: ResourceWarning: unclosed <socket.socket fd=7, family=2, type=1, proto=6>
  self._target(*self._args, **self._kwargs)
ResourceWarning: Enable tracemalloc to get the object allocation traceback
{RED}{BOLD}________________ test_job_runs_within_deadline_under_load _________________{RESET}

    def test_job_runs_within_deadline_under_load() -> None:
        scheduler = Scheduler(workers=4)
>       results = scheduler.run_all([sleep_job] * 8, seconds=0.2, deadline=0.9)
E       scheduler.DeadlineExceeded: 8 jobs did not finish within 0.9s (finished 7)

tests/test_scheduler.py:58: DeadlineExceeded"""

def pytest_collection_errors(log: Log) -> None:
    """What a system pytest prints against a venv whose install never finished: every
    module errors at import, no test runs. The cascade after the real cause."""
    log.group("Run pytest -v --cov=src --cov-report=term-missing -p no:cacheprovider")
    log.line("shell: /usr/bin/bash -e {0}")
    log.endgroup()
    log.line("============================= test session starts ==============================")
    log.line("platform linux -- Python 3.12.6, pytest-8.3.3, pluggy-1.5.0 -- /opt/hostedtoolcache/Python/3.12.6/x64/bin/python")
    log.line("cachedir: .pytest_cache")
    log.line("rootdir: /home/runner/work/harness-demo-repo/harness-demo-repo")
    log.line("configfile: pyproject.toml")
    log.line(f"collected 0 items / {len(MODULES)} errors")
    log.line("")
    log.line("==================================== ERRORS ====================================")
    for module in MODULES:
        importer = "pydantic" if log.rng.random() < 0.6 else "fastapi"
        symbol = "BaseModel" if importer == "pydantic" else "FastAPI"
        log.line(f"{RED}{BOLD}_______________ ERROR collecting {module} _______________{RESET}")
        log.line(f"ImportError while importing test module '/home/runner/work/harness-demo-repo/harness-demo-repo/{module}'.")
        log.line("Hint: make sure your test modules/packages have valid Python names.")
        log.line("Traceback:")
        log.line("/opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/importlib/__init__.py:90: in import_module")
        log.line("    return _bootstrap._gcd_import(name[level:], package, level)")
        log.line(f"{module}:{log.rng.randint(2, 6)}: in <module>")
        log.line(f"    from {importer} import {symbol}")
        log.line(f"E   ModuleNotFoundError: No module named '{importer}'")
    log.line("=========================== short test summary info ============================")
    for module in MODULES:
        log.line(f"{RED}ERROR {module}{RESET}")
    log.line(f"{RED}!!!!!!!!!!!!!!!!!!! Interrupted: {len(MODULES)} errors during collection !!!!!!!!!!!!!!!!!!!{RESET}")
    log.line(f"{RED}{BOLD}=========================== {len(MODULES)} errors in 3.91s ==========================={RESET}")


PYDANTIC_IMPORT_ERROR = (
    "pydantic.errors.PydanticImportError: `BaseSettings` has been moved to the "
    "`pydantic-settings` package. See "
    "https://docs.pydantic.dev/2.9/migration/#basesettings-has-moved-to-pydantic-settings "
    "for more details."
)


def pytest_pydantic_import_errors(log: Log) -> None:
    """What pytest prints when every test module transitively imports a settings module
    written against pydantic 1 and the venv now holds pydantic 2: the same
    `PydanticImportError` at collection, module after module, no test runs. The cascade
    is fifteen identical errors; the cause is one line in `src/config.py`."""
    log.group("Run pytest -v --cov=src --cov-report=term-missing -p no:cacheprovider")
    log.line("shell: /usr/bin/bash -e {0}")
    log.line("env:")
    log.line("  PYTHONHASHSEED: 0")
    log.endgroup()
    log.line("============================= test session starts ==============================")
    log.line("platform linux -- Python 3.12.6, pytest-8.3.3, pluggy-1.5.0 -- /opt/hostedtoolcache/Python/3.12.6/x64/bin/python")
    log.line("cachedir: .pytest_cache")
    log.line("rootdir: /home/runner/work/harness-demo-repo/harness-demo-repo")
    log.line("configfile: pyproject.toml")
    log.line("plugins: asyncio-0.24.0, cov-5.0.0, anyio-4.6.0")
    log.line("asyncio: mode=Mode.AUTO, default_loop_scope=None")
    log.line(f"collected 0 items / {len(MODULES)} errors")
    log.line("")
    log.line("==================================== ERRORS ====================================")
    for module in MODULES:
        package = module.split("test_", 1)[1][:-3]
        log.line(f"{RED}{BOLD}_______________ ERROR collecting {module} _______________{RESET}")
        log.line(f"ImportError while importing test module '/home/runner/work/harness-demo-repo/harness-demo-repo/{module}'.")
        log.line("Hint: make sure your test modules/packages have valid Python names.")
        log.line("Traceback:")
        log.line("/opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/importlib/__init__.py:90: in import_module")
        log.line("    return _bootstrap._gcd_import(name[level:], package, level)")
        log.line(f"{module}:{log.rng.randint(2, 6)}: in <module>")
        log.line(f"    from src.{package} import *  # noqa: F403")
        log.line(f"src/{package}/__init__.py:2: in <module>")
        log.line("    from src.config import settings")
        log.line("src/config.py:3: in <module>")
        log.line("    from pydantic import BaseSettings")
        log.line("/opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/site-packages/pydantic/__init__.py:412: in __getattr__")
        log.line("    return _getattr_migration(attr_name)")
        log.line("/opt/hostedtoolcache/Python/3.12.6/x64/lib/python3.12/site-packages/pydantic/_migration.py:296: in wrapper")
        log.line("    raise PydanticImportError(")
        log.line(f"E   {PYDANTIC_IMPORT_ERROR}")
    log.line("=========================== short test summary info ============================")
    for module in MODULES:
        log.line(f"{RED}ERROR {module} - {PYDANTIC_IMPORT_ERROR}{RESET}")
    log.line(f"{RED}!!!!!!!!!!!!!!!!!!! Interrupted: {len(MODULES)} errors during collection !!!!!!!!!!!!!!!!!!!{RESET}")
    log.line(f"{RED}{BOLD}=========================== {len(MODULES)} errors in 2.47s ==========================={RESET}")


def build_dependency_break(log: Log) -> None:
    repo = "octo-org/harness-demo-repo"
    head = "c3d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4"
    bootstrap(log, repo, head, "test (3.12)", runner=17)
    # The install succeeds -- pydantic 2.9.2 resolves and installs cleanly; the break is
    # at import time, one step later. `PACKAGES` already pins 2.9.2 and pydantic-core.
    pip_install(log)
    lint(log, mypy=False)
    pytest_pydantic_import_errors(log)
    post_steps(log, upload_warning=False)


def build_flaky(log: Log) -> None:
    repo = "octo-org/harness-demo-repo"
    head = "4f1e2d3c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e"
    bootstrap(log, repo, head, "test (3.12)", runner=22)
    pip_install(log)
    lint(log)
    pytest_session(
        log,
        total=3180,
        failing={
            "tests/test_scheduler.py": [
                "test_job_runs_within_deadline",
                "test_job_runs_within_deadline_under_load",
            ]
        },
        duration_s=104.6,
        trailer=[FLAKY_TRACEBACK],
    )
    post_steps(log)


def build_infra(log: Log) -> None:
    repo = "octo-org/harness-demo-repo"
    head = "7c2b9e1a5d4f3e2c1b0a9f8e7d6c5b4a3f2e1d0c"
    bootstrap(log, repo, head, "test (3.12)", runner=31)
    pip_install(log, fail_on="mypy", verbose=True)
    # `continue-on-error` on the install step is the realistic way a run gets past a
    # failed install into a test step that then fails for a *secondary* reason -- the
    # cascading error the fixture format asks for.
    log.line(f"{YELLOW}Warning: step 'Install dependencies' failed but continue-on-error is set{RESET}")
    log.group("Run ruff check . && mypy src")
    log.line("shell: /usr/bin/bash -e {0}")
    log.endgroup()
    log.line("/usr/bin/bash: line 1: ruff: command not found")
    log.line(f"{RED}##[error]Process completed with exit code 127.{RESET}")
    log.line(f"{YELLOW}Warning: step 'Run lint' failed but continue-on-error is set{RESET}")
    pytest_collection_errors(log)
    post_steps(log, upload_warning=False)


SCENARIOS = {
    "flaky_test": (build_flaky, 601234890, datetime(2026, 9, 8, 10, 15, 4, tzinfo=UTC), 11),
    "infra_timeout": (build_infra, 601235102, datetime(2026, 9, 9, 7, 42, 18, tzinfo=UTC), 13),
    "dependency_break": (
        build_dependency_break, 601235417, datetime(2026, 9, 10, 9, 3, 27, tzinfo=UTC), 17
    ),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SCENARIOS:
        print(f"usage: {argv[0]} <{'|'.join(SCENARIOS)}>", file=sys.stderr)
        return 2
    name = argv[1]
    build, job_id, start, seed = SCENARIOS[name]
    log = Log(start, seed)
    build(log)
    out = ROOT / "fixtures" / "scenarios" / name / "logs" / f"job_{job_id}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(log.lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)}: {len(log.lines)} lines, {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
