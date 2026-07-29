from __future__ import annotations

from pathlib import Path

from vetevidence.evaluation import (
    EvaluationCase,
    evaluate_research,
    evaluation_report_to_markdown,
    load_evaluation_cases,
)
from vetevidence.models import PubMedArticle
from vetevidence.journal_rankings import CsvJournalRankingProvider
from vetevidence.retrieval import run_research


def test_evaluation_dataset_has_at_least_twenty_cases() -> None:
    cases_path = Path("data/eval/cases.json")
    cases = load_evaluation_cases(cases_path)

    assert len(cases) >= 20
    assert len({case.id for case in cases}) == len(cases)


def test_evaluation_reports_passes_and_failures() -> None:
    article = PubMedArticle(
        pmid="TEST-1",
        title="Quercetin in a mouse model",
        abstract=(
            "Mice (n = 10) received quercetin. "
            "These findings indicated that the outcome was reduced."
        ),
        source_url="https://example.invalid/test-1",
    )
    result = run_research(
        "controlled",
        client=StubClient([article]),
        ranking_provider=CsvJournalRankingProvider.default(),
    )
    cases = [
        EvaluationCase(
            id="PASS",
            category="extraction",
            question="样本量是否为 10？",
            check="evidence_field_equals",
            pmid="TEST-1",
            field="sample_size",
            expected=10,
        ),
        EvaluationCase(
            id="FAIL",
            category="extraction",
            question="样本量是否为 99？",
            check="evidence_field_equals",
            pmid="TEST-1",
            field="sample_size",
            expected=99,
        ),
        EvaluationCase(
            id="BOUNDARY",
            category="robustness",
            question="无证据时是否拒绝回答？",
            check="insufficient_answer",
            expected=True,
        ),
        EvaluationCase(
            id="CITATION",
            category="citation",
            question="回答是否包含 PMID？",
            check="answer_contains",
            expected="TEST-1",
        ),
    ]

    report = evaluate_research(result, cases)

    assert report.summary.total == 4
    assert report.summary.passed == 3
    assert report.summary.failed == 1
    assert report.results[1].error_type == "expectation_mismatch"

    markdown = evaluation_report_to_markdown(report)
    assert "不是通用模型准确率" in markdown
    assert "`FAIL`" in markdown


class StubClient:
    def __init__(self, articles: list[PubMedArticle]) -> None:
        self.articles = articles
        self.request_count = 2

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[PubMedArticle]:
        return self.articles[:max_results]
