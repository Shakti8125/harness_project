# ruff: noqa: E501
"""Scrub credential-shaped strings out of the recorded fixtures before they are committed.

    uv run python scripts/scrub_fixtures.py            # rewrite in place, report what changed
    uv run python scripts/scrub_fixtures.py --check    # exit 1 if anything WOULD change
    uv run python scripts/scrub_fixtures.py --root <dir>   # a directory other than fixtures/scenarios

PLAN.md Appendix E / Open Risk: a scenario captured from a real repository with
`record_fixture.py` carries whatever the CI log and the API responses carried -- a token a
careless workflow echoed, an `Authorization` header a debug step printed. This script runs
the same `Redactor` the trace uses (the registered `Settings` secrets plus
`deps.SECRET_PATTERNS`) over every text file under the fixtures root and rewrites each
match to the redaction placeholder. `--check` is the pre-commit gate `fixtures/README.md`
names: it changes nothing and exits 1 when a file would be rewritten.

**One value is deliberately left alone.** `tests/test_no_secret_leak.py` plants a
`ghp_…` token in `real_regression`'s log to prove the regex barrier catches a credential
that never passed through `Settings`; scrubbing it here would remove the thing the test
exists to catch. `PLANTED_SENTINELS` lists that value, the test imports it from here, and
the scrub skips exactly those strings. Anything else that looks like a credential is
rewritten -- there is no allowlist for "looks fake".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.deps import FIXTURES_ROOT  # noqa: E402
from src.api.deps import build_redactor as _build_redactor  # noqa: E402
from src.harness.observability import REDACTION_PLACEHOLDER, Redactor  # noqa: E402
from src.settings import get_settings  # noqa: E402

#: Credential-shaped strings planted in the committed fixtures on purpose, by name. The
#: leak test asserts each one is scrubbed from every trace, row, body and byte the
#: harness produces; this script must therefore leave them in the fixture itself.
PLANTED_SENTINELS: Final[tuple[str, ...]] = (
    # `fixtures/scenarios/real_regression/logs/job_601234567.txt`: a careless workflow
    # step echoing its `Authorization` header (Phase 5 dispatch decision 8).
    "ghp_FIXTURELEAKSENTINEL00000000000000000",
)

_KEEP_MARKER: Final[str] = "\x00HARNESS-SENTINEL-{index}\x00"
TEXT_SUFFIXES: Final[frozenset[str]] = frozenset({".txt", ".json", ".yaml", ".yml", ".md", ".log"})


def build_redactor() -> Redactor:
    """The trace's redactor, exactly as the composition root builds it: registered
    secrets, the credential shapes, the assignment heuristics on plain text -- and a
    fixture file *is* plain text, so `api_key=` in a recorded log is rewritten."""
    return _build_redactor(get_settings())


def scrub_text(text: str, redactor: Redactor, keep: tuple[str, ...] = PLANTED_SENTINELS) -> str:
    """`redactor.scrub` over a whole file's text, with the planted sentinels preserved."""
    protected = text
    for index, sentinel in enumerate(keep):
        protected = protected.replace(sentinel, _KEEP_MARKER.format(index=index))
    scrubbed = redactor.scrub(protected)
    assert isinstance(scrubbed, str)
    for index, sentinel in enumerate(keep):
        scrubbed = scrubbed.replace(_KEEP_MARKER.format(index=index), sentinel)
    return scrubbed


def scrub_tree(root: Path, redactor: Redactor, *, check: bool) -> list[Path]:
    """Every file under `root` that changed (or would change, under `check`)."""
    changed: list[Path] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
        original = path.read_text(encoding="utf-8")
        scrubbed = scrub_text(original, redactor)
        if scrubbed == original:
            continue
        changed.append(path)
        if not check:
            path.write_text(scrubbed, encoding="utf-8", newline="")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=FIXTURES_ROOT, help="directory to scrub (default: fixtures/scenarios)")
    parser.add_argument("--check", action="store_true", help="report and exit 1 instead of rewriting")
    args = parser.parse_args()

    changed = scrub_tree(args.root, build_redactor(), check=args.check)
    verb = "would rewrite" if args.check else "rewrote"
    for path in changed:
        print(f"{verb} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if not changed:
        print(f"clean: nothing credential-shaped under {args.root} (placeholder {REDACTION_PLACEHOLDER!r})")
        return 0
    print(f"{len(changed)} file(s) {verb.split()[-1]}")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
