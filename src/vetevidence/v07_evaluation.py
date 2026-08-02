"""Offline, versioned evaluation for the v0.7 rule baseline.

The legacy evaluation module intentionally remains unchanged because the app
still displays its single-query v0.1 report.  This module evaluates independent
v0.7 product scenarios with frozen synthetic inputs and never opens a network
client or an LLM provider.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.experiment_analysis import (
    ExperimentAnalysisResult,
    analyze_fici_csv,
    analyze_growth_curve_csv,
)
from vetevidence.models import EvidenceRecord, PubMedArticle, ResearchResult
from vetevidence.providers import RuleBasedEvidenceProvider
from vetevidence.workbench import (
    EvidenceAdmissionStatus,
    ResearchQuestion,
    TaskStatus,
    build_task_event,
)
from vetevidence.workbench_pipeline import (
    assess_evidence,
    build_decision_report,
    build_experiment_conditions,
    experiment_analysis_matches_question,
    qualify_literature_evidence,
    run_multi_query_research,
)


V07_CATEGORIES = {
    "direct_evidence",
    "indirect_background",
    "conflicting_evidence",
    "no_evidence",
    "retrieval_not_supportive",
    "scope_mismatch",
    "citation_not_supportive",
    "prompt_injection",
    "tool_resilience",
}
V07_METRICS = {
    "retrieval_recall_at_k",
    "citation_precision",
    "unsupported_claim_rate",
    "abstention_accuracy",
    "task_completion_rate",
    "cost",
    "latency",
}
V07_EVALUATORS = {
    "retrieval_replay",
    "literature",
    "experiment",
    "citation",
    "tool_retrieval",
    "partial_analysis",
}


class V07Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class V07SourcePolicy(V07Model):
    mode: Literal["offline_frozen_replay"]
    allowed_data: str = Field(min_length=1)
    identifier_policy: str = Field(min_length=1)
    url_policy: str = Field(min_length=1)
    scientific_use: str = Field(min_length=1)
    model_policy: str = Field(min_length=1)


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk_mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_mappings(item)


class V07EvaluationCase(V07Model):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    question: ResearchQuestion
    input: dict[str, Any] = Field(min_length=1)
    applicable_metrics: list[str] = Field(min_length=1)
    data_status: Literal["synthetic_evaluation_only"]
    boundary_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_taxonomy(self) -> V07EvaluationCase:
        if self.category not in V07_CATEGORIES:
            raise ValueError(f"未知 v0.7 评测类别：{self.category}")
        if self.evaluator not in V07_EVALUATORS:
            raise ValueError(f"未知 v0.7 evaluator：{self.evaluator}")
        unknown_metrics = set(self.applicable_metrics) - V07_METRICS
        if unknown_metrics:
            raise ValueError(f"未知 v0.7 指标：{sorted(unknown_metrics)}")
        if len(self.applicable_metrics) != len(set(self.applicable_metrics)):
            raise ValueError(f"{self.id} 的 applicable_metrics 不得重复。")

        max_results = self.input.get("max_results")
        retrieval_k = self.input.get("retrieval_k")
        for field_name, value in (
            ("max_results", max_results),
            ("retrieval_k", retrieval_k),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{self.id} 的 {field_name} 必须是正整数。")

        required_fields = {
            "retrieval_replay": ("retrieval_batches",),
            "literature": ("articles",),
            "experiment": ("analysis_type", "csv_text"),
            "citation": ("records", "support_terms"),
            "tool_retrieval": ("retrieval_batches",),
            "partial_analysis": (
                "articles",
                "analysis_type",
                "csv_text",
            ),
        }
        missing = [
            field_name
            for field_name in required_fields[self.evaluator]
            if field_name not in self.input
        ]
        if missing:
            raise ValueError(f"{self.id} 缺少 evaluator 输入：{missing}")

        if self.evaluator in {"retrieval_replay", "tool_retrieval"}:
            batches = self.input["retrieval_batches"]
            if not isinstance(batches, list) or not 1 <= len(batches) <= 3:
                raise ValueError(f"{self.id} 必须包含 1 到 3 个冻结检索批次。")
            if any(
                not isinstance(batch, list)
                and not (
                    isinstance(batch, Mapping)
                    and isinstance(batch.get("error"), str)
                )
                for batch in batches
            ):
                raise ValueError(f"{self.id} 含无效冻结检索批次。")
        if self.evaluator in {"literature", "partial_analysis"} and not isinstance(
            self.input["articles"], list
        ):
            raise ValueError(f"{self.id} 的 articles 必须是数组。")
        if self.evaluator == "citation":
            records = self.input["records"]
            if not isinstance(records, list) or not records:
                raise ValueError(f"{self.id} 的 records 必须是非空数组。")
            if not isinstance(self.input["support_terms"], Mapping):
                raise ValueError(f"{self.id} 的 support_terms 必须是对象。")
        if self.evaluator in {"experiment", "partial_analysis"}:
            if self.input["analysis_type"] not in {"fici", "growth_curve"}:
                raise ValueError(f"{self.id} 含未知 analysis_type。")
            csv_text = self.input["csv_text"]
            if not isinstance(csv_text, str) or not csv_text.strip():
                raise ValueError(f"{self.id} 的 csv_text 必须是非空字符串。")

        markers = self.input.get("forbidden_markers", [])
        if not isinstance(markers, list) or any(
            not isinstance(marker, str) or not marker.strip()
            for marker in markers
        ):
            raise ValueError(f"{self.id} 的 forbidden_markers 必须是非空字符串数组。")
        return self


class V07EvaluationDataset(V07Model):
    schema_version: Literal["1.0"]
    dataset_version: Literal["v0.7.0"]
    name: str = Field(min_length=1)
    source_policy: V07SourcePolicy
    boundaries: list[str] = Field(min_length=1)
    metric_definitions: dict[str, dict[str, Any]]
    cases: list[V07EvaluationCase] = Field(min_length=20, max_length=30)

    @model_validator(mode="after")
    def validate_complete_balanced_set(self) -> V07EvaluationDataset:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("v0.7 评测用例 ID 必须唯一。")
        counts = Counter(case.category for case in self.cases)
        if set(counts) != V07_CATEGORIES:
            raise ValueError("v0.7 评测必须覆盖任务书规定的九类边界。")
        if any(count != 3 for count in counts.values()):
            raise ValueError("v0.7 当前版本固定为九类各 3 题。")
        if set(self.metric_definitions) != V07_METRICS:
            raise ValueError("v0.7 必须固定任务书规定的七项比较指标。")
        declared_metrics = {
            metric for case in self.cases for metric in case.applicable_metrics
        }
        if declared_metrics != V07_METRICS:
            raise ValueError("v0.7 七项固定指标都必须至少有一个适用场景。")
        for case in self.cases:
            for item in _walk_mappings(case.input):
                if "doi" in item:
                    raise ValueError(f"{case.id} 的合成夹具不得声明 DOI。")
                pmid = item.get("pmid")
                if pmid is not None and (
                    not isinstance(pmid, str) or not pmid.startswith("SYN-")
                ):
                    raise ValueError(f"{case.id} 的 PMID 样式标识必须使用 SYN-*。")
                source_url = item.get("source_url")
                if source_url is not None and (
                    not isinstance(source_url, str)
                    or not source_url.startswith("https://example.invalid/")
                ):
                    raise ValueError(
                        f"{case.id} 的来源 URL 必须位于 example.invalid。"
                    )
                relevant_ids = item.get("gold_relevant_ids")
                if relevant_ids is not None and (
                    not isinstance(relevant_ids, list)
                    or any(
                        not isinstance(source_id, str)
                        or not source_id.startswith("SYN-")
                        for source_id in relevant_ids
                    )
                ):
                    raise ValueError(
                        f"{case.id} 的 gold_relevant_ids 必须使用 SYN-*。"
                    )
        return self


class V07ExpectedCase(V07Model):
    id: str = Field(min_length=1)
    expected: dict[str, Any] = Field(min_length=1)
    gold_rationale: str = Field(min_length=1)


class V07ExpectedSet(V07Model):
    schema_version: Literal["1.0"]
    dataset_version: Literal["v0.7.0"]
    review_status: Literal["engineering_gold_pending_domain_expert_review"]
    cases: list[V07ExpectedCase] = Field(min_length=20, max_length=30)


class LoadedV07Evaluation(V07Model):
    dataset: V07EvaluationDataset
    expected: dict[str, V07ExpectedCase]
    review_status: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class V07CaseResult(V07Model):
    id: str
    category: str
    evaluator: str
    passed: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    error_type: str | None = None
    mismatches: list[str] = Field(default_factory=list)
    metric_observations: dict[str, dict[str, float]] = Field(
        default_factory=dict
    )
    latency_ms: float = Field(ge=0)


class V07CategorySummary(V07Model):
    total: int
    passed: int
    failed: int
    pass_rate: float


class V07Summary(V07Model):
    total: int
    passed: int
    failed: int
    evaluation_errors: int
    pass_rate: float
    macro_category_pass_rate: float
    by_category: dict[str, V07CategorySummary]


class V07MetricResult(V07Model):
    status: Literal["measured", "not_applicable"]
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    unit: str
    scope: str
    not_applicable_cases: int = 0


class V07RuleSystemProfile(V07Model):
    provider: Literal["rules_v1"] = "rules_v1"
    evidence_admission_rule: Literal["interaction-evidence-v2"] = (
        "interaction-evidence-v2"
    )
    llm_enabled: Literal[False] = False
    model_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0
    llm_api_cost_usd: Literal[0.0] = 0.0
    network_calls: Literal[0] = 0
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class V07BaselineReport(V07Model):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version: Literal["v0.7.0"] = "v0.7.0"
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_review_status: str = Field(min_length=1)
    generated_at: datetime
    system: V07RuleSystemProfile
    summary: V07Summary
    metrics: dict[str, V07MetricResult]
    results: list[V07CaseResult]
    deterministic_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundaries: list[str]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_v07_evaluation(
    cases_path: Path,
    expected_path: Path,
) -> LoadedV07Evaluation:
    dataset = V07EvaluationDataset.model_validate_json(
        cases_path.read_text(encoding="utf-8")
    )
    expected_set = V07ExpectedSet.model_validate_json(
        expected_path.read_text(encoding="utf-8")
    )
    if expected_set.dataset_version != dataset.dataset_version:
        raise ValueError("v0.7 评测输入与标准答案版本不一致。")
    expected = {item.id: item for item in expected_set.cases}
    case_ids = {case.id for case in dataset.cases}
    if set(expected) != case_ids or len(expected_set.cases) != len(expected):
        raise ValueError("v0.7 输入题与标准答案必须严格一一对应。")
    digest_payload = {
        "dataset": dataset.model_dump(mode="json"),
        "expected": expected_set.model_dump(mode="json"),
    }
    return LoadedV07Evaluation(
        dataset=dataset,
        expected=expected,
        review_status=expected_set.review_status,
        dataset_sha256=sha256(_canonical_json(digest_payload)).hexdigest(),
    )


class _FrozenPubMedClient:
    """Replay explicitly supplied batches; error sentinels exercise failures."""

    def __init__(self, batches: list[Any]) -> None:
        self._batches = list(batches)
        self.request_count = 0

    def search(self, query: str, max_results: int = 5) -> list[PubMedArticle]:
        del query
        index = self.request_count
        self.request_count += 1
        if index >= len(self._batches):
            return []
        batch = self._batches[index]
        if isinstance(batch, Mapping) and batch.get("error"):
            message = str(batch.get("message") or "frozen tool failure")
            if batch["error"] == "timeout":
                raise _FrozenToolFailure("TimeoutError", message)
            raise _FrozenToolFailure("RuntimeError", message)
        if not isinstance(batch, list):
            raise ValueError("冻结检索批次必须是文献数组或错误哨兵。")
        return [
            PubMedArticle.model_validate(article)
            for article in batch[:max_results]
        ]


class _OfflineRankingProvider:
    """The v0.7 fixtures do not score journal prestige or call LetPub."""

    def lookup_many(self, articles: list[PubMedArticle]) -> list[None]:
        return [None for _ in articles]


class _FrozenToolFailure(Exception):
    """Expected failure emitted only by the frozen evaluation client."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _answer_abstained(answer_markdown: str, citation_count: int) -> bool:
    return citation_count == 0 and "不足以回答" in answer_markdown


