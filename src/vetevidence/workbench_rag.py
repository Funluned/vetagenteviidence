"""No-key local retrieval adapter for the workbench literature sources.

This module deliberately stops at candidate retrieval.  It never turns a hit
into a scientific claim and never calls a remote model.  Evidence admission
and decision reporting remain the responsibility of the existing transparent
rules and human-review workflow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.agent_providers import DeterministicHashEmbeddingProvider
from vetevidence.literature_import import LiteratureImportResult
from vetevidence.local_rag import (
    EvidenceSource,
    IndexManifest,
    LocalRAGIndex,
    RetrievalResult,
)
from vetevidence.models import ResearchResult


WORKBENCH_RAG_VERSION = "workbench-local-rag-v1"
MAX_WORKBENCH_RAG_SOURCES = 500
MAX_WORKBENCH_RAG_CHARACTERS = 5_000_000
MAX_WORKBENCH_RAG_QUERY_CHARACTERS = 2_000
SearchMode = Literal["keyword_only", "hash_vector_only", "hybrid"]
_SEARCH_WEIGHTS: dict[SearchMode, tuple[float, float]] = {
    "keyword_only": (1.0, 0.0),
    "hash_vector_only": (0.0, 1.0),
    "hybrid": (0.5, 0.5),
}
_SYNTHETIC_MARKERS = (
    "synthetic export",
    "synthetic demo",
    "synthetic_evaluation_only",
    "must not be treated as scientific evidence",
    "合成演示",
    "example.invalid",
)


class _WorkbenchRAGModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkbenchRAGSearchOutcome(_WorkbenchRAGModel):
    """Auditable local candidate search without generation or model cost."""

    schema_version: Literal[1] = 1
    retrieval_status: Literal["candidate_matches", "insufficient_evidence"]
    mode: SearchMode
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    results: tuple[RetrievalResult, ...] = ()
    embedding_fake: Literal[True] = True
    network_calls: Literal[0] = 0
    real_model_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0
    model_api_cost_cny: Literal[0] = 0
    external_actions: Literal[0] = 0

    @model_validator(mode="after")
    def validate_status(self) -> WorkbenchRAGSearchOutcome:
        if self.retrieval_status == "candidate_matches" and not self.results:
            raise ValueError("candidate_matches 必须包含候选结果。")
        if self.retrieval_status == "insufficient_evidence" and self.results:
            raise ValueError("insufficient_evidence 不得包含候选结果。")
        if tuple(result.rank for result in self.results) != tuple(
            range(1, len(self.results) + 1)
        ):
            raise ValueError("本地检索结果必须按连续名次保存。")
        return self


@dataclass(frozen=True, slots=True)
class PreparedWorkbenchRAGSources:
    """Authorized source set plus explicit exclusions shown by the UI."""

    sources: tuple[EvidenceSource, ...]
    public_source_count: int
    user_authorized_source_count: int
    synthetic_source_count: int
    total_character_count: int
    skipped_missing_abstract_count: int
    excluded_unconfirmed_import_count: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.public_source_count,
                self.user_authorized_source_count,
                self.synthetic_source_count,
                self.total_character_count,
                self.skipped_missing_abstract_count,
                self.excluded_unconfirmed_import_count,
            )
        ):
            raise ValueError("来源计数不得为负数。")
        if (
            self.public_source_count + self.user_authorized_source_count
            != len(self.sources)
        ):
            raise ValueError("来源分类计数与实际来源数量不一致。")


def _document_content(title: str, abstract: str) -> str:
    return f"Title: {' '.join(title.split())}\nAbstract: {' '.join(abstract.split())}"


def _is_synthetic(*values: str | None) -> bool:
    combined = " ".join(value or "" for value in values).casefold()
    return any(marker in combined for marker in _SYNTHETIC_MARKERS)


def _metadata(**values: object) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in values.items()
        if value is not None and str(value).strip()
    }


def prepare_workbench_rag_sources(
    *,
    run_id: str,
    research: ResearchResult | None,
    imported: LiteratureImportResult | None,
    include_user_authorized_imports: bool = False,
) -> PreparedWorkbenchRAGSources:
    """Convert only abstract-bearing, authorized literature into RAG sources."""

    if not run_id.strip():
        raise ValueError("run_id 不能为空。")

    sources: list[EvidenceSource] = []
    skipped_missing_abstract = 0
    excluded_unconfirmed_imports = 0
    synthetic_count = 0

    if research is not None:
        version = f"pubmed-acquired:{research.generated_at.isoformat()}"
        for index, article in enumerate(research.articles):
            if not article.abstract or not article.abstract.strip():
                skipped_missing_abstract += 1
                continue
            synthetic = _is_synthetic(
                article.title,
                article.abstract,
                article.source_url,
            )
            synthetic_count += int(synthetic)
            sources.append(
                EvidenceSource(
                    source_id=f"PMID {article.pmid}",
                    source_type="pubmed_abstract",
                    title=article.title,
                    content=_document_content(article.title, article.abstract),
                    field_location=(
                        f"research.articles[{index}].title+abstract"
                    ),
                    version=version,
                    authorization_scope="public",
                    pmid=article.pmid,
                    doi=article.doi,
                    source_url=article.source_url,
                    metadata=_metadata(
                        source_origin="pubmed",
                        journal=article.journal,
                        year=article.year,
                        data_status=(
                            "synthetic_demo"
                            if synthetic
                            else "public_pubmed_abstract"
                        ),
                    ),
                )
            )

    imported_records = tuple(imported.records) if imported is not None else ()
    if imported_records and not include_user_authorized_imports:
        excluded_unconfirmed_imports = len(imported_records)
    elif imported_records:
        for index, record in enumerate(imported_records):
            if not record.abstract or not record.abstract.strip():
                skipped_missing_abstract += 1
                continue
            synthetic = _is_synthetic(
                record.title,
                record.abstract,
                record.source_url,
            )
            synthetic_count += int(synthetic)
            sources.append(
                EvidenceSource(
                    source_id=record.source_id,
                    source_type="user_imported_abstract",
                    title=record.title,
                    content=_document_content(record.title, record.abstract),
                    field_location=(
                        f"literature_import.records[{index}].title+abstract"
                    ),
                    version=f"workbench-run:{run_id}",
                    authorization_scope="user_authorized",
                    doi=record.doi,
                    source_url=record.source_url,
                    metadata=_metadata(
                        source_origin=record.source,
                        export_format=record.export_format,
                        journal=record.journal,
                        year=record.year,
                        data_status=(
                            "synthetic_demo"
                            if synthetic
                            else "user_authorized_import"
                        ),
                    ),
                )
            )

    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("本地 RAG 来源 ID 重复，不能安全建立索引。")
    ordered = tuple(sorted(sources, key=lambda source: source.source_id))
    if len(ordered) > MAX_WORKBENCH_RAG_SOURCES:
        raise ValueError(
            "本地 RAG 单次最多索引 "
            f"{MAX_WORKBENCH_RAG_SOURCES} 个来源；请缩小当前任务。"
        )
    total_characters = sum(len(source.content) for source in ordered)
    if total_characters > MAX_WORKBENCH_RAG_CHARACTERS:
        raise ValueError(
            "本地 RAG 单次最多索引 "
            f"{MAX_WORKBENCH_RAG_CHARACTERS} 个字符；请缩小当前任务。"
        )
    return PreparedWorkbenchRAGSources(
        sources=ordered,
        public_source_count=sum(
            source.authorization_scope == "public" for source in ordered
        ),
        user_authorized_source_count=sum(
            source.authorization_scope == "user_authorized"
            for source in ordered
        ),
        synthetic_source_count=synthetic_count,
        total_character_count=total_characters,
        skipped_missing_abstract_count=skipped_missing_abstract,
        excluded_unconfirmed_import_count=excluded_unconfirmed_imports,
    )


def source_set_sha256(sources: Sequence[EvidenceSource]) -> str:
    payload = [
        source.model_dump(mode="json")
        for source in sorted(sources, key=lambda item: item.source_id)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def literature_import_sha256(imported: LiteratureImportResult) -> str:
    """Hash the exact imported record set that requires local-use consent."""

    payload = [
        record.model_dump(mode="json")
        for record in sorted(imported.records, key=lambda item: item.source_id)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def open_workbench_rag_index(path: str | Path) -> LocalRAGIndex:
    """Open the built-in deterministic index; no custom Provider is accepted."""

    return LocalRAGIndex(
        path,
        DeterministicHashEmbeddingProvider(),
    )


def build_workbench_rag_index(
    path: str | Path,
    sources: Sequence[EvidenceSource],
) -> IndexManifest:
    if not sources:
        raise ValueError("没有可索引的已授权摘要。")
    return open_workbench_rag_index(path).build(sources)


def workbench_rag_index_is_current(
    path: str | Path,
    sources: Sequence[EvidenceSource],
) -> bool:
    """Verify persisted hashes, then compare the exact authorized source set."""

    index = open_workbench_rag_index(path)
    stored = index.sources()
    current = sorted(sources, key=lambda source: source.source_id)
    return stored == current


def search_workbench_rag(
    path: str | Path,
    query: str,
    *,
    mode: SearchMode = "keyword_only",
    limit: int = 3,
) -> WorkbenchRAGSearchOutcome:
    """Return untrusted candidate chunks or a fail-closed empty status."""

    clean_query = " ".join(query.split())
    if not clean_query:
        raise ValueError("本地检索问题不能为空。")
    if len(clean_query) > MAX_WORKBENCH_RAG_QUERY_CHARACTERS:
        raise ValueError(
            "本地检索问题最多允许 "
            f"{MAX_WORKBENCH_RAG_QUERY_CHARACTERS} 个字符。"
        )
    if mode not in _SEARCH_WEIGHTS:
        raise ValueError(f"不支持的本地检索模式：{mode}")
    keyword_weight, vector_weight = _SEARCH_WEIGHTS[mode]
    ranked = open_workbench_rag_index(path).search(
        clean_query,
        limit=limit,
        keyword_weight=keyword_weight,
        vector_weight=vector_weight,
    )
    if mode == "keyword_only":
        matched = [result for result in ranked if result.keyword_score > 0.0]
    else:
        matched = [result for result in ranked if result.score > 0.0]
    results = tuple(
        result.model_copy(update={"rank": rank})
        for rank, result in enumerate(matched, start=1)
    )
    return WorkbenchRAGSearchOutcome(
        retrieval_status=(
            "candidate_matches" if results else "insufficient_evidence"
        ),
        mode=mode,
        query_sha256=sha256(clean_query.encode("utf-8")).hexdigest(),
        results=results,
    )


__all__ = [
    "PreparedWorkbenchRAGSources",
    "MAX_WORKBENCH_RAG_CHARACTERS",
    "MAX_WORKBENCH_RAG_QUERY_CHARACTERS",
    "MAX_WORKBENCH_RAG_SOURCES",
    "SearchMode",
    "WORKBENCH_RAG_VERSION",
    "WorkbenchRAGSearchOutcome",
    "build_workbench_rag_index",
    "literature_import_sha256",
    "open_workbench_rag_index",
    "prepare_workbench_rag_sources",
    "search_workbench_rag",
    "source_set_sha256",
    "workbench_rag_index_is_current",
]
