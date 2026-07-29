from __future__ import annotations

from vetevidence.experiment_analysis import analyze_fici_csv
from vetevidence.journal_rankings import CsvJournalRankingProvider
from vetevidence.literature_import import parse_literature_export
from vetevidence.models import PubMedArticle
from vetevidence.workbench import (
    EvidenceGap,
    ResearchQuestion,
    TaskStatus,
    build_task_event,
    decompose_research_question,
)
from vetevidence.workbench_pipeline import (
    EvidenceAssessment,
    ExperimentCondition,
    assess_evidence,
    build_decision_report,
    build_experiment_conditions,
    decision_report_to_markdown,
    generate_search_queries,
    report_content_sha256,
    run_multi_query_research,
)


ABSTRACT = (
    "This study evaluated quercetin in a mouse model of Streptococcus "
    "agalactiae-induced mastitis. Mice (n = 12) received 25 mg/kg quercetin "
    "or control for 24 h. Quercetin reduced bacterial growth and inhibited "
    "the NF-kB pathway. These findings indicated that quercetin mitigated "
    "infection."
)


def sample_article() -> PubMedArticle:
    return PubMedArticle(
        pmid="123",
        title="Quercetin activity against Streptococcus agalactiae",
        journal="Research in Veterinary Science",
        issn="1532-2661",
        year=2025,
        doi="10.1000/pubmed.1",
        abstract=ABSTRACT,
        source_url="https://pubmed.ncbi.nlm.nih.gov/123/",
    )


class StubClient:
    def __init__(self) -> None:
        self.request_count = 0
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[PubMedArticle]:
        self.request_count += 1
        self.queries.append(query)
        return [sample_article()]


def question() -> ResearchQuestion:
    return ResearchQuestion(
        id="rq-synergy",
        text=(
            "quercetin 与 amoxicillin 对 Streptococcus agalactiae "
            "是否具有协同作用"
        ),
        population="Streptococcus agalactiae",
        intervention="quercetin",
        comparator="amoxicillin",
        outcomes=["FICI", "生长抑制"],
    )


def test_query_plan_is_structured_and_bounded() -> None:
    plan = generate_search_queries(question())

    assert len(plan.queries) == 3
    assert plan.queries[0] == "quercetin Streptococcus agalactiae"
    assert plan.queries[1] == "amoxicillin Streptococcus agalactiae"
    assert "synergy OR interaction" in plan.queries[2]


