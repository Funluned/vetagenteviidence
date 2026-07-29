from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from vetevidence.workbench import (
    ConclusionConfidence,
    EvidenceConflict,
    EvidenceGap,
    EvidenceReference,
    EvidenceSourceType,
    HumanReview,
    HypothesisKind,
    ResearchDecisionReport,
    ResearchQuestion,
    ReviewDecision,
    TaskStatus,
    TaskStatusSummary,
    TraceableConclusion,
    build_task_event,
    decompose_research_question,
    summarize_task_status,
)


def _reference(
    *,
    pmid: str = "12345678",
    doi: str = "10.1000/example",
    source_quote: str = "The combination reduced bacterial growth.",
) -> EvidenceReference:
    return EvidenceReference(
        pmid=pmid,
        doi=doi,
        source_quote=source_quote,
        source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def _conclusion(
    conclusion_id: str,
    statement: str,
    *,
    reference: EvidenceReference | None = None,
) -> TraceableConclusion:
    return TraceableConclusion(
        id=conclusion_id,
        statement=statement,
        confidence=ConclusionConfidence.MODERATE,
        evidence=[reference or _reference()],
    )


def test_evidence_reference_requires_a_traceable_field() -> None:
    with pytest.raises(ValidationError, match="文献引用至少需要"):
        EvidenceReference()

    assert EvidenceReference(pmid="42").pmid == "42"
    assert EvidenceReference(doi="10.1/test").doi == "10.1/test"
    assert EvidenceReference(source_quote="reported result").source_quote
    assert (
        EvidenceReference(source_url="https://example.test/article").source_url
        == "https://example.test/article"
    )

    with pytest.raises(ValidationError, match="任意来源 ID"):
        EvidenceReference(source_id="made-up-source")
    with pytest.raises(ValidationError):
        EvidenceReference(
            source_type="untrusted_source",
            source_id="made-up-source",
        )

    legacy = EvidenceReference.model_validate(
        {"pmid": "42", "source_type": None}
    )
    assert legacy.source_type is EvidenceSourceType.LITERATURE


def test_experiment_csv_reference_requires_reproducible_manifest() -> None:
    with pytest.raises(ValidationError, match="实验 CSV 引用"):
        EvidenceReference(
            source_id="sha256:abc",
            source_type="experiment_csv",
        )

    with pytest.raises(ValidationError):
        EvidenceReference(
            source_id="sha256:" + "g" * 64,
            source_type="experiment_csv",
            input_sha256="g" * 64,
            data_rows=[2],
            calculation="FICI=0.5",
        )

    with pytest.raises(ValidationError, match="严格等于"):
        EvidenceReference(
            source_id="sha256:" + "b" * 64,
            source_type="experiment_csv",
            input_sha256="a" * 64,
            data_rows=[2],
            calculation="FICI=0.5",
        )

    reference = EvidenceReference(
        source_id="sha256:" + "a" * 64,
        source_type="experiment_csv",
        source_name="checkerboard.csv",
        input_sha256="a" * 64,
        data_rows=[2],
        calculation="(1/4) + (2/8) = FICI=0.5",
    )
    assert reference.data_rows == [2]
    assert reference.source_quote is None
    assert reference.source_type is EvidenceSourceType.EXPERIMENT_CSV


def test_workbench_datetimes_must_be_timezone_aware() -> None:
    naive = datetime(2026, 7, 29, 10, 0)

    with pytest.raises(ValidationError, match="人工复核时间必须包含时区"):
        HumanReview(id="review-naive", requested_at=naive)

    with pytest.raises(ValidationError, match="任务状态时间必须包含时区"):
        TaskStatusSummary(
            task_id="task-1",
            current_status=TaskStatus.RUNNING,
            event_count=1,
            started_at=naive,
            updated_at=naive,
            latest_message="运行中",
            event_ids=["event-1"],
        )

    question = ResearchQuestion(id="RQ-TIME", text="候选药物是否有效？")
    event = build_task_event(
        "task-1",
        TaskStatus.AWAITING_REVIEW,
        "等待复核",
    )
    with pytest.raises(ValidationError, match="报告生成时间必须包含时区"):
        ResearchDecisionReport(
            id="report-naive",
            question=question,
            hypotheses=decompose_research_question(question),
            conclusions=[_conclusion("C-TIME", "当前证据支持继续验证。")],
            recommendation=_conclusion("R-TIME", "建议开展独立重复验证。"),
            task_status=summarize_task_status([event]),
            human_review=HumanReview(id="review-time"),
            generated_at=naive,
        )


def test_generated_conclusion_cannot_drop_its_evidence() -> None:
    with pytest.raises(ValidationError):
        TraceableConclusion(
            id="C1",
            statement="联合处理表现出协同效应。",
            confidence=ConclusionConfidence.HIGH,
            evidence=[],
        )


def test_generic_question_decomposes_into_two_testable_hypotheses() -> None:
    question = ResearchQuestion(
        id="RQ1",
        text="候选药物是否抑制病原菌生长？",
        intervention="候选药物",
        comparator="载体对照",
        outcomes=["菌落形成单位"],
    )

    hypotheses = decompose_research_question(question)

    assert [item.kind for item in hypotheses] == [
        HypothesisKind.PRIMARY_EFFECT,
        HypothesisKind.ROBUSTNESS,
    ]
    assert [item.id for item in hypotheses] == ["RQ1-H1", "RQ1-H2"]
    assert all(item.verification_method for item in hypotheses)
    assert all(item.success_criteria for item in hypotheses)
    assert hypotheses[0].dependent_variables == ["菌落形成单位"]
    assert hypotheses[0].rule_id == "primary-effect-v1"


def test_keyword_rules_are_deterministic_and_capped_at_four() -> None:
    question = ResearchQuestion(
        id="RQ-SYNERGY",
        text="联合用药是否协同，并呈剂量反应和作用机制变化？",
        intervention="候选药物与抗生素",
        comparator="单药组",
        outcomes=["细菌生长"],
    )

    first = decompose_research_question(question)
    second = decompose_research_question(question)

    assert first == second
    assert len(first) == 4
    assert [item.kind for item in first] == [
        HypothesisKind.PRIMARY_EFFECT,
        HypothesisKind.INTERACTION,
        HypothesisKind.DOSE_OR_TIME_RESPONSE,
        HypothesisKind.MECHANISM,
    ]
    assert "FICI" in first[1].verification_method
    assert "预先指定" in first[1].success_criteria


def test_string_question_gets_stable_id_and_limit_is_validated() -> None:
    first = decompose_research_question("Does treatment improve recovery?")
    second = decompose_research_question("Does treatment improve recovery?")

    assert first[0].research_question_id == second[0].research_question_id
    assert first[0].research_question_id.startswith("rq-")
    with pytest.raises(ValueError, match="2 到 4"):
        decompose_research_question("valid question", max_hypotheses=1)


def test_task_event_builder_infers_type_and_copies_metadata() -> None:
    timestamp = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    metadata = {"tool": "pubmed"}

    event = build_task_event(
        "task-1",
        TaskStatus.AWAITING_REVIEW,
        "等待人工复核",
        actor="workflow",
        occurred_at=timestamp,
        event_id="event-1",
        metadata=metadata,
    )
    metadata["tool"] = "changed"

    assert event.event_type.value == "review_requested"
    assert event.occurred_at == timestamp
    assert event.metadata == {"tool": "pubmed"}


def test_task_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="必须包含时区"):
        build_task_event(
            "task-1",
            TaskStatus.RUNNING,
            "开始",
            occurred_at=datetime(2026, 7, 29, 9, 0),
        )