def _fixture_source_id(source_id: str) -> str:
    """Expose the fixture PMID instead of the report's human-readable label."""

    prefix = "PMID "
    return source_id[len(prefix) :] if source_id.startswith(prefix) else source_id


def _common_research_actual(
    question: ResearchQuestion,
    research: ResearchResult,
    *,
    forbidden_markers: list[str] | None = None,
) -> dict[str, Any]:
    conditions = build_experiment_conditions(research, question=question)
    assessment = assess_evidence(conditions, question=question)
    answer_text = research.answer.answer_markdown
    markers = forbidden_markers or []
    cited_pmids = {citation.pmid for citation in research.answer.citations}
    emitted_claims = 0
    unsupported_claims = 0
    supported_citations = 0
    for record in research.evidence:
        if record.pmid not in cited_pmids:
            continue
        claim = " ".join((record.key_result or "").split()).casefold()
        quote = " ".join((record.source_quote or "").split()).casefold()
        claim_emitted = bool(record.key_result) and (
            record.key_result in answer_text
        )
        claim_supported = bool(claim) and claim in quote
        if claim_emitted:
            emitted_claims += 1
            if not claim_supported:
                unsupported_claims += 1
        if claim_emitted and claim_supported:
            supported_citations += 1
    return {
        "retrieved_ids": [article.pmid for article in research.articles],
        "grades": {
            _fixture_source_id(condition.source_id): (
                condition.qualification.grade.value
            )
            for condition in conditions
        },
        "interaction_outcomes": {
            _fixture_source_id(condition.source_id): (
                condition.qualification.interaction_outcome.value
                if condition.qualification.interaction_outcome is not None
                else None
            )
            for condition in conditions
        },
        "admission_status": assessment.evidence_admission.status.value,
        "target_claim_abstained": (
            assessment.evidence_admission.status
            is not EvidenceAdmissionStatus.ADMITTED
        ),
        "direct_source_ids": [
            _fixture_source_id(source_id)
            for source_id in assessment.evidence_admission.direct_source_ids
        ],
        "contextual_source_ids": (
            [
                _fixture_source_id(source_id)
                for source_id in (
                    assessment.evidence_admission.contextual_source_ids
                )
            ]
        ),
        "excluded_source_ids": [
            _fixture_source_id(source_id)
            for source_id in assessment.evidence_admission.excluded_source_ids
        ],
        "conflict_ids": [item.id for item in assessment.conflicts],
        "gap_ids": [item.id for item in assessment.gaps],
        "citation_count": len(research.answer.citations),
        "supported_citations": supported_citations,
        "emitted_claims": emitted_claims,
        "unsupported_claims": unsupported_claims,
        "answer_abstained": _answer_abstained(
            answer_text,
            len(research.answer.citations),
        ),
        "forbidden_markers_present": [
            marker for marker in markers if marker.casefold() in answer_text.casefold()
        ],
        "task_completed": True,
        "model_calls": 0,
        "network_calls": 0,
        "external_actions": 0,
    }


