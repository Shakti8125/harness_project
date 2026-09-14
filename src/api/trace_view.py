"""The trace view's view-model: `GET /runs/{run_id}/view` (PLAN.md Phase 5).

Everything on the page is derived from two bodies the JSON routes already serve -- the
run outcome as `GET /v1/runs/{id}` serves it (digested and scrubbed by
`main._serialize_run_outcome`) and the scrubbed `TraceResponse` -- plus the loaded
`PolicyEngine`, for the rule text a decision card quotes. The template renders the
result with autoescaping on; nothing here reads the store, the settings, or the raw
models, so a secret that cannot reach the JSON cannot reach the HTML.

The page is one server-rendered document: a header, the escalation (if any), the stage
table, a waterfall of every span, and one card per stage. Citations are joined to their
verify verdicts **by index** -- the evaluate agent builds one claim per citation, in
order (`claim_id = cl_<n>`) -- and the evaluate card renders the report's `reason`
verbatim next to the verdict, so a share-rule `fail` reads as "0 of 1 verified" and not
as "refuted". The rule quoted on a decision is read off `PolicyEngine.rule(rule_id)`,
the engine that made the decision, rendered as YAML.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.harness.guardrails import PolicyEngine

TEMPLATES_DIR: Final[Path] = Path(__file__).with_name("templates")

#: Span attributes worth showing on a waterfall row, in display order. Anything else
#: stays in the JSON trace; the page is a reading aid, not a second serialisation.
_ROW_ATTRIBUTES: Final[tuple[str, ...]] = (
    "stage", "agent", "tool", "side_effect", "rule_id", "effect", "downgraded_from",
    "kind", "result", "verdict", "action", "attempt", "schema", "status", "ok", "dry_run",
    "cached", "error_kind", "sections", "estimated_tokens", "found", "occurrences",
    "prompt_version", "max_output_tokens", "tokens.total", "final_confidence",
    "confidence_delta", "degraded_component",
)
_ROW_VALUE_CHARS: Final[int] = 72
#: The narrowest bar drawn, in percent of the run, so a sub-millisecond span is visible.
_MIN_BAR_PCT: Final[float] = 0.4

_environment: Environment | None = None


def environment() -> Environment:
    """The one Jinja2 environment: filesystem loader over `templates/`, autoescape on."""
    global _environment
    if _environment is None:
        _environment = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(default=True, default_for_string=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _environment


def render_trace_page(
    outcome: dict[str, Any], trace: dict[str, Any] | None, engine: PolicyEngine
) -> str:
    """The HTML for one run."""
    return environment().get_template("trace.html").render(**build_view(outcome, trace, engine))


# ---------------------------------------------------------------------------
# The view-model
# ---------------------------------------------------------------------------


def build_view(
    outcome: dict[str, Any], trace: dict[str, Any] | None, engine: PolicyEngine
) -> dict[str, Any]:
    final = _dict(outcome.get("final"))
    tokens = _dict(outcome.get("total_tokens"))
    cost = tokens.get("estimated_cost_usd")
    return {
        "run": {
            "run_id": outcome.get("run_id"),
            "status": outcome.get("status"),
            "original_run_id": outcome.get("original_run_id"),
            "integration": outcome.get("integration"),
            "created_at": _when(outcome.get("created_at")),
            "completed_at": _when(outcome.get("completed_at")),
            "duration_ms": outcome.get("duration_ms"),
            "duration": _duration(outcome.get("duration_ms")),
            "tokens": {
                "prompt": tokens.get("prompt", 0),
                "completion": tokens.get("completion", 0),
                "thinking": tokens.get("thinking", 0),
                "total": tokens.get("total", 0),
            },
            "cost": _cost(cost),
            "degraded": list(outcome.get("degraded_components") or []),
            "trace_url": outcome.get("trace_url"),
        },
        "escalation": _escalation(outcome.get("escalation")),
        "stages": [_stage_row(record) for record in outcome.get("stages") or []],
        "waterfall": _waterfall(trace),
        "cards": {
            "investigate": _investigate_card(final.get("bundle")),
            "diagnose": _diagnose_card(final.get("diagnosis"), final.get("evaluation")),
            "evaluate": _evaluate_card(final.get("evaluation")),
            "remediate": _remediate_card(final.get("remediation"), engine),
        },
    }


# -- header and stages ------------------------------------------------------------


def _escalation(raw: object) -> dict[str, Any] | None:
    record = _dict(raw)
    if not record:
        return None
    return {
        "reason": record.get("reason"),
        "message": record.get("message"),
        "channels": list(record.get("channels") or []),
        "delivered_at": _when(record.get("delivered_at")),
        "delivery_error": record.get("delivery_error"),
        "payload": _pretty(record.get("payload")),
    }


def _stage_row(raw: object) -> dict[str, Any]:
    record = _dict(raw)
    tokens = _dict(record.get("tokens"))
    return {
        "stage": record.get("stage"),
        "agent": record.get("agent") or "—",
        "status": record.get("status"),
        "attempts": record.get("attempts"),
        "tokens": tokens.get("total", 0),
        "duration": _duration(record.get("duration_ms")),
        "summary": record.get("summary"),
    }


# -- the waterfall ---------------------------------------------------------------


def _waterfall(trace: dict[str, Any] | None) -> dict[str, Any]:
    spans = [s for s in (trace or {}).get("spans") or [] if isinstance(s, dict)]
    if not spans:
        return {"rows": [], "count": 0, "total_ms": 0, "components": [], "missing": trace is None}

    by_id = {str(s.get("span_id")): s for s in spans}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        parent = span.get("parent_span_id")
        key = str(parent) if isinstance(parent, str) and parent in by_id else None
        children.setdefault(key, []).append(span)

    starts = [_ts(s.get("started_at")) for s in spans]
    ends = [_ts(s.get("ended_at")) for s in spans]
    origin = min(t for t in starts if t is not None)
    last = max((t for t in ends if t is not None), default=origin)
    total_ms = max(1.0, (last - origin).total_seconds() * 1000)

    rows: list[dict[str, Any]] = []

    def walk(parent: str | None, depth: int) -> None:
        ordered = sorted(children.get(parent, []), key=lambda s: _ts(s.get("started_at")) or origin)
        for span in ordered:
            started = _ts(span.get("started_at")) or origin
            duration = span.get("duration_ms")
            duration_ms = float(duration) if isinstance(duration, (int, float)) else 0.0
            offset_pct = max(0.0, (started - origin).total_seconds() * 1000 / total_ms * 100)
            width_pct = max(_MIN_BAR_PCT, duration_ms / total_ms * 100)
            width_pct = min(width_pct, max(_MIN_BAR_PCT, 100 - offset_pct))
            attributes = _dict(span.get("attributes"))
            error = _dict(span.get("error"))
            rows.append(
                {
                    "span_id": span.get("span_id"),
                    "depth": depth,
                    "name": span.get("name"),
                    "component": span.get("component"),
                    "status": span.get("status"),
                    "duration": _duration(duration),
                    "offset_pct": round(offset_pct, 3),
                    "width_pct": round(width_pct, 3),
                    "attributes": _row_attributes(attributes),
                    "error": (
                        f"{error.get('type', 'error')}: {error.get('message', '')}"
                        if error else None
                    ),
                }
            )
            walk(str(span.get("span_id")), depth + 1)

    walk(None, 0)
    return {
        "rows": rows,
        "count": len(rows),
        "total_ms": int(total_ms),
        "components": sorted({str(s.get("component")) for s in spans}),
        "missing": False,
    }


def _row_attributes(attributes: dict[str, Any]) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []
    for key in _ROW_ATTRIBUTES:
        if key in attributes and attributes[key] is not None:
            shown.append((key, _short(attributes[key])))
    return shown


# -- the cards -------------------------------------------------------------------


def _investigate_card(raw: object) -> dict[str, Any] | None:
    bundle = _dict(raw)
    if not bundle:
        return None
    job = _dict(bundle.get("job"))
    diff = _dict(bundle.get("diff"))
    prior = _dict(bundle.get("prior_history"))
    notes = _dict(bundle.get("notes"))
    files = [f for f in diff.get("files") or [] if isinstance(f, dict)]
    return {
        "job": {
            "repo": job.get("repo"),
            "workflow": job.get("workflow_name"),
            "job": job.get("job_name"),
            "run_id": job.get("run_id"),
            "attempt": job.get("run_attempt"),
            "branch": job.get("branch"),
            "head_sha": _sha(job.get("head_sha")),
            "conclusion": job.get("conclusion"),
        },
        "cold_start": bool(bundle.get("cold_start")),
        "diff": {
            "baseline_kind": diff.get("baseline_kind"),
            "base_sha": _sha(diff.get("base_sha")),
            "head_sha": _sha(diff.get("head_sha")),
            "commits": len(diff.get("commit_shas") or []),
            "commits_behind": diff.get("commits_behind"),
            "files": [
                {
                    "path": f.get("path"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in files
            ],
            "truncated": bool(diff.get("truncated")),
            "total_files": diff.get("total_files"),
        },
        "dependency_changes": [
            {
                "ecosystem": c.get("ecosystem"),
                "package": c.get("package"),
                "from_version": c.get("from_version"),
                "to_version": c.get("to_version"),
                "manifest_path": c.get("manifest_path"),
            }
            for c in bundle.get("dependency_changes") or []
            if isinstance(c, dict)
        ],
        "logs": [
            {
                "job_id": entry.get("job_id"),
                "total_lines": entry.get("total_lines"),
                "included_lines": entry.get("included_lines"),
                "excerpt_length": entry.get("excerpt_length"),
                "anchors": len(entry.get("anchor_line_numbers") or []),
                "anchors_dropped": _dict(entry.get("truncation")).get("anchors_dropped", 0),
            }
            for entry in bundle.get("logs") or []
            if isinstance(entry, dict)
        ],
        "prior": {
            "signature_id": prior.get("signature_id"),
            "occurrences": prior.get("occurrences", 0),
            "verdict_counts": _pretty(prior.get("verdict_counts") or {}, inline=True),
            "prior_hint": prior.get("prior_hint"),
            "retries_in_24h": prior.get("retries_in_24h"),
            "last_retry_outcome": prior.get("last_retry_outcome"),
            "unavailable": bool(prior.get("unavailable")),
        },
        "observations": [str(o) for o in notes.get("observations") or []],
        "gateway_errors": [
            f"{_dict(e).get('kind')}: {_dict(e).get('message')}"
            for e in bundle.get("gateway_errors") or []
        ],
        "additional_tool_outcomes": [
            f"{_dict(o).get('tool')}: {_dict(o).get('outcome')}"
            for o in bundle.get("additional_tool_outcomes") or []
        ],
    }


def _diagnose_card(raw: object, evaluation_raw: object) -> dict[str, Any] | None:
    diagnosis = _dict(raw)
    if not diagnosis:
        return None
    verdicts = [v for v in _dict(evaluation_raw).get("verdicts") or [] if isinstance(v, dict)]
    citations = []
    for index, citation in enumerate(diagnosis.get("citations") or []):
        if not isinstance(citation, dict):
            continue
        verdict = verdicts[index] if index < len(verdicts) else None
        citations.append(
            {
                "claim_kind": citation.get("claim_kind"),
                "locator": citation.get("locator"),
                "quote": citation.get("quote"),
                "note": citation.get("note"),
                "verdict": (
                    {
                        "result": verdict.get("result"),
                        "detail": verdict.get("detail"),
                        "matched_locator": verdict.get("matched_locator"),
                    }
                    if verdict is not None else None
                ),
            }
        )
    adjustments = [
        {"name": a.get("name"), "delta": _signed(a.get("delta")), "reason": a.get("reason")}
        for a in diagnosis.get("confidence_adjustments") or []
        if isinstance(a, dict)
    ]
    self_confidence = _number(diagnosis.get("self_confidence"))
    final_confidence = _number(diagnosis.get("final_confidence"))
    return {
        "category": diagnosis.get("category"),
        "summary": diagnosis.get("summary"),
        "reasoning": diagnosis.get("reasoning"),
        "suggested_action": diagnosis.get("suggested_action"),
        "suspected_commit_sha": diagnosis.get("suspected_commit_sha"),
        "suspected_test_ids": [str(t) for t in diagnosis.get("suspected_test_ids") or []],
        "suspected_package": diagnosis.get("suspected_package"),
        "self_confidence": f"{self_confidence:.2f}",
        "final_confidence": f"{final_confidence:.2f}",
        "delta": _signed(final_confidence - self_confidence),
        "adjustments": adjustments,
        "citations": citations,
    }


def _evaluate_card(raw: object) -> dict[str, Any] | None:
    report = _dict(raw)
    if not report:
        return None
    return {
        "verdict": report.get("verdict"),
        "verified": report.get("verified", 0),
        "refuted": report.get("refuted", 0),
        "unverifiable": report.get("unverifiable", 0),
        "total": len(report.get("verdicts") or []),
        "confidence_delta": _signed(report.get("confidence_delta")),
        "reason": report.get("reason"),
    }


def _remediate_card(raw: object, engine: PolicyEngine) -> dict[str, Any] | None:
    remediation = _dict(raw)
    if not remediation:
        return None
    plan = _dict(remediation.get("plan"))
    pr_draft = _dict(plan.get("pr_draft"))
    ticket_draft = _dict(plan.get("ticket_draft"))
    pending = _dict(remediation.get("pending_approval"))
    return {
        "status": remediation.get("status"),
        "plan": {
            "action": plan.get("action"),
            "rationale": plan.get("rationale"),
            "tool_calls": [
                {"tool": c.get("tool"), "args": _pretty(c.get("args"), inline=True)}
                for c in plan.get("tool_calls") or []
                if isinstance(c, dict)
            ],
            "pr_draft": (
                {
                    "branch": pr_draft.get("branch"),
                    "base": pr_draft.get("base"),
                    "title": pr_draft.get("title"),
                    "body": pr_draft.get("body"),
                    "files": [
                        str(_dict(f).get("path")) for f in pr_draft.get("files") or []
                    ],
                    "labels": [str(label) for label in pr_draft.get("labels") or []],
                }
                if pr_draft else None
            ),
            "ticket_draft": (
                {
                    "title": ticket_draft.get("title"),
                    "body": ticket_draft.get("body"),
                    "labels": [str(label) for label in ticket_draft.get("labels") or []],
                }
                if ticket_draft else None
            ),
        },
        "decisions": [_decision(d, engine) for d in remediation.get("decisions") or []],
        "executed": [
            {
                "tool": r.get("tool"),
                "ok": bool(r.get("ok")),
                "dry_run": bool(r.get("dry_run")),
                "cached": bool(r.get("cached")),
                "error": (
                    f"{_dict(r.get('error')).get('kind')}: {_dict(r.get('error')).get('message')}"
                    if _dict(r.get("error")) else None
                ),
                "data": _pretty(r.get("data"), inline=True),
            }
            for r in remediation.get("executed") or []
            if isinstance(r, dict)
        ],
        "pending_approval": (
            {
                "approval_id": pending.get("approval_id"),
                "state": pending.get("state"),
                "requested_at": _when(pending.get("requested_at")),
                "expires_at": _when(pending.get("expires_at")),
            }
            if pending else None
        ),
    }


def _decision(raw: object, engine: PolicyEngine) -> dict[str, Any]:
    decision = _dict(raw)
    rule_id = str(decision.get("rule_id") or "")
    return {
        "tool": decision.get("tool"),
        "effect": decision.get("effect"),
        "rule_id": rule_id,
        "downgraded_from": decision.get("downgraded_from"),
        "reason": decision.get("reason"),
        "obligations": [str(o) for o in decision.get("obligations") or []],
        "rule": rule_text(engine, rule_id),
    }


def rule_text(engine: PolicyEngine, rule_id: str) -> str:
    """The rule a decision cites, as the YAML the engine loaded -- quoted, not copied.

    `<default>` and `<forbidden>` are not rules in the file; for those the page quotes
    the clause that answered instead (`default_effect`, the `forbidden` list).
    """
    spec = engine.spec
    if rule_id == "<forbidden>":
        return yaml.safe_dump({"forbidden": list(spec.forbidden)}, sort_keys=False).strip()
    if rule_id == "<default>":
        return yaml.safe_dump({"default_effect": spec.default_effect}, sort_keys=False).strip()
    rule = engine.rule(rule_id)
    if rule is None:
        return f"# no rule named {rule_id!r} in the loaded policy"
    dumped = rule.model_dump(by_alias=True, exclude_none=True, mode="json")
    return yaml.safe_dump({"rules": [dumped]}, sort_keys=False).strip()


# -- small helpers -------------------------------------------------------------


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _signed(value: object) -> str:
    return f"{_number(value):+.2f}"


def _sha(value: object) -> str | None:
    return str(value)[:12] if isinstance(value, str) and value else None


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _when(value: object) -> str | None:
    stamp = _ts(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC") if stamp is not None else None


def _duration(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    ms = float(value)
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{int(ms)} ms"


def _cost(value: object) -> str:
    amount = _number(value)
    return f"${amount:.4f}" if amount > 0 else "unpriced"


def _pretty(value: object, *, inline: bool = False) -> str:
    if value is None:
        return ""
    if inline:
        return _short(value, limit=400)
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _short(value: object, limit: int = _ROW_VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
