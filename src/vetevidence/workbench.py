"""Auditable domain models and transparent rules for the research workbench."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkbenchModel(BaseModel):
    """Shared validation behavior for workbench domain models."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)


class EvidenceSourceType(StrEnum):
    LITERATURE = "literature"
    EXPERIMENT_CSV = "experiment_csv"


class EvidenceReference(WorkbenchModel):
    """A verifiable pointer supporting a generated statement."""

    pmid: str | None = None
    doi: str | None = None
    source_quote: str | None = None
    source_url: str | None = None
    source_id: str | None = None
    source_type: EvidenceSourceType = EvidenceSourceType.LITERATURE
    source_name: str | None = None
    input_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    data_rows: list[int] = Field(default_factory=list)
    calculation: str | None = None

    @field_validator("source_type", mode="before")
    @classmethod
    def default_legacy_source_type(
        cls,
        value: EvidenceSourceType | str | None,
    ) -> EvidenceSourceType | str:
        return EvidenceSourceType.LITERATURE if value is None else value

    @field_validator("input_sha256", mode="before")
    @classmethod
    def normalize_sha256(cls, value: str | None) -> str | None:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_traceable_field(self) -> EvidenceReference:
        if self.source_type is EvidenceSourceType.LITERATURE:
            if any((self.pmid, self.doi, self.source_quote, self.source_url)):
                return self
            raise ValueError(
                "文献引用至少需要 PMID、DOI、来源片段或来源 URL 中的一项；"
                "任意来源 ID 不能单独作为文献证据。"
            )

        if not all(
            (self.source_id, self.input_sha256, self.data_rows, self.calculation)
        ):
            raise ValueError(
                "实验 CSV 引用必须记录来源 ID、SHA-256、数据行和计算过程。"
            )
        expected_source_id = f"sha256:{self.input_sha256}"
        if self.source_id != expected_source_id:
            raise ValueError(
                "实验 CSV 来源 ID 必须严格等于 sha256:<input_sha256>。"
            )
        if any(row < 1 for row in self.data_rows):
            raise ValueError("实验 CSV 数据行必须是正整数。")
        return self


class LiteratureEvidenceGrade(StrEnum):
    """Question-specific admission grade for one literature source."""

    UNASSESSED = "unassessed"
    OUT_OF_SCOPE = "out_of_scope"
    CONTEXTUAL = "contextual"
    DIRECT_INTERACTION = "direct_interaction"


class EvidenceQualification(WorkbenchModel):
    """Transparent rule output deciding whether literature can answer the question."""

    grade: LiteratureEvidenceGrade = LiteratureEvidenceGrade.UNASSESSED
    rule_id: str = "interaction-evidence-v1"
    matched_population: bool = False
    matched_intervention: bool = False
    matched_comparator: bool = False
    interaction_marker: str | None = None
    interaction_result_signal: str | None = None
    supporting_quote: str | None = None
    reasons: list[str] = Field(default_factory=lambda: ["尚未按科研问题评估。"])

    @model_validator(mode="after")
    def direct_evidence_requires_all_explicit_matches(self) -> EvidenceQualification:
        if self.grade is LiteratureEvidenceGrade.DIRECT_INTERACTION and not (
            self.matched_population
            and self.matched_intervention
            and self.matched_comparator
            and self.interaction_marker
            and self.interaction_result_signal
            and self.supporting_quote
        ):
            raise ValueError(
                "直接交互证据必须同时命中研究对象、两种干预、明确交互结果"
                "和可引用原句。"
            )
        return self


class EvidenceAdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    BLOCKED_NO_DIRECT_EVIDENCE = "blocked_no_direct_evidence"


class EvidenceAdmission(WorkbenchModel):
    """Report-level audit of which literature sources may support the answer."""

    status: EvidenceAdmissionStatus = (
        EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
    )
    rule_id: str = "interaction-evidence-v1"
    direct_source_ids: list[str] = Field(default_factory=list)
    contextual_source_ids: list[str] = Field(default_factory=list)
    excluded_source_ids: list[str] = Field(default_factory=list)
    reason: str = "尚无经过直接文献证据规则准入的来源。"

    @model_validator(mode="after")
    def admitted_reports_require_direct_sources(self) -> EvidenceAdmission:
        if (
            self.status is EvidenceAdmissionStatus.ADMITTED
            and not self.direct_source_ids
        ):
            raise ValueError("准入状态为 admitted 时必须至少记录一个直接证据来源。")
        if (
            self.status is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
            and self.direct_source_ids
        ):
            raise ValueError("证据不足状态不能同时包含直接证据来源。")
        return self