def _research_from_articles(
    question: ResearchQuestion,
    articles_payload: list[dict[str, Any]],
) -> ResearchResult:
    articles = [PubMedArticle.model_validate(item) for item in articles_payload]
    provider = RuleBasedEvidenceProvider()
    evidence = [provider.extract(article) for article in articles]
    answer_evidence = [
        record
        for article, record in zip(articles, evidence, strict=True)
        if qualify_literature_evidence(
            question,
            title=article.title,
            abstract=article.abstract,
        ).grade.value
        in {"direct_interaction", "contextual"}
    ]
    return ResearchResult(
        query=question.text,
        articles=articles,
        evidence=evidence,
        answer=provider.answer(question.text, answer_evidence),
        provider_name=provider.name,
        retrieval_request_count=0,
        estimated_llm_cost_usd=0.0,
    )


def _evaluate_retrieval_replay(case: V07EvaluationCase) -> dict[str, Any]:
    batches = case.input.get("retrieval_batches")
    if not isinstance(batches, list) or not 1 <= len(batches) <= 3:
        raise ValueError("retrieval_replay 需要 1 到 3 个冻结检索批次。")
    client = _FrozenPubMedClient(batches)
    research = run_multi_query_research(
        case.question,
        max_results=int(case.input.get("max_results", 8)),
        max_queries=len(batches),
        client=client,
        provider=RuleBasedEvidenceProvider(),
        ranking_provider=_OfflineRankingProvider(),
    ).research
    actual = _common_research_actual(
        case.question,
        research,
        forbidden_markers=list(case.input.get("forbidden_markers", [])),
    )
    actual["replay_request_count"] = client.request_count
    return actual


