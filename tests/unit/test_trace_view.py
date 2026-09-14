# ruff: noqa: E501
"""`src/api/trace_view.py` -- the view-model behind `GET /runs/{id}/view` (PLAN.md Phase 5).

Built from hand-made outcome and trace bodies so each rule the page relies on is pinned
without a run: the waterfall's tree and bar geometry, citations joined to verdicts by
index, the evaluate card's verbatim reason, the rule quoted from the loaded policy, and
autoescaping of everything model- or caller-authored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.api.trace_view import build_view, render_trace_page, rule_text
from src.harness.guardrails import PolicyEngine
from src.integrations.cicd.wiring import load_policy_spec

T0 = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
RUN_ID = "run_01J8ABCDEFGHJKMNPQRSTVWXYZ"


def _span(
    span_id: str, name: str, component: str, parent: str | None, start_ms: int, dur_ms: int,
    status: str = "ok", **attributes: Any,
) -> dict[str, Any]:
    started = T0 + timedelta(milliseconds=start_ms)
    return {
        "span_id": span_id, "parent_span_id": parent, "run_id": RUN_ID, "name": name,
        "component": component, "status": status, "started_at": started.isoformat(),
        "ended_at": (started + timedelta(milliseconds=dur_ms)).isoformat(),
        "duration_ms": dur_ms, "attributes": attributes,
        "error": {"type": "Boom", "message": "it broke"} if status == "error" else None,
    }


def _trace() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "spans": [
            # Insertion order is children-first, as the recorder persists them.
            _span("sp_llm", "llm.attempt", "llm", "sp_agent", 20, 50, attempt=1, **{"tokens.total": 1500}),
            _span("sp_agent", "agent.run", "agent", "sp_stage", 10, 80, agent="diagnostician"),
            _span("sp_stage", "stage", "orchestrator", "sp_run", 5, 90, stage="diagnose"),
            _span("sp_mem", "memory.lookup", "memory", "sp_run", 96, 2, status="error", signature_id="sig_x"),
            _span("sp_run", "run", "orchestrator", None, 0, 100, mode="replay"),
        ],
        "totals": {"prompt": 1200, "completion": 300, "thinking": 0, "total": 1500, "estimated_cost_usd": 0.0},
        "duration_ms": 100,
        "degraded_components": [],
    }


def _outcome(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": RUN_ID,
        "integration": "cicd",
        "status": "awaiting_approval",
        "original_run_id": None,
        "created_at": T0.isoformat(),
        "completed_at": (T0 + timedelta(seconds=1)).isoformat(),
        "duration_ms": 1234,
        "stages": [
            {"stage": "investigate", "agent": "investigator", "status": "ok", "started_at": T0.isoformat(),
             "duration_ms": 400, "attempts": 1, "tokens": {"total": 500}, "summary": "investigate produced FailureBundle"},
            {"stage": "remediate", "agent": None, "status": "gated", "started_at": T0.isoformat(),
             "duration_ms": 0, "attempts": 0, "tokens": {"total": 0}, "summary": "evaluator verdict fail: only 0 of 1 claim(s) verified"},
        ],
        "degraded_components": ["memory"],
        "total_tokens": {"prompt": 1200, "completion": 300, "thinking": 0, "total": 1500, "estimated_cost_usd": 0.0},
        "final": {
            "diagnosis": {
                "category": "real_regression",
                "summary": "<b>bold</b> summary",
                "reasoning": "because",
                "self_confidence": 0.92,
                "final_confidence": 0.77,
                "confidence_adjustments": [
                    {"name": "evidence_refuted", "delta": -0.15, "reason": "1 of 2 claims refuted"},
                ],
                "citations": [
                    {"claim_kind": "quote_exists", "locator": "log:job/1", "quote": "assert 91 == 90", "note": ""},
                    {"claim_kind": "file_in_diff", "locator": "diff:x.py", "quote": "<script>alert(1)</script>", "note": ""},
                    {"claim_kind": "test_in_log", "locator": "log:job/1", "quote": "unjudged", "note": ""},
                ],
                "suspected_commit_sha": "e2cdf1b4",
                "suspected_test_ids": [],
                "suspected_package": None,
                "suggested_action": "open_fix_pr",
            },
            "evaluation": {
                "verdicts": [
                    {"claim_id": "cl_1", "result": "verified", "detail": "exact", "matched_locator": "log:job/1"},
                    {"claim_id": "cl_2", "result": "refuted", "detail": "path not in diff (1 files)", "matched_locator": None},
                ],
                "verified": 1, "refuted": 1, "unverifiable": 0, "verdict": "fail",
                "confidence_delta": -0.15, "reason": "1 of 2 claim(s) refuted",
            },
            "remediation": {
                "status": "awaiting_approval",
                "plan": {"action": "open_fix_pr", "rationale": "fix it", "tool_calls": [
                    {"tool": "create_branch", "args": {"name": "agent/fix/e2cdf1b4"}}],
                    "pr_draft": None, "ticket_draft": None},
                "decisions": [
                    {"tool": "create_branch", "rule_id": "open-fix-pr", "effect": "require_approval",
                     "reason": "matched", "obligations": ["draft_only"], "downgraded_from": "allow",
                     "evaluated_at": T0.isoformat()},
                    {"tool": "merge_pull_request", "rule_id": "<forbidden>", "effect": "deny",
                     "reason": "forbidden", "obligations": [], "downgraded_from": None,
                     "evaluated_at": T0.isoformat()},
                ],
                "executed": [],
                "pending_approval": {"approval_id": "apr_1", "state": "pending",
                                     "requested_at": T0.isoformat(), "expires_at": T0.isoformat()},
            },
        },
        "escalation": None,
        "trace_url": f"/v1/runs/{RUN_ID}/trace",
    }
    body.update(overrides)
    return body


def engine() -> PolicyEngine:
    return PolicyEngine(load_policy_spec())


# ---------------------------------------------------------------------------
# The waterfall
# ---------------------------------------------------------------------------


def test_waterfall_orders_the_tree_depth_first_by_start_and_computes_bars() -> None:
    view = build_view(_outcome(), _trace(), engine())
    rows = view["waterfall"]["rows"]
    assert [(r["name"], r["depth"]) for r in rows] == [
        ("run", 0), ("stage", 1), ("agent.run", 2), ("llm.attempt", 3), ("memory.lookup", 1),
    ]
    run, stage, agent, llm, memory = rows
    assert (run["offset_pct"], run["width_pct"]) == (0.0, 100.0)
    assert (stage["offset_pct"], stage["width_pct"]) == (5.0, 90.0)
    assert llm["offset_pct"] == 20.0 and llm["width_pct"] == 50.0
    # A bar never overflows the track, and never vanishes.
    assert memory["offset_pct"] + memory["width_pct"] <= 100.0
    assert memory["width_pct"] >= 0.4
    assert memory["status"] == "error" and memory["error"] == "Boom: it broke"
    assert view["waterfall"]["count"] == 5 and view["waterfall"]["total_ms"] == 100
    assert view["waterfall"]["components"] == ["agent", "llm", "memory", "orchestrator"]
    # The identifying attributes ride on the row, in the display order.
    assert dict(llm["attributes"]) == {"attempt": "1", "tokens.total": "1500"}
    assert dict(agent["attributes"]) == {"agent": "diagnostician"}


def test_waterfall_without_a_trace_says_so() -> None:
    view = build_view(_outcome(), None, engine())
    assert view["waterfall"] == {
        "rows": [], "count": 0, "total_ms": 0, "components": [], "missing": True,
    }


# ---------------------------------------------------------------------------
# The cards
# ---------------------------------------------------------------------------


def test_citations_are_joined_to_verdicts_by_index() -> None:
    card = build_view(_outcome(), _trace(), engine())["cards"]["diagnose"]
    results = [c["verdict"]["result"] if c["verdict"] else None for c in card["citations"]]
    assert results == ["verified", "refuted", None]
    assert card["citations"][1]["verdict"]["detail"] == "path not in diff (1 files)"
    assert card["self_confidence"] == "0.92" and card["final_confidence"] == "0.77"
    assert card["delta"] == "-0.15"
    assert card["adjustments"] == [
        {"name": "evidence_refuted", "delta": "-0.15", "reason": "1 of 2 claims refuted"},
    ]


def test_evaluate_card_carries_the_reports_reason_verbatim() -> None:
    outcome = _outcome()
    outcome["final"]["evaluation"].update(
        {"verified": 0, "refuted": 0, "unverifiable": 1, "reason": "only 0 of 1 claim(s) verified",
         "verdicts": [{"claim_id": "cl_1", "result": "unverifiable", "detail": "no log", "matched_locator": None}]}
    )
    card = build_view(outcome, None, engine())["cards"]["evaluate"]
    assert card["verdict"] == "fail"
    assert card["reason"] == "only 0 of 1 claim(s) verified"
    assert (card["verified"], card["refuted"], card["unverifiable"], card["total"]) == (0, 0, 1, 1)


def test_decisions_quote_the_rule_the_engine_loaded_and_show_downgrades() -> None:
    card = build_view(_outcome(), None, engine())["cards"]["remediate"]
    first, second = card["decisions"]
    assert first["downgraded_from"] == "allow"
    assert "- id: open-fix-pr" in first["rule"]
    assert "effect: require_approval" in first["rule"]
    assert "- create_branch" in first["rule"]
    assert "diagnosis.final_confidence:" in first["rule"] and "gte: 0.85" in first["rule"]
    assert second["rule"].startswith("forbidden:")
    assert "- merge_pull_request" in second["rule"]


def test_rule_text_for_the_default_and_an_unknown_rule() -> None:
    assert rule_text(engine(), "<default>") == "default_effect: deny"
    assert rule_text(engine(), "no-such-rule").startswith("# no rule named")


def test_header_and_stages() -> None:
    view = build_view(_outcome(), None, engine())
    run = view["run"]
    assert run["duration"] == "1.2 s"
    assert run["cost"] == "unpriced"
    assert run["degraded"] == ["memory"]
    assert run["tokens"]["total"] == 1500
    assert [s["stage"] for s in view["stages"]] == ["investigate", "remediate"]
    assert view["stages"][1]["agent"] == "—" and view["stages"][1]["status"] == "gated"
    assert view["escalation"] is None
    assert view["cards"]["investigate"] is None


def test_escalation_card_shows_delivery_error() -> None:
    outcome = _outcome(
        status="escalated",
        escalation={
            "escalation_id": "esc_1", "reason": "evidence_unverifiable",
            "message": "evaluator verdict fail: only 0 of 1 claim(s) verified",
            "payload": {"stage": "remediate"}, "channels": ["log", "db", "webhook"],
            "delivered_at": None, "delivery_error": "HTTPStatusError 503",
        },
    )
    card = build_view(outcome, None, engine())["escalation"]
    assert card is not None
    assert card["reason"] == "evidence_unverifiable"
    assert card["delivery_error"] == "HTTPStatusError 503"
    assert card["delivered_at"] is None
    assert '"stage": "remediate"' in card["payload"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_escapes_model_authored_text() -> None:
    html = render_trace_page(_outcome(), _trace(), engine())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;bold&lt;/b&gt; summary" in html
    assert RUN_ID in html
    assert "open-fix-pr" in html and "require_approval" in html
    assert "evidence_refuted" in html
    assert 'class="row"' in html and html.count('class="row"') == 5