class ResearchQuestion(WorkbenchModel):
    """A scoped research question that can be decomposed into hypotheses."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=3)
    population: str | None = None
    intervention: str | None = None
    comparator: str | None = None
    outcomes: list[str] = Field(default_factory=list)
    context: str | None = None


class HypothesisKind(StrEnum):
    PRIMARY_EFFECT = "primary_effect"
    INTERACTION = "interaction"
    DOSE_OR_TIME_RESPONSE = "dose_or_time_response"
    MECHANISM = "mechanism"
    SAFETY = "safety"
    ROBUSTNESS = "robustness"


class TestableHypothesis(WorkbenchModel):
    """A hypothesis with an explicit verification method and pass criterion."""

    id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    kind: HypothesisKind
    statement: str = Field(min_length=3)
    independent_variables: list[str] = Field(min_length=1)
    dependent_variables: list[str] = Field(min_length=1)
    verification_method: str = Field(min_length=3)
    success_criteria: str = Field(min_length=3)
    rule_id: str = Field(
        min_length=1,
        description="The transparent rule that generated this hypothesis.",
    )
    supporting_evidence: list[EvidenceReference] = Field(default_factory=list)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskEventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    REVIEW_REQUESTED = "review_requested"
    FAILED = "failed"


class TaskEvent(WorkbenchModel):
    """An append-only audit event for one workbench task."""

    event_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    event_type: TaskEventType
    status: TaskStatus
    message: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("任务事件时间必须包含时区。")
        return value


class TaskStatusSummary(WorkbenchModel):
    """Current state derived only from an append-only event sequence."""

    task_id: str
    current_status: TaskStatus
    event_count: int = Field(ge=1)
    started_at: datetime
    updated_at: datetime
    latest_message: str
    event_ids: list[str] = Field(min_length=1)
    failure_messages: list[str] = Field(default_factory=list)

    @field_validator("started_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("任务状态时间必须包含时区。")
        return value


class ReviewDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class HumanReview(WorkbenchModel):
    """The explicit human checkpoint for a generated research report."""

    id: str = Field(min_length=1)
    decision: ReviewDecision = ReviewDecision.PENDING
    reviewer: str | None = None
    comments: list[str] = Field(default_factory=list)
    requested_at: datetime = Field(default_factory=_utc_now)
    reviewed_at: datetime | None = None

    @field_validator("requested_at", "reviewed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("人工复核时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def validate_review_completion(self) -> HumanReview:
        completed = self.decision is not ReviewDecision.PENDING
        if completed and (not self.reviewer or self.reviewed_at is None):
            raise ValueError(
                "已完成的人工复核必须记录复核人和复核时间。"
            )
        if not completed and self.reviewed_at is not None:
            raise ValueError("待复核记录不能包含复核完成时间。")
        return self


class ConclusionConfidence(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class TraceableConclusion(WorkbenchModel):
    """A generated conclusion that cannot exist without traceable evidence."""

    id: str = Field(min_length=1)
    statement: str = Field(min_length=3)
    confidence: ConclusionConfidence
    evidence: list[EvidenceReference] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class ConflictResolutionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class EvidenceConflict(WorkbenchModel):
    """Two or more evidence-backed claims that disagree on one topic."""

    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    description: str = Field(min_length=3)
    claims: list[TraceableConclusion] = Field(min_length=2)
    impact: str = Field(min_length=3)
    resolution_status: ConflictResolutionStatus = ConflictResolutionStatus.OPEN
    resolution: str | None = None

    @model_validator(mode="after")
    def require_resolution_when_resolved(self) -> EvidenceConflict:
        if (
            self.resolution_status is ConflictResolutionStatus.RESOLVED
            and not self.resolution
        ):
            raise ValueError("已解决的证据冲突必须记录解决依据。")
        return self


class EvidenceGap(WorkbenchModel):
    """A missing piece of evidence and its decision impact."""

    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    missing_evidence: str = Field(min_length=3)
    impact: str = Field(min_length=3)
    recommended_action: str = Field(min_length=3)
    related_evidence: list[EvidenceReference] = Field(default_factory=list)


class ResearchDecisionReport(WorkbenchModel):
    """A reviewable decision report linking claims, gaps, conflicts and audit."""

    id: str = Field(min_length=1)
    question: ResearchQuestion
    hypotheses: list[TestableHypothesis] = Field(min_length=2, max_length=4)
    conclusions: list[TraceableConclusion] = Field(min_length=1)
    recommendation: TraceableConclusion
    evidence_admission: EvidenceAdmission = Field(default_factory=EvidenceAdmission)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    task_status: TaskStatusSummary
    human_review: HumanReview
    generated_at: datetime = Field(default_factory=_utc_now)
    disclaimer: str = (
        "本报告仅用于科研证据整理，不提供医疗或兽医诊断建议。"
    )

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("报告生成时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def review_must_follow_generation(self) -> ResearchDecisionReport:
        reviewed_at = self.human_review.reviewed_at
        if reviewed_at is not None and reviewed_at < self.generated_at:
            raise ValueError("人工复核时间不能早于报告生成时间。")
        return self


_INTERACTION_TERMS = (
    "协同",
    "联合",
    "棋盘",
    "combination",
    "synerg",
    "checkerboard",
    "fici",
)
_DOSE_OR_TIME_TERMS = (
    "剂量",
    "浓度",
    "时间",
    "时程",
    "dose",
    "concentration",
    "time",
    "duration",
)
_MECHANISM_TERMS = ("机制", "通路", "靶点", "mechanism", "pathway", "target")
_SAFETY_TERMS = (
    "安全",
    "毒性",
    "不良",
    "safety",
    "toxicity",
    "adverse",
)


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def _coerce_question(question: ResearchQuestion | str) -> ResearchQuestion:
    if isinstance(question, ResearchQuestion):
        return question
    text = question.strip()
    if not text:
        raise ValueError("科研问题不能为空。")
    digest = sha256(text.encode("utf-8")).hexdigest()[:12]
    return ResearchQuestion(id=f"rq-{digest}", text=text)


def decompose_research_question(
    question: ResearchQuestion | str,
    *,
    max_hypotheses: int = 4,
) -> list[TestableHypothesis]:
    """Split a question into 2-4 hypotheses using inspectable keyword rules.

    The function is deterministic and does not call a language model. The
    ``rule_id`` on every output records which rule produced it.
    """

    if not 2 <= max_hypotheses <= 4:
        raise ValueError("max_hypotheses 必须在 2 到 4 之间。")

    scoped_question = _coerce_question(question)
    normalized = scoped_question.text.casefold()
    intervention = scoped_question.intervention or "研究干预"
    comparator = scoped_question.comparator or "预先定义的对照"
    outcomes = scoped_question.outcomes or ["主要结局"]
    topic = scoped_question.text

    specifications: list[dict[str, Any]] = [
        {
            "kind": HypothesisKind.PRIMARY_EFFECT,
            "statement": (
                f"与{comparator}相比，{intervention}会使“{topic}”对应的"
                "主要结局产生可测量变化。"
            ),
            "independent_variables": [intervention, comparator],
            "dependent_variables": outcomes,
            "verification_method": (
                "设置干预组与对照组，盲法测量预先指定的主要结局并比较效应量。"
            ),
            "success_criteria": (
                "完整报告效应方向、效应量和不确定性区间，并达到预先登记的"
                "判定阈值。"
            ),
            "rule_id": "primary-effect-v1",
        }
    ]

    if _contains_any(normalized, _INTERACTION_TERMS):
        specifications.append(
            {
                "kind": HypothesisKind.INTERACTION,
                "statement": (
                    f"{intervention}的联合效应不同于各单一处理效应，且可用"
                    "预先指定的相互作用指标量化。"
                ),
                "independent_variables": ["联合处理", "各单一处理", comparator],
                "dependent_variables": ["FICI 或等价的预设相互作用指标"],
                "verification_method": (
                    "使用棋盘稀释或等价的析因设计，同时测量联合处理和各"
                    "单一处理，并计算 FICI 或预先指定的相互作用指标。"
                ),
                "success_criteria": (
                    "按研究开始前预先指定并锁定的阈值解释相互作用，并报告"
                    "原始测量值、重复数及不确定性。"
                ),
                "rule_id": "interaction-keywords-v1",
            }
        )

    if _contains_any(normalized, _DOSE_OR_TIME_TERMS):
        specifications.append(
            {
                "kind": HypothesisKind.DOSE_OR_TIME_RESPONSE,
                "statement": (
                    f"{intervention}的效应随剂量、浓度或暴露时间呈可量化变化。"
                ),
                "independent_variables": ["剂量或浓度", "暴露时间"],
                "dependent_variables": outcomes,
                "verification_method": (
                    "至少设置三个剂量、浓度或时间水平，拟合预先指定的"
                    "剂量-反应或时间-反应模型。"
                ),
                "success_criteria": (
                    "报告各水平原始结果、模型参数及不确定性，并达到预先"
                    "登记的趋势或模型拟合标准。"
                ),
                "rule_id": "dose-time-keywords-v1",
            }
        )

    if _contains_any(normalized, _MECHANISM_TERMS):
        specifications.append(
            {
                "kind": HypothesisKind.MECHANISM,
                "statement": (
                    f"{intervention}对主要结局的影响伴随预先指定机制指标的"
                    "可重复变化。"
                ),
                "independent_variables": [intervention, "机制阻断或阴性对照"],
                "dependent_variables": ["预先指定的机制指标", *outcomes],
                "verification_method": (
                    "同步测量主要结局和机制指标，并通过阻断、救援或等价"
                    "对照区分相关性与因果支持。"
                ),
                "success_criteria": (
                    "机制指标与主要结局的方向符合预先登记的因果预测，且"
                    "阻断或救援对照产生预期变化。"
                ),
                "rule_id": "mechanism-keywords-v1",
            }
        )

    if _contains_any(normalized, _SAFETY_TERMS):
        specifications.append(
            {
                "kind": HypothesisKind.SAFETY,
                "statement": (
                    f"在有效暴露范围内，{intervention}不会使预先指定的安全性"
                    "指标超过可接受阈值。"
                ),
                "independent_variables": [intervention, comparator],
                "dependent_variables": ["预先指定的安全性或毒性指标"],
                "verification_method": (
                    "在与效应实验相同的暴露范围内测量安全性指标，并设置"
                    "阴性对照及适用的阳性对照。"
                ),
                "success_criteria": (
                    "安全性指标及其不确定性不超过研究开始前锁定的阈值。"
                ),
                "rule_id": "safety-keywords-v1",
            }
        )

    specifications.append(
        {
            "kind": HypothesisKind.ROBUSTNESS,
            "statement": (
                f"{intervention}对主要结局的效应在至少两个相关实验条件或"
                "独立重复中方向一致。"
            ),
            "independent_variables": ["实验批次或相关实验条件"],
            "dependent_variables": outcomes,
            "verification_method": (
                "使用独立重复或第二个相关实验条件复现实验，并比较效应方向"
                "和效应量。"
            ),
            "success_criteria": (
                "至少两个独立重复或相关条件的效应方向一致，并完整报告"
                "异质性和不确定性。"
            ),
            "rule_id": "robustness-v1",
        }
    )

    selected = specifications[:max_hypotheses]
    if len(selected) < 2:
        raise RuntimeError("透明拆解规则未能生成至少两个假设。")

    return [
        TestableHypothesis(
            id=f"{scoped_question.id}-H{index}",
            research_question_id=scoped_question.id,
            **specification,
        )
        for index, specification in enumerate(selected, start=1)
    ]


def build_task_event(
    task_id: str,
    status: TaskStatus,
    message: str,
    *,
    actor: str = "system",
    event_type: TaskEventType | None = None,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TaskEvent:
    """Construct one audit event without mutating caller-owned metadata."""

    inferred_type = {
        TaskStatus.PENDING: TaskEventType.CREATED,
        TaskStatus.AWAITING_REVIEW: TaskEventType.REVIEW_REQUESTED,
        TaskStatus.FAILED: TaskEventType.FAILED,
    }.get(status, TaskEventType.STATUS_CHANGED)
    return TaskEvent(
        event_id=event_id or f"evt-{uuid4()}",
        task_id=task_id,
        event_type=event_type or inferred_type,
        status=status,
        message=message,
        actor=actor,
        occurred_at=occurred_at or _utc_now(),
        metadata=dict(metadata or {}),
    )


def summarize_task_status(
    events: Sequence[TaskEvent],
    *,
    task_id: str | None = None,
) -> TaskStatusSummary:
    """Derive task state from audit events; the latest timestamp wins."""

    if not events:
        raise ValueError("至少需要一个任务事件才能汇总状态。")

    available_task_ids = {event.task_id for event in events}
    selected_task_id = task_id
    if selected_task_id is None:
        if len(available_task_ids) != 1:
            raise ValueError("事件包含多个任务，请明确提供 task_id。")
        selected_task_id = next(iter(available_task_ids))

    selected = [event for event in events if event.task_id == selected_task_id]
    if not selected:
        raise ValueError(f"没有找到任务 {selected_task_id} 的事件。")

    ordered = sorted(
        enumerate(selected),
        key=lambda item: (item[1].occurred_at, item[0]),
    )
    ordered_events = [event for _, event in ordered]
    latest = ordered_events[-1]
    return TaskStatusSummary(
        task_id=selected_task_id,
        current_status=latest.status,
        event_count=len(ordered_events),
        started_at=ordered_events[0].occurred_at,
        updated_at=latest.occurred_at,
        latest_message=latest.message,
        event_ids=[event.event_id for event in ordered_events],
        failure_messages=[
            event.message
            for event in ordered_events
            if event.status is TaskStatus.FAILED
        ],
    )