def _evaluate_literature(case: V07EvaluationCase) -> dict[str, Any]:
    articles = case.input.get("articles", [])
    if not isinstance(articles, list):
        raise ValueError("literature evaluator 的 articles 必须是数组。")
    research = _research_from_articles(case.question, articles)
    return _common_research_actual(
        case.question,
        research,
        forbidden_markers=list(case.input.get("forbidden_markers", [])),
    )


def _analysis_from_case(case: V07EvaluationCase) -> ExperimentAnalysisResult:
    csv_text = case.input.get("csv_text")
    if not isinstance(csv_text, str):
        raise ValueError("experiment evaluator 缺少内嵌 csv_text。")
    analysis_type = case.input.get("analysis_type")
    if analysis_type == "fici":
        return analyze_fici_csv(csv_text, source_name=f"{case.id}.csv")
    if analysis_type == "growth_curve":
        return analyze_growth_curve_csv(csv_text, source_name=f"{case.id}.csv")
    raise ValueError(f"未知实验分析类型：{analysis_type}")


def _evaluate_experiment(case: V07EvaluationCase) -> dict[str, Any]:
    analysis = _analysis_from_case(case)
    matches = experiment_analysis_matches_question(case.question, analysis)
    assessment = assess_evidence([], analysis, question=case.question)
    return {
        "analysis_type": analysis.analysis_type,
        "analysis_valid": analysis.valid,
        "analysis_admitted": matches,
        "valid_row_count": analysis.valid_row_count,
        "error_count": len(analysis.errors),
        "conflict_ids": [item.id for item in assessment.conflicts],
        "gap_ids": [item.id for item in assessment.gaps],
        "scope_gap_ids": [
            item.id
            for item in assessment.gaps
            if item.id
            in {
                "gap-fici-intervention-identity",
                "gap-growth-curve-scope-identity",
                "gap-invalid-analysis",
            }
        ],
        "task_completed": True,
        "model_calls": 0,
        "network_calls": 0,
        "external_actions": 0,
    }


