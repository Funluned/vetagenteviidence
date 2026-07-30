"""Local, JSON-based persistence for auditable workbench runs."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from vetevidence.answering import build_cited_answer
from vetevidence.experiment_analysis import ExperimentAnalysisResult
from vetevidence.literature_import import LiteratureImportResult
from vetevidence.mechanism_prediction import MechanismPredictionBundle
from vetevidence.models import ResearchResult
from vetevidence.workbench import (
    LiteratureEvidenceGrade,
    ResearchDecisionReport,
    ResearchQuestion,
    TaskEvent,
    TaskStatus,
    TestableHypothesis,
    build_task_event,
)
from vetevidence.workbench_pipeline import (
    EvidenceAssessment,
    ExperimentCondition,
    QueryPlan,
    build_experiment_conditions,
    generate_search_queries,
)


CURRENT_SNAPSHOT_SCHEMA_VERSION = 6


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rebuild_legacy_research_answer(
    research: ResearchResult,
    conditions: list[ExperimentCondition],
    question: ResearchQuestion,
) -> ResearchResult:
    admitted_pmids = {
        condition.pmid
        for condition in conditions
        if condition.pmid
        and condition.qualification.grade
        in {
            LiteratureEvidenceGrade.DIRECT_INTERACTION,
            LiteratureEvidenceGrade.CONTEXTUAL,
        }
    }
    answer_evidence = [
        record
        for record in research.evidence
        if record.pmid in admitted_pmids
    ]
    return research.model_copy(
        update={
            "answer": build_cited_answer(
                question.text,
                answer_evidence,
            )
        }
    )


class StoreModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ToolCallRecord(StoreModel):
    call_id: str
    tool_name: str
    status: Literal["running", "succeeded", "failed"]
    input_summary: str
    output_summary: str | None = None
    error: str | None = None
    retry_of: str | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("工具调用时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def validate_completion(self) -> ToolCallRecord:
        if self.status == "running" and self.finished_at is not None:
            raise ValueError("运行中的工具调用不能包含完成时间。")
        if self.status != "running" and self.finished_at is None:
            raise ValueError("已结束的工具调用必须包含完成时间。")
        if self.status == "failed" and not self.error:
            raise ValueError("失败的工具调用必须记录错误。")
        return self


class WorkbenchRunSnapshot(StoreModel):
    schema_version: Literal[CURRENT_SNAPSHOT_SCHEMA_VERSION] = (
        CURRENT_SNAPSHOT_SCHEMA_VERSION
    )
    run_id: str
    question: ResearchQuestion
    query_plan: QueryPlan
    hypotheses: list[TestableHypothesis] = Field(default_factory=list)
    task_events: list[TaskEvent] = Field(min_length=1)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    research: ResearchResult | None = None
    literature_import: LiteratureImportResult | None = None
    conditions: list[ExperimentCondition] = Field(default_factory=list)
    assessment: EvidenceAssessment | None = None
    analysis: ExperimentAnalysisResult | None = None
    mechanism_prediction: MechanismPredictionBundle = Field(
        default_factory=MechanismPredictionBundle
    )
    report: ResearchDecisionReport | None = None
    updated_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_snapshot(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        payload = dict(value)
        raw_version = payload.get("schema_version", 1)
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("快照 schema_version 必须是整数。") from exc
        if version > CURRENT_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                "快照版本高于当前程序支持的版本，不能安全恢复。"
            )
        if version == CURRENT_SNAPSHOT_SCHEMA_VERSION:
            return payload

        migration_notes: list[str] = []
        question = ResearchQuestion.model_validate(payload.get("question"))
        if not payload.get("query_plan"):
            payload["query_plan"] = generate_search_queries(question).model_dump(
                mode="python"
            )
            migration_notes.append("补建检索计划")

        analysis = payload.get("analysis")
        if isinstance(analysis, dict) and not analysis.get("input_sha256"):
            payload["analysis"] = None
            payload["assessment"] = None
            payload["report"] = None
            migration_notes.append("排除缺少原始输入哈希的旧分析和派生报告")
        elif version < 5 and analysis is not None:
            payload["analysis"] = None
            payload["assessment"] = None
            payload["report"] = None
            migration_notes.append(
                "排除缺少科研问题范围身份的旧实验分析和派生报告"
            )

        if version < 4:
            research_payload = payload.get("research")
            import_payload = payload.get("literature_import")
            research = (
                ResearchResult.model_validate(research_payload)
                if research_payload is not None
                else None
            )
            imported = (
                LiteratureImportResult.model_validate(import_payload)
                if import_payload is not None
                else None
            )
            conditions = build_experiment_conditions(
                research,
                imported,
                question=question,
            )
            payload["conditions"] = [
                condition.model_dump(mode="python")
                for condition in conditions
            ]
            if research is not None:
                payload["research"] = _rebuild_legacy_research_answer(
                    research,
                    conditions,
                    question,
                ).model_dump(mode="python")
            payload["assessment"] = None
            payload["report"] = None
            migration_notes.append(
                "按直接证据准入规则重建文献条件和检索答案，"
                "并使旧评估和报告安全失效"
            )
        if version == 4:
            research_payload = payload.get("research")
            import_payload = payload.get("literature_import")
            research = (
                ResearchResult.model_validate(research_payload)
                if research_payload is not None
                else None
            )
            imported = (
                LiteratureImportResult.model_validate(import_payload)
                if import_payload is not None
                else None
            )
            conditions = build_experiment_conditions(
                research,
                imported,
                question=question,
            )
            payload["conditions"] = [
                condition.model_dump(mode="python")
                for condition in conditions
            ]
            if research is not None:
                payload["research"] = _rebuild_legacy_research_answer(
                    research,
                    conditions,
                    question,
                ).model_dump(mode="python")
            payload["analysis"] = None
            payload["assessment"] = None
            payload["report"] = None
            migration_notes.append(
                "按交互结局 v2 与实验范围规则重建证据和检索答案，"
                "并排除缺少范围身份的旧分析"
            )
        if version < 5:
            payload.setdefault("mechanism_prediction", {})
            migration_notes.append("新增独立的网络药理学与分子对接预测层")
        if version < 6:
            had_report = payload.get("report") is not None
            payload["report"] = None
            migration_notes.append(
                "升级机制预测的通路名称与本机 Vina 执行审计字段"
                + (
                    "，并使旧决策报告和人工复核安全失效"
                    if had_report
                    else ""
                )
            )

        events = list(payload.get("task_events") or [])
        if not events:
            migration_notes.append("补建任务事件")
        if migration_notes:
            run_id = payload.get("run_id")
            if run_id:
                events.append(
                    build_task_event(
                        str(run_id),
                        TaskStatus.PENDING,
                        "旧版快照已安全迁移：" + "；".join(migration_notes) + "。",
                        actor="migration",
                        metadata={
                            "from_schema_version": version,
                            "to_schema_version": (
                                CURRENT_SNAPSHOT_SCHEMA_VERSION
                            ),
                        },
                    )
                )
        payload["task_events"] = events
        payload["schema_version"] = CURRENT_SNAPSHOT_SCHEMA_VERSION
        return payload

    @field_validator("updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("快照更新时间必须包含时区。")
        return value


def build_tool_call(
    tool_name: str,
    input_summary: str,
    *,
    status: Literal["running", "succeeded", "failed"] = "running",
    output_summary: str | None = None,
    error: str | None = None,
    retry_of: str | None = None,
    call_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolCallRecord:
    return ToolCallRecord(
        call_id=call_id or f"call-{uuid4()}",
        tool_name=tool_name,
        status=status,
        input_summary=input_summary,
        output_summary=output_summary,
        error=error,
        retry_of=retry_of,
        finished_at=None if status == "running" else _utc_now(),
        metadata=dict(metadata or {}),
    )


class RunStore:
    """Persist one JSON file per run using an atomic replace."""

    def __init__(self, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = root or project_root / ".workbench" / "runs"

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise ValueError("run_id 只能包含字母、数字、点、下划线和连字符。")
        return run_id

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{self._safe_run_id(run_id)}.json"

    def save(self, snapshot: WorkbenchRunSnapshot) -> Path:
        target = self.path_for(snapshot.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = snapshot.model_copy(update={"updated_at": _utc_now()})
        temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            for attempt in range(5):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    # Windows file watchers and antivirus scanners can briefly
                    # hold a newly written JSON file without delete sharing.
                    time.sleep(0.05 * (2**attempt))
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return target

    def load(self, run_id: str) -> WorkbenchRunSnapshot:
        target = self.path_for(run_id)
        return WorkbenchRunSnapshot.model_validate_json(
            target.read_text(encoding="utf-8")
        )

    def list_run_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            (path.stem for path in self.root.glob("*.json")),
            reverse=True,
        )
