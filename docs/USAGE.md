# Using the Agent Harness

The Agent Harness triages failed GitHub Actions runs. It reads the failed job's log and the
diff against the last green run, asks a model for a diagnosis, checks every quote the model
cites against that evidence, and then lets a policy allow the fix, hold it for a person's
approval, or refuse it. Writes run in **dry run** unless you deliberately turn that off.

This guide covers using it: the hosted demo, a local install, recorded and live runs,
approvals, the free model quota, evaluation and testing. `PLAN.md` is the design;
`docs/architecture/system-architecture.html` shows how the parts fit.

| I want to… | Go to |
|---|---|
| See it work without installing anything | [1. Try the hosted Space](#1-try-the-hosted-space) |
| Run it on my machine | [2. Run it locally](#2-run-it-locally) |
| Replay one of the recorded failures | [3. Replay a recorded scenario](#3-replay-a-recorded-scenario) |
| Read a run's diagnosis, evidence checks and trace | [4. Read what happened](#4-read-what-happened) |
| Approve or reject a fix it held | [5. Approve or reject a held fix](#5-approve-or-reject-a-held-fix) |
| Point it at a real repository | [6. Connect a real repository](#6-connect-a-real-repository-live-mode) |
| Use it on my own GitHub repository | [6. Use it on your own repository](#use-it-on-your-own-repository) |
| Host it | [7. Deploy to a Hugging Face Space](#7-deploy-to-a-hugging-face-space) |
| Stay inside the free model quota | [8. Manage the free-tier quota](#8-manage-the-free-tier-quota) |
| Measure accuracy, add scenarios, run the tests | [9](#9-evaluate-accuracy) · [10](#10-record-a-real-failure-as-a-scenario) · [11](#11-develop-and-test) |

---

## 1. Try the hosted Space

The demo runs at <https://shakti-agent-harness.hf.space>. Open it in a browser for a small
UI: pick a scenario and press **Diagnose**. Or use the HTTP API:

```bash
BASE=https://shakti-agent-harness.hf.space

# Replay a recorded failure end to end (synchronous, about 10–90 s, 2–3 model requests)
curl -s -X POST $BASE/v1/replay/real_regression | jq '.final.diagnosis'

# The same, in the background: returns 202 with a run_id at once
RID=$(curl -s -X POST "$BASE/v1/replay/flaky_test?sync=false" | jq -r .run_id)
curl -s $BASE/v1/runs/$RID | jq '{status, category: .final.diagnosis.category}'
```

Then open `$BASE/runs/<run_id>/view` in a browser for a readable trace of the run.

Two things to know about the hosted Space:

- **It shares one free model quota of 20 requests a day.** Each run costs 2–3. When the
  day is spent, runs escalate as `rate_limited` until midnight Pacific time.
- **It runs in live mode** against one demo repository (`Shakti8125/harness-demo-repo`), with
  dry run on. There, `POST /v1/runs` and `POST /v1/approvals/{id}` need the operator's token
  and answer `401` without it. `POST /v1/replay/{scenario}` and every read route stay open.

---

## 2. Run it locally

**You need:** Python 3.12, [uv](https://docs.astral.sh/uv/), and a Gemini API key (the free
tier works). Docker is optional. For live mode you also need the GitHub CLI (`gh`) and
Git Bash on Windows.

1. **Install the dependencies.**

    ```bash
    uv sync
    ```

2. **Configure.** Copy `.env.example` to `.env` and fill in the three required secrets.
   The process refuses to start without them.

    | Key | For a local replay-only setup |
    |---|---|
    | `HARNESS_GEMINI_API_KEY` | Your Gemini API key |
    | `HARNESS_GITHUB_TOKEN` | Any non-empty value; replay mode never calls GitHub |
    | `HARNESS_GITHUB_WEBHOOK_SECRET` | A random value: `python -c "import secrets; print(secrets.token_hex(32))"` |

    On the free tier, also uncomment the **free-tier retry profile** in `.env.example`
    (`HARNESS_LLM_TRANSIENT_MAX_ATTEMPTS=2`, `..._BACKOFF_BASE_S=5`, `..._BACKOFF_MAX_S=15`,
    `..._BACKOFF_JITTER=none`). See [section 8](#8-manage-the-free-tier-quota) for why.

    Every key is validated at boot. An unknown `HARNESS_` name stops the process with an
    error that names the variable, so a typo cannot pass silently.

3. **Start the server.** Choose one:

    ```bash
    # Directly, on loopback only
    uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000

    # Or in Docker (published on 127.0.0.1:8000 only; data in ./data)
    docker compose up -d --build
    ```

4. **Check that it is up.**

    ```bash
    curl -s http://127.0.0.1:8000/healthz     # liveness
    curl -s http://127.0.0.1:8000/readyz      # database reachable and writable
    ```

Keep the server on `127.0.0.1`. Several routes are deliberately open (replay, reads), and
binding to every interface would expose them to your network.

---

## 3. Replay a recorded scenario

Five real failures are recorded under `fixtures/scenarios/`. A replay serves GitHub's side
from those files, but the model calls are real.

| Scenario | What failed | Expected outcome |
|---|---|---|
| `real_regression` | An off-by-one in `discount()` breaks two pricing tests | `real_regression` against the right commit; a fix PR **held for approval** |
| `flaky_test` | A timing assertion fails on a loaded runner | `flaky_test`; the failed job is **re-run** (dry run) |
| `infra_timeout` | The package registry times out under an empty commit | `infra_transient`; the job is **re-run** (dry run) |
| `dependency_break` | A pydantic 1 → 2 bump fails at import | `dependency_break`; a fix PR **held for approval** |
| `cold_start` | The same timing failure, but no green run exists to compare against | No autonomous action: the re-run is **refused** |

**From the command line** (no server needed):

```bash
uv run python scripts/replay.py real_regression
```

**Through the API:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/replay/real_regression | jq '.status, .final.diagnosis.category'
```

- `?sync=false` returns `202` with a `run_id` at once and runs in the background.
- `?fresh=false` claims the scenario under its real webhook key, so a second replay is
  deduplicated, exactly like a GitHub redelivery.

**As GitHub would deliver it**, signed with your webhook secret, to a running server:

```bash
uv run python scripts/replay.py --post-signed fixtures/scenarios/flaky_test/webhook.json --wait
```

Post the same file again, and the answer is `deduplicated`, with no second run.

---

## 4. Read what happened

| Route | Returns |
|---|---|
| `GET /v1/runs?limit=20&status=escalated` | Runs, newest first; page with `?cursor=` and the response's `next_cursor` |
| `GET /v1/runs/{run_id}` | The outcome: `status`, `final.diagnosis`, `final.evaluation`, `final.remediation`, `escalation` |
| `GET /v1/runs/{run_id}/trace` | Every span: each stage, tool call and model attempt, with secrets redacted |
| `GET /runs/{run_id}/view` | The same trace as a readable HTML page |
| `GET /v1/escalations` | Runs handed to a person, and why |

**Run statuses:**

| Status | Meaning |
|---|---|
| `in_progress` | Still running |
| `completed` | Diagnosed, and the allowed action ran (or nothing needed doing) |
| `awaiting_approval` | Diagnosed; the fix is held for a person (see [section 5](#5-approve-or-reject-a-held-fix)) |
| `escalated` | Handed to a person; `escalation.reason` says why |
| `failed` | The run itself crashed; the outcome carries the error |
| `deduplicated` | The same delivery or idempotency key arrived again; the answer points at the original run |

**Common escalation reasons:**

| Reason | Meaning |
|---|---|
| `evidence_refuted` | A citation the model gave was not found in the log or diff, so the fix was not attempted |
| `evidence_unverifiable` | Too few citations could be checked |
| `low_confidence` | Final confidence under 0.70 (`HARNESS_ESCALATION_THRESHOLD`) |
| `unknown_category` | The model could not classify the failure |
| `policy_denied` | The policy refused the action, for example a re-run with no green baseline |
| `llm_upstream`, `rate_limited` | The model provider was overloaded, or the day's quota is spent |

---

## 5. Approve or reject a held fix

A run in `awaiting_approval` carries its approval id at
`final.remediation.pending_approval.approval_id`, with `expires_at` beside it (24 hours by
default, `HARNESS_APPROVAL_TTL_H`).

```bash
APR=$(curl -s http://127.0.0.1:8000/v1/runs/$RID | jq -r .final.remediation.pending_approval.approval_id)

curl -s -X POST http://127.0.0.1:8000/v1/approvals/$APR \
  -H 'content-type: application/json' \
  -d '{"decision": "approve", "actor": "your-name", "note": "looks right"}'
```

- The decision is `approve` or `reject`. The policy is checked again before anything runs.
- An approval is single use: `409` once decided, `410` once expired.
- **In live mode** add `-H "Authorization: Bearer $HARNESS_OPERATOR_TOKEN"`. The decision
  is then recorded as made by `operator`, whatever `actor` says.
- With dry run on, an approved fix PR is planned and checked, but not created.

---

## 6. Connect a real repository (live mode)

Live mode reads a real repository through the GitHub API. Keep `HARNESS_DRY_RUN=true`
throughout. Write mode needs two open security items fixed first (SEC-10 and SEC-11 in
`docs/security/assessment-2026-09-30.md`).

1. **Seed a demo repository** (Git Bash, logged in with `gh`). It creates a public repository
   whose four workflows fail in the four recorded ways:

    ```bash
    scripts/seed_demo_repo.sh <you>/harness-demo-repo
    gh run list -R <you>/harness-demo-repo --branch demo/regression --json databaseId,conclusion
    ```

    Run it **without** `--webhook` the first time, so the seeded failures do not spend
    model requests. Use `--force` only against this exact repository. On any other
    repository it replaces the tree of `main`.

2. **Create a fine-grained token** scoped to that one repository, expiring in 7–30 days.
   Give it **read-only** access to Actions, Contents, Metadata, Pull requests and Issues.
   Never give it Workflows or Administration.

3. **Set live mode in `.env`:**

    ```bash
    HARNESS_GATEWAY=github
    HARNESS_ALLOWED_REPOS=["<you>/harness-demo-repo"]   # a JSON list, not a bare string
    HARNESS_DRY_RUN=true
    HARNESS_GITHUB_TOKEN=<the fine-grained token>
    HARNESS_GITHUB_WEBHOOK_SECRET=<a new random value; never the .env.example placeholder>
    HARNESS_OPERATOR_TOKEN=<another random value>
    ```

4. **Diagnose one real failure** in-process, without a server or webhook:

    ```bash
    uv run python scripts/replay.py --live --repo <you>/harness-demo-repo --run-id <failed run id>
    ```

    Every GitHub call it makes is a read, or a write reported as `dry_run=True`.

5. **Receive webhooks** on a deployed instance. Register the hook, entering the secret
   without echoing it into your shell history:

    ```bash
    read -rs HARNESS_GITHUB_WEBHOOK_SECRET && export HARNESS_GITHUB_WEBHOOK_SECRET
    scripts/seed_demo_repo.sh <you>/harness-demo-repo --webhook-only --webhook https://<host>/webhooks/github
    unset HARNESS_GITHUB_WEBHOOK_SECRET
    ```

    Then fire a failure with `gh workflow run flaky.yml -R <you>/harness-demo-repo`. It fails
    about 80% of the time; a green run is ignored with `204` and costs nothing. The repository's
    **Settings → Webhooks → Recent Deliveries** should show `202`. **Redeliver** answers `200`,
    with no second run.

6. **Start runs through the API** in live mode with the operator token:

    ```bash
    curl -s -X POST http://127.0.0.1:8000/v1/runs \
      -H "Authorization: Bearer $HARNESS_OPERATOR_TOKEN" -H 'content-type: application/json' \
      -d '{"integration": "cicd", "subject": <the workflow_run webhook body>,
           "idempotency_key": "cicd:<repo>:<run id>:<attempt>", "mode": "live"}'
    ```

**One behaviour to expect:** the baseline is the last green run of the same workflow on an
**earlier commit**. A green run on the failing commit itself is not used. So a flaky failure
on a commit that has only ever been tested once is treated as a cold start, and its re-run is
refused. Push one more commit after the seeded baselines go green to avoid this in the demo.

### Use it on your own repository

Nothing in the code is tied to the demo repository: any repository with GitHub Actions
works. Run your own instance (local, Docker, or your own Space), allowlist your repository,
give it a read-only token for that repository, and send it your failures.

The hosted demo Space cannot be used for this. It allowlists only its demo repository and
holds a token for that repository alone.

1. **Create a read-only token for your repository.** Use a fine-grained token: set its
   resource owner to the repository's owner, choose **Only select repositories**, and give
   read-only access to Actions, Contents, Metadata, Pull requests and Issues. One fine-grained
   token covers repositories of one owner only. An organisation may need an admin to approve
   it.

2. **Configure your instance** (in `.env`, or as variables and secrets on your Space):

    ```bash
    HARNESS_GATEWAY=github
    HARNESS_ALLOWED_REPOS=["your-org/your-repo"]   # a JSON list; several repositories are allowed
    HARNESS_DRY_RUN=true
    HARNESS_GITHUB_TOKEN=<that token>
    HARNESS_GITHUB_WEBHOOK_SECRET=<a random value>
    HARNESS_OPERATOR_TOKEN=<another random value>
    HARNESS_GEMINI_API_KEY=<your key>
    ```

3. **Send it failures**, in whichever of three ways fits:

    - **On demand**, with no server or webhook: find a failed run and diagnose it.

        ```bash
        gh run list -R your-org/your-repo --status failure
        uv run python scripts/replay.py --live --repo your-org/your-repo --run-id <run id>
        ```

    - **Automatically, by webhook.** This needs admin rights on the repository and a URL
      GitHub can reach. In the repository, open **Settings → Webhooks → Add webhook** and set:
        - Payload URL: `https://<your-host>/webhooks/github`
        - Content type: `application/json`
        - Secret: your webhook secret
        - Events: **Workflow runs** only

      `scripts/seed_demo_repo.sh your-org/your-repo --webhook-only --webhook <url>` registers
      the same hook from the command line and touches no branch. On a real repository, use
      it only with `--webhook-only`.

    - **Through the API**: `POST /v1/runs` with `Authorization: Bearer <operator token>` and
      the run's `workflow_run` webhook body as `subject` (step 6 above).

**What to expect on another repository:**

| Topic | What happens |
|---|---|
| Which runs trigger it | Only completed runs that failed. Every failing workflow in the repository triggers one, at 2–3 model requests each; there is no per-workflow filter, so a busy repository spends the free 20-a-day quota quickly |
| Languages and test runners | Best with Python and pytest logs. Other stacks are still diagnosed from generic error lines (`##[error]`, `Error:`), but fewer citations verify, so more runs escalate to a person |
| Dependency changes | Version bumps are recognised in pip, npm, Go and Cargo manifests and lockfiles |
| Baselines | It needs an earlier green run of the same workflow, on the branch or the default branch. Without one it still diagnoses, but takes no action on its own |
| Writes | None while dry run is on: it recommends a re-run or holds a fix plan for approval. Write mode needs write permissions on the token and SEC-10 and SEC-11 fixed first |
| Privacy | Job logs and diffs are sent to the Gemini API. The harness redacts secrets in its own storage, but not in model prompts (SEC-14), and on the free tier Google may use submitted content to improve its products. Take care with private code, or with logs that print secrets |

---

## 7. Deploy to a Hugging Face Space

`docs/deploy-huggingface.md` explains how the Gradio Space runs this app (`app.py` mounts the
FastAPI app unchanged). Push with `git push space master:main`. To confirm the new build is
serving, read `runtime.sha` from `https://huggingface.co/api/spaces/<owner>/<space>`.

Configure the Space under **Settings → Variables and secrets**. Each save restarts the
Space and clears its run database.

| Kind | Name | Notes |
|---|---|---|
| Secret | `HARNESS_GEMINI_API_KEY` | Always |
| Secret | `HARNESS_GITHUB_TOKEN`, `HARNESS_GITHUB_WEBHOOK_SECRET` | Always required; real values for live mode. Paste without a trailing newline |
| Secret | `HARNESS_OPERATOR_TOKEN` | Live mode |
| Variable | `HARNESS_GATEWAY`, `HARNESS_ALLOWED_REPOS` | Live mode only (`github`, `["owner/name"]`) |
| Variable | `HARNESS_LLM_*` (four values) | The free-tier profile |
| Variable | `HARNESS_GEMINI_MODEL` | Optional; the demo uses `gemini-3.5-flash-lite` |

`uv run python scripts/check_space.py` runs free post-deploy checks. In live mode, a
`POST /v1/runs` without the token answering `401` proves the restart applied the settings.

**To return a live Space to replay mode:** delete the repository's webhook, set
`HARNESS_GATEWAY=replay`, delete `HARNESS_ALLOWED_REPOS` and the token secret, and revoke the
token on GitHub.

---

## 8. Manage the free-tier quota

The Gemini free tier allows **20 requests per model per day**. The day resets at midnight
Pacific time, which is 12:30 IST in summer and 13:30 IST in winter. **A request that fails
with `503` (overloaded) still counts.**

- **A run costs 2–3 requests**, one per agent that calls the model: Investigator,
  Diagnostician and Remediator.
- **Use the free-tier retry profile** (section 2). It retries a failed call twice, 5 s
  apart, so an overloaded agent costs 2 requests instead of 4.
- **Count before you spend.** This costs nothing; it counts the Pacific day's model attempts,
  failed ones included, and lists a Space's runs:

    ```bash
    uv run python scripts/quota_ledger.py --db ./data/harness.db [--space https://<host>]
    ```

- **Probe before a replay.** This spends 1 request to ask whether the model is answering.
  An `OK` is not a forecast: full-size calls can still be refused minutes later.

    ```bash
    uv run python scripts/probe_gemini.py [--model gemini-3.5-flash-lite]
    ```

- **Switch models when one is overloaded.** Set `HARNESS_GEMINI_MODEL`, or per agent
  `HARNESS_MODEL_INVESTIGATOR`, `..._DIAGNOSTICIAN` or `..._REMEDIATOR`. Each model has its
  own 20-a-day pool. Accuracy was measured on `gemini-3.6-flash`; the lighter
  `gemini-3.5-flash-lite` answers faster, but follows citation formats less strictly.

---

## 9. Evaluate accuracy

```bash
uv run python scripts/eval.py --runs 1 --concurrency 1               # real model, about 15 requests
uv run python scripts/eval.py --runs 5 --concurrency 1 --llm stub    # free: the pipeline only
uv run python scripts/eval.py --scenario dependency_break --runs 3
```

Every run gets its own temporary database. The report goes to `eval_report.json`, and the
command exits `1` when category accuracy is under 100% or any forbidden action executed.
`--llm stub` answers from canned diagnoses, so it tests the evidence checks and the policy,
not the model.

---

## 10. Record a real failure as a scenario

```bash
uv run python scripts/record_fixture.py --repo <you>/harness-demo-repo --run-id <run id> --name <new_scenario>
uv run python scripts/scrub_fixtures.py --check
```

Recording makes no model call. It needs the same live-mode token and allowlist as
section 6, and scrubs credential-shaped strings on the way in. Run the check **with the
default root**: a mistyped `--root` passes without checking anything. Then fill in the
label in the new `scenario.yaml`, and read the files for secrets before you commit them.

---

## 11. Develop and test

```bash
uv run pytest -q          # about 980 tests; no network, no model calls
uv run ruff check .
uv run mypy src/harness   # strict
```

- **`src/harness/` is domain-agnostic.** `tests/test_layering.py` fails the build if CI/CD
  vocabulary or an import from `src/integrations/` appears there.
- **The test suite ignores your `.env` live settings and retry profile.** `tests/conftest.py`
  pins them, so a live local configuration does not change test results.
- **Failure drills:** with `HARNESS_ENV=dev`, `HARNESS_FAULT_INJECT` can simulate a locked
  database, bad model JSON, `429`s, or a fabricated citation (`sqlite_locked`,
  `llm_bad_json:N`, `llm_429:N`, `diagnostician_fabricate_citation`).

---

## Configuration reference

All settings are environment variables with the `HARNESS_` prefix, read from the
environment or `.env`. `.env.example` lists every one.

| Setting | Default | What it controls |
|---|---|---|
| `GEMINI_API_KEY` | required | The model provider key |
| `GITHUB_TOKEN` | required | GitHub API token (used only in live mode) |
| `GITHUB_WEBHOOK_SECRET` | required | HMAC secret for `/webhooks/github`; must match the hook |
| `OPERATOR_TOKEN` | unset | Bearer token for `/v1/runs` and `/v1/approvals` in live mode |
| `GATEWAY` | `replay` | `replay` (recorded fixtures) or `github` (live) |
| `ALLOWED_REPOS` | `[]` | JSON list of `owner/name` repositories live mode may touch |
| `DRY_RUN` | `true` | Write tools report what they would do instead of doing it |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Model for every agent unless a per-agent override is set |
| `ESCALATION_THRESHOLD` | `0.70` | Final confidence below this escalates |
| `LLM_TRANSIENT_MAX_ATTEMPTS` | `4` | Attempts on a `429`/`503` (free-tier profile: `2`) |
| `LLM_BACKOFF_BASE_S` / `_MAX_S` / `_JITTER` | `0.5` / `8.0` / `full` | Backoff between those attempts (profile: `5` / `15` / `none`) |
| `MAX_CONCURRENT_RUNS` | `4` | Runs executing at once; up to three times as many are admitted before `429` |
| `APPROVAL_TTL_H` | `24` | Hours before a held fix expires |
| `DATABASE_PATH` | `./data/harness.db` | SQLite file for runs, approvals, history and traces |
| `ESCALATION_WEBHOOK_URL` | unset | Optional URL that receives each escalation |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The process exits at boot naming a variable | A missing required secret, or a mistyped `HARNESS_` name | Fix the name or add the value |
| Boot fails on `HARNESS_ALLOWED_REPOS` | A bare string instead of a JSON list | Use `["owner/name"]` |
| `401` on `/v1/runs` or `/v1/approvals` | Live mode needs the operator token | Send `Authorization: Bearer <token>` |
| `403` "not available on this deployment" | Live mode with no operator token configured | Set `HARNESS_OPERATOR_TOKEN` |
| Webhook delivery `401` | The secret differs between the hook and the server, or has a trailing newline | Re-enter the secret on the side that is wrong |
| Webhook delivery `403` | The repository is not allowlisted, or the server is in replay mode | Check `HARNESS_ALLOWED_REPOS` and `HARNESS_GATEWAY` |
| Webhook delivery `204` | Not a completed, failed run (for example a green flaky run) | Expected; fire another failure |
| `413` | A request body over 1 MiB | Send a smaller body |
| `429` "Too many runs" | The run queue is full | Retry after the `Retry-After` interval |
| Run `escalated` with `llm_upstream` or `rate_limited` | The model is overloaded, or the day's quota is spent | Stop for the day, or switch `HARNESS_GEMINI_MODEL` |
| Run `escalated` with `policy_denied` on a flaky failure | No green run on an earlier commit (cold start) | Expected; see the note at the end of section 6 |
| A tool call fails `invalid_args` naming a path or ref | The harness refused a model-supplied path or ref | The guard working; read the trace to see what was tried |

---

## Further reading

- `README.md`: overview, the eval numbers and the live result.
- `PLAN.md`: the normative design, contracts and phase plan.
- `docs/architecture/system-architecture.html`: an interactive architecture diagram.
- `docs/deploy-huggingface.md`: how the Space runs the app.
- `docs/progress/`: each phase's verification record; `phase-5/verify.md` has the live
  step-5 run.
- `docs/security/assessment-2026-09-30.md`: the security assessment, with each finding's
  status.