def _support_terms_for_record(
    support_terms: Any,
    record: EvidenceRecord,
) -> list[str]:
    if isinstance(support_terms, list):
        return [str(item) for item in support_terms]
    if isinstance(support_terms, Mapping):
        value = support_terms.get(record.pmid, [])
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _evaluate_citation(case: V07EvaluationCase) -> dict[str, Any]:
    records_payload = case.input.get("records")
    if not isinstance(records_payload, list) or not records_payload:
        raise ValueError("citation evaluator 至少需要一条结构化证据。")
    records = [EvidenceRecord.model_validate(item) for item in records_payload]
    provider = RuleBasedEvidenceProvider()
    answer = provider.answer(case.question.text, records)
    supported_citations = 0
    unsupported_claims = 0
    emitted_claims = 0
    support_terms = case.input.get("support_terms", {})
    support_by_pmid: dict[str, bool] = {}
    for record in records:
        terms = _support_terms_for_record(support_terms, record)
        if not terms:
            raise ValueError(
                f"{case.id} 缺少 claim 专属的 support_terms：{record.pmid}"
            )
        claim = (record.key_result or "").casefold()
        if not claim or any(term.casefold() not in claim for term in terms):
            raise ValueError(
                f"{case.id} 的 support_terms 必须来自待评分 key_result："
                f"{record.pmid}"
            )
        quote = (record.source_quote or "").casefold()
        supported = bool(record.source_quote) and all(
            term.casefold() in quote for term in terms
        )
        support_by_pmid[record.pmid] = supported
        citation_emitted = any(
            citation.pmid == record.pmid for citation in answer.citations
        )
        if citation_emitted and supported:
            supported_citations += 1
        if record.key_result and record.key_result in answer.answer_markdown:
            emitted_claims += 1
            if not supported:
                unsupported_claims += 1
    citation_count = len(answer.citations)
    return {
        "citation_count": citation_count,
        "supported_citations": supported_citations,
        "emitted_claims": emitted_claims,
        "unsupported_claims": unsupported_claims,
        "support_by_pmid": support_by_pmid,
        "answer_abstained": _answer_abstained(
            answer.answer_markdown,
            citation_count,
        ),
        "task_completed": True,
        "model_calls": 0,
        "network_calls": 0,
        "external_actions": 0,
    }


def _evaluate_tool_retrieval(case: V07EvaluationCase) -> dict[str, Any]:
    batches = case.input.get("retrieval_batches")
    if not isinstance(batches, list) or not 1 <= len(batches) <= 3:
        raise ValueError("tool_retrieval 需要 1 到 3 个冻结批次。")
    client = _FrozenPubMedClient(batches)
    try:
        research = run_multi_query_research(
            case.question,
            max_results=int(case.input.get("max_results", 8)),
            max_queries=len(batches),
            client=client,
            provider=RuleBasedEvidenceProvider(),
            ranking_provider=_OfflineRankingProvider(),
        ).research
    except _FrozenToolFailure as exc:
        failure_event = build_task_event(
            f"task-{case.id}",
            TaskStatus.FAILED,
            f"冻结检索失败：{exc}",
            event_id=f"event-{case.id}-failure",
        )
        single_failed_request = len(batches) == 1
        return {
            "task_state": failure_event.status.value,
            "task_completed": single_failed_request,
            "error_type": exc.error_type,
            "error_message": str(exc),
            "failure_recorded": True,
            "partial_results_preserved": False,
            "retrieved_ids": [],
            "replay_request_count": client.request_count,
            "model_calls": 0,
            "network_calls": 0,
            "external_actions": 0,
        }
    actual = _common_research_actual(case.question, research)
    actual.update(
        {
            "task_state": "awaiting_review",
            "partial_results_preserved": True,
            "replay_request_count": client.request_count,
        }
    )
    return actual