def test_multi_query_research_deduplicates_pmids() -> None:
    client = StubClient()
    result = run_multi_query_research(
        question(),
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    assert client.request_count == 3
    assert len(result.research.articles) == 1
    assert result.research.articles[0].pmid == "123"
    assert result.research.retrieval_request_count == 3
    assert result.research.estimated_llm_cost_usd == 0


def test_imported_literature_enters_condition_matrix_without_fake_pmid() -> None:
    research = run_multi_query_research(
        question(),
        client=StubClient(),
        ranking_provider=CsvJournalRankingProvider.default(),
    ).research
    imported = parse_literature_export(
        """\
TY  - JOUR
TI  - Imported checkerboard study
PY  - 2024
AB  - Mice (n = 8) received 10 mg/kg quercetin for 12 h. Quercetin reduced growth.
UR  - https://example.org/imported
ER  -
"""
    )

    conditions = build_experiment_conditions(research, imported)
    imported_condition = conditions[-1]

    assert imported_condition.source_type == "user_import"
    assert imported_condition.pmid is None
    assert imported_condition.sample_size == 8
    assert imported_condition.dose == "10 mg/kg"
    assert imported_condition.source_quote == "Quercetin reduced growth."


def test_assessment_detects_explicit_conflict_and_missing_fields() -> None:
    conditions = [
        ExperimentCondition(
            source_id="PMID 1",
            source_type="pubmed",
            title="Down result",
            mechanisms=["NF-κB"],
            key_result="Treatment reduced NF-kB activity.",
            pmid="1",
            source_quote="Treatment reduced NF-kB activity.",
        ),
        ExperimentCondition(
            source_id="IMPORTED-2",
            source_type="user_import",
            title="Up result",
            mechanisms=["NF-κB"],
            key_result="Treatment increased NF-kB activity.",
            doi="10.1000/imported",
            source_quote="Treatment increased NF-kB activity.",
        ),
    ]

    assessment = assess_evidence(conditions)

    assert assessment.conflicts[0].topic == "NF-κB"
    assert len(assessment.conflicts[0].claims) == 2
    assert any(gap.topic == "剂量" for gap in assessment.gaps)


def test_decision_report_links_literature_and_csv_sources() -> None:
    research = run_multi_query_research(
        question(),
        client=StubClient(),
        ranking_provider=CsvJournalRankingProvider.default(),
    ).research
    conditions = build_experiment_conditions(research)
    analysis = analyze_fici_csv(
        """\
drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
8,2,4,1
"""
    )
    events = [
        build_task_event("run-1", TaskStatus.PENDING, "任务已创建"),
        build_task_event("run-1", TaskStatus.RUNNING, "正在分析"),
        build_task_event("run-1", TaskStatus.AWAITING_REVIEW, "等待人工复核"),
    ]

    report = build_decision_report(
        question(),
        conditions=conditions,
        analysis=analysis,
        task_events=events,
        hypotheses=decompose_research_question(question()),
    )
    markdown = decision_report_to_markdown(report)

    assert report.task_status.current_status == TaskStatus.AWAITING_REVIEW
    assert report.human_review.decision == "pending"
    assert any(item.id == "analysis-fici" for item in report.conclusions)
    analysis_conclusion = next(
        item for item in report.conclusions if item.id == "analysis-fici"
    )
    csv_reference = analysis_conclusion.evidence[0]
    assert csv_reference.source_type == "experiment_csv"
    assert csv_reference.source_quote is None
    assert len(csv_reference.input_sha256) == 64
    assert csv_reference.data_rows == [2]
    assert "(2.0/8.0)" in csv_reference.calculation
    assert "time-kill" in report.recommendation.statement
    assert "PMID 123" in markdown
    assert "FICI=0.5" in markdown
    assert "SHA-256" in markdown
    assert "科研内容 SHA-256" in markdown
    assert "### 建议依据" in markdown
    assert "空白相关证据" in markdown
    assert "科研决策报告" in markdown


def test_synthetic_demo_is_explicitly_excluded_or_labeled() -> None:
    demo_import = parse_literature_export(
        """\
TY  - JOUR
TI  - Demonstration record
DO  - 10.1000/demo
AB  - This synthetic export record must not be treated as scientific evidence.
ER  -
"""
    )
    conditions = build_experiment_conditions(None, demo_import)
    analysis = analyze_fici_csv(
        """\
drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo,data_status
8,2,4,1,synthetic_demo
"""
    )
    events = [
        build_task_event("run-demo", TaskStatus.AWAITING_REVIEW, "等待复核")
    ]

    report = build_decision_report(
        question(),
        conditions=conditions,
        analysis=analysis,
        task_events=events,
    )

    assert len(report.conclusions) == 1
    assert report.conclusions[0].id == "analysis-fici"
    assert "合成演示数据" in report.conclusions[0].statement
    assert "不可作为科研证据" in report.conclusions[0].statement
    assert "不得据此形成科研建议" in report.recommendation.statement


def test_invalid_partial_csv_cannot_support_report_conclusion() -> None:
    invalid_analysis = analyze_fici_csv(
        """\
drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
8,2,4,1
8,not-a-number,4,1
"""
    )
    events = [
        build_task_event("run-invalid", TaskStatus.AWAITING_REVIEW, "等待复核")
    ]

    try:
        build_decision_report(
            question(),
            conditions=[],
            analysis=invalid_analysis,
            task_events=events,
        )
    except ValueError as exc:
        assert "可追溯" in str(exc)
    else:
        raise AssertionError("invalid partial CSV must not support a report")


def test_invalid_analysis_is_recomputed_disclosed_and_cannot_poison_report() -> None:
    conditions = build_experiment_conditions(
        run_multi_query_research(
            question(),
            client=StubClient(),
            ranking_provider=CsvJournalRankingProvider.default(),
        ).research
    )
    invalid_analysis = analyze_fici_csv(
        """\
drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
8,2,4,1
8,not-a-number,4,1
"""
    )
    poisoned_assessment = EvidenceAssessment(
        gaps=[
            EvidenceGap(
                id="gap-poisoned",
                topic="伪造评估",
                missing_evidence="调用方伪造的旧评估。",
                impact="不得进入报告。",
                recommended_action="重新计算。",
            )
        ]
    )
    events = [
        build_task_event("run-invalid", TaskStatus.AWAITING_REVIEW, "等待复核")
    ]

    report = build_decision_report(
        question(),
        conditions=conditions,
        analysis=invalid_analysis,
        assessment=poisoned_assessment,
        task_events=events,
    )

    gap_ids = {gap.id for gap in report.evidence_gaps}
    assert "gap-invalid-analysis" in gap_ids
    assert "gap-poisoned" not in gap_ids
    assert not any(item.id == "analysis-fici" for item in report.conclusions)
    assert "实验 CSV 无效" in decision_report_to_markdown(report)


def test_report_revision_is_unique_but_scientific_content_hash_is_stable() -> None:
    conditions = build_experiment_conditions(
        run_multi_query_research(
            question(),
            client=StubClient(),
            ranking_provider=CsvJournalRankingProvider.default(),
        ).research
    )
    events = [
        build_task_event("run-revision", TaskStatus.AWAITING_REVIEW, "等待复核")
    ]

    first = build_decision_report(
        question(),
        conditions=conditions,
        task_events=events,
    )
    second = build_decision_report(
        question(),
        conditions=conditions,
        task_events=events,
    )

    assert first.id != second.id
    assert report_content_sha256(first) == report_content_sha256(second)
