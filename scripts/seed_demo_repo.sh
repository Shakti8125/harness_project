#!/usr/bin/env bash
# Seed the demo repository the four recorded scenarios describe, so a real failure can
# drive the harness end to end (PLAN.md Phase 5, Verify step 5).
#
#   scripts/seed_demo_repo.sh <owner>/harness-demo-repo [--webhook https://<space>/webhooks/github] [--force]
#
# What it does, in order:
#   1. builds the repository tree in a temp dir -- the pricing package, the scheduler, the
#      config module, tests, `requirements.txt`, and four `workflow_dispatch` workflows --
#      and pushes it to a NEW public repository with `gh repo create --push`;
#   2. waits for the two push-triggered workflows on `main` (regression, dependency) to go
#      green, and dispatches the other two (flaky with `load=low`, infra with the real
#      index) so every workflow has a green baseline -- Appendix D's cold start is a
#      separate scenario, not the default;
#   3. pushes two one-commit branches over `main`: `demo/regression` (the `+ 1` in
#      `discount()`) and `demo/dependency` (`pydantic==1.10.13` -> `2.9.2`), whose pushes
#      fire the failing runs of their workflows; `flaky.yml` and `infra.yml` fail on demand
#      with their default inputs (`gh workflow run flaky.yml -R <repo>`);
#   4. with `--webhook`, registers the `workflow_run` webhook on the repository, secret from
#      `$HARNESS_GITHUB_WEBHOOK_SECRET` (never echoed); without it, prints the command.
#
# Refuses to touch a repository that already exists unless `--force` (which then only
# re-pushes branches; it never deletes anything). Needs `gh` logged in as the owner.
set -euo pipefail

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