def _evaluate_partial_analysis(case: V07EvaluationCase) -> dict[str, Any]:
    articles = case.input.get("articles", [])
    if not isinstance(articles, list):
        raise ValueError("partial_analysis 的 articles 必须是数组。")
    research = _research_from_articles(case.question, articles)
    conditions = build_experiment_conditions(research, question=case.question)
    analysis = _analysis_from_case(case)
    assessment = assess_evidence(
        conditions,
        analysis,
        question=case.question,
    )
    task_event = build_task_event(
        f"task-{case.id}",
        TaskStatus.AWAITING_REVIEW,
        "规则基线已形成可人工复核的部分结果。",
        event_id=f"event-{case.id}",
    )
    report = build_decision_report(
        case.question,
        conditions=conditions,
        analysis=analysis,
        assessment=assessment,
        task_events=[task_event],
    )
    return {
        "admission_status": report.evidence_admission.status.value,
        "analysis_valid": analysis.valid,
        "analysis_admitted": experiment_analysis_matches_question(
            case.question,
            analysis,
        ),
        "conflict_ids": [item.id for item in report.conflicts],
        "gap_ids": [item.id for item in report.evidence_gaps],
        "report_generated": True,
        "task_state": report.task_status.current_status.value,
        "task_completed": True,
        "model_calls": 0,
        "network_calls": 0,
        "external_actions": 0,
    }


_EVALUATORS = {
    "retrieval_replay": _evaluate_retrieval_replay,
    "literature": _evaluate_literature,
    "experiment": _evaluate_experiment,
    "citation": _evaluate_citation,
    "tool_retrieval": _evaluate_tool_retrieval,
    "partial_analysis": _evaluate_partial_analysis,
}


def _expected_mismatches(
    actual: Any,
    expected: Any,
    *,
    path: str = "$",
) -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, actual={actual!r}"]
        mismatches: list[str] = []
        for key, expected_value in expected.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing")
                continue
            mismatches.extend(
                _expected_mismatches(
                    actual[key],
                    expected_value,
                    path=f"{path}.{key}",
                )
            )
        return mismatches
    if actual != expected:
        return [f"{path}: expected={expected!r}, actual={actual!r}"]
    return []


def _metric_observations(
    case: V07EvaluationCase,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, dict[str, float]]:
    observations: dict[str, dict[str, float]] = {}
    applicable = set(case.applicable_metrics)
    if "retrieval_recall_at_k" in applicable:
        relevant = [str(item) for item in case.input.get("gold_relevant_ids", [])]
        k = int(case.input.get("retrieval_k", case.input.get("max_results", 8)))
        retrieved = [str(item) for item in actual.get("retrieved_ids", [])][:k]
        if relevant:
            observations["retrieval_recall_at_k"] = {
                "numerator": float(len(set(relevant) & set(retrieved))),
                "denominator": float(len(set(relevant))),
            }
    if "citation_precision" in applicable:
        denominator = float(actual.get("citation_count", 0))
        if denominator:
            observations["citation_precision"] = {
                "numerator": float(actual.get("supported_citations", 0)),
                "denominator": denominator,
            }
    if "unsupported_claim_rate" in applicable:
        denominator = float(actual.get("emitted_claims", 0))
        if denominator:
            observations["unsupported_claim_rate"] = {
                "numerator": float(actual.get("unsupported_claims", 0)),
                "denominator": denominator,
            }
    if "abstention_accuracy" in applicable:
        if (
            "target_claim_abstained" in expected
            and "target_claim_abstained" in actual
        ):
            correct = (
                actual["target_claim_abstained"]
                == expected["target_claim_abstained"]
            )
        elif "answer_abstained" in expected and "answer_abstained" in actual:
            correct = actual["answer_abstained"] == expected["answer_abstained"]
        elif "analysis_admitted" in expected and "analysis_admitted" in actual:
            correct = actual["analysis_admitted"] == expected["analysis_admitted"]
        elif "admission_status" in expected and "admission_status" in actual:
            correct = actual["admission_status"] == expected["admission_status"]
        else:
            correct = False
        observations["abstention_accuracy"] = {
            "numerator": float(bool(correct)),
            "denominator": 1.0,
        }
    if "task_completion_rate" in applicable and "task_completed" in actual:
        observations["task_completion_rate"] = {
            "numerator": float(bool(actual["task_completed"])),
            "denominator": 1.0,
        }
    return observations


