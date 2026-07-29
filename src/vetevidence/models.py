from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class CASCategoryPartition(BaseModel):
    category: str
    zone: str


class JCRCategoryPartition(BaseModel):
    category: str
    quartile: str
    collection: str | None = None
    rank: str | None = None
    metric: str = "JIF"


class JournalRanking(BaseModel):
    journal_title: str
    matched_by: str | None = None
    cas_edition: str = "2025年3月升级版"
    cas_large_category: str | None = None
    cas_large_zone: str | None = None
    cas_categories: list[CASCategoryPartition] = Field(default_factory=list)
    cas_source_url: str | None = None
    jcr_edition: str = "2025-2026（JIF）"
    jcr_categories: list[JCRCategoryPartition] = Field(default_factory=list)
    jcr_source_url: str | None = None
    source_note: str | None = None
    data_status: str = "not_found"

    def cas_display(self) -> str:
        if self.data_status == "not_found" or not self.cas_large_zone:
            return f"未收录（中科院 {self.cas_edition}）"
        details = [
            f"大类：{self.cas_large_category or '未报告'} {self.cas_large_zone}"
        ]
        details.extend(
            f"小类：{item.category} {item.zone}"
            for item in self.cas_categories
        )
        return "；".join(details)

    def jcr_display(self) -> str:
        if self.data_status == "not_found" or not self.jcr_categories:
            return f"未收录（JCR {self.jcr_edition}）"
        details = []
        for item in self.jcr_categories:
            suffix = []
            if item.collection:
                suffix.append(item.collection)
            if item.rank:
                suffix.append(item.rank)
            metadata = f"（{item.metric} {'，'.join(suffix)}）" if suffix else ""
            details.append(f"{item.category} {item.quartile}{metadata}")
        return "；".join(details)


class PubMedArticle(BaseModel):
    """Normalized public metadata returned by PubMed."""

    pmid: str
    title: str
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    issn: str | None = None
    issn_type: str | None = None
    issn_linking: str | None = None
    year: int | None = None
    doi: str | None = None
    abstract: str | None = None
    source_url: str
    journal_ranking: JournalRanking | None = None


class EvidenceRecord(BaseModel):
    """Structured evidence extracted without filling unreported values."""

    pathogen: str | None = None
    disease_or_condition: str | None = None
    species: str | None = None
    model: str | None = None
    sample_size: int | None = None
    intervention: str | None = None
    drug: str | None = None
    dose: str | None = None
    route: str | None = None
    duration: str | None = None
    control: str | None = None
    outcomes: list[str] = Field(default_factory=list)
    mechanism: list[str] = Field(default_factory=list)
    key_result: str | None = None
    limitations: list[str] = Field(default_factory=list)
    journal: str | None = None
    issn: str | None = None
    cas_partition_edition: str | None = None
    cas_partition: str | None = None
    jcr_partition_edition: str | None = None
    jcr_partition: str | None = None
    journal_ranking_note: str | None = None
    pmid: str
    doi: str | None = None
    source_quote: str | None = None
    source_url: str
    extraction_method: str = "rules_v1"


class EvidenceCitation(BaseModel):
    pmid: str
    doi: str | None = None
    source_quote: str | None = None
    source_url: str


class CitedAnswer(BaseModel):
    question: str
    answer_markdown: str
    citations: list[EvidenceCitation] = Field(default_factory=list)


class ResearchResult(BaseModel):
    query: str
    articles: list[PubMedArticle] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    answer: CitedAnswer
    provider_name: str
    retrieval_request_count: int = 0
    estimated_llm_cost_usd: float = 0.0
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
