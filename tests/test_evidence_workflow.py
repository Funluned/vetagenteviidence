from __future__ import annotations

import csv
import io

from vetevidence.answering import build_cited_answer
from vetevidence.export import evidence_to_csv, research_to_markdown
from vetevidence.extraction import extract_evidence
from vetevidence.journal_rankings import CsvJournalRankingProvider
from vetevidence.models import PubMedArticle, ResearchResult
from vetevidence.retrieval import run_research


SAMPLE_ABSTRACT = (
    "This study evaluated quercetin in a mouse model of Streptococcus "
    "agalactiae-induced mastitis. Mice (n = 25) were randomized into five "
    "groups: control, GBS model, and QUE (25, 50, 100 mg/kg). QUE was "
    "administered intraperitoneally 24 h before mammary duct injection of GBS. "
    "The results demonstrated that QUE reduced IL-6 and inhibited the NF-κB "
    "pathway and NLRP3 inflammasome. These findings indicated that QUE "
    "mitigated mammary gland injury by inhibiting NF-κB/NLRP3 signaling and "
    "ferroptosis."
)


def sample_article() -> PubMedArticle:
    return PubMedArticle(
        pmid="42250334",
        title=(
            "Quercetin alleviates Streptococcus agalactiae-induced mastitis "
            "in mice"
        ),
        authors=["Yaying Jia", "Qi Sun"],
        journal="Research in Veterinary Science",
        issn="1532-2661",
        issn_type="Electronic",
        issn_linking="0034-5288",
        year=2026,
        doi="10.1016/j.rvsc.2026.106289",
        abstract=SAMPLE_ABSTRACT,
        source_url="https://pubmed.ncbi.nlm.nih.gov/42250334/",
    )


def test_rule_extraction_captures_reported_fields() -> None:
    evidence = extract_evidence(sample_article())

    assert evidence.pathogen == "Streptococcus agalactiae"
    assert evidence.disease_or_condition == "乳腺炎"
    assert evidence.species == "小鼠"
    assert evidence.sample_size == 25
    assert evidence.drug == "Quercetin"
    assert evidence.dose == "25, 50, 100 mg/kg"
    assert evidence.route == "腹腔注射"
    assert evidence.duration.startswith("24 h before")
    assert "control" in evidence.control
    assert evidence.mechanism == ["NF-κB", "NLRP3", "铁死亡"]
    assert evidence.key_result == evidence.source_quote
    assert evidence.pmid == "42250334"


def test_unreported_fields_remain_empty() -> None:
    article = PubMedArticle(
        pmid="1",
        title="Observational report",
        source_url="https://pubmed.ncbi.nlm.nih.gov/1/",
    )

    evidence = extract_evidence(article)

    assert evidence.pathogen is None
    assert evidence.sample_size is None
    assert evidence.dose is None
    assert evidence.outcomes == []
    assert evidence.source_quote is None
    assert any("未提供摘要" in item for item in evidence.limitations)


def test_answer_has_traceable_citation_and_limitations() -> None:
    evidence = extract_evidence(sample_article())
    answer = build_cited_answer("quercetin 是否影响相关机制？", [evidence])

    assert "PMID 42250334" in answer.answer_markdown
    assert "10.1016/j.rvsc.2026.106289" in answer.answer_markdown
    assert "NF-κB、NLRP3、铁死亡" in answer.answer_markdown
    assert "不能据此直接外推临床疗效" in answer.answer_markdown
    assert answer.citations[0].source_quote == evidence.source_quote


def test_csv_and_markdown_exports_preserve_evidence() -> None:
    result = run_research(
        "quercetin mastitis",
        client=StubPubMedClient([sample_article()]),
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    csv_text = evidence_to_csv(result.evidence)
    csv_row = next(csv.DictReader(io.StringIO(csv_text)))
    assert csv_row["pmid"] == "42250334"
    assert csv_row["dose"] == "25, 50, 100 mg/kg"
    assert csv_row["mechanism"] == "NF-κB；NLRP3；铁死亡"
    assert csv_row["cas_partition_edition"] == "2025年3月升级版"
    assert "3区" in csv_row["cas_partition"]
    assert csv_row["jcr_partition_edition"] == "2025-2026（JIF）"
    assert "Q2" in csv_row["jcr_partition"]

    markdown = research_to_markdown(result)
    assert "# VetEvidence AI 科研证据报告" in markdown
    assert "PMID 42250334" in markdown
    assert "中科院分区（2025年3月升级版）" in markdown
    assert "JCR 分区（2025-2026（JIF））" in markdown
    assert "来源原句" in markdown


def test_full_workflow_is_serializable_for_streamlit_session() -> None:
    result = run_research(
        "quercetin mastitis",
        client=StubPubMedClient([sample_article()]),
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    restored = ResearchResult.model_validate(result.model_dump(mode="json"))

    assert restored.provider_name == "rules_v1"
    assert restored.articles[0].pmid == "42250334"
    assert restored.articles[0].journal_ranking.cas_large_zone == "3区"
    assert restored.articles[0].journal_ranking.jcr_categories[0].quartile == "Q2"
    assert restored.evidence[0].sample_size == 25


class StubPubMedClient:
    def __init__(self, articles: list[PubMedArticle]) -> None:
        self.articles = articles

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[PubMedArticle]:
        return self.articles[:max_results]