def _aggregate_rate_metric(
    name: str,
    results: list[V07CaseResult],
    *,
    scope: str,
    cases_by_id: dict[str, V07EvaluationCase],
) -> V07MetricResult:
    declared = sum(
        name in cases_by_id[result.id].applicable_metrics
        for result in results
    )
    observations = [
        result.metric_observations[name]
        for result in results
        if name in result.metric_observations
    ]
    numerator = sum(item["numerator"] for item in observations)
    denominator = sum(item["denominator"] for item in observations)
    if not denominator:
        return V07MetricResult(
            status="not_applicable",
            unit="rate",
            scope=scope,
            not_applicable_cases=declared,
        )
    return V07MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        unit="rate",
        scope=scope,
        not_applicable_cases=max(declared - len(observations), 0),
    )


def _implementation_sha256(project_root: Path) -> str:
    paths = [
        "src/vetevidence/providers.py",
        "src/vetevidence/answering.py",
        "src/vetevidence/extraction.py",
        "src/vetevidence/models.py",
        "src/vetevidence/workbench.py",
        "src/vetevidence/workbench_pipeline.py",
        "src/vetevidence/experiment_analysis.py",
        "src/vetevidence/v07_evaluation.py",
        "scripts/run_v07_rule_baseline.py",
    ]
    digest = sha256()
    for relative in paths:
        path = project_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        normalized_source = (
            path.read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        digest.update(normalized_source.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _deterministic_report_payload(report: V07BaselineReport) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload.pop("generated_at", None)
    payload.pop("deterministic_result_sha256", None)
    for result in payload["results"]:
        result.pop("latency_ms", None)
    latency = payload["metrics"]["latency"]
    latency["value"] = None
    return payload


def v07_deterministic_result_sha256(report: V07BaselineReport) -> str:
    """Recompute the stable hash, including for an on-disk report audit."""

    return sha256(_canonical_json(_deterministic_report_payload(report))).hexdigest()


def run_v07_rule_baseline(
    loaded: LoadedV07Evaluation,
    *,
    project_root: Path | None = None,
) -> V07BaselineReport:
    root = project_root or Path(__file__).resolve().parents[2]
    results: list[V07CaseResult] = []
    cases_by_id = {case.id: case for case in loaded.dataset.cases}
    for case in loaded.dataset.cases:
        started = perf_counter()
        expected = loaded.expected[case.id].expected
        try:
            actual = _EVALUATORS[case.evaluator](case)
            mismatches = _expected_mismatches(actual, expected)
            error_type = None if not mismatches else "expectation_mismatch"
        except Exception as exc:  # case isolation is an evaluation contract
            actual = {
                "evaluation_error": type(exc).__name__,
                "message": str(exc),
                "task_completed": False,
                "model_calls": 0,
                "network_calls": 0,
            }
            mismatches = [f"evaluation error: {type(exc).__name__}: {exc}"]
            error_type = "evaluation_error"
        latency_ms = (perf_counter() - started) * 1000
        observations = _metric_observations(case, expected, actual)
        results.append(
            V07CaseResult(
                id=case.id,
                category=case.category,
                evaluator=case.evaluator,
                passed=not mismatches,
                expected=expected,
                actual=actual,
                error_type=error_type,
                mismatches=mismatches,
                metric_observations=observations,
                latency_ms=latency_ms,
            )
        )

    by_category_items: dict[str, list[V07CaseResult]] = defaultdict(list)
    for result in results:
        by_category_items[result.category].append(result)
    by_category: dict[str, V07CategorySummary] = {}
    for category in sorted(by_category_items):
        items = by_category_items[category]
        passed = sum(item.passed for item in items)
        by_category[category] = V07CategorySummary(
            total=len(items),
            passed=passed,
            failed=len(items) - passed,
            pass_rate=passed / len(items),
        )
    passed = sum(result.passed for result in results)
    summary = V07Summary(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        evaluation_errors=sum(
            result.error_type == "evaluation_error" for result in results
        ),
        pass_rate=passed / len(results) if results else 0.0,
        macro_category_pass_rate=(
            sum(item.pass_rate for item in by_category.values())
            / len(by_category)
            if by_category
            else 0.0
        ),
        by_category=by_category,
    )
    scopes = {
        "retrieval_recall_at_k": (
            "冻结候选回放中的 Recall@3；包含部分失败恢复能力，"
            "不是实时 PubMed 或 RAG 召回。"
        ),
        "citation_precision": (
            "声明适用的合成 claim-citation 对中，由来源原句支持的比例；"
            "不是全系统总体值。"
        ),
        "unsupported_claim_rate": (
            "声明适用的合成原子结论中，不被来源原句支持的比例；"
            "不是全系统或 LLM 总体值。"
        ),
        "abstention_accuracy": (
            "结构化目标结论或实验准入动作与金标准一致的比例；允许背景引用。"
        ),
        "task_completion_rate": (
            "返回可复核回答、明确拒答或部分结果算完成；未处理异常不算。"
        ),
    }
    metrics = {
        name: _aggregate_rate_metric(
            name,
            results,
            scope=scope,
            cases_by_id=cases_by_id,
        )
        for name, scope in scopes.items()
    }
    metrics["cost"] = V07MetricResult(
        status="measured",
        value=0.0,
        unit="USD",
        scope=(
            "本次规则基线实际发生的 LLM API 费用；模型调用和 Token 均为零。"
            "CPU、电力和人工成本未计量。"
        ),
    )
    metrics["latency"] = V07MetricResult(
        status="measured",
        value=median(item.latency_ms for item in results),
        unit="ms",
        scope="本机 27 个离线单次任务耗时中位数；不纳入稳定哈希且不可跨机器直比。",
    )
    provisional = V07BaselineReport(
        dataset_sha256=loaded.dataset_sha256,
        gold_review_status=loaded.review_status,
        generated_at=datetime.now(timezone.utc),
        system=V07RuleSystemProfile(
            implementation_sha256=_implementation_sha256(root)
        ),
        summary=summary,
        metrics=metrics,
        results=results,
        deterministic_result_sha256="0" * 64,
        boundaries=loaded.dataset.boundaries,
    )
    deterministic_sha = v07_deterministic_result_sha256(provisional)
    return provisional.model_copy(
        update={"deterministic_result_sha256": deterministic_sha}
    )


def v07_baseline_to_markdown(report: V07BaselineReport) -> str:
    lines = [
        "# VetResearch Workbench v0.7 规则基线评测",
        "",
        f"- 数据集版本：`{report.dataset_version}`",
        f"- 数据集 SHA-256：`{report.dataset_sha256}`",
        f"- 金标准复核状态：`{report.gold_review_status}`",
        f"- 规则实现 SHA-256：`{report.system.implementation_sha256}`",
        f"- 稳定结果 SHA-256：`{report.deterministic_result_sha256}`",
        f"- 运行时间：`{report.generated_at.isoformat()}`",
        "- Provider：`rules_v1`",
        "- LLM／网络调用：`0 / 0`",
        f"- 用例：`{report.summary.passed}/{report.summary.total}` 符合金标准",
        "",
        "> 这是 27 个完全离线、合成的工程评测场景，不是科研证据，也不是"
        "通用模型准确率。规则基线允许失败；失败项正是后续 RAG／Agent 需要"
        "改善的对照。",
        "旧版 30 条定向评测是单次实时查询的字段回归，不等于本套 v0.7 产品"
        "边界成绩。",
        "",
        "## 固定指标",
        "",
        "| 指标 | 状态 | 值 | 分子/分母 | 口径 |",
        "|---|---|---:|---:|---|",
    ]
    for name in sorted(report.metrics):
        metric = report.metrics[name]
        if metric.status == "not_applicable" or metric.value is None:
            value = "N/A"
        elif metric.unit == "rate":
            value = f"{metric.value:.1%}"
        elif metric.unit == "USD":
            value = f"${metric.value:.4f}"
        else:
            value = f"{metric.value:.3f} {metric.unit}"
        ratio = (
            "—"
            if metric.numerator is None or metric.denominator is None
            else f"{metric.numerator:g}/{metric.denominator:g}"
        )
        lines.append(
            f"| `{name}` | {metric.status} | {value} | {ratio} | "
            f"{metric.scope.replace('|', '/')} |"
        )

    lines.extend(
        [
            "",
            "## 九类覆盖",
            "",
            "| 类别 | 通过/总数 | 通过率 |",
            "|---|---:|---:|",
        ]
    )
    for category, summary in report.summary.by_category.items():
        lines.append(
            f"| `{category}` | {summary.passed}/{summary.total} | "
            f"{summary.pass_rate:.1%} |"
        )

    lines.extend(
        [
            "",
            "## 逐题结果",
            "",
            "| ID | 类别 | 结果 | 差异摘要 |",
            "|---|---|---|---|",
        ]
    )
    for result in report.results:
        status = "符合" if result.passed else "不符合"
        mismatch = "；".join(result.mismatches) or "—"
        lines.append(
            f"| `{result.id}` | `{result.category}` | {status} | "
            f"{mismatch.replace('|', '/').replace(chr(10), ' ')[:240]} |"
        )

    lines.extend(["", "## 已知边界", ""])
    lines.extend(f"- {item}" for item in report.boundaries)
    lines.extend(
        [
            "- 金标准是工程验收口径；涉及科研语义的正式对外结论仍需领域人工复核。",
        ]
    )
    return "\n".join(lines)
