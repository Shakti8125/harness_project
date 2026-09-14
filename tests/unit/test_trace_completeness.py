# ruff: noqa: E501
"""The three components that never wrote a span before Phase 5, plus the stage span.

PLAN.md's trace model: "one span per: run, stage, agent attempt, LLM call, tool call,
policy decision, memory query, evaluator check". Verify step 1 reads the component set
`agent, context_manager, evaluator, gateway, guardrails, llm, memory, orchestrator` off
a replay run; the gateway half is `test_gateway_replay_writes.py`, the stage span is
pinned by the replay e2e trace test, and this file covers the memory store and the
context manager -- including that a faulted store still gets its error span written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.api.deps import SECRET_PATTERNS
from src.harness.context_manager import (
    ContextBudget,
    ContextManager,
    ContextRequest,
    Section,
)
from src.harness.memory import (
    FAULT_SQLITE_LOCKED,
    MemoryQuery,
    MemoryStoreError,
    Observation,
    SignatureKey,
    SqliteMemoryStore,
    signature_id_for,
)
from src.harness.observability import Redactor, SecretRegistry, TraceRecorder

RUN_ID = "run_01J8ABCDEFGHJKMNPQRSTVWXYZ"
KEY = SignatureKey(scope="repo:octo-org/harness-demo-repo", subject_key="ci:9001", fingerprint="fp_x")


@pytest.fixture
async def recorder(tmp_db_path: Path) -> TraceRecorder:
    rec = TraceRecorder(db_path=tmp_db_path, redactor=Redactor(SecretRegistry(), SECRET_PATTERNS))
    await rec.initialize()
    return rec


async def test_memory_operations_are_spans(tmp_db_path: Path, recorder: TraceRecorder) -> None:
    store = SqliteMemoryStore(tmp_db_path, recorder=recorder)
    await store.initialize()
    with recorder.run_scope(RUN_ID):
        miss = await store.lookup(MemoryQuery(key=KEY))
        signature_id = await store.upsert_signature(KEY, "flaky_test", RUN_ID)
        await store.record_observation(
            Observation(
                observation_id="obs_1", signature_id=signature_id, run_id=RUN_ID,
                occurred_at=datetime.now(UTC), verdict="flaky_test", confidence=0.9,
                action_taken="rerun_failed_jobs", action_outcome="pending", commit_sha=None,
            )
        )
        await store.update_observation_outcome("obs_1", "passed_on_retry")
        hit = await store.lookup(MemoryQuery(key=KEY))
    assert miss.record is None and hit.record is not None

    trace = await recorder.read_trace(RUN_ID)
    assert trace is not None
    memory_spans = [s for s in trace.spans if s.component == "memory"]
    assert [s.name for s in memory_spans] == [
        "memory.lookup", "memory.upsert_signature", "memory.record_observation",
        "memory.update_observation_outcome", "memory.lookup",
    ]
    assert all(s.status == "ok" for s in memory_spans)
    first, upsert, record, update, second = memory_spans
    assert first.attributes == {
        "signature_id": signature_id_for(KEY), "found": False, "occurrences": 0,
        "recent": 0, "actions_in_window": {},
    }
    assert upsert.attributes == {"signature_id": signature_id_for(KEY), "verdict": "flaky_test"}
    assert record.attributes["observation_id"] == "obs_1"
    assert record.attributes["action_taken"] == "rerun_failed_jobs"
    assert update.attributes == {"observation_id": "obs_1", "outcome": "passed_on_retry"}
    assert second.attributes["found"] is True and second.attributes["occurrences"] == 1
    assert second.attributes["recent"] == 1
    assert second.attributes["actions_in_window"] == {"rerun_failed_jobs": 1}


async def test_a_locked_store_still_writes_its_error_span(
    tmp_db_path: Path, recorder: TraceRecorder
) -> None:
    """The span is persisted through `storage.connect`, not the store's faulted path, so
    the trace says where memory failed (dispatch decision 3)."""
    store = SqliteMemoryStore(tmp_db_path, recorder=recorder, fault_inject=FAULT_SQLITE_LOCKED)
    await store.initialize()
    with recorder.run_scope(RUN_ID), pytest.raises(MemoryStoreError):
        await store.lookup(MemoryQuery(key=KEY))
    trace = await recorder.read_trace(RUN_ID)
    assert trace is not None
    (span,) = [s for s in trace.spans if s.name == "memory.lookup"]
    assert span.status == "error"
    assert span.error is not None and span.error["type"] == "MemoryStoreError"


async def test_bookkeeping_is_not_traced(tmp_db_path: Path, recorder: TraceRecorder) -> None:
    """Claims, heartbeats and saves are the run's plumbing, not memory queries."""
    store = SqliteMemoryStore(tmp_db_path, recorder=recorder)
    await store.initialize()
    with recorder.run_scope(RUN_ID):
        claim = await store.claim_run("cicd:test-key-0001", "cicd")
        await store.heartbeat(claim.run_id)
        await store.get_run(claim.run_id)
        await store.list_runs(limit=5)
    assert await recorder.read_trace(RUN_ID) is None


async def test_a_store_without_a_recorder_traces_nothing(
    tmp_db_path: Path, recorder: TraceRecorder
) -> None:
    store = SqliteMemoryStore(tmp_db_path)
    await store.initialize()
    with recorder.run_scope(RUN_ID):
        await store.lookup(MemoryQuery(key=KEY))
    assert await recorder.read_trace(RUN_ID) is None


# ---------------------------------------------------------------------------
# context_manager
# ---------------------------------------------------------------------------


def _request(lines: int) -> ContextRequest:
    log = "\n".join(f"line {i}" if i != 500 else "E   AssertionError: expected 42" for i in range(lines))
    return ContextRequest(
        sections=[Section(key="log", content=log, priority=10), Section(key="diff", content="+x", priority=5)],
        budget=ContextBudget(total_chars=4_000, reserve_chars=500, head_lines=20, tail_lines=20),
        anchor_patterns=[r"^E\s"],
    )


async def test_assemble_traced_records_the_truncation_report(recorder: TraceRecorder) -> None:
    manager = ContextManager(recorder=recorder)
    with recorder.run_scope(RUN_ID):
        bundle = await manager.assemble_traced(_request(2_000))
    assert "AssertionError: expected 42" in bundle.text
    trace = await recorder.read_trace(RUN_ID)
    assert trace is not None
    (span,) = trace.spans
    assert (span.name, span.component, span.status) == ("context.assemble", "context_manager", "ok")
    assert span.attributes["sections"] == ["log", "diff"]
    assert span.attributes["budget_chars"] == 4_000 and span.attributes["reserve_chars"] == 500
    assert span.attributes["estimated_tokens"] == bundle.estimated_tokens
    assert span.attributes["text_chars"] == len(bundle.text)
    truncation = span.attributes["truncation"]
    assert isinstance(truncation, dict)
    log_report = truncation["log"]
    assert isinstance(log_report, dict)
    assert log_report["original_lines"] == 2_000
    assert log_report["kept_lines"] < 2_000
    assert log_report["anchors_found"] == 1 and log_report["anchors_kept"] == 1
    assert log_report["anchors_dropped"] == 0


async def test_assemble_traced_is_assemble_when_unrecorded(recorder: TraceRecorder) -> None:
    manager = ContextManager()
    with recorder.run_scope(RUN_ID):
        traced = await manager.assemble_traced(_request(200))
    assert traced == manager.assemble(_request(200))
    assert await recorder.read_trace(RUN_ID) is None
