from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from vetevidence.run_store import (
    CURRENT_SNAPSHOT_SCHEMA_VERSION,
    RunStore,
    ToolCallRecord,
    WorkbenchRunSnapshot,
    build_tool_call,
)
from vetevidence.workbench import ResearchQuestion, TaskStatus, build_task_event
from vetevidence.workbench_pipeline import generate_search_queries


def test_run_store_round_trip_preserves_events_and_failures(tmp_path) -> None:
    question = ResearchQuestion(id="rq-1", text="候选药物是否具有协同作用")
    events = [
        build_task_event("run-1", TaskStatus.PENDING, "任务已创建"),
        build_task_event("run-1", TaskStatus.FAILED, "NCBI 暂时不可用"),
        build_task_event("run-1", TaskStatus.RUNNING, "人工触发重试"),
    ]
    failed_call = build_tool_call(
        "pubmed.search",
        "3 个查询变体",
        status="failed",
        error="temporary network error",
    )
    retry = build_tool_call(
        "pubmed.search",
        "重试 3 个查询变体",
        status="succeeded",
        output_summary="返回 2 个唯一 PMID",
        retry_of=failed_call.call_id,
    )
    snapshot = WorkbenchRunSnapshot(
        run_id="run-1",
        question=question,
        query_plan=generate_search_queries(question),
        task_events=events,
        tool_calls=[failed_call, retry],
    )
    store = RunStore(tmp_path / "runs")

    path = store.save(snapshot)
    restored = store.load("run-1")

    assert path.is_file()
    assert restored.task_events[1].status == TaskStatus.FAILED
    assert restored.tool_calls[1].retry_of == failed_call.call_id
    assert restored.tool_calls[1].status == "succeeded"
    assert restored.schema_version == CURRENT_SNAPSHOT_SCHEMA_VERSION
    assert store.list_run_ids() == ["run-1"]