def test_status_summary_uses_chronology_and_preserves_failures() -> None:
    start = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    created = build_task_event(
        "task-1",
        TaskStatus.PENDING,
        "已创建",
        occurred_at=start,
        event_id="event-created",
    )
    failed = build_task_event(
        "task-1",
        TaskStatus.FAILED,
        "PubMed 暂时不可用",
        occurred_at=start + timedelta(minutes=1),
        event_id="event-failed",
    )
    retried = build_task_event(
        "task-1",
        TaskStatus.RUNNING,
        "人工确认后重试",
        occurred_at=start + timedelta(minutes=2),
        event_id="event-retried",
    )

    summary = summarize_task_status([retried, created, failed])

    assert summary.current_status is TaskStatus.RUNNING
    assert summary.event_count == 3
    assert summary.started_at == start
    assert summary.updated_at == start + timedelta(minutes=2)
    assert summary.event_ids == [
        "event-created",
        "event-failed",
        "event-retried",
    ]
    assert summary.failure_messages == ["PubMed 暂时不可用"]


def test_status_summary_requires_task_id_for_mixed_events() -> None:
    first = build_task_event("task-1", TaskStatus.PENDING, "one")
    second = build_task_event("task-2", TaskStatus.PENDING, "two")

    with pytest.raises(ValueError, match="多个任务"):
        summarize_task_status([first, second])

    assert (
        summarize_task_status([first, second], task_id="task-2").task_id
        == "task-2"
    )