REPO="${1:-}"; shift || true
[[ -z "$REPO" || "$REPO" != */* ]] && usage
WEBHOOK_URL=""; FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --webhook) WEBHOOK_URL="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

command -v gh >/dev/null || { echo "gh is required (https://cli.github.com)" >&2; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "gh is not logged in; run: gh auth login" >&2; exit 2; }

if gh repo view "$REPO" >/dev/null 2>&1; then
  if [[ $FORCE -eq 0 ]]; then
    echo "$REPO already exists; pass --force to re-push the demo branches over it" >&2
    exit 2
  fi
  EXISTS=1
else
  EXISTS=0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
git init -q -b main
git config user.name "harness-demo-seed"
git config user.email "harness-demo-seed@users.noreply.github.com"

# --- the tree the fixtures describe ---------------------------------------------------
mkdir -p src/pricing tests .github/workflows

cat > README.md <<'EOF'
# harness-demo-repo

Seeded by `scripts/seed_demo_repo.sh` of the agent harness. Four workflows, four ways to fail:

| workflow | fails when | scenario |
|---|---|---|
| `flaky.yml` | dispatched with `load=high` (the default): a wall-clock deadline test slips on a loaded runner | `flaky_test` |
| `infra.yml` | dispatched with the default (unreachable) package index | `infra_timeout` |
| `regression.yml` | pushed on `demo/regression`: an off-by-one in `discount()` | `real_regression` |
| `dependency.yml` | pushed on `demo/dependency`: `pydantic` 1.10.13 -> 2.9.2 moves `BaseSettings` | `dependency_break` |

Nothing here is real software; every failure is on purpose.
EOF

cat > requirements.txt <<'EOF'
pydantic==1.10.13
EOF
cat > requirements-dev.txt <<'EOF'
pytest==8.3.3
EOF

cat > src/__init__.py <<'EOF'
EOF
cat > src/pricing/__init__.py <<'EOF'
from src.pricing.discount import discount
from src.pricing.format import format_currency

__all__ = ["discount", "format_currency"]
EOF
cat > src/pricing/discount.py <<'EOF'
def discount(price: int, percent: int) -> int:
    """
    Apply a percent discount to price, rounded down to the nearest integer.
    """
    return price - (price * percent) // 100
EOF
cat > src/pricing/format.py <<'EOF'
def format_currency(amount: int, symbol: str = "$") -> str:
    """Format an integer amount with thin-space thousands separators."""
    digits = f"{amount:,}".replace(",", " ")
    return f"{symbol}{digits}"
EOF
cat > src/config.py <<'EOF'
from pydantic import BaseSettings


class Settings(BaseSettings):
    """Service settings, read from the environment (pydantic v1 style)."""

    app_name: str = "harness-demo"
    deadline_s: float = 1.0
EOF
cat > src/scheduler.py <<'EOF'
import os
import random
import time


def run_job(name: str) -> float:
    """Run a 'job' and return how long it took. Under load (DEMO_SCHEDULER_LOAD=high) the
    job takes 0.9-1.4 s and the deadline test's 1.0 s budget slips more often than not --
    which is what a flaky, timing-dependent test looks like on a shared runner."""
    load = os.environ.get("DEMO_SCHEDULER_LOAD", "low")
    started = time.perf_counter()
    time.sleep(random.uniform(0.9, 1.4) if load == "high" else random.uniform(0.05, 0.2))
    return time.perf_counter() - started
EOF

cat > tests/__init__.py <<'EOF'
EOF
cat > tests/test_pricing.py <<'EOF'
from src.pricing import discount, format_currency


def test_discount_applies():
    assert discount(100, 10) == 90


def test_discount_rounds_down():
    assert discount(99, 10) == 90


def test_format_currency_thin_space():
    assert format_currency(1234567) == "$1 234 567"
EOF
cat > tests/test_scheduler.py <<'EOF'
from src.config import Settings
from src.scheduler import run_job


def test_job_runs_within_deadline():
    deadline = Settings().deadline_s
    took = run_job("nightly-rollup")
    assert took < deadline, f"job took {took:.3f}s, expected < {deadline:.1f}s"
EOF

cat > .github/workflows/flaky.yml <<'EOF'
name: flaky
on:
  workflow_dispatch:
    inputs:
      load:
        description: "runner load (high makes the deadline test slip)"
        default: "high"
        type: choice
        options: [high, low]
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      DEMO_SCHEDULER_LOAD: ${{ inputs.load }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest -v tests/test_scheduler.py
EOF

cat > .github/workflows/infra.yml <<'EOF'
name: infra
on:
  workflow_dispatch:
    inputs:
      index_url:
        description: "package index (the default is unreachable on purpose)"
        default: "https://pypi.invalid/simple"
        type: string
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install --index-url "${{ inputs.index_url }}" --timeout 15 --retries 1 -r requirements.txt -r requirements-dev.txt
      - run: pytest -v tests/test_pricing.py
EOF

cat > .github/workflows/regression.yml <<'EOF'
name: regression
on:
  push:
    branches: [main, demo/regression]
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest -v tests/test_pricing.py
EOF

cat > .github/workflows/dependency.yml <<'EOF'
name: dependency
on:
  push:
    branches: [main, demo/dependency]
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest -v tests/
EOF

git add -A
git commit -q -m "Seed the demo repository: pricing, scheduler, config, four workflows"

# --- 1. the repository --------------------------------------------------------------
if [[ $EXISTS -eq 0 ]]; then
  gh repo create "$REPO" --public --source . --push \
    --description "Demo repository seeded with canned CI failure scenarios for the agent harness."
else
  git remote add origin "https://github.com/$REPO.git"
  git push -q --force origin main
fi
echo "pushed main to $REPO"

# --- 2. green baselines --------------------------------------------------------------
wait_for_run() {  # workflow branch
  local workflow="$1" branch="$2" status="" conclusion="" tries=0
  until [[ "$status" == "completed" ]]; do
    sleep 10
    read -r status conclusion < <(gh run list -R "$REPO" --workflow "$workflow" --branch "$branch" --limit 1 \
      --json status,conclusion --jq '.[0] | "\(.status) \(.conclusion)"' 2>/dev/null || echo "queued ")
    tries=$((tries + 1))
    [[ $tries -gt 60 ]] && { echo "gave up waiting for $workflow on $branch" >&2; return 1; }
  done
  echo "$workflow on $branch: $conclusion"
}

sleep 5
gh workflow run flaky.yml -R "$REPO" -f load=low
gh workflow run infra.yml -R "$REPO" -f index_url=https://pypi.org/simple
wait_for_run regression.yml main
wait_for_run dependency.yml main
wait_for_run flaky.yml main
wait_for_run infra.yml main

# --- 3. the failing branches ---------------------------------------------------------
git checkout -q -b demo/regression main
python3 - <<'EOF'
import pathlib
p = pathlib.Path("src/pricing/discount.py")
p.write_text(p.read_text().replace("return price - (price * percent) // 100", "return price - (price * percent) // 100 + 1"))
EOF
git commit -q -am "Round discounts up so customers never see a half-cent short"
git push -q -u origin demo/regression

git checkout -q -b demo/dependency main
printf 'pydantic==2.9.2\n' > requirements.txt
git commit -q -am "Bump pydantic to 2.9.2"
git push -q -u origin demo/dependency
git checkout -q main
echo "pushed demo/regression and demo/dependency (their workflows are running and will fail)"

# --- 4. the webhook ------------------------------------------------------------------
if [[ -n "$WEBHOOK_URL" ]]; then
  : "${HARNESS_GITHUB_WEBHOOK_SECRET:?set HARNESS_GITHUB_WEBHOOK_SECRET in the environment (the secret the Space holds)}"
  gh api -X POST "repos/$REPO/hooks" \
    -f name=web -F active=true -f 'events[]=workflow_run' \
    -f config[url]="$WEBHOOK_URL" -f config[content_type]=json \
    -f config[secret]="$HARNESS_GITHUB_WEBHOOK_SECRET" >/dev/null
  echo "webhook registered: $WEBHOOK_URL (workflow_run)"
fi

cat <<EOF

Done. What is left is by hand:
  1. A fine-grained PAT scoped to $REPO only (Appendix E): Actions read, Contents read+write,
     Pull requests write, Issues write, Metadata read. Never a classic token.
  2. On the Space: secrets HARNESS_GITHUB_TOKEN (the PAT) and HARNESS_GITHUB_WEBHOOK_SECRET;
     variables HARNESS_GATEWAY=github and HARNESS_ALLOWED_REPOS=["$REPO"]. HARNESS_DRY_RUN stays true.
  3. The webhook, if not registered above:
       HARNESS_GITHUB_WEBHOOK_SECRET=... $0 $REPO --webhook https://<space>/webhooks/github --force
  4. Fire a failure:  gh workflow run flaky.yml -R $REPO
     then Settings -> Webhooks -> Recent Deliveries: 202; Redeliver: 202 and no second run.
EOF
