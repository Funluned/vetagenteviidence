from __future__ import annotations

from vetevidence.experiment_analysis import analyze_fici_csv
from vetevidence.journal_rankings import CsvJournalRankingProvider
from vetevidence.literature_import import parse_literature_export
from vetevidence.models import PubMedArticle
from vetevidence.workbench import (
    EvidenceAdmissionStatus,
    EvidenceGap,
    LiteratureEvidenceGrade,
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
    qualify_literature_evidence,
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
DIRECT_ABSTRACT = (
    "Seventy-nine swine Pasteurella multocida isolates were tested with "
    "florfenicol and thiamphenicol. Florfenicol and thiamphenicol showed "
    "synergistic activity against Pasteurella multocida with FICI <= 0.5 "
    "in 24% of isolates and were confirmed by time-kill assays."
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


def direct_article() -> PubMedArticle:
    return PubMedArticle(
        pmid="31749775",
        title=(
            "Synergistic activity of florfenicol and thiamphenicol against "
            "Pasteurella multocida"
        ),
        journal="Frontiers in Microbiology",
        year=2019,
        doi="10.3389/fmicb.2019.02430",
        abstract=DIRECT_ABSTRACT,
        source_url="https://pubmed.ncbi.nlm.nih.gov/31749775/",
    )


def direct_question() -> ResearchQuestion:
    return ResearchQuestion(
        id="rq-direct-synergy",
        text=(
            "florfenicol 与 thiamphenicol 对 Pasteurella multocida "
            "是否存在协同抗菌作用"
        ),
        population="Pasteurella multocida",
        intervention="florfenicol",
        comparator="thiamphenicol",
        outcomes=["FICI", "time-kill"],
    )


class StubClient:
    def __init__(self) -> None:
        self.request_count = 0
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[PubMedArticle]:
        self.request_count += 1
        self.queries.append(query)
        return [sample_article()]


class RankedStubClient:
    def __init__(self, batches: list[list[PubMedArticle]]) -> None:
        self.batches = batches
        self.request_count = 0
        self.limits: list[int] = []

    def search(self, query: str, max_results: int = 5) -> list[PubMedArticle]:
        batch = self.batches[self.request_count]
        self.request_count += 1
        self.limits.append(max_results)
        return batch[:max_results]


def ranked_article(pmid: str) -> PubMedArticle:
    return sample_article().model_copy(
        update={
            "pmid": pmid,
            "title": f"Ranked article {pmid}",
            "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
    )


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


def test_relevance_rule_admits_only_explicit_direct_interaction_evidence() -> None:
    direct = qualify_literature_evidence(
        direct_question(),
        title=direct_article().title,
        abstract=direct_article().abstract,
    )
    contextual = qualify_literature_evidence(
        question(),
        title=sample_article().title,
        abstract=sample_article().abstract,
    )
    no_marker = qualify_literature_evidence(
        direct_question(),
        title="Florfenicol and thiamphenicol combination study",
        abstract=(
            "Pasteurella multocida isolates were exposed to florfenicol and "
            "thiamphenicol in a combination treatment."
        ),
    )
    method_only = qualify_literature_evidence(
        direct_question(),
        title="Checkerboard evaluation of an amphenicol combination",
        abstract=(
            "Checkerboard and time-kill assays were used to test florfenicol "
            "and thiamphenicol against Pasteurella multocida."
        ),
    )
    threshold_definition = qualify_literature_evidence(
        direct_question(),
        title="FICI interpretation methods",
        abstract=(
            "For florfenicol and thiamphenicol against Pasteurella multocida, "
            "FICI <= 0.5 was defined as synergy."
        ),
    )
    equivalent_threshold_definition = qualify_literature_evidence(
        direct_question(),
        title="Synergy interpretation methods",
        abstract=(
            "Synergistic activity of florfenicol and thiamphenicol against "
            "Pasteurella multocida was defined as FICI <= 0.5."
        ),
    )
    wrong_pathogen = qualify_literature_evidence(
        direct_question(),
        title="Synergy against Escherichia coli",
        abstract=(
            "Florfenicol and thiamphenicol showed synergistic activity with "
            "FICI 0.5 against Escherichia coli."
        ),
    )
    interaction_for_other_population = qualify_literature_evidence(
        direct_question(),
        title="Amphenicol activity survey",
        abstract=(
            "Florfenicol and thiamphenicol showed synergistic activity against "
            "Staphylococcus aureus. Pasteurella multocida isolates were also surveyed."
        ),
    )

    assert direct.grade is LiteratureEvidenceGrade.DIRECT_INTERACTION
    assert direct.supporting_quote is not None
    assert "synergistic" in direct.supporting_quote.casefold()
    assert contextual.grade is LiteratureEvidenceGrade.CONTEXTUAL
    assert no_marker.grade is LiteratureEvidenceGrade.CONTEXTUAL
    assert method_only.grade is LiteratureEvidenceGrade.CONTEXTUAL
    assert method_only.matched_population
    assert method_only.matched_intervention
    assert method_only.matched_comparator
    assert method_only.interaction_marker == "checkerboard"
    assert method_only.interaction_result_signal is None
    assert "明确交互结果" in "；".join(method_only.reasons)
    assert threshold_definition.grade is LiteratureEvidenceGrade.CONTEXTUAL
    assert threshold_definition.interaction_result_signal is None
    assert (
        equivalent_threshold_definition.grade
        is LiteratureEvidenceGrade.CONTEXTUAL
    )
    assert equivalent_threshold_definition.interaction_result_signal is None
    assert wrong_pathogen.grade is LiteratureEvidenceGrade.OUT_OF_SCOPE
    assert (
        interaction_for_other_population.grade
        is LiteratureEvidenceGrade.CONTEXTUAL
    )


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


def test_multi_query_fusion_prevents_first_query_from_filling_the_limit() -> None:
    client = RankedStubClient(
        [
            [ranked_article(f"1{index}") for index in range(1, 8)],
            [ranked_article(f"2{index}") for index in range(1, 9)],
            [],
        ]
    )

    result = run_multi_query_research(
        question(),
        max_results=8,
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    assert [article.pmid for article in result.research.articles] == [
        "11",
        "21",
        "12",
        "22",
        "13",
        "23",
        "14",
        "24",
    ]
    assert client.request_count == 3
    assert client.limits == [24, 24, 24]


def test_multi_query_duplicates_do_not_consume_a_querys_fair_turn() -> None:
    client = RankedStubClient(
        [
            [
                ranked_article("101"),
                ranked_article("102"),
                ranked_article("103"),
            ],
            [ranked_article("101"), ranked_article("201")],
            [ranked_article("101"), ranked_article("301")],
        ]
    )

    result = run_multi_query_research(
        question(),
        max_results=3,
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    assert [article.pmid for article in result.research.articles] == [
        "101",
        "201",
        "301",
    ]
    assert client.limits == [20, 20, 20]


def test_multi_query_empty_or_exhausted_queries_are_backfilled() -> None:
    client = RankedStubClient(
        [
            [ranked_article("101")],
            [ranked_article("101")],
            [
                ranked_article("301"),
                ranked_article("302"),
                ranked_article("303"),
            ],
        ]
    )

    result = run_multi_query_research(
        question(),
        max_results=4,
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    assert [article.pmid for article in result.research.articles] == [
        "101",
        "301",
        "302",
        "303",
    ]
    assert len({article.pmid for article in result.research.articles}) == 4


def test_direct_evidence_below_display_cutoff_is_still_retained() -> None:
    client = RankedStubClient(
        [
            [
                *[ranked_article(f"10{index}") for index in range(1, 9)],
                direct_article(),
            ],
            [],
            [],
        ]
    )

    result = run_multi_query_research(
        direct_question(),
        max_results=4,
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    )

    assert client.limits == [20, 20, 20]
    assert result.research.articles[0].pmid == "31749775"
    assert len(result.research.articles) == 4
    conditions = build_experiment_conditions(
        result.research,
        question=direct_question(),
    )
    assessment = assess_evidence(conditions, question=direct_question())
    assert assessment.evidence_admission.status is EvidenceAdmissionStatus.ADMITTED


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

    conditions = build_experiment_conditions(
        research,
        imported,
        question=question(),
    )
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
    conditions = build_experiment_conditions(research, question=question())
    analysis = analyze_fici_csv(
        """\
drug_a,drug_b,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
quercetin,amoxicillin,8,2,4,1
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
    assert (
        report.evidence_admission.status
        is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
    )
    assert "直接文献证据" in report.evidence_admission.reason
    assert "仅凭本次文献检索" in report.evidence_admission.reason
    assert {
        item.id for item in report.conclusions
    } >= {"literature-direct-evidence-insufficient", "analysis-fici"}
    direct_literature_gap = next(
        gap for gap in report.evidence_gaps if gap.id == "gap-direct-interaction"
    )
    assert "独立证据链" in direct_literature_gap.impact
    assert "不能判断或宣称存在协同作用" not in report.recommendation.statement
    analysis_conclusion = next(
        item for item in report.conclusions if item.id == "analysis-fici"
    )
    assert "synergy" in analysis_conclusion.statement
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
    assert "直接文献证据准入" in markdown
    assert "### 建议依据" in markdown
    assert "空白相关证据" in markdown
    assert "科研决策报告" in markdown


def test_report_states_insufficient_evidence_when_only_contextual_articles_exist() -> None:
    research = run_multi_query_research(
        question(),
        client=StubClient(),
        ranking_provider=CsvJournalRankingProvider.default(),
    ).research
    conditions = build_experiment_conditions(research, question=question())
    report = build_decision_report(
        question(),
        conditions=conditions,
        task_events=[
            build_task_event(
                "run-insufficient",
                TaskStatus.AWAITING_REVIEW,
                "等待复核",
            )
        ],
    )
    markdown = decision_report_to_markdown(report)

    assert (
        report.evidence_admission.status
        is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
    )
    assert report.evidence_admission.direct_source_ids == []
    assert report.evidence_admission.contextual_source_ids == ["PMID 123"]
    assert [item.id for item in report.conclusions] == [
        "literature-direct-evidence-insufficient"
    ]
    assert "不能判断或宣称存在协同作用" in report.recommendation.statement
    assert "不等于证明协同作用不存在" in report.conclusions[0].limitations[0]
    assert "gap-direct-interaction" in {
        gap.id for gap in report.evidence_gaps
    }
    assert "blocked_no_direct_evidence" in markdown


def test_direct_pubmed_evidence_is_admitted_with_verifiable_quote() -> None:
    client = RankedStubClient([[direct_article()], [], []])
    research = run_multi_query_research(
        direct_question(),
        client=client,
        ranking_provider=CsvJournalRankingProvider.default(),
    ).research
    conditions = build_experiment_conditions(
        research,
        question=direct_question(),
    )
    report = build_decision_report(
        direct_question(),
        conditions=conditions,
        task_events=[
            build_task_event(
                "run-direct",
                TaskStatus.AWAITING_REVIEW,
                "等待复核",
            )
        ],
    )

    assert (
        report.evidence_admission.status
        is EvidenceAdmissionStatus.ADMITTED
    )
    assert report.evidence_admission.direct_source_ids == ["PMID 31749775"]
    assert "literature-direct-evidence-insufficient" not in {
        item.id for item in report.conclusions
    }
    literature_claim = report.conclusions[0]
    assert "synergistic" in literature_claim.statement.casefold()
    assert literature_claim.evidence[0].pmid == "31749775"
    assert literature_claim.evidence[0].doi == "10.3389/fmicb.2019.02430"
    assert literature_claim.evidence[0].source_quote == literature_claim.statement


def test_fici_for_a_different_drug_pair_cannot_support_the_report() -> None:
    research = run_multi_query_research(
        question(),
        client=StubClient(),
        ranking_provider=CsvJournalRankingProvider.default(),
    ).research
    conditions = build_experiment_conditions(research, question=question())
    mismatched = analyze_fici_csv(
        """\
drug_a,drug_b,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
vancomycin,rifampicin,8,2,4,1
"""
    )
    report = build_decision_report(
        question(),
        conditions=conditions,
        analysis=mismatched,
        task_events=[
            build_task_event(
                "run-wrong-pair",
                TaskStatus.AWAITING_REVIEW,
                "等待复核",
            )
        ],
    )

    assert not any(item.id == "analysis-fici" for item in report.conclusions)
    assert "gap-fici-intervention-identity" in {
        gap.id for gap in report.evidence_gaps
    }
    assert "不能判断或宣称存在协同作用" in report.recommendation.statement


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
    conditions = build_experiment_conditions(
        None,
        demo_import,
        question=question(),
    )
    analysis = analyze_fici_csv(
        """\
drug_a,drug_b,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo,data_status
quercetin,amoxicillin,8,2,4,1,synthetic_demo
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

    analysis_conclusion = next(
        item for item in report.conclusions if item.id == "analysis-fici"
    )
    assert "合成演示数据" in analysis_conclusion.statement
    assert "不可作为科研证据" in analysis_conclusion.statement
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
        ).research,
        question=question(),
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
        ).research,
        question=question(),
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
