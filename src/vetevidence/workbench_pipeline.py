"""Sequential, auditable workflow for the VetResearch Workbench vertical slice."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.experiment_analysis import (
    ExperimentAnalysisResult,
    FICIAnalysisResult,
    GrowthCurveAnalysisResult,
)
from vetevidence.imported_extraction import (
    extract_imported_experimental_fields,
)
from vetevidence.journal_rankings import (
    JournalRankingProvider,
    LetPubJournalRankingProvider,
)
from vetevidence.literature_import import (
    ImportedLiterature,
    LiteratureImportResult,
)
from vetevidence.mechanism_prediction import (
    MechanismPredictionBundle,
    VinaTaskManifest,
    require_docking_scope,
    require_network_scope,
)
from vetevidence.models import (
    EvidenceRecord,
    PubMedArticle,
    ResearchResult,
)
from vetevidence.providers import EvidenceProvider, RuleBasedEvidenceProvider
from vetevidence.pubmed import PubMedClient
from vetevidence.workbench import (
    ConclusionConfidence,
    ConflictResolutionStatus,
    EvidenceAdmission,
    EvidenceAdmissionStatus,
    EvidenceConflict,
    EvidenceGap,
    EvidenceQualification,
    EvidenceReference,
    HumanReview,
    InteractionOutcome,
    LiteratureEvidenceGrade,
    ResearchDecisionReport,
    ResearchQuestion,
    TaskEvent,
    TestableHypothesis,
    TraceableConclusion,
    decompose_research_question,
    summarize_task_status,
)


class PipelineModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class QueryPlan(PipelineModel):
    question: ResearchQuestion
    queries: list[str] = Field(min_length=1, max_length=3)
    rule_id: str = "synergy-search-v1"


class MultiQueryResearchResult(PipelineModel):
    query_plan: QueryPlan
    research: ResearchResult


class LiteratureItem(PipelineModel):
    """Common literature view without requiring every source to have a PMID."""

    source_id: str
    source_type: Literal["pubmed", "user_import"]
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    pmid: str | None = None
    doi: str | None = None
    abstract: str | None = None
    source_url: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_pubmed(cls, article: PubMedArticle) -> LiteratureItem:
        warnings = []
        if not article.abstract:
            warnings.append("PubMed 未提供摘要，不能自动提取实验细节。")
        return cls(
            source_id=f"PMID {article.pmid}",
            source_type="pubmed",
            title=article.title,
            authors=article.authors,
            year=article.year,
            journal=article.journal,
            pmid=article.pmid,
            doi=article.doi,
            abstract=article.abstract,
            source_url=article.source_url,
            warnings=warnings,
        )

    @classmethod
    def from_import(cls, record: ImportedLiterature) -> LiteratureItem:
        return cls(
            source_id=record.source_id,
            source_type="user_import",
            title=record.title,
            authors=record.authors,
            year=record.year,
            journal=record.journal,
            doi=record.doi,
            abstract=record.abstract,
            source_url=record.source_url,
            warnings=record.warnings,
        )


class ExperimentCondition(PipelineModel):
    """Comparable experimental fields while preserving the source identity."""

    source_id: str
    source_type: Literal["pubmed", "user_import"]
    title: str
    abstract: str | None = None
    pathogen: str | None = None
    condition: str | None = None
    species: str | None = None
    model: str | None = None
    sample_size: int | None = None
    intervention: str | None = None
    dose: str | None = None
    route: str | None = None
    duration: str | None = None
    control: str | None = None
    metrics: list[str] = Field(default_factory=list)
    mechanisms: list[str] = Field(default_factory=list)
    key_result: str | None = None
    pmid: str | None = None
    doi: str | None = None
    source_url: str | None = None
    source_quote: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    qualification: EvidenceQualification = Field(
        default_factory=EvidenceQualification
    )

    def reference(self) -> EvidenceReference | None:
        if not any((self.pmid, self.doi, self.source_quote)):
            return None
        return EvidenceReference(
            pmid=self.pmid,
            doi=self.doi,
            source_quote=self.source_quote,
            source_url=self.source_url,
        )


class EvidenceAssessment(PipelineModel):
    consistencies: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    gaps: list[EvidenceGap] = Field(default_factory=list)
    evidence_admission: EvidenceAdmission = Field(default_factory=EvidenceAdmission)


_MISSING_FIELD_LABELS = {
    "species": "物种",
    "model": "模型",
    "sample_size": "样本量",
    "intervention": "干预",
    "dose": "剂量",
    "duration": "时间/时长",
    "control": "对照",
    "metrics": "结局指标",
}


def _question(value: ResearchQuestion | str) -> ResearchQuestion:
    if isinstance(value, ResearchQuestion):
        return value
    text = value.strip()
    if not text:
        raise ValueError("科研问题不能为空。")
    digest = sha256(text.encode("utf-8")).hexdigest()[:12]
    return ResearchQuestion(id=f"rq-{digest}", text=text)


_KNOWN_TERM_ALIASES = {
    "streptococcus agalactiae": (
        "s. agalactiae",
        "group b streptococcus",
        "group b streptococci",
        "group b strep",
        "gbs",
    ),
    "pasteurella multocida": ("p. multocida",),
}
_INTERACTION_MARKERS = (
    ("fractional inhibitory concentration index", "FICI"),
    ("fractional inhibitory concentration", "FIC"),
    ("checkerboard", "checkerboard"),
    ("time kill", "time-kill"),
    ("time-kill", "time-kill"),
    ("synerg", "synergy"),
    ("antagonis", "antagonism"),
    ("additive effect", "additive effect"),
    ("indifferent interaction", "indifferent interaction"),
    ("协同", "协同"),
    ("拮抗", "拮抗"),
    ("相加", "相加"),
    ("无关", "无关"),
    ("棋盘", "棋盘法"),
)
_INTERACTION_RESULT_PATTERNS = (
    (
        r"\b(?:show(?:ed|s)?|demonstrat(?:ed|es)?|exhibit(?:ed|s)?|"
        r"indicat(?:ed|es)?|confirm(?:ed|s)?|observ(?:ed|es)?|found)\b"
        r".{0,80}\b(?:synerg(?:y|ism|istic)|antagon(?:ism|istic)|"
        r"additive|indifferent|interaction)\b",
        "报告交互结果",
    ),
    (
        r"\b(?:synerg(?:y|ism|istic)|antagon(?:ism|istic)|additive|"
        r"indifferent|interaction)\b.{0,80}\b"
        r"(?:was|were|is|are)\s+(?:observed|found|confirmed|demonstrated)\b",
        "报告交互结果",
    ),
    (
        r"\b(?:fici|fic\s+index|fractional inhibitory concentration"
        r"(?:\s+index)?)\b\s*(?:was|were|of|=|:|≤|>=|<=|<|>)?\s*"
        r"(?:≤|>=|<=|<|>)?\s*\d+(?:\.\d+)?",
        "量化 FICI 结果",
    ),
    (r"(?:显示|表明|观察到|证实).{0,40}(?:协同|拮抗|相加|无关)", "报告交互结果"),
)
_METHOD_DEFINITION_PATTERN = (
    r"(?:\b(?:defined|classified|interpreted)\s+(?:as|by)\b|"
    r"\bconsidered(?:\s+\w+){0,5}\s+(?:as|when)\b|"
    r"\b(?:threshold|cutoff|criterion|criteria|calculation|formula)\b|"
    r"(?:定义为|界定为|判定为|归类为|解释为|阈值|临界值|"
    r"定义依据|视为|当.{0,30}时判定为|"
    r"判定标准|评价标准|计算公式|计算方法))"
)
_PURPOSE_HYPOTHESIS_PATTERN = (
    r"(?:\b(?:(?:aim|objective|purpose)s?\s*:|"
    r"(?:the\s+)?(?:aim|objective|purpose)s?\s+"
    r"(?:was|were|is|are)\s+to|aim(?:ed|s)?\s+to|sought\s+to|"
    r"(?:designed|undertaken)\s+to|we\s+hypothes(?:ized|ised)|"
    r"(?:the|our)\s+hypothesis)\b|"
    r"(?:旨在|研究目的|目的(?:是|为)|本研究拟|我们假设|研究假设))"
)
_METHOD_ONLY_PATTERN = (
    r"(?:\b(?:was|were|is|are)\s+(?:used|performed|conducted|applied)\s+to\b|"
    r"\b(?:we\s+)?(?:used|applied)\b.{0,50}\bto\s+"
    r"(?:test|evaluate|assess|determine|measure)\b|"
    r"\b(?:(?:this|the)\s+study|we)\s+"
    r"(?:evaluat(?:ed|es)|assess(?:ed|es)|investigat(?:ed|es)|"
    r"examin(?:ed|es))\b|"
    r"\b(?:assays?|methods?)\b.{0,40}\b(?:used|performed|conducted|applied)\b|"
    r"\b(?:methods?|materials\s+and\s+methods)\s*:|"
    r"(?:本研究|我们).{0,10}(?:评估了?|评价了?|考察了?|研究了?)|"
    r"(?:采用|使用|应用).{0,40}(?:检测|测定|评估|评价|判断|研究)"
    r".{0,40}(?:协同|拮抗|相加|无关|交互|联合|fici)|"
    r"(?:用于|以便).{0,30}(?:检测|测定|评估|评价|判断)"
    r".{0,30}(?:协同|拮抗|相加|无关|交互|联合|fici))"
)
_PREDICTIVE_OR_UNCERTAIN_PATTERN = (
    r"(?:\b(?:predict(?:s|ed|ing|ion|ions|ive|ively)?|"
    r"may|might|could|potential(?:ly)?|possibl(?:e|y)|"
    r"in[\s-]*silico|(?:molecular\s+)?docking|"
    r"computational\s+(?:simulat(?:ion|ions|ed|ing)|"
    r"model(?:s|ed|ing)?))\b|"
    r"(?:预测|预计|推测|可能|潜在|或许|也许|理论上|"
    r"分子对接|计算模拟|计算机模拟))"
)
_EXPLICIT_EXPERIMENTAL_METHOD_PATTERN = (
    r"(?:\b(?:checkerboard|time[\s-]*kill|in\s+vitro|in\s+vivo|"
    r"assays?|tests?|experiments?)\b|"
    r"(?:棋盘(?:法|实验|试验)?|时间杀菌(?:实验|试验)?|"
    r"体外(?:实验|试验)|体内(?:实验|试验)|实验|试验|测定))"
)
_ASSERTIVE_EXPERIMENTAL_RESULT_PATTERN = (
    r"(?:\b(?:show(?:ed|s)?|demonstrat(?:ed|es)?|exhibit(?:ed|s)?|"
    r"indicat(?:ed|es)?|confirm(?:ed|s)?|observ(?:ed|es)?|found|reported)\b"
    r".{0,100}\b(?:synerg(?:y|ism|istic)|antagon(?:ism|istic)|"
    r"additive|indifferent|interaction)\b|"
    r"\b(?:synerg(?:y|ism|istic)|antagon(?:ism|istic)|additive|"
    r"indifferent|interaction)\b.{0,100}\b"
    r"(?:was|were|is|are)\s+(?:observed|found|confirmed|demonstrated|"
    r"reported)\b|"
    r"\b(?:fici|fic\s+index|fractional inhibitory concentration"
    r"(?:\s+index)?)\b\s*(?:was|were|is|are|of|=|:|≤|>=|<=|<|>)\s*"
    r"(?:≤|>=|<=|<|>)?\s*\d+(?:\.\d+)?|"
    r"(?:显示|表明|观察到|证实|测得|发现|报告).{0,60}"
    r"(?:协同|拮抗|相加|无关|交互|fici))"
)
_RESULT_CLAUSE_SPLIT_PATTERN = (
    r"(?:[;；]|,\s*(?=(?:and|but|whereas|while|however)\b)|"
    r"，\s*(?=(?:并且|且|但|而|然而)))"
)
_COMBINATION_ANAPHORA_PATTERN = (
    r"(?:\b(?:the|this|that|such)\s+(?:drug\s+)?combination\b|"
    r"\bcombined\s+(?:treatment|therapy|regimen)\b|"
    r"(?:该|此|上述)(?:药物)?组合|(?:两药|二者|两者)(?:联用|联合)|"
    r"(?:该|此|上述)联合(?:用药|方案|处理))"
)
_SYNTHETIC_SOURCE_MARKERS = (
    "synthetic_demo",
    "synthetic export",
    "must not be treated as scientific evidence",
    "合成演示",
)
_EVIDENCE_GRADE_LABELS = {
    LiteratureEvidenceGrade.UNASSESSED: "未评估",
    LiteratureEvidenceGrade.OUT_OF_SCOPE: "主题不匹配",
    LiteratureEvidenceGrade.CONTEXTUAL: "间接背景",
    LiteratureEvidenceGrade.DIRECT_INTERACTION: "直接证据",
}


def _normalized_match_text(value: str | None) -> str:
    casefolded = (value or "").casefold()
    separated_scripts = re.sub(
        r"(?<=[a-z0-9])(?=[\u3400-\u9fff])|"
        r"(?<=[\u3400-\u9fff])(?=[a-z0-9])",
        " ",
        casefolded,
    )
    return re.sub(r"[^\w]+", " ", separated_scripts).strip()


def _term_aliases(value: str | None) -> list[str]:
    normalized = _normalized_match_text(value)
    if not normalized:
        return []
    aliases = [normalized]
    words = normalized.split()
    if len(words) >= 2 and words[0]:
        aliases.append(f"{words[0][0]} {words[1]}")
    aliases.extend(
        _normalized_match_text(alias)
        for alias in _KNOWN_TERM_ALIASES.get(normalized, ())
    )
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _matches_term(normalized_text: str, value: str | None) -> bool:
    padded = f" {normalized_text} "
    compact_text = normalized_text.replace(" ", "")
    for alias in _term_aliases(value):
        if re.search(r"[\u3400-\u9fff]", alias):
            compact_alias = alias.replace(" ", "")
            if len(compact_alias) >= 2 and compact_alias in compact_text:
                return True
        elif f" {alias} " in padded:
            return True
    return False


def _source_sentences(title: str, abstract: str | None) -> list[str]:
    combined = "\n".join(part for part in (title, abstract or "") if part)
    return [
        sentence.strip()
        for sentence in re.split(
            r"(?<=[.!?])\s+|(?<=[。！？])\s*|\n+",
            combined,
        )
        if sentence.strip()
    ]


def _interaction_marker(sentence: str) -> tuple[str, str] | None:
    normalized = _normalized_match_text(sentence)
    raw = sentence.casefold()
    for needle, label in _INTERACTION_MARKERS:
        normalized_needle = _normalized_match_text(needle)
        if normalized_needle in normalized or needle in raw:
            return label, sentence
    return None


def _interaction_result_signal(sentence: str) -> str | None:
    normalized = sentence.casefold()
    if any(
        re.search(pattern, normalized)
        for pattern in (
            _METHOD_DEFINITION_PATTERN,
            _PURPOSE_HYPOTHESIS_PATTERN,
            _METHOD_ONLY_PATTERN,
        )
    ):
        return None
    if re.search(_PREDICTIVE_OR_UNCERTAIN_PATTERN, normalized):
        explicit_experimental_result = False
        for clause in re.split(_RESULT_CLAUSE_SPLIT_PATTERN, normalized):
            if not re.search(_EXPLICIT_EXPERIMENTAL_METHOD_PATTERN, clause):
                continue
            for result_match in re.finditer(
                _ASSERTIVE_EXPERIMENTAL_RESULT_PATTERN,
                clause,
            ):
                context_through_result = clause[: result_match.end()]
                if not re.search(
                    _PREDICTIVE_OR_UNCERTAIN_PATTERN,
                    context_through_result,
                ):
                    explicit_experimental_result = True
                    break
            if explicit_experimental_result:
                break
        if not explicit_experimental_result:
            return None
    for pattern, label in _INTERACTION_RESULT_PATTERNS:
        if re.search(pattern, normalized):
            return label
    return None


def _interaction_outcome(sentence: str) -> InteractionOutcome:
    normalized = sentence.casefold()
    outcomes: list[InteractionOutcome] = []
    outcome_patterns = (
        (
            InteractionOutcome.SYNERGY,
            r"(?:\bsynerg(?:y|ism|istic)\b|协同)",
            r"(?:\b(?:no|not|without)\b.{0,20}\bsynerg|"
            r"(?:未|无).{0,12}协同)",
        ),
        (
            InteractionOutcome.ANTAGONISM,
            r"(?:\bantagon(?:ism|istic)\b|拮抗)",
            r"(?:\b(?:no|not|without)\b.{0,20}\bantagon|"
            r"(?:未|无).{0,12}拮抗)",
        ),
        (
            InteractionOutcome.ADDITIVE,
            r"(?:\badditive\b|相加)",
            r"(?:\b(?:no|not|without)\b.{0,20}\badditive|"
            r"(?:未|无).{0,12}相加)",
        ),
        (
            InteractionOutcome.INDIFFERENT,
            r"(?:\bindifferent\b|无关)",
            r"(?:\b(?:no|not|without)\b.{0,20}\bindifferent|"
            r"未.{0,12}无关)",
        ),
    )
    for outcome, positive_pattern, negative_pattern in outcome_patterns:
        if re.search(positive_pattern, normalized) and not re.search(
            negative_pattern,
            normalized,
        ):
            outcomes.append(outcome)
    if len(outcomes) == 1:
        return outcomes[0]
    if re.search(
        r"\b(?:fici|fic\s+index|fractional inhibitory concentration"
        r"(?:\s+index)?)\b.{0,20}\d",
        normalized,
    ):
        return InteractionOutcome.QUANTITATIVE_UNCLASSIFIED
    return InteractionOutcome.INTERACTION_UNCLASSIFIED


def _matches_all_question_entities(
    sentence: str,
    *,
    population: str | None,
    intervention: str | None,
    comparator: str | None,
) -> bool:
    normalized = _normalized_match_text(sentence)
    return (
        _matches_term(normalized, population)
        and _matches_term(normalized, intervention)
        and _matches_term(normalized, comparator)
    )


def _best_interaction_quote(
    title: str,
    abstract: str | None,
    *,
    population: str | None,
    intervention: str | None,
    comparator: str | None,
) -> tuple[
    str | None,
    str | None,
    InteractionOutcome | None,
    str | None,
]:
    candidates: list[
        tuple[int, int, str, str, InteractionOutcome, str]
    ] = []
    contextual_markers: list[tuple[int, int, str, str]] = []
    sentences = _source_sentences(title, abstract)
    for index, sentence in enumerate(sentences):
        hit = _interaction_marker(sentence)
        if hit is None:
            continue
        marker, result_sentence = hit
        same_sentence_binding = _matches_all_question_entities(
            result_sentence,
            population=population,
            intervention=intervention,
            comparator=comparator,
        )
        result_signal = _interaction_result_signal(result_sentence)
        if result_signal is None:
            if same_sentence_binding:
                contextual_markers.append(
                    (1, -index, marker, result_sentence)
                )
            continue
        adjacent_binding = False
        context_sentence: str | None = None
        if (
            not same_sentence_binding
            and index > 0
            and re.search(
                _COMBINATION_ANAPHORA_PATTERN,
                result_sentence.casefold(),
            )
            and _matches_all_question_entities(
                sentences[index - 1],
                population=population,
                intervention=intervention,
                comparator=comparator,
            )
        ):
            adjacent_binding = True
            context_sentence = sentences[index - 1]
        if not same_sentence_binding and not adjacent_binding:
            continue
        quote = (
            f"{context_sentence} {result_sentence}"
            if context_sentence
            else result_sentence
        )
        outcome = _interaction_outcome(result_sentence)
        normalized = _normalized_match_text(result_sentence)
        score = 1
        score += 8
        if same_sentence_binding:
            score += 2
        if any(term in normalized for term in ("synerg", "antagon", "additive")):
            score += 4
        if any(term in normalized for term in ("fici", "fractional inhibitory")):
            score += 2
        if re.search(r"\d|%|≤|<|>", quote):
            score += 1
        if quote != title:
            score += 1
        candidates.append(
            (score, -index, marker, result_signal, outcome, quote)
        )
    if not candidates:
        if contextual_markers:
            _, _, marker, quote = max(contextual_markers)
            return marker, None, None, quote
        return None, None, None, None
    _, _, marker, result_signal, outcome, quote = max(candidates)
    return marker, result_signal, outcome, quote


def qualify_literature_evidence(
    question: ResearchQuestion | str,
    *,
    title: str,
    abstract: str | None,
) -> EvidenceQualification:
    """Conservatively grade whether one title/abstract can answer an interaction question."""

    scoped = _question(question)
    source_text = "\n".join(part for part in (title, abstract or "") if part)
    normalized = _normalized_match_text(source_text)
    synthetic = any(
        marker in source_text.casefold() for marker in _SYNTHETIC_SOURCE_MARKERS
    )
    matched_population = _matches_term(normalized, scoped.population)
    matched_intervention = _matches_term(normalized, scoped.intervention)
    matched_comparator = _matches_term(normalized, scoped.comparator)
    marker, result_signal, interaction_outcome, quote = _best_interaction_quote(
        title,
        abstract,
        population=scoped.population,
        intervention=scoped.intervention,
        comparator=scoped.comparator,
    )

    missing_question_fields = [
        label
        for label, value in (
            ("研究对象", scoped.population),
            ("候选干预", scoped.intervention),
            ("联合药物", scoped.comparator),
        )
        if not value
    ]
    if synthetic:
        return EvidenceQualification(
            grade=LiteratureEvidenceGrade.OUT_OF_SCOPE,
            matched_population=matched_population,
            matched_intervention=matched_intervention,
            matched_comparator=matched_comparator,
            interaction_marker=marker,
            interaction_result_signal=result_signal,
            interaction_outcome=interaction_outcome,
            supporting_quote=quote,
            reasons=["来源明确标记为合成演示数据，不能进入科研结论。"],
        )
    if missing_question_fields:
        return EvidenceQualification(
            grade=LiteratureEvidenceGrade.OUT_OF_SCOPE,
            matched_population=matched_population,
            matched_intervention=matched_intervention,
            matched_comparator=matched_comparator,
            interaction_marker=marker,
            interaction_result_signal=result_signal,
            interaction_outcome=interaction_outcome,
            supporting_quote=quote,
            reasons=[
                "科研问题缺少" + "、".join(missing_question_fields) + "，无法判定直接证据。"
            ],
        )
    if (
        matched_population
        and matched_intervention
        and matched_comparator
        and marker
        and result_signal
        and interaction_outcome
        and quote
    ):
        return EvidenceQualification(
            grade=LiteratureEvidenceGrade.DIRECT_INTERACTION,
            matched_population=True,
            matched_intervention=True,
            matched_comparator=True,
            interaction_marker=marker,
            interaction_result_signal=result_signal,
            interaction_outcome=interaction_outcome,
            supporting_quote=quote,
            reasons=[
                f"同时命中研究对象、两种干预、交互指标“{marker}”和"
                f"{result_signal}。"
            ],
        )

    reasons = []
    if matched_population:
        reasons.append("命中研究对象")
    if matched_intervention:
        reasons.append("命中候选干预")
    if matched_comparator:
        reasons.append("命中联合药物")
    if marker:
        reasons.append(f"命中交互指标“{marker}”")
    if result_signal:
        reasons.append(f"命中{result_signal}")
    if matched_population and (matched_intervention or matched_comparator):
        missing = []
        if not matched_intervention:
            missing.append("候选干预")
        if not matched_comparator:
            missing.append("联合药物")
        if not marker:
            missing.append("明确交互指标")
        elif not result_signal:
            missing.append("明确交互结果")
        reasons.append("仍缺少" + "、".join(missing) + "，只能作为间接背景。")
        grade = LiteratureEvidenceGrade.CONTEXTUAL
    else:
        reasons.append("未同时覆盖研究对象和至少一种目标干预，不能回答当前问题。")
        grade = LiteratureEvidenceGrade.OUT_OF_SCOPE
    return EvidenceQualification(
        grade=grade,
        matched_population=matched_population,
        matched_intervention=matched_intervention,
        matched_comparator=matched_comparator,
        interaction_marker=marker,
        interaction_result_signal=result_signal,
        interaction_outcome=interaction_outcome,
        supporting_quote=quote,
        reasons=reasons,
    )


def _qualified_condition(
    question: ResearchQuestion,
    condition: ExperimentCondition,
) -> ExperimentCondition:
    qualification = qualify_literature_evidence(
        question,
        title=condition.title,
        abstract=condition.abstract or condition.source_quote or condition.key_result,
    )
    return condition.model_copy(update={"qualification": qualification})


def _evidence_admission(
    conditions: list[ExperimentCondition],
) -> EvidenceAdmission:
    direct = [
        condition.source_id
        for condition in conditions
        if condition.qualification.grade
        is LiteratureEvidenceGrade.DIRECT_INTERACTION
    ]
    contextual = [
        condition.source_id
        for condition in conditions
        if condition.qualification.grade is LiteratureEvidenceGrade.CONTEXTUAL
    ]
    excluded = [
        condition.source_id
        for condition in conditions
        if condition.qualification.grade
        in {
            LiteratureEvidenceGrade.OUT_OF_SCOPE,
            LiteratureEvidenceGrade.UNASSESSED,
        }
    ]
    if direct:
        return EvidenceAdmission(
            status=EvidenceAdmissionStatus.ADMITTED,
            direct_source_ids=direct,
            contextual_source_ids=contextual,
            excluded_source_ids=excluded,
            reason=(
                f"{len(direct)} 个文献来源同时覆盖研究对象、两种干预、"
                "明确交互指标和结果，可进入当前问题的直接文献证据结论。"
            ),
        )
    return EvidenceAdmission(
        status=EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE,
        contextual_source_ids=contextual,
        excluded_source_ids=excluded,
        reason=(
            "当前检索未发现同时覆盖研究对象、两种干预、明确交互指标和结果的"
            "直接文献证据；仅凭本次文献检索不能判断或宣称存在协同作用。"
        ),
    )


def generate_search_queries(
    question: ResearchQuestion | str,
    *,
    max_queries: int = 3,
) -> QueryPlan:
    """Create inspectable PubMed query variants for the synergy vertical slice."""

    if not 1 <= max_queries <= 3:
        raise ValueError("max_queries 必须在 1 到 3 之间。")
    scoped = _question(question)
    if scoped.intervention and scoped.population:
        intervention_query = f"{scoped.intervention} {scoped.population}"
        comparator_query = (
            f"{scoped.comparator} {scoped.population}"
            if scoped.comparator
            else f"{intervention_query} (dose OR mechanism)"
        )
        interaction_terms = " ".join(
            term
            for term in (
                scoped.intervention,
                scoped.comparator,
                scoped.population,
            )
            if term
        )
        candidates = [
            intervention_query,
            comparator_query,
            f"{interaction_terms} (synergy OR interaction OR combination)",
        ]
    else:
        candidates = [
            scoped.text,
            f"{scoped.text} (synergy OR interaction OR combination)",
            f"{scoped.text} (checkerboard OR FICI OR \"growth curve\")",
        ]
    unique = list(dict.fromkeys(query.strip() for query in candidates if query.strip()))
    return QueryPlan(question=scoped, queries=unique[:max_queries])


def _fuse_ranked_articles(
    ranked_results: list[list[PubMedArticle]],
    *,
    max_results: int,
) -> list[PubMedArticle]:
    """Fairly interleave per-query rankings while de-duplicating PMIDs."""

    iterators = [iter(articles) for articles in ranked_results]
    selected: dict[str, PubMedArticle] = {}
    while len(selected) < max_results:
        progressed = False
        for iterator in iterators:
            for article in iterator:
                if article.pmid in selected:
                    continue
                selected[article.pmid] = article
                progressed = True
                break
            if len(selected) == max_results:
                break
        if not progressed:
            break
    return list(selected.values())


def _prioritize_question_evidence(
    question: ResearchQuestion,
    articles: list[PubMedArticle],
    *,
    max_results: int,
) -> list[PubMedArticle]:
    """Keep fair retrieval order within each question-specific evidence grade."""

    priority = {
        LiteratureEvidenceGrade.DIRECT_INTERACTION: 0,
        LiteratureEvidenceGrade.CONTEXTUAL: 1,
        LiteratureEvidenceGrade.OUT_OF_SCOPE: 2,
        LiteratureEvidenceGrade.UNASSESSED: 3,
    }
    qualified = [
        (
            priority[
                qualify_literature_evidence(
                    question,
                    title=article.title,
                    abstract=article.abstract,
                ).grade
            ],
            index,
            article,
        )
        for index, article in enumerate(articles)
    ]
    qualified.sort(key=lambda item: (item[0], item[1]))
    return [article for _, _, article in qualified[:max_results]]


def run_multi_query_research(
    question: ResearchQuestion | str,
    *,
    max_results: int = 8,
    max_queries: int = 3,
    client: PubMedClient | None = None,
    provider: EvidenceProvider | None = None,
    ranking_provider: JournalRankingProvider | None = None,
) -> MultiQueryResearchResult:
    """Retrieve broadly, fairly de-duplicate, then retain question-relevant evidence."""

    if max_results < 1:
        raise ValueError("max_results 必须大于 0。")
    plan = generate_search_queries(question, max_queries=max_queries)
    active_client = client or PubMedClient()
    active_provider = provider or RuleBasedEvidenceProvider()
    active_ranking = ranking_provider or LetPubJournalRankingProvider.default()
    owns_client = client is None
    owns_ranking = ranking_provider is None

    try:
        candidate_limit = min(max(max_results * 3, 20), 100)
        ranked_results = [
            active_client.search(query, max_results=candidate_limit)
            for query in plan.queries
        ]
        fused_candidates = _fuse_ranked_articles(
            ranked_results,
            max_results=sum(len(batch) for batch in ranked_results),
        )
        raw_articles = _prioritize_question_evidence(
            plan.question,
            fused_candidates,
            max_results=max_results,
        )
        rankings = active_ranking.lookup_many(raw_articles)
        articles = [
            article.model_copy(update={"journal_ranking": ranking})
            for article, ranking in zip(raw_articles, rankings, strict=True)
        ]
        evidence = [active_provider.extract(article) for article in articles]
        answer_evidence = [
            record
            for article, record in zip(articles, evidence, strict=True)
            if qualify_literature_evidence(
                plan.question,
                title=article.title,
                abstract=article.abstract,
            ).grade
            in {
                LiteratureEvidenceGrade.DIRECT_INTERACTION,
                LiteratureEvidenceGrade.CONTEXTUAL,
            }
        ]
        research = ResearchResult(
            query=plan.question.text,
            articles=articles,
            evidence=evidence,
            answer=active_provider.answer(
                plan.question.text,
                answer_evidence,
            ),
            provider_name=active_provider.name,
            retrieval_request_count=getattr(active_client, "request_count", 0),
            estimated_llm_cost_usd=0.0,
        )
        return MultiQueryResearchResult(query_plan=plan, research=research)
    finally:
        if owns_client:
            active_client.close()
        if owns_ranking:
            active_ranking.close()


def literature_items(
    articles: list[PubMedArticle],
    imported: LiteratureImportResult | None = None,
) -> list[LiteratureItem]:
    items = [LiteratureItem.from_pubmed(article) for article in articles]
    items.extend(
        LiteratureItem.from_import(record)
        for record in (imported.records if imported else [])
    )
    return items


def _missing_fields(condition: ExperimentCondition) -> list[str]:
    missing = []
    for field_name in _MISSING_FIELD_LABELS:
        value = getattr(condition, field_name)
        if value is None or value == "" or value == []:
            missing.append(field_name)
    return missing


def build_experiment_conditions(
    research: ResearchResult | None,
    imported: LiteratureImportResult | None = None,
    *,
    question: ResearchQuestion | str | None = None,
) -> list[ExperimentCondition]:
    """Build one comparison row per source without inventing missing fields."""

    scoped_question = _question(question) if question is not None else None
    articles = {
        article.pmid: article for article in (research.articles if research else [])
    }
    conditions: list[ExperimentCondition] = []
    for evidence in research.evidence if research else []:
        article = articles[evidence.pmid]
        condition = ExperimentCondition(
            source_id=f"PMID {evidence.pmid}",
            source_type="pubmed",
            title=article.title,
            abstract=article.abstract,
            pathogen=evidence.pathogen,
            condition=evidence.disease_or_condition,
            species=evidence.species,
            model=evidence.model,
            sample_size=evidence.sample_size,
            intervention=evidence.intervention,
            dose=evidence.dose,
            route=evidence.route,
            duration=evidence.duration,
            control=evidence.control,
            metrics=evidence.outcomes,
            mechanisms=evidence.mechanism,
            key_result=evidence.key_result,
            pmid=evidence.pmid,
            doi=evidence.doi,
            source_url=evidence.source_url,
            source_quote=evidence.source_quote,
            qualification=(
                qualify_literature_evidence(
                    scoped_question,
                    title=article.title,
                    abstract=article.abstract,
                )
                if scoped_question is not None
                else EvidenceQualification()
            ),
        )
        conditions.append(
            condition.model_copy(update={"missing_fields": _missing_fields(condition)})
        )

    for record in imported.records if imported else []:
        fields = extract_imported_experimental_fields(
            record.title,
            record.abstract,
        )
        condition = ExperimentCondition(
            source_id=record.source_id,
            source_type="user_import",
            title=record.title,
            abstract=record.abstract,
            pathogen=fields.pathogen,
            condition=fields.disease_or_condition,
            species=fields.species,
            model=fields.model,
            sample_size=fields.sample_size,
            intervention=fields.intervention,
            dose=fields.dose,
            route=fields.route,
            duration=fields.duration,
            control=fields.control,
            metrics=fields.outcomes,
            mechanisms=fields.mechanism,
            key_result=fields.key_result,
            doi=record.doi,
            source_url=record.source_url,
            source_quote=fields.source_quote,
            qualification=(
                qualify_literature_evidence(
                    scoped_question,
                    title=record.title,
                    abstract=record.abstract,
                )
                if scoped_question is not None
                else EvidenceQualification()
            ),
        )
        conditions.append(
            condition.model_copy(update={"missing_fields": _missing_fields(condition)})
        )
    return conditions


def experiment_condition_rows(
    conditions: list[ExperimentCondition],
) -> list[dict[str, object]]:
    return [
        {
            "来源": condition.source_id,
            "来源类型": "PubMed" if condition.source_type == "pubmed" else "用户导入",
            "证据等级": _EVIDENCE_GRADE_LABELS[condition.qualification.grade],
            "准入理由": "；".join(condition.qualification.reasons),
            "直接证据原句": condition.qualification.supporting_quote or "",
            "物种": condition.species or "",
            "模型": condition.model or "",
            "样本量": condition.sample_size,
            "干预": condition.intervention or "",
            "剂量": condition.dose or "",
            "时间/时长": condition.duration or "",
            "对照": condition.control or "",
            "结局指标": "；".join(condition.metrics),
            "机制": "；".join(condition.mechanisms),
            "缺失字段": "；".join(
                _MISSING_FIELD_LABELS[field_name]
                for field_name in condition.missing_fields
            ),
        }
        for condition in conditions
    ]


def _direction(text: str | None) -> Literal["up", "down", "no_effect", "unknown"]:
    normalized = (text or "").casefold()
    if re.search(r"\b(no|not)\s+(significant\s+)?(effect|change|difference)", normalized):
        return "no_effect"
    if any(
        marker in normalized
        for marker in (
            "reduced",
            "decreased",
            "inhibited",
            "alleviated",
            "mitigated",
            "降低",
            "减少",
            "抑制",
        )
    ):
        return "down"
    if any(
        marker in normalized
        for marker in (
            "increased",
            "enhanced",
            "promoted",
            "upregulated",
            "升高",
            "增加",
            "促进",
        )
    ):
        return "up"
    return "unknown"


def _claim_from_condition(
    condition: ExperimentCondition,
    *,
    confidence: ConclusionConfidence = ConclusionConfidence.LOW,
) -> TraceableConclusion | None:
    reference = condition.reference()
    statement = (
        condition.qualification.supporting_quote
        if condition.qualification.grade
        is LiteratureEvidenceGrade.DIRECT_INTERACTION
        else condition.key_result
    )
    if reference is None or not statement:
        return None
    if (
        condition.qualification.grade
        is LiteratureEvidenceGrade.DIRECT_INTERACTION
        and condition.qualification.supporting_quote
    ):
        reference = reference.model_copy(
            update={"source_quote": condition.qualification.supporting_quote}
        )
    if (
        condition.source_type == "user_import"
        and "must not be treated as scientific evidence" in statement.casefold()
    ):
        return None
    digest = sha256(
        f"{condition.source_id}|{statement}".encode("utf-8")
    ).hexdigest()[:12]
    return TraceableConclusion(
        id=f"claim-{digest}",
        statement=statement,
        confidence=confidence,
        evidence=[reference],
        limitations=["仅依据当前可见摘要或导出片段，尚未完成全文人工核查。"],
    )


def assess_evidence(
    conditions: list[ExperimentCondition],
    analysis: ExperimentAnalysisResult | None = None,
    *,
    question: ResearchQuestion | str | None = None,
) -> EvidenceAssessment:
    """Apply question admission, detect explicit conflicts and report evidence gaps."""

    consistencies: list[str] = []
    conflicts: list[EvidenceConflict] = []
    gaps: list[EvidenceGap] = []
    scoped_question: ResearchQuestion | None = None
    if question is not None:
        scoped_question = _question(question)
        qualified_conditions = [
            _qualified_condition(scoped_question, condition)
            for condition in conditions
        ]
        admission = _evidence_admission(qualified_conditions)
        working_conditions = [
            condition
            for condition in qualified_conditions
            if condition.qualification.grade
            in {
                LiteratureEvidenceGrade.DIRECT_INTERACTION,
                LiteratureEvidenceGrade.CONTEXTUAL,
            }
        ]
        if (
            admission.status
            is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
        ):
            references = [
                reference
                for condition in qualified_conditions
                if condition.qualification.grade
                is LiteratureEvidenceGrade.CONTEXTUAL
                if (reference := condition.reference()) is not None
            ]
            gaps.append(
                EvidenceGap(
                    id="gap-direct-interaction",
                    topic="直接文献协同证据",
                    missing_evidence=admission.reason,
                    impact=(
                        "单药、背景或无关文献不能支持两种干预存在协同作用的"
                        "文献结论；匹配的实验数据如有，将使用独立证据链呈现。"
                    ),
                    recommended_action=(
                        "优化联合检索，并用 checkerboard/FICI 与 time-kill "
                        "等正交实验验证。"
                    ),
                    related_evidence=references,
                )
            )
    else:
        admission = EvidenceAdmission()
        working_conditions = conditions

    if (
        scoped_question is not None
        and isinstance(analysis, FICIAnalysisResult)
        and analysis.valid
        and not _fici_analysis_matches_question(scoped_question, analysis)
    ):
        gaps.append(
            EvidenceGap(
                id="gap-fici-intervention-identity",
                topic="FICI 科研问题范围",
                missing_evidence=(
                    "FICI CSV 的 drug_a、drug_b 或 population_or_strain "
                    "未在每个有效数据行中明确匹配当前科研问题，分析未纳入结论。"
                ),
                impact="无法确认该 FICI 结果回答的是当前药物组合和病原体/菌株。",
                recommended_action=(
                    "在 CSV 中填写当前两种干预及病原体/菌株名称后重新分析。"
                ),
            )
        )
        analysis = None
    if (
        scoped_question is not None
        and isinstance(analysis, GrowthCurveAnalysisResult)
        and analysis.valid
        and not _growth_curve_analysis_matches_question(
            scoped_question,
            analysis,
        )
    ):
        gaps.append(
            EvidenceGap(
                id="gap-growth-curve-scope-identity",
                topic="生长曲线科研问题范围",
                missing_evidence=(
                    "生长曲线 CSV 的 population_or_strain、intervention 或 "
                    "comparator 未在每个有效数据行中匹配当前科研问题，"
                    "分析未纳入结论。"
                ),
                impact="无法确认该生长曲线回答的是当前药物组合和病原体/菌株。",
                recommended_action=(
                    "在 CSV 中填写当前两种干预及病原体/菌株名称后重新分析。"
                ),
            )
        )
        analysis = None

    if analysis is not None and not analysis.valid:
        error_summary = "；".join(analysis.errors) or "实验 CSV 未通过完整性校验"
        gaps.append(
            EvidenceGap(
                id="gap-invalid-analysis",
                topic="实验 CSV 校验",
                missing_evidence=f"实验 CSV 无效，未纳入结论：{error_summary}",
                impact="不能使用部分有效行或旧分析结果形成实验结论与建议。",
                recommended_action="修正全部校验错误后重新上传并生成报告。",
            )
        )
        analysis = None

    if scoped_question is not None:
        direct_interaction_conditions = [
            condition
            for condition in working_conditions
            if condition.qualification.grade
            is LiteratureEvidenceGrade.DIRECT_INTERACTION
        ]
        by_interaction_outcome: dict[
            InteractionOutcome,
            list[ExperimentCondition],
        ] = {}
        for condition in direct_interaction_conditions:
            outcome = condition.qualification.interaction_outcome
            if outcome is not None:
                by_interaction_outcome.setdefault(outcome, []).append(condition)
        if (
            InteractionOutcome.SYNERGY in by_interaction_outcome
            and InteractionOutcome.ANTAGONISM in by_interaction_outcome
        ):
            conflicting_conditions = [
                *by_interaction_outcome[InteractionOutcome.SYNERGY],
                *by_interaction_outcome[InteractionOutcome.ANTAGONISM],
            ]
            claims = [
                claim
                for condition in conflicting_conditions
                if (claim := _claim_from_condition(condition)) is not None
            ]
            if len(claims) >= 2:
                topic = (
                    f"{scoped_question.intervention} + "
                    f"{scoped_question.comparator} / "
                    f"{scoped_question.population}"
                )
                conflicts.append(
                    EvidenceConflict(
                        id=(
                            "conflict-literature-interaction-"
                            f"{sha256(topic.encode('utf-8')).hexdigest()[:10]}"
                        ),
                        topic=topic,
                        description=(
                            "同一科研问题的直接文献分别报告协同与拮抗结果。"
                        ),
                        claims=claims,
                        impact=(
                            "交互方向相反，不能直接合并为单一结论；需核对菌株、"
                            "剂量、时间、方法阈值和实验质量差异。"
                        ),
                        resolution_status=ConflictResolutionStatus.OPEN,
                    )
                )

    grouped: dict[str, list[ExperimentCondition]] = {}
    for condition in working_conditions:
        topics = condition.mechanisms or (
            [condition.pathogen] if condition.pathogen else ["主要效应"]
        )
        for topic in topics:
            grouped.setdefault(topic, []).append(condition)

    for topic, members in grouped.items():
        by_direction: dict[str, list[ExperimentCondition]] = {}
        for member in members:
            direction = _direction(member.key_result)
            if direction != "unknown":
                by_direction.setdefault(direction, []).append(member)
        supported_directions = [
            direction for direction, rows in by_direction.items() if rows
        ]
        if len(supported_directions) == 1 and len(by_direction[supported_directions[0]]) >= 2:
            consistencies.append(
                f"{topic}：{len(by_direction[supported_directions[0]])} 个来源的"
                "摘要级效应方向一致。"
            )
        if (
            ("up" in by_direction and "down" in by_direction)
            or ("no_effect" in by_direction and len(supported_directions) > 1)
        ):
            claims = [
                claim
                for member in members
                if (claim := _claim_from_condition(member)) is not None
                and _direction(member.key_result) != "unknown"
            ]
            if len(claims) >= 2:
                conflicts.append(
                    EvidenceConflict(
                        id=f"conflict-{sha256(topic.encode('utf-8')).hexdigest()[:10]}",
                        topic=topic,
                        description=f"同一主题“{topic}”出现方向不一致的摘要级结果。",
                        claims=claims,
                        impact="直接合并结论可能掩盖模型、剂量或时间条件差异。",
                        resolution_status=ConflictResolutionStatus.OPEN,
                    )
                )

    for field_name, label in _MISSING_FIELD_LABELS.items():
        missing = [
            condition
            for condition in working_conditions
            if (
                (value := getattr(condition, field_name)) is None
                or value == ""
                or value == []
            )
        ]
        if not missing:
            continue
        references = [
            reference
            for condition in missing
            if (reference := condition.reference()) is not None
        ]
        gaps.append(
            EvidenceGap(
                id=f"gap-{field_name}",
                topic=label,
                missing_evidence=(
                    f"{len(missing)}/{len(working_conditions)} 个相关来源未报告{label}。"
                ),
                impact=f"无法充分比较不同研究的{label}条件。",
                recommended_action=f"人工核对全文或原始记录并补录{label}。",
                related_evidence=references,
            )
        )

    if isinstance(analysis, FICIAnalysisResult):
        classifications = {
            row.classification
            for row in analysis.rows
            if row.valid and row.classification
        }
        if len(classifications) == 1 and analysis.valid_row_count >= 2:
            consistencies.append(
                f"FICI：{analysis.valid_row_count} 个有效数据行均分类为"
                f" {next(iter(classifications))}。"
            )
        if "synergy" in classifications and "antagonism" in classifications:
            claims = []
            for row in analysis.rows:
                if row.valid and row.classification in {"synergy", "antagonism"}:
                    calculation = (
                        f"CSV row {row.row_number}: "
                        f"{row.drug_a} + {row.drug_b} / "
                        f"{row.population_or_strain}; "
                        f"({row.drug_a_mic_combo}/{row.drug_a_mic_alone}) + "
                        f"({row.drug_b_mic_combo}/{row.drug_b_mic_alone}) = "
                        f"FICI={row.fici:.4g}; classification={row.classification}"
                    )
                    claims.append(
                        TraceableConclusion(
                            id=f"fici-row-{row.row_number}",
                            statement=calculation,
                            confidence=ConclusionConfidence.MODERATE,
                            evidence=[
                                EvidenceReference(
                                    source_id=(
                                        f"sha256:{analysis.input_sha256}"
                                    ),
                                    source_type="experiment_csv",
                                    source_name=analysis.source_name,
                                    input_sha256=analysis.input_sha256,
                                    data_rows=[row.row_number],
                                    calculation=calculation,
                                )
                            ],
                            limitations=["这是描述性 FICI 分类，未包含独立重复验证。"],
                        )
                    )
            conflicts.append(
                EvidenceConflict(
                    id="conflict-fici",
                    topic="FICI",
                    description="不同 CSV 数据行同时出现协同与拮抗分类。",
                    claims=claims,
                    impact="需要先排查菌株、批次、剂量和录入差异再形成判断。",
                    resolution_status=ConflictResolutionStatus.OPEN,
                )
            )

    return EvidenceAssessment(
        consistencies=consistencies,
        conflicts=conflicts,
        gaps=gaps,
        evidence_admission=admission,
    )


def _is_synthetic_analysis(analysis: ExperimentAnalysisResult) -> bool:
    return any(
        (row.raw_row.get("data_status") or "").casefold() == "synthetic_demo"
        for row in analysis.rows
    )


def _fici_analysis_matches_question(
    question: ResearchQuestion,
    analysis: FICIAnalysisResult,
) -> bool:
    expected_drugs = {
        _normalized_match_text(question.intervention),
        _normalized_match_text(question.comparator),
    }
    expected_population = _normalized_match_text(question.population)
    if (
        "" in expected_drugs
        or len(expected_drugs) != 2
        or not expected_population
    ):
        return False
    valid_rows = [row for row in analysis.rows if row.valid]
    if not valid_rows:
        return False
    for row in valid_rows:
        actual_drugs = {
            _normalized_match_text(row.drug_a),
            _normalized_match_text(row.drug_b),
        }
        actual_population = _normalized_match_text(row.population_or_strain)
        if (
            actual_drugs != expected_drugs
            or actual_population != expected_population
        ):
            return False
    return True


def _growth_curve_analysis_matches_question(
    question: ResearchQuestion,
    analysis: GrowthCurveAnalysisResult,
) -> bool:
    expected_drugs = {
        _normalized_match_text(question.intervention),
        _normalized_match_text(question.comparator),
    }
    expected_population = _normalized_match_text(question.population)
    if (
        "" in expected_drugs
        or len(expected_drugs) != 2
        or not expected_population
    ):
        return False
    valid_rows = [row for row in analysis.rows if row.valid]
    if not valid_rows:
        return False
    return all(
        {
            _normalized_match_text(row.intervention),
            _normalized_match_text(row.comparator),
        }
        == expected_drugs
        and _normalized_match_text(row.population_or_strain)
        == expected_population
        for row in valid_rows
    )


def experiment_analysis_matches_question(
    question: ResearchQuestion,
    analysis: ExperimentAnalysisResult,
) -> bool:
    """Return whether a valid CSV analysis belongs to the current question."""

    if not analysis.valid:
        return False
    if isinstance(analysis, FICIAnalysisResult):
        return _fici_analysis_matches_question(question, analysis)
    if isinstance(analysis, GrowthCurveAnalysisResult):
        return _growth_curve_analysis_matches_question(question, analysis)
    return False


def _analysis_conclusion(
    analysis: ExperimentAnalysisResult,
) -> TraceableConclusion | None:
    is_synthetic_demo = _is_synthetic_analysis(analysis)
    demo_prefix = (
        "合成演示数据（不可作为科研证据）："
        if is_synthetic_demo
        else ""
    )
    demo_limitation = (
        ["当前输入是合成演示数据，只用于验证计算流程。"]
        if is_synthetic_demo
        else []
    )
    if isinstance(analysis, FICIAnalysisResult):
        rows = [row for row in analysis.rows if row.valid and row.fici is not None]
        if not rows:
            return None
        counts: dict[str, int] = {}
        references = []
        for row in rows:
            classification = row.classification or "unclassified"
            counts[classification] = counts.get(classification, 0) + 1
            calculation = (
                f"CSV row {row.row_number}: "
                f"{row.drug_a} + {row.drug_b} / "
                f"{row.population_or_strain}; "
                f"({row.drug_a_mic_combo}/{row.drug_a_mic_alone}) + "
                f"({row.drug_b_mic_combo}/{row.drug_b_mic_alone}) = "
                f"FICI={row.fici:.4g}; classification={classification}"
            )
            references.append(
                EvidenceReference(
                    source_id=f"sha256:{analysis.input_sha256}",
                    source_type="experiment_csv",
                    source_name=analysis.source_name,
                    input_sha256=analysis.input_sha256,
                    data_rows=[row.row_number],
                    calculation=calculation,
                )
            )
        summary = "；".join(f"{key} {value} 行" for key, value in sorted(counts.items()))
        return TraceableConclusion(
            id="analysis-fici",
            statement=(
                f"{demo_prefix}FICI 描述性分析包含 {len(rows)} 个有效数据行："
                f"{summary}。"
            ),
            confidence=ConclusionConfidence.MODERATE,
            evidence=references,
            limitations=[
                *demo_limitation,
                "FICI 阈值分类不能替代独立重复和 time-kill 验证。",
            ],
        )

    if isinstance(analysis, GrowthCurveAnalysisResult) and analysis.auc_by_group:
        references = []
        valid_rows = [row for row in analysis.rows if row.valid]
        scope_text = ""
        if valid_rows:
            first_scope = valid_rows[0]
            scope_text = (
                f"{first_scope.intervention} + {first_scope.comparator} / "
                f"{first_scope.population_or_strain}；"
            )
        for row in analysis.auc_by_group:
            timepoints = [
                point
                for point in analysis.timepoints
                if point.group == row.group
            ]
            data_rows = sorted(
                {
                    source_row
                    for point in timepoints
                    for source_row in point.source_row_numbers
                }
            )
            point_text = ", ".join(
                f"(t={point.time:g}, mean={point.mean:.6g}, n={point.n})"
                for point in timepoints
            )
            calculation = (
                f"CSV group {row.group}: trapezoid AUC over {point_text} = "
                f"{row.auc:.6g}; range={row.start_time:g}-{row.end_time:g}"
            )
            references.append(
                EvidenceReference(
                    source_id=f"sha256:{analysis.input_sha256}",
                    source_type="experiment_csv",
                    source_name=analysis.source_name,
                    input_sha256=analysis.input_sha256,
                    data_rows=data_rows,
                    calculation=calculation,
                )
            )
        statement = "；".join(
            f"{row.group} AUC={row.auc:.4g}" for row in analysis.auc_by_group
        )
        return TraceableConclusion(
            id="analysis-growth-curve",
            statement=(
                f"{demo_prefix}{scope_text}生长曲线梯形积分的描述性结果为："
                f"{statement}。"
            ),
            confidence=ConclusionConfidence.MODERATE,
            evidence=references,
            limitations=[
                *demo_limitation,
                "AUC 为描述性统计，尚未进行显著性推断或模型比较。",
            ],
        )
    return None


def _mechanism_prediction_for_question(
    question: ResearchQuestion,
    prediction: MechanismPredictionBundle,
) -> MechanismPredictionBundle:
    """Fail closed when a prediction bundle belongs to another question."""

    expected_compounds = [
        question.intervention or "",
        question.comparator or "",
    ]
    expected_organism = question.population or ""
    network = prediction.network
    if network is not None:
        try:
            require_network_scope(
                network,
                expected_compounds=expected_compounds,
                expected_organism=expected_organism,
            )
        except ValueError:
            network = None

    def manifest_matches_question(manifest: VinaTaskManifest) -> bool:
        try:
            require_docking_scope(
                manifest,
                expected_compounds=expected_compounds,
                expected_organism=expected_organism,
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return True

    prepared_manifests = [
        manifest
        for manifest in prediction.prepared_manifests
        if manifest_matches_question(manifest)
    ]
    docking_runs = [
        run
        for run in prediction.docking_runs
        if manifest_matches_question(run.manifest)
    ]
    return prediction.model_copy(
        update={
            "network": network,
            "prepared_manifests": prepared_manifests,
            "docking_runs": docking_runs,
        }
    )


def build_decision_report(
    question: ResearchQuestion | str,
    *,
    conditions: list[ExperimentCondition],
    task_events: list[TaskEvent],
    analysis: ExperimentAnalysisResult | None = None,
    assessment: EvidenceAssessment | None = None,
    mechanism_prediction: MechanismPredictionBundle | None = None,
    hypotheses: list[TestableHypothesis] | None = None,
    human_review: HumanReview | None = None,
) -> ResearchDecisionReport:
    """Create a source-backed report; refuse to emit conclusions without evidence."""

    scoped = _question(question)
    active_hypotheses = hypotheses or decompose_research_question(scoped)
    qualified_conditions = [
        _qualified_condition(scoped, condition) for condition in conditions
    ]
    usable_analysis = (
        analysis if analysis is not None and analysis.valid else None
    )
    if (
        usable_analysis is not None
        and not experiment_analysis_matches_question(scoped, usable_analysis)
    ):
        usable_analysis = None
    # Assessment is always recomputed from the same validated inputs as the
    # report.  A stale or caller-supplied assessment must not smuggle invalid
    # CSV results into conflicts, gaps, or recommendations.
    _ = assessment
    active_assessment = assess_evidence(
        qualified_conditions,
        analysis,
        question=scoped,
    )
    admission = active_assessment.evidence_admission
    conclusions = [
        claim
        for condition in qualified_conditions
        if condition.qualification.grade
        is LiteratureEvidenceGrade.DIRECT_INTERACTION
        if (claim := _claim_from_condition(condition)) is not None
    ][:5]
    if (
        admission.status
        is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
    ):
        candidate_references = [
            reference
            for condition in qualified_conditions
            if condition.qualification.grade
            is LiteratureEvidenceGrade.CONTEXTUAL
            if (reference := condition.reference()) is not None
        ]
        if candidate_references:
            conclusions.append(
                TraceableConclusion(
                    id="literature-direct-evidence-insufficient",
                    statement=admission.reason,
                    confidence=ConclusionConfidence.LOW,
                    evidence=candidate_references,
                    limitations=[
                        "该判断仅描述本次可见检索结果，不等于证明协同作用不存在。",
                        "相关性规则基于题名和摘要，仍需人工核查全文及术语同义词。",
                    ],
                )
            )
    if usable_analysis is not None:
        analysis_conclusion = _analysis_conclusion(usable_analysis)
        if analysis_conclusion:
            conclusions.append(analysis_conclusion)
    if not conclusions:
        raise ValueError("没有可追溯的来源片段，不能生成科研决策报告。")

    evidence = [
        reference
        for conclusion in conclusions
        for reference in conclusion.evidence
    ]
    if usable_analysis is not None and _is_synthetic_analysis(usable_analysis):
        recommendation_text = (
            "当前输入是合成演示数据，只能验证工作流和计算过程；不得据此"
            "形成科研建议。请上传真实、可追溯的实验数据后重新生成报告。"
        )
    elif isinstance(usable_analysis, FICIAnalysisResult):
        synergy_rows = [
            row
            for row in usable_analysis.rows
            if row.valid and row.classification == "synergy"
        ]
        if synergy_rows:
            recommendation_text = (
                "当前 FICI 数据包含协同分类；下一步应进行独立生物学重复，"
                "并使用 time-kill 等正交方法验证协同是否稳定。"
            )
        else:
            recommendation_text = (
                "当前有效 FICI 数据未形成协同分类；在调整菌株、剂量或时间"
                "条件前，不应宣称存在协同效应。"
            )
    elif isinstance(usable_analysis, GrowthCurveAnalysisResult):
        recommendation_text = (
            "当前生长曲线仅支持描述性比较；下一步应确认各时间点生物学"
            "重复数，并预先登记效应指标后再做统计推断。"
        )
    elif (
        admission.status
        is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
    ):
        recommendation_text = (
            "当前检索未发现同时覆盖研究对象、两种干预、明确交互指标和结果的"
            "直接文献证据，仅凭本次文献检索不能判断或宣称存在协同作用。"
            "下一步应优化联合检索，并通过 checkerboard/FICI 与 time-kill "
            "等正交实验验证。"
        )
    else:
        recommendation_text = (
            "当前证据可用于设计验证性实验，但在全文核查、补齐实验条件并"
            "完成独立重复前，不应把摘要级结果视为确定性结论。"
        )

    generated_at = datetime.now(timezone.utc)
    report_id = f"report-{scoped.id}-{uuid4().hex[:12]}"
    active_prediction = _mechanism_prediction_for_question(
        scoped,
        mechanism_prediction or MechanismPredictionBundle(),
    )
    review = human_review or HumanReview(
        id=f"review-{report_id}",
        requested_at=generated_at,
    )
    return ResearchDecisionReport(
        id=report_id,
        question=scoped,
        hypotheses=active_hypotheses,
        conclusions=conclusions,
        recommendation=TraceableConclusion(
            id=f"recommendation-{scoped.id}",
            statement=recommendation_text,
            confidence=ConclusionConfidence.LOW,
            evidence=evidence,
            limitations=[
                "建议仅用于确定下一步科研验证，不构成诊疗、处方或临床决策。"
            ],
        ),
        evidence_admission=admission,
        conflicts=active_assessment.conflicts,
        evidence_gaps=active_assessment.gaps,
        mechanism_prediction=active_prediction,
        task_status=summarize_task_status(task_events),
        human_review=review,
        generated_at=generated_at,
    )


def _reference_text(reference: EvidenceReference) -> str:
    parts = []
    if reference.pmid:
        parts.append(f"PMID {reference.pmid}")
    if reference.doi:
        parts.append(f"DOI {reference.doi}")
    if reference.source_quote:
        parts.append(f"来源片段：{reference.source_quote}")
    if reference.source_type == "experiment_csv":
        parts.append(f"CSV：{reference.source_name or reference.source_id}")
        parts.append(f"SHA-256：{reference.input_sha256}")
        parts.append(
            "数据行：" + ",".join(str(row) for row in reference.data_rows)
        )
        parts.append(f"计算：{reference.calculation}")
    return "；".join(parts)


def report_content_sha256(report: ResearchDecisionReport) -> str:
    """Hash the immutable scientific content independently of review state."""

    payload = report.model_dump(
        mode="json",
        exclude={
            "id": True,
            "generated_at": True,
            "human_review": True,
            "task_status": True,
        },
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def decision_report_to_markdown(report: ResearchDecisionReport) -> str:
    lines = [
        "# VetResearch Workbench 科研决策报告",
        "",
        f"- 报告 ID：{report.id}",
        f"- 科研内容 SHA-256：{report_content_sha256(report)}",
        f"- 科研问题：{report.question.text}",
        f"- 任务状态：{report.task_status.current_status}",
        f"- 人工复核：{report.human_review.decision}",
        f"- 生成时间：{report.generated_at.isoformat()}",
        "",
        "## 直接文献证据准入",
        "",
        f"- 状态：{report.evidence_admission.status}",
        f"- 规则：{report.evidence_admission.rule_id}",
        f"- 结论：{report.evidence_admission.reason}",
        (
            "- 直接证据来源："
            + ("、".join(report.evidence_admission.direct_source_ids) or "无")
        ),
        (
            "- 间接背景来源："
            + ("、".join(report.evidence_admission.contextual_source_ids) or "无")
        ),
        (
            "- 排除来源："
            + ("、".join(report.evidence_admission.excluded_source_ids) or "无")
        ),
        "",
        "## 可检验假设",
        "",
    ]
    for hypothesis in report.hypotheses:
        lines.append(f"- {hypothesis.statement}（规则：{hypothesis.rule_id}）")

    lines.extend(["", "## 可追溯结论", ""])
    for conclusion in report.conclusions:
        lines.append(f"### {conclusion.statement}")
        lines.append("")
        lines.extend(
            f"- {_reference_text(reference)}" for reference in conclusion.evidence
        )
        if conclusion.limitations:
            lines.append(f"- 局限：{'；'.join(conclusion.limitations)}")
        lines.append("")

    lines.extend(["## 计算预测（不等同于实验或直接文献证据）", ""])
    prediction = report.mechanism_prediction
    if (
        prediction.network is None
        and not prediction.prepared_manifests
        and not prediction.docking_runs
    ):
        lines.append("- 当前未导入网络药理学或分子对接结果。")
    if prediction.network is not None:
        network = prediction.network
        lines.extend(
            [
                "### 网络药理学",
                "",
                f"- 证据等级：{network.evidence_grade}",
                f"- 算法：{network.parameters.algorithm_version}",
                f"- 排名规则：{network.parameters.ranking_method}",
                "- 研究对象：" + "、".join(network.organisms),
                "- 化合物：" + "、".join(network.compounds),
                (
                    f"- 交集靶点：{network.summary.intersection_target_count}；"
                    f"交集通路：{network.summary.intersection_pathway_count}"
                ),
            ]
        )
        for source in network.sources:
            lines.append(
                f"- 输入来源：{source.source_name}；accession={source.accession}；"
                f"version={source.version}；SHA-256={source.sha256}"
            )
        for target in network.ranked_targets[:10]:
            lines.append(
                f"- 靶点排名 {target.rank}：{target.target} "
                f"({target.target_accession})；organism={target.organism}；"
                f"score={target.network_score}；"
                f"compound_degree={target.compound_degree}；"
                f"pathway_degree={target.pathway_degree}；compounds="
                + "、".join(
                    f"{link.compound} ({link.compound_accession})"
                    for link in target.compounds
                )
            )
        lines.append(
            "- 局限：网络排名只反映用户导入关系的透明拓扑统计，"
            "不能证明靶点有效、药物结合或协同作用。"
        )
        lines.append("")
    completed_task_ids = {
        run.manifest.task_id for run in prediction.docking_runs
    }
    for manifest in prediction.prepared_manifests:
        if manifest.task_id in completed_task_ids:
            continue
        lines.extend(
            [
                f"### 待运行对接任务：{manifest.compound_name} × "
                f"{manifest.receptor_name}",
                "",
                f"- 任务 ID：{manifest.task_id}",
                f"- 任务清单 SHA-256：{manifest.manifest_sha256}",
                f"- 引擎：{manifest.engine} {manifest.engine_version}",
                f"- 研究对象：{manifest.receptor_organism}",
                "- 状态：仅生成可复现任务清单，尚未导入任务哈希与版本"
                "匹配的用户输出，因此没有对接分数。",
                "",
            ]
        )
    for run in prediction.docking_runs:
        manifest = run.manifest
        lines.extend(
            [
                f"### 分子对接：{manifest.compound_name} × "
                f"{manifest.receptor_name}",
                "",
                f"- 证据等级：{run.evidence_grade}",
                f"- 任务 ID：{manifest.task_id}",
                f"- 任务清单 SHA-256：{manifest.manifest_sha256}",
                f"- 引擎：{manifest.engine} {manifest.engine_version}",
                f"- 配体：{manifest.ligand_accession}；"
                f"SHA-256={manifest.ligand_source.sha256}",
                f"- 受体：{manifest.receptor_accession}；"
                f"研究对象={manifest.receptor_organism}；"
                f"SHA-256={manifest.receptor_source.sha256}",
                f"- 原始输出：{run.output_source.accession}；"
                f"SHA-256={run.output_source.sha256}",
                (
                    f"- 搜索框中心：({manifest.parameters.center_x}, "
                    f"{manifest.parameters.center_y}, "
                    f"{manifest.parameters.center_z})；尺寸："
                    f"({manifest.parameters.size_x}, "
                    f"{manifest.parameters.size_y}, "
                    f"{manifest.parameters.size_z})"
                ),
                f"- exhaustiveness={manifest.parameters.exhaustiveness}；"
                f"num_modes={manifest.parameters.num_modes}；"
                f"seed={manifest.parameters.seed}",
                f"- 最佳解析得分：{run.best_affinity_kcal_mol} kcal/mol",
                (
                    "- 局限：对接得分只表示该结构、质子化状态、搜索框和"
                    "评分函数下的计算结果，不能证明体内外活性或药物协同；"
                    "系统只核验文件格式、任务哈希、版本与内容哈希，不能"
                    "认证该文件确由 Vina 实际运行产生。"
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## 证据一致性、冲突与空白",
            "",
        ]
    )
    if not report.conflicts:
        lines.append("- 当前未检测到满足规则定义的显式方向冲突。")
    for conflict in report.conflicts:
        lines.append(f"- 冲突：{conflict.description} 影响：{conflict.impact}")
        for claim in conflict.claims:
            lines.append(f"  - 冲突证据：{claim.statement}")
            lines.extend(
                f"    - {_reference_text(reference)}"
                for reference in claim.evidence
            )
    for gap in report.evidence_gaps:
        lines.append(
            f"- 空白：{gap.missing_evidence} 下一步：{gap.recommended_action}"
        )
        lines.extend(
            f"  - 空白相关证据：{_reference_text(reference)}"
            for reference in gap.related_evidence
        )

    lines.extend(
        [
            "",
            "## 下一步建议",
            "",
            report.recommendation.statement,
            "",
            "### 建议依据",
            "",
            *(
                f"- {_reference_text(reference)}"
                for reference in report.recommendation.evidence
            ),
            "",
            "## 风险与边界",
            "",
            f"- {report.disclaimer}",
            "- 用户导入题录和 CSV 在本地处理；正式结论仍需核对全文与原始数据。",
        ]
    )
    return "\n".join(lines)