def test_completed_human_review_requires_reviewer_and_timestamp() -> None:
    with pytest.raises(ValidationError, match="复核人和复核时间"):
        HumanReview(id="review-1", decision=ReviewDecision.APPROVED)

    review = HumanReview(
        id="review-1",
        decision=ReviewDecision.APPROVED,
        reviewer="researcher-a",
        reviewed_at=datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
        comments=["核对了来源原句。"],
    )
    assert review.decision is ReviewDecision.APPROVED


def test_conflict_requires_two_traceable_claims_and_resolution_basis() -> None:
    claim = _conclusion("C1", "研究一报告有效。")

    with pytest.raises(ValidationError):
        EvidenceConflict(
            id="conflict-1",
            topic="主要结局",
            description="研究结果不一致。",
            claims=[claim],
            impact="无法确定效应方向。",
        )

    with pytest.raises(ValidationError, match="解决依据"):
        EvidenceConflict(
            id="conflict-1",
            topic="主要结局",
            description="研究结果不一致。",
            claims=[claim, _conclusion("C2", "研究二报告无效。")],
            impact="无法确定效应方向。",
            resolution_status="resolved",
        )


def test_decision_report_round_trip_preserves_full_audit_and_citations() -> None:
    question = ResearchQuestion(
        id="RQ1",
        text="候选药物与抗生素联合是否协同？",
        intervention="候选药物与抗生素",
        comparator="单药组",
        outcomes=["FICI"],
    )
    hypotheses = decompose_research_question(question)
    start = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    events = [
        build_task_event(
            "task-1",
            TaskStatus.PENDING,
            "任务已创建",
            occurred_at=start,
            event_id="event-1",
        ),
        build_task_event(
            "task-1",
            TaskStatus.AWAITING_REVIEW,
            "报告等待复核",
            occurred_at=start + timedelta(minutes=5),
            event_id="event-2",
        ),
    ]
    conclusion = _conclusion("C1", "现有体外证据支持联合处理具有协同作用。")
    report = ResearchDecisionReport(
        id="report-1",
        question=question,
        hypotheses=hypotheses,
        conclusions=[conclusion],
        recommendation=_conclusion(
            "R1",
            "进入独立重复验证，但暂不外推到临床疗效。",
        ),
        evidence_gaps=[
            EvidenceGap(
                id="gap-1",
                topic="外部有效性",
                missing_evidence="缺少动物模型和临床样本验证。",
                impact="不能外推临床疗效。",
                recommended_action="完成动物模型和独立临床样本验证。",
                related_evidence=[_reference()],
            )
        ],
        task_status=summarize_task_status(events),
        human_review=HumanReview(id="review-1"),
    )

    restored = ResearchDecisionReport.model_validate(
        report.model_dump(mode="json")
    )

    reference = restored.conclusions[0].evidence[0]
    assert reference.pmid == "12345678"
    assert reference.doi == "10.1000/example"
    assert reference.source_quote == (
        "The combination reduced bacterial growth."
    )
    assert restored.task_status.current_status is TaskStatus.AWAITING_REVIEW
    assert restored.human_review.decision is ReviewDecision.PENDING
    assert "不提供医疗或兽医诊断建议" in restored.disclaimer


def test_old_review_cannot_approve_new_report() -> None:
    question = ResearchQuestion(id="RQ1", text="候选药物是否有效？")
    hypotheses = decompose_research_question(question)
    generated_at = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    event = build_task_event(
        "task-1",
        TaskStatus.COMPLETED,
        "完成",
        occurred_at=generated_at,
    )
    old_review = HumanReview(
        id="review-old",
        decision=ReviewDecision.APPROVED,
        reviewer="researcher",
        reviewed_at=generated_at - timedelta(minutes=1),
    )

    with pytest.raises(ValidationError, match="不能早于报告生成时间"):
        ResearchDecisionReport(
            id="report-new",
            question=question,
            hypotheses=hypotheses,
            conclusions=[_conclusion("C1", "当前有效结论")],
            recommendation=_conclusion("R1", "建议继续验证"),
            task_status=summarize_task_status([event]),
            human_review=old_review,
            generated_at=generated_at,
        )
