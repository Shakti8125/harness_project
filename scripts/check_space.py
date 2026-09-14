"""Post-deploy check of the Space, free of model calls unless asked.

    uv run python scripts/check_space.py [--base https://shakti-agent-harness.hf.space]
                                         [--wait-for-new-build] [--replay real_regression]

Without `--replay` nothing here costs a Gemini call: `healthz`, `readyz`, the run and
escalation lists, an unsigned `POST /webhooks/github` (must be `401` with the
`WWW-Authenticate` challenge -- the one answer only a Phase 5 image gives), an unknown
run's `/view` (`404 application/problem+json`), and a credential-shape scan over every
body served. `--wait-for-new-build` polls the webhook route until it answers `401`
(the old image has no such route and answers `404`), up to ten minutes.

`--replay <scenario>` spends three calls: one replay through the real model, then that
run's JSON, trace and `/view`, with the component set printed the way PLAN.md's Verify
step 1 asks (`[.spans[].component] | unique`). Count the day first.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import httpx

CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|AIza[0-9A-Za-z_\-]{35}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
EXPECTED_COMPONENTS = [
    "agent", "context_manager", "evaluator", "gateway", "guardrails", "llm", "memory",
    "orchestrator",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="https://shakti-agent-harness.hf.space")
    parser.add_argument("--wait-for-new-build", action="store_true")
    parser.add_argument("--replay", metavar="SCENARIO", default=None)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    served: list[str] = []
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}{(': ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    with httpx.Client(base_url=base, timeout=60.0) as client:
        if args.wait_for_new_build:
            deadline = time.monotonic() + 600
            while True:
                code: int | str
                try:
                    code = client.post(
                        "/webhooks/github", content=b"{}",
                        headers={"X-GitHub-Event": "workflow_run"},
                    ).status_code
                except httpx.HTTPError as exc:
                    code = f"({exc.__class__.__name__})"
                print(f"     webhook route answers {code}")
                if code == 401:
                    break
                if time.monotonic() > deadline:
                    check("new build answered within 10 min", False)
                    return 1
                time.sleep(15)

        health = client.get("/healthz")
        served.append(health.text)
        healthy = health.status_code == 200 and health.json().get("status") == "ok"
        check("healthz", healthy, health.text)

        ready = client.get("/readyz")
        served.append(ready.text)
        is_json = ready.headers.get("content-type", "").startswith("application/json")
        body = ready.json() if is_json else {}
        check(
            "readyz", ready.status_code == 200 and body.get("migrations_applied") is True,
            ready.text,
        )

        for path in ("/v1/runs", "/v1/escalations"):
            listing = client.get(path)
            served.append(listing.text)
            check(f"GET {path}", listing.status_code == 200, f"{len(listing.text)} bytes")

        unsigned = client.post(
            "/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "workflow_run"},
        )
        served.append(unsigned.text)
        check(
            "unsigned webhook is 401 with the HMAC challenge",
            unsigned.status_code == 401
            and unsigned.headers.get("www-authenticate", "").startswith("HMAC-SHA256")
            and unsigned.headers.get("content-type", "").startswith("application/problem+json"),
            f"{unsigned.status_code} {unsigned.headers.get('www-authenticate', '')!r}",
        )

        missing = client.get("/runs/run_01J8NOPENOPENOPENOPENOPENO/view")
        served.append(missing.text)
        check(
            "unknown run's view is a 404 problem document",
            missing.status_code == 404
            and missing.headers.get("content-type", "").startswith("application/problem+json"),
            f"{missing.status_code} {missing.headers.get('content-type', '')}",
        )

        if args.replay:
            print(f"     replaying {args.replay} through the real model (three calls) ...")
            started = time.monotonic()
            replay = client.post(f"/v1/replay/{args.replay}", timeout=300.0)
            served.append(replay.text)
            outcome = replay.json()
            run_id = outcome.get("run_id", "?")
            check(
                f"POST /v1/replay/{args.replay}",
                replay.status_code in (200, 202),
                f"{replay.status_code} {run_id} {outcome.get('status')} "
                f"in {time.monotonic() - started:.0f}s",
            )
            for _ in range(120):
                current = client.get(f"/v1/runs/{run_id}").json()
                if current.get("status") != "in_progress":
                    break
                time.sleep(2)
            served.append(json.dumps(current))
            diag = (current.get("final") or {}).get("diagnosis") or {}
            print(
                f"     run {run_id}: {current.get('status')}, category {diag.get('category')}, "
                f"final_confidence {diag.get('final_confidence')}, "
                f"escalation {(current.get('escalation') or {}).get('reason')}"
            )
            trace = client.get(f"/v1/runs/{run_id}/trace")
            served.append(trace.text)
            spans = trace.json().get("spans", [])
            components = sorted({s.get("component") for s in spans})
            print(
                f"     spans: {len(spans)}; [.spans[].component] | unique: "
                f"{json.dumps(components)}"
            )
            check("trace has >= 12 spans", len(spans) >= 12)
            check("the eight components", components == EXPECTED_COMPONENTS, json.dumps(components))
            view = client.get(f"/runs/{run_id}/view")
            served.append(view.text)
            check(
                "GET /runs/{id}/view",
                view.status_code == 200
                and view.headers.get("content-type", "").startswith("text/html"),
                f"{view.status_code} {len(view.content)} bytes; open {base}/runs/{run_id}/view",
            )
            for needle in ("waterfall", "investigate", "diagnose", "evaluate", "remediate"):
                check(f"view mentions {needle!r}", needle in view.text.lower())

    total = sum(len(b) for b in served)
    hits = [m.group(0)[:12] for b in served for m in CREDENTIAL_SHAPES.finditer(b)]
    check(f"no credential shape in {total} bytes served", not hits, ", ".join(hits))
    print("\n" + ("ALL OK" if not failures else f"FAILED: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
