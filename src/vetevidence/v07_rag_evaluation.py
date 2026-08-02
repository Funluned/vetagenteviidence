"""Frozen, offline Recall@3 evaluation for the v0.7 local RAG baseline.

The evaluator deliberately does not load ``expected.json``.  Retrieval gold is
read only from ``cases[].input.gold_relevant_ids`` and is never included in
indexed text or in the retrieval query.
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.agent_providers import DeterministicHashEmbeddingProvider
from vetevidence.local_rag import (
    EVIDENCE_ROLE,
    EvidenceSource,
    IndexManifest,
    LocalRAGIndex,
)


RAG_CASE_SLICES: dict[str, Literal["semantic_direct", "resilience_partial"]] = {
    "DIR-01": "semantic_direct",
    "DIR-02": "semantic_direct",
    "DIR-03": "semantic_direct",
    "TOOL-02": "resilience_partial",
}
RETRIEVAL_MODES = (
    "keyword_only",
    "hash_vector_only",
    "hybrid",
)
RetrievalMode = Literal["keyword_only", "hash_vector_only", "hybrid"]
IMPLEMENTATION_PATHS = (
    "src/vetevidence/agent_providers.py",
    "src/vetevidence/local_rag.py",
    "src/vetevidence/v07_rag_evaluation.py",
    "scripts/run_v07_rag_baseline.py",
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_sha256(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


class _RAGReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class V07RAGCaseConfig(_RAGReportModel):
    case_id: str = Field(min_length=1)
    slice: Literal["semantic_direct", "resilience_partial"]


class V07RAGRetrievalManifest(_RAGReportModel):
    schema_version: Literal["1.0"]
    manifest_version: Literal["local_hash_rag_v1"]
    dataset_version: Literal["v0.7.0"]
    cases_sha256_algorithm: Literal["sha256-canonical-json-v1"]
    cases_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieval_k: Literal[3]
    query_builder: Literal["question_scope_v1"]
    document_builder: Literal["title_abstract_v1"]
    chunk_size: int = Field(gt=0)
    overlap_chars: int = Field(ge=0)
    keyword_weight: float = Field(ge=0)
    vector_weight: float = Field(ge=0)
    hard_negative_source_ids: tuple[str, ...] = Field(min_length=1)
    cases: tuple[V07RAGCaseConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frozen_design(self) -> V07RAGRetrievalManifest:
        if self.overlap_chars >= self.chunk_size:
            raise ValueError("RAG 评测 overlap_chars 必须小于 chunk_size。")
        if not math.isclose(
            self.keyword_weight + self.vector_weight,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("RAG 评测关键词与向量权重之和必须为 1。")
        if len(self.hard_negative_source_ids) != len(
            set(self.hard_negative_source_ids)
        ):
            raise ValueError("RAG 评测硬负例来源 ID 不得重复。")
        if any(
            not source_id.startswith("SYN-")
            for source_id in self.hard_negative_source_ids
        ):
            raise ValueError("RAG 评测来源 ID 必须使用 SYN-*。")
        configured = {item.case_id: item.slice for item in self.cases}
        if len(configured) != len(self.cases):
            raise ValueError("RAG 评测 case_id 不得重复。")
        if configured != RAG_CASE_SLICES:
            raise ValueError("local_hash_rag_v1 必须固定四题及两个切片。")
        if len(self.hard_negative_source_ids) + 1 <= self.retrieval_k:
            raise ValueError("RAG 评测候选池必须大于 K。")
        return self


class V07RAGSystemProfile(_RAGReportModel):
    provider: Literal["local_hash_rag_v1"] = "local_hash_rag_v1"
    implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_provider_name: str = Field(min_length=1)
    embedding_model_name: str = Field(min_length=1)
    embedding_model_version: str = Field(min_length=1)
    embedding_dimensions: int = Field(gt=0)
    embedding_fake: Literal[True] = True
    llm_enabled: Literal[False] = False
    network_calls: Literal[0] = 0
    model_calls: Literal[0] = 0
    real_model_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0
    llm_api_cost_cny: Literal[0.0] = 0.0
    llm_api_cost_usd: Literal[0.0] = 0.0
    external_actions: Literal[0] = 0


class V07RAGRecallResult(_RAGReportModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)
    value: float = Field(ge=0.0, le=1.0)
    case_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ratio(self) -> V07RAGRecallResult:
        if self.numerator > self.denominator:
            raise ValueError("Recall 分子不得大于分母。")
        if not math.isclose(
            self.value,
            self.numerator / self.denominator,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("Recall value 与分子、分母不一致。")
        return self


class V07RAGRetrievalModeResult(_RAGReportModel):
    retrieved_source_ids: tuple[str, ...]
    hit_source_ids: tuple[str, ...]
    recall_at_3: V07RAGRecallResult

    @model_validator(mode="after")
    def validate_unique_top_three(self) -> V07RAGRetrievalModeResult:
        if len(self.retrieved_source_ids) != len(set(self.retrieved_source_ids)):
            raise ValueError("每种检索模式的 Top K 必须按 source_id 去重。")
        if len(self.retrieved_source_ids) > 3:
            raise ValueError("每种检索模式最多只能返回 Top 3。")
        return self


class V07RAGCaseResult(_RAGReportModel):
    id: str = Field(min_length=1)
    slice: Literal["semantic_direct", "resilience_partial"]
    query_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_source_ids: tuple[str, ...] = Field(min_length=1)
    candidate_count: int = Field(gt=0)
    gold_relevant_ids: tuple[str, ...] = Field(min_length=1)
    retrieved_source_ids: tuple[str, ...]
    hit_source_ids: tuple[str, ...]
    recall_at_3: V07RAGRecallResult
    retrieval_modes: dict[RetrievalMode, V07RAGRetrievalModeResult]
    passed: bool
    failed_retrieval_batch_count: int = Field(ge=0)
    preserved_success_source_ids: tuple[str, ...]
    index_manifest: IndexManifest
    evidence_role: Literal["untrusted_evidence"] = EVIDENCE_ROLE
    network_calls: Literal[0] = 0
    model_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0
    llm_api_cost_cny: Literal[0.0] = 0.0
    llm_api_cost_usd: Literal[0.0] = 0.0
    external_actions: Literal[0] = 0

    @model_validator(mode="after")
    def validate_counts(self) -> V07RAGCaseResult:
        if self.candidate_count != len(self.candidate_source_ids):
            raise ValueError("候选数量与 candidate_source_ids 不一致。")
        if len(self.candidate_source_ids) != len(set(self.candidate_source_ids)):
            raise ValueError("候选 source_id 不得重复。")
        if self.candidate_count <= 3:
            raise ValueError("每题候选数量必须大于固定 K=3。")
        if len(self.gold_relevant_ids) != len(set(self.gold_relevant_ids)):
            raise ValueError("gold_relevant_ids 不得重复。")
        if not set(self.gold_relevant_ids).issubset(self.candidate_source_ids):
            raise ValueError("所有 gold 来源都必须位于固定候选池。")
        if self.index_manifest.source_count != self.candidate_count:
            raise ValueError("索引来源数量与固定候选池不一致。")
        if len(self.retrieved_source_ids) != len(set(self.retrieved_source_ids)):
            raise ValueError("Top K 必须按 source_id 去重。")
        if len(self.retrieved_source_ids) != 3:
            raise ValueError("local_hash_rag_v1 必须返回唯一来源 Top 3。")
        if set(self.retrieval_modes) != set(RETRIEVAL_MODES):
            raise ValueError("每题必须保存三种固定检索模式。")
        candidate_ids = set(self.candidate_source_ids)
        for mode, mode_result in self.retrieval_modes.items():
            if len(mode_result.retrieved_source_ids) != 3:
                raise ValueError(f"{mode} 必须返回唯一来源 Top 3。")
            if not set(mode_result.retrieved_source_ids).issubset(candidate_ids):
                raise ValueError(f"{mode} 返回了固定候选池之外的来源。")
            expected_hits = tuple(
                source_id
                for source_id in self.gold_relevant_ids
                if source_id in mode_result.retrieved_source_ids
            )
            if mode_result.hit_source_ids != expected_hits:
                raise ValueError(f"{mode} 的命中来源与 Top 3／gold 不一致。")
            recall = mode_result.recall_at_3
            if (
                recall.numerator != len(expected_hits)
                or recall.denominator != len(self.gold_relevant_ids)
                or recall.case_ids != (self.id,)
            ):
                raise ValueError(f"{mode} 的 Recall@3 审计字段不一致。")
        hybrid = self.retrieval_modes["hybrid"]
        if (
            self.retrieved_source_ids != hybrid.retrieved_source_ids
            or self.hit_source_ids != hybrid.hit_source_ids
            or self.recall_at_3 != hybrid.recall_at_3
        ):
            raise ValueError("兼容字段必须与 hybrid 模式完全一致。")
        if not set(self.preserved_success_source_ids).issubset(candidate_ids):
            raise ValueError("保留的成功来源必须位于固定候选池。")
        expected_passed = len(self.hit_source_ids) == len(
            set(self.gold_relevant_ids)
        )
        if self.passed != expected_passed:
            raise ValueError("case passed 与 gold 命中数不一致。")
        return self


class V07RAGModeSummary(_RAGReportModel):
    retrieval_recall_at_3: V07RAGRecallResult
    slices: dict[
        Literal["semantic_direct", "resilience_partial"],
        V07RAGRecallResult,
    ]

    @model_validator(mode="after")
    def validate_slices(self) -> V07RAGModeSummary:
        if set(self.slices) != {"semantic_direct", "resilience_partial"}:
            raise ValueError("每种检索模式必须包含两个固定切片。")
        return self


class V07RAGSummary(_RAGReportModel):
    case_count: int = Field(gt=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    evaluation_errors: Literal[0] = 0
    retrieval_recall_at_3: V07RAGRecallResult
    slices: dict[
        Literal["semantic_direct", "resilience_partial"],
        V07RAGRecallResult,
    ]
    retrieval_modes: dict[RetrievalMode, V07RAGModeSummary]

    @model_validator(mode="after")
    def validate_counts(self) -> V07RAGSummary:
        if self.passed_cases + self.failed_cases != self.case_count:
            raise ValueError("RAG case 通过数与失败数不等于总数。")
        if set(self.slices) != {"semantic_direct", "resilience_partial"}:
            raise ValueError("RAG 报告必须包含两个固定切片。")
        if set(self.retrieval_modes) != set(RETRIEVAL_MODES):
            raise ValueError("RAG 报告必须包含三种固定检索模式。")
        hybrid = self.retrieval_modes["hybrid"]
        if (
            self.retrieval_recall_at_3 != hybrid.retrieval_recall_at_3
            or self.slices != hybrid.slices
        ):
            raise ValueError("汇总兼容字段必须与 hybrid 模式完全一致。")
        return self


class V07RAGBaselineReport(_RAGReportModel):
    schema_version: Literal["1.0"] = "1.0"
    report_version: Literal["local_hash_rag_v1"] = "local_hash_rag_v1"
    dataset_version: Literal["v0.7.0"]
    cases_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieval_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieval_k: Literal[3]
    system: V07RAGSystemProfile
    summary: V07RAGSummary
    results: tuple[V07RAGCaseResult, ...] = Field(min_length=1)
    deterministic_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    boundaries: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class V07RAGSourceCatalog:
    sources: dict[str, EvidenceSource]
    successful_retrieval_ids: dict[str, tuple[str, ...]]
    failed_retrieval_batch_counts: dict[str, int]


@dataclass(frozen=True)
class LoadedV07RAGEvaluation:
    manifest: V07RAGRetrievalManifest
    cases_document: dict[str, Any]
    cases_by_id: dict[str, dict[str, Any]]
    source_catalog: V07RAGSourceCatalog
    cases_sha256: str
    retrieval_manifest_sha256: str


def _clean_required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串。")
    return " ".join(value.strip().split())


def _article_content(title: str, abstract: str | None) -> str:
    parts = [f"Title: {title}"]
    if abstract:
        parts.append(f"Abstract: {abstract}")
    return "\n".join(parts)


def _source_from_article(
    article: dict[str, Any],
    *,
    field_location: str,
    dataset_version: str,
) -> EvidenceSource:
    source_id = _clean_required_text(article.get("pmid"), label="pmid")
    if not source_id.startswith("SYN-"):
        raise ValueError("v0.7 RAG 评测来源必须使用 SYN-*。")
    title = _clean_required_text(article.get("title"), label="title")
    raw_abstract = article.get("abstract")
    if raw_abstract is not None and not isinstance(raw_abstract, str):
        raise ValueError(f"{source_id} 的 abstract 必须是字符串或 null。")
    abstract = (
        " ".join(raw_abstract.strip().split())
        if isinstance(raw_abstract, str) and raw_abstract.strip()
        else None
    )
    content = _article_content(title, abstract)
    return EvidenceSource(
        source_id=source_id,
        source_type="synthetic_evaluation_abstract",
        title=title,
        content=content,
        content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        field_location=field_location,
        version=dataset_version,
        authorization_scope="user_authorized",
        pmid=source_id,
        source_url=article.get("source_url"),
        metadata={
            "data_status": "synthetic_evaluation_only",
            "document_builder": "title_abstract_v1",
        },
    )


def extract_v07_rag_source_catalog(
    cases_document: dict[str, Any],
) -> V07RAGSourceCatalog:
    """Extract only article-shaped title/abstract records from cases.json."""

    dataset_version = _clean_required_text(
        cases_document.get("dataset_version"),
        label="dataset_version",
    )
    raw_cases = cases_document.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases.json 的 cases 必须是数组。")

    sources: dict[str, EvidenceSource] = {}
    successful: dict[str, tuple[str, ...]] = {}
    failed_counts: dict[str, int] = {}

    def register(article: Any, *, field_location: str) -> str:
        if not isinstance(article, dict):
            raise ValueError(f"{field_location} 必须是文献对象。")
        source = _source_from_article(
            article,
            field_location=field_location,
            dataset_version=dataset_version,
        )
        previous = sources.get(source.source_id)
        if previous is not None and previous != source:
            raise ValueError(f"重复来源内容不一致：{source.source_id}")
        sources[source.source_id] = source
        return source.source_id

    for case_index, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise ValueError("cases.json 中每个 case 都必须是对象。")
        case_id = _clean_required_text(case.get("id"), label="case.id")
        case_input = case.get("input")
        if not isinstance(case_input, dict):
            raise ValueError(f"{case_id} 的 input 必须是对象。")

        articles = case_input.get("articles", [])
        if not isinstance(articles, list):
            raise ValueError(f"{case_id} 的 articles 必须是数组。")
        for article_index, article in enumerate(articles):
            register(
                article,
                field_location=(
                    f"cases.json#/cases/{case_index}/input/articles/"
                    f"{article_index}/title+abstract"
                ),
            )

        case_successes: list[str] = []
        failed_count = 0
        batches = case_input.get("retrieval_batches", [])
        if not isinstance(batches, list):
            raise ValueError(f"{case_id} 的 retrieval_batches 必须是数组。")
        for batch_index, batch in enumerate(batches):
            if isinstance(batch, list):
                for article_index, article in enumerate(batch):
                    case_successes.append(
                        register(
                            article,
                            field_location=(
                                f"cases.json#/cases/{case_index}/input/"
                                f"retrieval_batches/{batch_index}/"
                                f"{article_index}/title+abstract"
                            ),
                        )
                    )
                continue
            if isinstance(batch, dict) and isinstance(batch.get("error"), str):
                failed_count += 1
                continue
            raise ValueError(f"{case_id} 含无效冻结检索批次。")
        successful[case_id] = tuple(dict.fromkeys(case_successes))
        failed_counts[case_id] = failed_count

    return V07RAGSourceCatalog(
        sources=sources,
        successful_retrieval_ids=successful,
        failed_retrieval_batch_counts=failed_counts,
    )


def load_v07_rag_evaluation(
    cases_path: Path,
    retrieval_manifest_path: Path,
) -> LoadedV07RAGEvaluation:
    cases_document = json.loads(cases_path.read_text(encoding="utf-8"))
    if not isinstance(cases_document, dict):
        raise ValueError("cases.json 顶层必须是对象。")
    cases_sha256 = _canonical_json_sha256(cases_document)
    manifest_document = json.loads(
        retrieval_manifest_path.read_text(encoding="utf-8")
    )
    manifest = V07RAGRetrievalManifest.model_validate(manifest_document)
    if manifest.cases_sha256 != cases_sha256:
        raise ValueError("RAG 检索清单与 cases.json SHA-256 不一致。")
    if manifest.dataset_version != cases_document.get("dataset_version"):
        raise ValueError("RAG 检索清单与 cases.json 版本不一致。")

    raw_cases = cases_document.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases.json 的 cases 必须是数组。")
    cases_by_id: dict[str, dict[str, Any]] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("cases.json 中每个 case 都必须是对象。")
        case_id = _clean_required_text(raw_case.get("id"), label="case.id")
        if case_id in cases_by_id:
            raise ValueError(f"cases.json case.id 重复：{case_id}")
        cases_by_id[case_id] = raw_case
    if not set(RAG_CASE_SLICES).issubset(cases_by_id):
        raise ValueError("cases.json 缺少固定 RAG 评测题。")

    source_catalog = extract_v07_rag_source_catalog(cases_document)
    needed_source_ids = set(manifest.hard_negative_source_ids)
    for case_id in RAG_CASE_SLICES:
        case_input = cases_by_id[case_id].get("input")
        if not isinstance(case_input, dict):
            raise ValueError(f"{case_id} 的 input 必须是对象。")
        gold = case_input.get("gold_relevant_ids")
        if (
            not isinstance(gold, list)
            or not gold
            or len(gold) != len(set(gold))
            or any(
                not isinstance(source_id, str)
                or not source_id.startswith("SYN-")
                for source_id in gold
            )
        ):
            raise ValueError(
                f"{case_id} 必须在 cases.json 声明唯一的 SYN-* gold。"
            )
        if set(gold) & set(manifest.hard_negative_source_ids):
            raise ValueError(f"{case_id} 的 gold 不得同时是硬负例。")
        needed_source_ids.update(gold)
    missing_sources = needed_source_ids - set(source_catalog.sources)
    if missing_sources:
        raise ValueError(f"RAG 检索清单引用未知来源：{sorted(missing_sources)}")

    return LoadedV07RAGEvaluation(
        manifest=manifest,
        cases_document=cases_document,
        cases_by_id=cases_by_id,
        source_catalog=source_catalog,
        cases_sha256=cases_sha256,
        retrieval_manifest_sha256=_canonical_json_sha256(manifest_document),
    )


def build_v07_rag_query(case: dict[str, Any]) -> str:
    """Build a deterministic query from user-visible scope, never case context."""

    question = case.get("question")
    if not isinstance(question, dict):
        raise ValueError("RAG 评测题缺少 question。")
    outcomes = question.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        raise ValueError("RAG 评测 question.outcomes 必须是非空数组。")
    clean_outcomes = [
        _clean_required_text(item, label="question.outcomes")
        for item in outcomes
    ]
    lines = [
        "Question: "
        + _clean_required_text(question.get("text"), label="question.text"),
        "Population: "
        + _clean_required_text(
            question.get("population"), label="question.population"
        ),
        "Intervention: "
        + _clean_required_text(
            question.get("intervention"), label="question.intervention"
        ),
        "Comparator: "
        + _clean_required_text(
            question.get("comparator"), label="question.comparator"
        ),
        "Outcomes: " + " | ".join(clean_outcomes),
    ]
    return "\n".join(lines)


def _implementation_sha256(project_root: Path) -> str:
    digest = sha256()
    for relative in IMPLEMENTATION_PATHS:
        path = project_root / relative
        normalized_source = (
            path.read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized_source.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _recall_result(
    results: list[V07RAGCaseResult],
) -> V07RAGRecallResult:
    numerator = sum(len(result.hit_source_ids) for result in results)
    denominator = sum(len(set(result.gold_relevant_ids)) for result in results)
    return V07RAGRecallResult(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        case_ids=tuple(result.id for result in results),
    )


def _mode_recall_result(
    results: list[V07RAGCaseResult],
    mode: RetrievalMode,
) -> V07RAGRecallResult:
    numerator = sum(
        len(result.retrieval_modes[mode].hit_source_ids)
        for result in results
    )
    denominator = sum(len(set(result.gold_relevant_ids)) for result in results)
    return V07RAGRecallResult(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        case_ids=tuple(result.id for result in results),
    )


def _unique_top_k_source_ids(
    ranked: list[Any],
    *,
    k: int,
) -> tuple[str, ...]:
    retrieved: list[str] = []
    for item in ranked:
        source_id = item.chunk.source_id
        if source_id in retrieved:
            continue
        retrieved.append(source_id)
        if len(retrieved) == k:
            break
    return tuple(retrieved)


def _deterministic_report_payload(
    report: V07RAGBaselineReport,
) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload.pop("deterministic_result_sha256", None)
    return payload


def v07_rag_deterministic_result_sha256(
    report: V07RAGBaselineReport,
) -> str:
    return _canonical_json_sha256(_deterministic_report_payload(report))


def run_v07_local_hash_rag_baseline(
    loaded: LoadedV07RAGEvaluation,
    *,
    project_root: Path | None = None,
) -> V07RAGBaselineReport:
    root = project_root or Path(__file__).resolve().parents[2]
    manifest = loaded.manifest
    provider = DeterministicHashEmbeddingProvider()
    results: list[V07RAGCaseResult] = []

    with tempfile.TemporaryDirectory(prefix="vetevidence-v07-rag-") as directory:
        temporary_root = Path(directory)
        for order, config in enumerate(manifest.cases):
            case = loaded.cases_by_id[config.case_id]
            case_input = case["input"]
            gold_relevant_ids = tuple(case_input["gold_relevant_ids"])
            candidate_ids = tuple(
                sorted(
                    set(gold_relevant_ids)
                    | set(manifest.hard_negative_source_ids)
                )
            )
            if len(candidate_ids) <= manifest.retrieval_k:
                raise ValueError(f"{config.case_id} 的候选数量必须大于 K。")
            candidate_sources = [
                loaded.source_catalog.sources[source_id]
                for source_id in candidate_ids
            ]
            query = build_v07_rag_query(case)
            index = LocalRAGIndex(
                temporary_root / f"case-{order}.sqlite3",
                embedding_provider=provider,
            )
            index_manifest = index.build(
                candidate_sources,
                chunk_size=manifest.chunk_size,
                overlap_chars=manifest.overlap_chars,
            )
            mode_weights: dict[RetrievalMode, tuple[float, float]] = {
                "keyword_only": (1.0, 0.0),
                "hash_vector_only": (0.0, 1.0),
                "hybrid": (
                    manifest.keyword_weight,
                    manifest.vector_weight,
                ),
            }
            retrieval_modes: dict[
                RetrievalMode, V07RAGRetrievalModeResult
            ] = {}
            for mode, (keyword_weight, vector_weight) in mode_weights.items():
                ranked = index.search(
                    query,
                    limit=len(candidate_ids),
                    keyword_weight=keyword_weight,
                    vector_weight=vector_weight,
                )
                retrieved_ids = _unique_top_k_source_ids(
                    ranked,
                    k=manifest.retrieval_k,
                )
                hit_ids = tuple(
                    source_id
                    for source_id in gold_relevant_ids
                    if source_id in retrieved_ids
                )
                retrieval_modes[mode] = V07RAGRetrievalModeResult(
                    retrieved_source_ids=retrieved_ids,
                    hit_source_ids=hit_ids,
                    recall_at_3=V07RAGRecallResult(
                        numerator=len(hit_ids),
                        denominator=len(set(gold_relevant_ids)),
                        value=len(hit_ids) / len(set(gold_relevant_ids)),
                        case_ids=(config.case_id,),
                    ),
                )
            hybrid = retrieval_modes["hybrid"]
            results.append(
                V07RAGCaseResult(
                    id=config.case_id,
                    slice=config.slice,
                    query_sha256=sha256(query.encode("utf-8")).hexdigest(),
                    candidate_source_ids=candidate_ids,
                    candidate_count=len(candidate_ids),
                    gold_relevant_ids=gold_relevant_ids,
                    retrieved_source_ids=hybrid.retrieved_source_ids,
                    hit_source_ids=hybrid.hit_source_ids,
                    recall_at_3=hybrid.recall_at_3,
                    retrieval_modes=retrieval_modes,
                    passed=(
                        len(hybrid.hit_source_ids)
                        == len(set(gold_relevant_ids))
                    ),
                    failed_retrieval_batch_count=(
                        loaded.source_catalog.failed_retrieval_batch_counts[
                            config.case_id
                        ]
                    ),
                    preserved_success_source_ids=(
                        loaded.source_catalog.successful_retrieval_ids[
                            config.case_id
                        ]
                    ),
                    index_manifest=index_manifest,
                )
            )

    slice_results = {
        slice_name: [
            result for result in results if result.slice == slice_name
        ]
        for slice_name in ("semantic_direct", "resilience_partial")
    }
    mode_summaries: dict[RetrievalMode, V07RAGModeSummary] = {
        mode: V07RAGModeSummary(
            retrieval_recall_at_3=_mode_recall_result(results, mode),
            slices={
                slice_name: _mode_recall_result(items, mode)
                for slice_name, items in slice_results.items()
            },
        )
        for mode in RETRIEVAL_MODES
    }
    summary = V07RAGSummary(
        case_count=len(results),
        passed_cases=sum(result.passed for result in results),
        failed_cases=sum(not result.passed for result in results),
        retrieval_recall_at_3=_recall_result(results),
        slices={
            slice_name: _recall_result(items)
            for slice_name, items in slice_results.items()
        },
        retrieval_modes=mode_summaries,
    )
    system = V07RAGSystemProfile(
        implementation_sha256=_implementation_sha256(root),
        embedding_provider_name=provider.name,
        embedding_model_name=provider.model_name,
        embedding_model_version=provider.model_version,
        embedding_dimensions=provider.dimensions,
    )
    keyword_recall = summary.retrieval_modes[
        "keyword_only"
    ].retrieval_recall_at_3
    vector_recall = summary.retrieval_modes[
        "hash_vector_only"
    ].retrieval_recall_at_3
    hybrid_recall = summary.retrieval_modes["hybrid"].retrieval_recall_at_3
    hybrid_gain = hybrid_recall.numerator - keyword_recall.numerator
    report = V07RAGBaselineReport(
        dataset_version=manifest.dataset_version,
        cases_sha256=loaded.cases_sha256,
        retrieval_manifest_sha256=loaded.retrieval_manifest_sha256,
        retrieval_k=manifest.retrieval_k,
        system=system,
        summary=summary,
        results=tuple(results),
        deterministic_result_sha256="0" * 64,
        boundaries=(
            "四题全部来自 v0.7.0 合成工程夹具，不是科研证据或通用检索准确率。",
            "semantic_direct 切片衡量三条直接证据；resilience_partial 切片衡量 TOOL-02 两条成功分支，不能写成纯向量排序收益。",
            "候选池由本题 gold 与同一组硬负例固定构造；每题候选数大于 K=3，因此这里只衡量标签知情候选池内排序，不是端到端文献召回。",
            "DeterministicHashEmbeddingProvider 是离线特征哈希假 Provider，不是训练模型或真实语义 Embedding。",
            (
                "同一候选池结果：keyword-only "
                f"{keyword_recall.numerator}/{keyword_recall.denominator}，"
                "hash-vector-only "
                f"{vector_recall.numerator}/{vector_recall.denominator}，"
                f"hybrid {hybrid_recall.numerator}/{hybrid_recall.denominator}；"
                f"hybrid 相对 keyword 命中变化 {hybrid_gain:+d}，"
                "不得据此暗示 hash vector 有效。"
            ),
            "内置固定评测不读取 expected.json、环境变量或 API Key，不联网、不调用真实模型，Token 与模型 API 费用均为零；本地特征哈希 embed() 会运行，但不属于真实模型调用。",
            "零联网保证只覆盖内置 DeterministicHashEmbeddingProvider 和固定评测；LocalRAGIndex 对自定义 Provider 的 network_used 只是契约校验，不是进程级断网沙箱。",
            "零新增模型/API/云向量库依赖不等于零运行条件；仍需要 Python、Pydantic、本机 CPU、磁盘和 SQLite 文件写入。",
            "所有索引正文均是不可信证据数据；文档中的提示或 SYSTEM 字样没有执行权限。",
        ),
    )
    digest = v07_rag_deterministic_result_sha256(report)
    return V07RAGBaselineReport.model_validate(
        {
            **report.model_dump(mode="json"),
            "deterministic_result_sha256": digest,
        }
    )


__all__ = [
    "LoadedV07RAGEvaluation",
    "RAG_CASE_SLICES",
    "RETRIEVAL_MODES",
    "RetrievalMode",
    "V07RAGBaselineReport",
    "V07RAGCaseConfig",
    "V07RAGCaseResult",
    "V07RAGRecallResult",
    "V07RAGRetrievalModeResult",
    "V07RAGRetrievalManifest",
    "V07RAGSourceCatalog",
    "V07RAGSummary",
    "V07RAGModeSummary",
    "V07RAGSystemProfile",
    "build_v07_rag_query",
    "extract_v07_rag_source_catalog",
    "load_v07_rag_evaluation",
    "run_v07_local_hash_rag_baseline",
    "v07_rag_deterministic_result_sha256",
]