def test_run_store_retries_transient_windows_replace_lock(
    tmp_path,
    monkeypatch,
) -> None:
    question = ResearchQuestion(id="rq-retry", text="候选药物是否有效？")
    snapshot = WorkbenchRunSnapshot(
        run_id="run-retry",
        question=question,
        query_plan=generate_search_queries(question),
        task_events=[
            build_task_event("run-retry", TaskStatus.PENDING, "任务已创建")
        ],
    )
    store = RunStore(tmp_path / "runs")
    original_replace = Path.replace
    attempts = 0

    def transient_lock(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient file watcher lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", transient_lock)

    saved = store.save(snapshot)

    assert attempts == 2
    assert saved.is_file()
    assert store.load("run-retry").run_id == "run-retry"
    assert list(saved.parent.glob("*.tmp")) == []


def test_run_store_rejects_path_traversal(tmp_path) -> None:
    store = RunStore(tmp_path / "runs")

    try:
        store.path_for("../outside")
    except ValueError as exc:
        assert "run_id" in str(exc)
    else:
        raise AssertionError("path traversal run_id must be rejected")


def test_failed_tool_call_requires_error() -> None:
    try:
        build_tool_call("csv.analysis", "payload", status="failed")
    except ValueError as exc:
        assert "错误" in str(exc)
    else:
        raise AssertionError("failed tool call without error must be rejected")


def test_current_snapshot_requires_query_plan_and_task_event() -> None:
    question = ResearchQuestion(id="rq-required", text="候选药物是否有效？")
    query_plan = generate_search_queries(question)
    event = build_task_event(
        "run-required",
        TaskStatus.PENDING,
        "任务已创建",
    )

    with pytest.raises(ValidationError):
        WorkbenchRunSnapshot(
            schema_version=CURRENT_SNAPSHOT_SCHEMA_VERSION,
            run_id="run-required",
            question=question,
            task_events=[event],
        )

    with pytest.raises(ValidationError):
        WorkbenchRunSnapshot(
            schema_version=CURRENT_SNAPSHOT_SCHEMA_VERSION,
            run_id="run-required",
            question=question,
            query_plan=query_plan,
            task_events=[],
        )


def test_legacy_snapshot_migrates_without_inventing_analysis_hash() -> None:
    legacy_payload = {
        "run_id": "run-legacy",
        "question": {
            "id": "rq-legacy",
            "text": "候选药物是否有效？",
        },
        "analysis": {
            "analysis_type": "fici",
            "headers": [],
            "rows": [],
            "valid": False,
            "valid_row_count": 0,
            "invalid_row_count": 0,
            "errors": ["legacy analysis"],
        },
    }

    restored = WorkbenchRunSnapshot.model_validate(legacy_payload)

    assert restored.schema_version == CURRENT_SNAPSHOT_SCHEMA_VERSION
    assert restored.query_plan.queries
    assert len(restored.task_events) == 1
    assert restored.task_events[0].actor == "migration"
    assert restored.task_events[0].metadata == {
        "from_schema_version": 1,
        "to_schema_version": CURRENT_SNAPSHOT_SCHEMA_VERSION,
    }
    assert restored.analysis is None
    assert restored.assessment is None
    assert restored.report is None
    assert "排除缺少原始输入哈希" in restored.task_events[0].message


def test_legacy_snapshot_invalidates_derived_evidence_state() -> None:
    question = ResearchQuestion(id="rq-legacy", text="候选药物是否有效？")
    query_plan = generate_search_queries(question)
    event = build_task_event(
        "run-legacy",
        TaskStatus.PENDING,
        "任务已创建",
    )
    legacy_payload = {
        "run_id": "run-legacy",
        "question": question.model_dump(mode="json"),
        "query_plan": query_plan.model_dump(mode="json"),
        "task_events": [event.model_dump(mode="json")],
    }

    restored = WorkbenchRunSnapshot.model_validate(legacy_payload)

    assert restored.schema_version == CURRENT_SNAPSHOT_SCHEMA_VERSION
    assert restored.task_events[0].event_id == event.event_id
    assert restored.task_events[-1].actor == "migration"
    assert "直接证据准入规则" in restored.task_events[-1].message
    assert restored.assessment is None
    assert restored.report is None


def test_schema_v3_snapshot_rebuilds_stale_direct_evidence_qualification() -> None:
    question = ResearchQuestion(
        id="rq-v3",
        text="A 与 B 对 target 是否协同？",
        population="target",
        intervention="A",
        comparator="B",
    )
    event = build_task_event("run-v3", TaskStatus.PENDING, "任务已创建")
    legacy_payload = {
        "schema_version": 3,
        "run_id": "run-v3",
        "question": question.model_dump(mode="json"),
        "query_plan": generate_search_queries(question).model_dump(mode="json"),
        "task_events": [event.model_dump(mode="json")],
        "conditions": [
            {
                "source_id": "PMID stale",
                "source_type": "pubmed",
                "title": "Checkerboard methods for A and B against target",
                "pmid": "stale",
                "source_quote": "Checkerboard testing was performed.",
                "qualification": {
                    "grade": "direct_interaction",
                    "matched_population": True,
                    "matched_intervention": True,
                    "matched_comparator": True,
                    "interaction_marker": "checkerboard",
                    "supporting_quote": "Checkerboard testing was performed.",
                    "reasons": ["旧规则错误准入"],
                },
            }
        ],
        "assessment": {"stale": True},
        "report": {"stale": True},
    }

    restored = WorkbenchRunSnapshot.model_validate(legacy_payload)

    assert restored.schema_version == CURRENT_SNAPSHOT_SCHEMA_VERSION
    assert restored.conditions == []
    assert restored.assessment is None
    assert restored.report is None
    assert restored.task_events[-1].actor == "migration"
    assert restored.task_events[-1].metadata["from_schema_version"] == 3


def test_store_datetimes_must_be_timezone_aware() -> None:
    naive = datetime(2026, 7, 29, 10, 0)
    with pytest.raises(ValidationError, match="工具调用时间必须包含时区"):
        ToolCallRecord(
            call_id="call-naive",
            tool_name="pubmed.search",
            status="running",
            input_summary="query",
            started_at=naive,
        )

    question = ResearchQuestion(id="rq-naive", text="候选药物是否有效？")
    with pytest.raises(ValidationError, match="快照更新时间必须包含时区"):
        WorkbenchRunSnapshot(
            run_id="run-naive",
            question=question,
            query_plan=generate_search_queries(question),
            task_events=[
                build_task_event(
                    "run-naive",
                    TaskStatus.PENDING,
                    "任务已创建",
                )
            ],
            updated_at=naive,
        )
