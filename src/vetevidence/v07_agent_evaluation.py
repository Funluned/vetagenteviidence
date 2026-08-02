"""Deterministic v0.7 fixtures and scoring for the single-agent experiment.

The module is deliberately an adapter around the frozen v0.7 dataset.  It
does not run a provider, read credentials, open arbitrary paths, or perform
network requests.  Gold-only fields are projected into a separate object so
that they cannot accidentally enter a provider prompt.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.agent_runtime import AgentPhase, AgentState
from vetevidence.agent_tools import (
    AgentEvidenceGrade,
    AgentToolName,
    ToolEvidence,
    ToolExecutionResult,
    ToolFailure,
    ValidatedToolCall,
    contains_prompt_injection,
)
from vetevidence.experiment_analysis import (
    ExperimentAnalysisResult,
    analyze_fici_csv,
    analyze_growth_curve_csv,
)
from vetevidence.local_rag import EvidenceSource, LocalRAGIndex
from vetevidence.models import CitedAnswer, PubMedArticle, ResearchResult
from vetevidence.providers import RuleBasedEvidenceProvider
from vetevidence.v07_evaluation import (
    V07CategorySummary,
    V07EvaluationCase,
    V07ExpectedCase,
    V07MetricResult,
    V07_METRICS,
)
from vetevidence.v07_rag_evaluation import build_v07_rag_query
from vetevidence.workbench_pipeline import (
    assess_evidence,
    build_experiment_conditions,
    experiment_analysis_matches_question,
    qualify_literature_evidence,
)
from vetevidence.workbench import ResearchQuestion


_RATE_METRICS = (
    "retrieval_recall_at_k",
    "citation_precision",
    "unsupported_claim_rate",
    "abstention_accuracy",
    "task_completion_rate",
)
_EXTERNAL_LOCATOR = re.compile(
    r"(?:https?://|file://|[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|(?:^|\s)/(?:[^/\s]+/)+)",
    re.IGNORECASE,
)
_OPEN_CONFLICT_PATTERN = re.compile(
    r"\b(?:open\s+)?conflict(?:s|ed|ing)?\b|"
    r"\bcontradict(?:s|ed|ory|ion)?\b|"
    r"\binconsisten(?:t|cy)\b|"
    r"\bnot\s+consistent\b|"
    r"\b(?:opposing|discordant|divergent)\b|"
    r"(?:开放)?冲突|矛盾|不一致|方向相反|分类相反|不能合并|无法合并",
    re.IGNORECASE,
)
_UNRESOLVED_CONFLICT_PATTERN = re.compile(
    r"\b(?:conflict|contradiction|inconsistency)\b.{0,32}"
    r"\b(?:unresolved|not\s+resolved|cannot\s+be\s+resolved|"
    r"could\s+not\s+be\s+resolved|remains?\s+open)\b|"
    r"\b(?:unresolved|not\s+resolved)\b.{0,20}\bconflict\b|"
    r"(?:冲突|矛盾|不一致).{0,8}(?:尚未|未|无法|不能)(?:解决|消除|化解|闭合)",
    re.IGNORECASE,
)
_NEGATED_UNRESOLVED_PATTERN = re.compile(
    r"\bno\s+unresolved\s+(?:conflict|contradiction|inconsistency)\b|"
    r"\b(?:conflict|contradiction|inconsistency)\b.{0,16}"
    r"\b(?:is\s+)?not\s+unresolved\b|"
    r"(?:不存在|没有|无).{0,6}(?:未解决|尚未解决)(?:的)?(?:冲突|矛盾|不一致)",
    re.IGNORECASE,
)
_NEGATED_CONFLICT_PATTERN = re.compile(
    r"\b(?:no|without)\s+(?:open\s+)?"
    r"(?:conflict|contradiction|inconsistency)\b|"
    r"\bnot\s+(?:conflicting|contradictory|inconsistent)\b|"
    r"\b(?:conflict|contradiction|inconsistency)\b.{0,24}"
    r"\b(?:resolved|reconciled|closed)\b|"
    r"(?:没有|不存在|无)(?:明显)?(?:冲突|矛盾|不一致)|"
    r"(?:冲突|矛盾|不一致).{0,8}(?:已)?(?:解决|消除|化解|闭合)",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _opaque_id(prefix: str, value: Any) -> str:
    digest = sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class V07SourceAlias(_FrozenModel):
    """One scorer-side identifier mapping.

    ``original_source_id`` must never be copied into a provider request.
    """

    original_source_id: str = Field(min_length=1, repr=False)
    provider_source_id: str = Field(pattern=r"^source-[0-9]{3}$")


class V07AgentRunContext(_FrozenModel):
    """The complete case-specific context that a provider is allowed to see."""

    available_tools: tuple[AgentToolName, ...]
    dataset_ids: tuple[str, ...] = ()
    report_input_id: str | None = None

    @model_validator(mode="after")
    def unique_values(self) -> "V07AgentRunContext":
        if len(self.available_tools) != len(set(self.available_tools)):
            raise ValueError("available_tools must be unique")
        if len(self.dataset_ids) != len(set(self.dataset_ids)):
            raise ValueError("dataset_ids must be unique")
        return self

    @property
    def provider_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class V07QuestionScope(_FrozenModel):
    population: str | None = None
    intervention: str | None = None
    comparator: str | None = None


class V07FrozenPubMedBatch(_FrozenModel):
    status: Literal["succeeded", "failed"]
    evidence: tuple[ToolEvidence, ...] = ()
    failure: ToolFailure | None = None

    @model_validator(mode="after")
    def valid_shape(self) -> "V07FrozenPubMedBatch":
        if self.status == "succeeded" and self.failure is not None:
            raise ValueError("successful batch cannot contain a failure")
        if self.status == "failed" and self.failure is None:
            raise ValueError("failed batch requires a failure")
        if self.status == "failed" and self.evidence:
            raise ValueError("failed batch cannot contain evidence")
        return self


class V07FrozenAnalysisFixture(_FrozenModel):
    analysis_type: Literal["fici", "growth_curve"]
    dataset_id: str = Field(min_length=1)
    csv_text: str = Field(min_length=1, repr=False)
    question_scope: V07QuestionScope = Field(repr=False)


class V07AgentFixture(_FrozenModel):
    """One case split into provider-visible data and scorer-only metadata."""

    case_id: str = Field(min_length=1, repr=False)
    category: str = Field(min_length=1, repr=False)
    evaluator: str = Field(min_length=1, repr=False)
    run_id: str = Field(pattern=r"^run-[0-9a-f]{20}$")
    visible_question: str = Field(min_length=1, repr=False)
    run_context: V07AgentRunContext
    source_aliases: tuple[V07SourceAlias, ...] = Field(repr=False)
    pubmed_batches: tuple[V07FrozenPubMedBatch, ...] = Field(
        default=(), repr=False
    )
    rag_evidence: tuple[ToolEvidence, ...] = Field(default=(), repr=False)
    analysis: V07FrozenAnalysisFixture | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_aliases(self) -> "V07AgentFixture":
        originals = [item.original_source_id for item in self.source_aliases]
        aliases = [item.provider_source_id for item in self.source_aliases]
        if len(originals) != len(set(originals)):
            raise ValueError("source aliases contain duplicate originals")
        if len(aliases) != len(set(aliases)):
            raise ValueError("source aliases contain duplicate provider IDs")
        allowed = set(aliases)
        for evidence in self.provider_evidence:
            if evidence.source_id not in allowed:
                raise ValueError("evidence uses an unregistered provider source ID")
        return self

    @property
    def alias_by_original(self) -> dict[str, str]:
        return {
            item.original_source_id: item.provider_source_id
            for item in self.source_aliases
        }

    @property
    def original_by_alias(self) -> dict[str, str]:
        return {
            item.provider_source_id: item.original_source_id
            for item in self.source_aliases
        }

    @property
    def provider_question(self) -> str:
        return (
            f"{self.visible_question}\n\n"
            f"RUN_CONTEXT={_canonical_json(self.run_context.provider_payload)}"
        )

    @property
    def provider_evidence(self) -> tuple[ToolEvidence, ...]:
        seen: set[tuple[str, str]] = set()
        items: list[ToolEvidence] = []
        for evidence in (
            *(item for batch in self.pubmed_batches for item in batch.evidence),
            *self.rag_evidence,
        ):
            key = (evidence.source_id, evidence.chunk_id)
            if key not in seen:
                seen.add(key)
                items.append(evidence)
        return tuple(items)

    @property
    def provider_payload(self) -> dict[str, Any]:
        """Return every byte this fixture may expose to a provider."""

        return {
            "question": self.provider_question,
            "run_context": self.run_context.provider_payload,
            "potential_evidence": [
                item.model_dump(mode="json") for item in self.provider_evidence
            ],
        }

    @property
    def canonical_sha256(self) -> str:
        return sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def _source_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("pmid", record.get("source_id"))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _case_source_ids(case: V07EvaluationCase) -> tuple[str, ...]:
    found: set[str] = set()
    case_input = case.input
    for collection_name in ("articles", "records"):
        collection = case_input.get(collection_name, [])
        if isinstance(collection, list):
            for raw in collection:
                if isinstance(raw, Mapping) and (identifier := _source_id(raw)):
                    found.add(identifier)
    batches = case_input.get("retrieval_batches", [])
    if isinstance(batches, list):
        for batch in batches:
            if isinstance(batch, list):
                for raw in batch:
                    if isinstance(raw, Mapping) and (
                        identifier := _source_id(raw)
                    ):
                        found.add(identifier)
    for identifier in case_input.get("gold_relevant_ids", []):
        if isinstance(identifier, str) and identifier:
            found.add(identifier)
    support_terms = case_input.get("support_terms", {})
    if isinstance(support_terms, Mapping):
        found.update(
            str(identifier) for identifier in support_terms if str(identifier)
        )
    return tuple(sorted(found))


def _aliases_for_case(case: V07EvaluationCase) -> tuple[V07SourceAlias, ...]:
    return tuple(
        V07SourceAlias(
            original_source_id=identifier,
            provider_source_id=f"source-{index:03d}",
        )
        for index, identifier in enumerate(_case_source_ids(case), start=1)
    )


def _article_evidence(
    raw: Mapping[str, Any],
    aliases: Mapping[str, str],
    *,
    question: ResearchQuestion,
    source_type: str,
) -> ToolEvidence | None:
    identifier = _source_id(raw)
    abstract = raw.get("abstract")
    if identifier is None or not isinstance(abstract, str) or not abstract.strip():
        return None
    alias = aliases[identifier]
    title = raw.get("title")
    clean_title = title.strip() if isinstance(title, str) and title.strip() else None
    content = abstract.strip()
    if clean_title:
        content = f"Title: {clean_title}\nAbstract: {content}"
    qualification = qualify_literature_evidence(
        question,
        title=clean_title or "",
        abstract=abstract.strip(),
    )
    evidence_grade = AgentEvidenceGrade(qualification.grade.value)
    if contains_prompt_injection(abstract) or (
        isinstance(title, str) and contains_prompt_injection(title)
    ):
        evidence_grade = AgentEvidenceGrade.OUT_OF_SCOPE
    return ToolEvidence(
        source_id=alias,
        chunk_id=f"{alias}:abstract",
        content=content,
        source_type=source_type,
        title=clean_title,
        locator=None,
        evidence_grade=evidence_grade,
    )


def _record_evidence(
    raw: Mapping[str, Any], aliases: Mapping[str, str]
) -> ToolEvidence | None:
    identifier = _source_id(raw)
    key_result = raw.get("key_result")
    source_quote = raw.get("source_quote")
    if (
        identifier is None
        or not isinstance(key_result, str)
        or not key_result.strip()
        or not isinstance(source_quote, str)
        or not source_quote.strip()
    ):
        return None
    alias = aliases[identifier]
    return ToolEvidence(
        source_id=alias,
        chunk_id=f"{alias}:record",
        content=(
            f"Candidate extracted claim (untrusted): {key_result.strip()}\n"
            f"Source quote: {source_quote.strip()}"
        ),
        source_type="frozen_citation_record",
        title="Candidate claim and source quote",
        locator=None,
    )


def _tool_set(case: V07EvaluationCase) -> tuple[AgentToolName, ...]:
    if case.evaluator in {"retrieval_replay", "tool_retrieval"}:
        return (AgentToolName.PUBMED_SEARCH,)
    if case.evaluator in {"literature", "citation"}:
        return (AgentToolName.LOCAL_RAG_SEARCH,)
    analysis_type = case.input.get("analysis_type")
    analysis_tool = (
        AgentToolName.EXPERIMENT_FICI
        if analysis_type == "fici"
        else AgentToolName.EXPERIMENT_GROWTH_CURVE
    )
    if case.evaluator == "partial_analysis":
        return (
            AgentToolName.LOCAL_RAG_SEARCH,
            analysis_tool,
            AgentToolName.REPORT_BUILD,
        )
    return (analysis_tool, AgentToolName.REPORT_BUILD)


def build_v07_agent_fixture(case: V07EvaluationCase) -> V07AgentFixture:
    """Build one leak-resistant, deterministic agent fixture."""

    if not isinstance(case, V07EvaluationCase):
        raise TypeError("case must be V07EvaluationCase")
    aliases = _aliases_for_case(case)
    alias_by_original = {
        item.original_source_id: item.provider_source_id for item in aliases
    }
    visible_question = build_v07_rag_query(case.model_dump(mode="json"))

    evidence_by_original: dict[str, ToolEvidence] = {}
    pubmed_batches: list[V07FrozenPubMedBatch] = []
    raw_batches = case.input.get("retrieval_batches", [])
    if isinstance(raw_batches, list):
        for raw_batch in raw_batches:
            if isinstance(raw_batch, Mapping):
                error = str(raw_batch.get("error", "failure")).casefold()
                code = "TimeoutError" if error == "timeout" else "RuntimeError"
                message = raw_batch.get("message")
                pubmed_batches.append(
                    V07FrozenPubMedBatch(
                        status="failed",
                        failure=ToolFailure(
                            code=code,
                            message=(
                                str(message).strip()
                                if message is not None and str(message).strip()
                                else "Frozen retrieval batch failed."
                            ),
                            retryable=error == "timeout",
                        ),
                    )
                )
                continue
            batch_evidence: list[ToolEvidence] = []
            if isinstance(raw_batch, list):
                for raw in raw_batch:
                    if not isinstance(raw, Mapping):
                        continue
                    evidence = _article_evidence(
                        raw,
                        alias_by_original,
                        question=case.question,
                        source_type="frozen_pubmed_abstract",
                    )
                    if evidence is not None:
                        identifier = _source_id(raw)
                        assert identifier is not None
                        evidence_by_original.setdefault(identifier, evidence)
                        batch_evidence.append(evidence)
            pubmed_batches.append(
                V07FrozenPubMedBatch(
                    status="succeeded", evidence=tuple(batch_evidence)
                )
            )

    for raw in case.input.get("articles", []):
        if isinstance(raw, Mapping):
            evidence = _article_evidence(
                raw,
                alias_by_original,
                question=case.question,
                source_type="frozen_local_document",
            )
            if evidence is not None:
                identifier = _source_id(raw)
                assert identifier is not None
                evidence_by_original.setdefault(identifier, evidence)
    for raw in case.input.get("records", []):
        if isinstance(raw, Mapping):
            evidence = _record_evidence(raw, alias_by_original)
            if evidence is not None:
                identifier = _source_id(raw)
                assert identifier is not None
                evidence_by_original.setdefault(identifier, evidence)

    analysis: V07FrozenAnalysisFixture | None = None
    dataset_ids: tuple[str, ...] = ()
    if case.evaluator in {"experiment", "partial_analysis"}:
        analysis_type = case.input["analysis_type"]
        csv_text = case.input["csv_text"]
        dataset_id = _opaque_id(
            "dataset",
            {
                "analysis_type": analysis_type,
                "csv_sha256": sha256(csv_text.encode("utf-8")).hexdigest(),
                "question": visible_question,
            },
        )
        dataset_ids = (dataset_id,)
        analysis = V07FrozenAnalysisFixture(
            analysis_type=analysis_type,
            dataset_id=dataset_id,
            csv_text=csv_text,
            question_scope=V07QuestionScope(
                population=case.question.population,
                intervention=case.question.intervention,
                comparator=case.question.comparator,
            ),
        )

    tools = _tool_set(case)
    report_input_id = (
        _opaque_id("report", {"question": visible_question, "tools": tools})
        if AgentToolName.REPORT_BUILD in tools
        else None
    )
    context = V07AgentRunContext(
        available_tools=tools,
        dataset_ids=dataset_ids,
        report_input_id=report_input_id,
    )
    return V07AgentFixture(
        case_id=case.id,
        category=case.category,
        evaluator=case.evaluator,
        run_id=_opaque_id(
            "run",
            {"question": visible_question, "context": context.provider_payload},
        ),
        visible_question=visible_question,
        run_context=context,
        source_aliases=aliases,
        pubmed_batches=tuple(pubmed_batches),
        rag_evidence=tuple(
            evidence_by_original[key] for key in sorted(evidence_by_original)
        ),
        analysis=analysis,
    )


def build_v07_agent_fixtures(
    cases: Sequence[V07EvaluationCase],
) -> tuple[V07AgentFixture, ...]:
    """Build a deterministic fixture tuple without loading files or providers."""

    return tuple(build_v07_agent_fixture(case) for case in cases)


class V07FrozenToolExecutor:
    """Execute only the data embedded in one v0.7 fixture.

    PubMed calls replay batches in declaration order.  Local retrieval uses a
    private temporary keyword index containing only this case.  Experiment
    tools resolve one opaque in-memory CSV identifier.  No method accepts a
    filesystem path or URL.
    """

    def __init__(self, fixture: V07AgentFixture) -> None:
        if not isinstance(fixture, V07AgentFixture):
            raise TypeError("fixture must be V07AgentFixture")
        self.fixture = fixture
        self._calls: list[ValidatedToolCall] = []
        self._pubmed_cursor = 0
        self._temporary: TemporaryDirectory[str] | None = None
        self._rag_index: LocalRAGIndex | None = None
        self._rag_by_source = {item.source_id: item for item in fixture.rag_evidence}
        if fixture.rag_evidence:
            self._temporary = TemporaryDirectory(prefix="vetevidence-v07-rag-")
            self._rag_index = LocalRAGIndex(
                f"{self._temporary.name}/case-index.sqlite3"
            )
            self._rag_index.build(
                [
                    EvidenceSource(
                        source_id=item.source_id,
                        source_type=item.source_type,
                        title=item.title or "Frozen evaluation evidence",
                        content=item.content,
                        field_location=item.chunk_id,
                        version="v0.7.0",
                        authorization_scope="public",
                    )
                    for item in fixture.rag_evidence
                ],
                chunk_size=20_000,
                overlap_chars=0,
            )

    @property
    def calls(self) -> tuple[ValidatedToolCall, ...]:
        return tuple(self._calls)

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
            self._rag_index = None

    def __enter__(self) -> "V07FrozenToolExecutor":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _failed(call: ValidatedToolCall, code: str, message: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status="failed",
            failure=ToolFailure(code=code, message=message),
            frozen_replay=True,
        )

    def execute(self, call: ValidatedToolCall) -> ToolExecutionResult:
        if not isinstance(call, ValidatedToolCall):
            raise TypeError("call must be ValidatedToolCall")
        self._calls.append(call)
        if call.tool_name not in self.fixture.run_context.available_tools:
            return self._failed(
                call, "tool_not_available", "Tool is not available for this case."
            )
        if call.tool_name in {
            AgentToolName.PUBMED_SEARCH,
            AgentToolName.LOCAL_RAG_SEARCH,
        }:
            query = str(call.arguments["query"])
            if _EXTERNAL_LOCATOR.search(query):
                return self._failed(
                    call,
                    "external_locator_forbidden",
                    "URLs and filesystem paths are forbidden in frozen evaluation.",
                )
        if call.tool_name == AgentToolName.PUBMED_SEARCH:
            return self._execute_pubmed(call)
        if call.tool_name == AgentToolName.LOCAL_RAG_SEARCH:
            return self._execute_rag(call)
        if call.tool_name in {
            AgentToolName.EXPERIMENT_FICI,
            AgentToolName.EXPERIMENT_GROWTH_CURVE,
        }:
            return self._execute_analysis(call)
        if call.tool_name == AgentToolName.REPORT_BUILD:
            return self._execute_report(call)
        return self._failed(call, "tool_not_available", "Tool is not available.")

    def _execute_pubmed(self, call: ValidatedToolCall) -> ToolExecutionResult:
        if self._pubmed_cursor >= len(self.fixture.pubmed_batches):
            return self._failed(
                call,
                "frozen_replay_exhausted",
                "No further frozen PubMed batch is available.",
            )
        batch = self.fixture.pubmed_batches[self._pubmed_cursor]
        self._pubmed_cursor += 1
        if batch.status == "failed":
            assert batch.failure is not None
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                call_signature_sha256=call.signature_sha256,
                status="failed",
                failure=batch.failure,
                frozen_replay=True,
            )
        maximum = int(call.arguments["max_results"])
        evidence = batch.evidence[:maximum]
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status="succeeded",
            evidence=evidence,
            output={"retrieved_count": len(evidence)},
            frozen_replay=True,
        )

    def _execute_rag(self, call: ValidatedToolCall) -> ToolExecutionResult:
        if self._rag_index is None:
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                call_signature_sha256=call.signature_sha256,
                status="succeeded",
                output={"retrieved_count": 0},
                frozen_replay=True,
            )
        hits = self._rag_index.search(
            str(call.arguments["query"]),
            limit=int(call.arguments["limit"]),
            keyword_weight=1.0,
            vector_weight=0.0,
        )
        evidence: list[ToolEvidence] = []
        seen: set[str] = set()
        for hit in hits:
            source_id = hit.chunk.source_id
            if hit.keyword_score <= 0.0 or source_id in seen:
                continue
            item = self._rag_by_source.get(source_id)
            if item is not None:
                seen.add(source_id)
                evidence.append(item)
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status="succeeded",
            evidence=tuple(evidence),
            output={"retrieved_count": len(evidence), "retrieval_mode": "keyword"},
            frozen_replay=True,
        )

    def _execute_analysis(self, call: ValidatedToolCall) -> ToolExecutionResult:
        fixture = self.fixture.analysis
        if fixture is None or call.arguments["dataset_id"] != fixture.dataset_id:
            return self._failed(
                call,
                "dataset_not_authorized",
                "Opaque dataset identifier is not authorized for this case.",
            )
        expected_tool = (
            AgentToolName.EXPERIMENT_FICI
            if fixture.analysis_type == "fici"
            else AgentToolName.EXPERIMENT_GROWTH_CURVE
        )
        if call.tool_name != expected_tool:
            return self._failed(
                call, "analysis_type_mismatch", "Dataset and analysis tool differ."
            )
        analysis: ExperimentAnalysisResult
        if fixture.analysis_type == "fici":
            analysis = analyze_fici_csv(
                fixture.csv_text, source_name=fixture.dataset_id
            )
        else:
            analysis = analyze_growth_curve_csv(
                fixture.csv_text, source_name=fixture.dataset_id
            )
        admitted = bool(
            analysis.valid
            and experiment_analysis_matches_question(
                fixture.question_scope, analysis  # type: ignore[arg-type]
            )
        )
        summary = {
            "analysis_type": analysis.analysis_type,
            "valid": analysis.valid,
            "analysis_admitted": admitted,
            "valid_row_count": analysis.valid_row_count,
            "invalid_row_count": analysis.invalid_row_count,
            "errors": list(analysis.errors),
        }
        if analysis.analysis_type == "fici":
            classification_counts = Counter(
                row.classification
                for row in analysis.rows
                if row.valid and row.classification is not None
            )
            summary.update(
                {
                    "classification_counts": {
                        classification: classification_counts[classification]
                        for classification in (
                            "synergy",
                            "additive",
                            "indifferent",
                            "antagonism",
                        )
                        if classification_counts[classification]
                    },
                    "conflict_detected": bool(
                        classification_counts["synergy"]
                        and classification_counts["antagonism"]
                    ),
                }
            )
        source_id = _opaque_id("analysis", fixture.dataset_id)
        evidence = ToolEvidence(
            source_id=source_id,
            chunk_id=f"{source_id}:summary",
            content=_canonical_json(summary),
            source_type="frozen_experiment_summary",
            title="Validated experiment summary",
            locator=None,
            evidence_grade=(
                AgentEvidenceGrade.VALIDATED_EXPERIMENT
                if admitted
                else AgentEvidenceGrade.OUT_OF_SCOPE
            ),
        )
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status="succeeded",
            evidence=(evidence,),
            output=summary,
            frozen_replay=True,
        )

    def _execute_report(self, call: ValidatedToolCall) -> ToolExecutionResult:
        expected = self.fixture.run_context.report_input_id
        if expected is None or call.arguments["report_input_id"] != expected:
            return self._failed(
                call,
                "report_input_not_authorized",
                "Opaque report input is not authorized for this case.",
            )
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status="succeeded",
            output={"report_generated": True},
            frozen_replay=True,
        )


class V07AgentGold(_FrozenModel):
    case_id: str = Field(min_length=1, repr=False)
    category: str = Field(min_length=1, repr=False)
    applicable_metrics: tuple[str, ...]
    relevant_source_ids: tuple[str, ...] = ()
    direct_source_ids: tuple[str, ...] = ()
    support_terms: dict[str, tuple[str, ...]] = Field(default_factory=dict, repr=False)
    forbidden_markers: tuple[str, ...] = Field(default=(), repr=False)
    abstention_field: str | None = None
    abstention_expected: bool | str | None = None
    expected: dict[str, Any] = Field(repr=False)


def _alias_gold_value(value: Any, aliases: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [_alias_gold_value(item, aliases) for item in value]
    if isinstance(value, Mapping):
        return {
            aliases.get(str(key), str(key)): _alias_gold_value(item, aliases)
            for key, item in value.items()
        }
    return value


def project_v07_agent_gold(
    case: V07EvaluationCase,
    expected: V07ExpectedCase,
    fixture: V07AgentFixture,
) -> V07AgentGold:
    """Project scorer-only gold through the same opaque source aliases."""

    if case.id != expected.id or case.id != fixture.case_id:
        raise ValueError("case, expected result, and fixture IDs must match")
    aliases = fixture.alias_by_original
    raw_support = case.input.get("support_terms", {})
    support_terms = {
        aliases[str(source_id)]: tuple(str(term) for term in terms)
        for source_id, terms in raw_support.items()
        if str(source_id) in aliases and isinstance(terms, list)
    } if isinstance(raw_support, Mapping) else {}
    projected = _alias_gold_value(expected.expected, aliases)
    field: str | None = None
    value: bool | str | None = None
    for candidate in (
        "target_claim_abstained",
        "answer_abstained",
        "analysis_admitted",
        "admission_status",
    ):
        if candidate in expected.expected:
            field = candidate
            raw = expected.expected[candidate]
            value = raw if isinstance(raw, (bool, str)) else None
            break
    return V07AgentGold(
        case_id=case.id,
        category=case.category,
        applicable_metrics=tuple(case.applicable_metrics),
        relevant_source_ids=tuple(
            aliases[item]
            for item in case.input.get("gold_relevant_ids", [])
            if item in aliases
        ),
        direct_source_ids=tuple(projected.get("direct_source_ids", [])),
        support_terms=support_terms,
        forbidden_markers=tuple(case.input.get("forbidden_markers", [])),
        abstention_field=field,
        abstention_expected=value,
        expected=projected,
    )


class V07AgentCitationObservation(_FrozenModel):
    claim_id: str
    claim_text: str = Field(repr=False)
    source_id: str
    chunk_id: str
    support_quote: str = Field(repr=False)


class V07AgentActual(_FrozenModel):
    phase: AgentPhase
    task_state: str
    task_completed: bool
    target_claim_abstained: bool
    admission_status: str
    retrieved_ids: tuple[str, ...] = ()
    evidence: tuple[ToolEvidence, ...] = Field(default=(), repr=False)
    citations: tuple[V07AgentCitationObservation, ...] = ()
    claim_ids: tuple[str, ...] = ()
    failed_batch_count: int = Field(default=0, ge=0)
    replay_request_count: int = Field(default=0, ge=0)
    partial_results_preserved: bool = False
    error_type: str | None = None
    analysis_type: str | None = None
    analysis_valid: bool | None = None
    analysis_admitted: bool | None = None
    valid_row_count: int | None = Field(default=None, ge=0)
    invalid_row_count: int | None = Field(default=None, ge=0)
    report_generated: bool = False
    model_calls: int = Field(default=0, ge=0)
    real_model_calls: int = Field(default=0, ge=0)
    network_calls: int = Field(default=0, ge=0)
    external_actions: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_amount: Decimal = Field(default=Decimal("0"), ge=0)
    cost_currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    latency_ms: float = Field(default=0.0, ge=0)
    answer: str | None = Field(default=None, repr=False)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _has_admitted_target_claim(state: AgentState) -> bool:
    ledger = state.evidence_ledger
    if not ledger.evidence_policy_enabled:
        return bool(state.claims)
    direct_keys = ledger.direct_support_keys
    return any(
        any(
            (citation.source_id, citation.chunk_id) in direct_keys
            for citation in claim.citations
        )
        for claim in state.claims
    )


def project_agent_state(
    case: V07EvaluationCase,
    state: AgentState,
    fixture: V07AgentFixture,
    *,
    latency_ms: float = 0.0,
) -> V07AgentActual:
    """Project one runtime state into stable, evaluator-facing observations."""

    if case.id != fixture.case_id:
        raise ValueError("case and fixture IDs must match")
    retrieval_tools = {
        AgentToolName.PUBMED_SEARCH,
        AgentToolName.LOCAL_RAG_SEARCH,
    }
    retrieved = _unique(
        [
            item.source_id
            for result in state.tool_results
            if result.tool_name in retrieval_tools
            for item in result.evidence
        ]
    )
    pubmed_results = [
        result
        for result in state.tool_results
        if result.tool_name == AgentToolName.PUBMED_SEARCH
    ]
    failed_pubmed = [result for result in pubmed_results if result.status == "failed"]
    partial = bool(failed_pubmed and retrieved)
    if failed_pubmed and not retrieved:
        task_state = "failed"
    elif failed_pubmed or case.evaluator == "partial_analysis":
        task_state = "awaiting_review"
    elif state.phase == AgentPhase.FAILED:
        task_state = "failed"
    else:
        task_state = "awaiting_review"

    analysis_output: Mapping[str, Any] = {}
    report_generated = False
    for result in state.tool_results:
        if result.tool_name in {
            AgentToolName.EXPERIMENT_FICI,
            AgentToolName.EXPERIMENT_GROWTH_CURVE,
        } and result.output:
            analysis_output = result.output
        if result.tool_name == AgentToolName.REPORT_BUILD:
            report_generated = report_generated or bool(
                result.output.get("report_generated")
            )

    citations: list[V07AgentCitationObservation] = []
    seen_citations: set[tuple[str, str, str]] = set()
    for claim in state.claims:
        for citation in claim.citations:
            key = (claim.claim_id, citation.source_id, citation.chunk_id)
            if key in seen_citations:
                continue
            seen_citations.add(key)
            citations.append(
                V07AgentCitationObservation(
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    source_id=citation.source_id,
                    chunk_id=citation.chunk_id,
                    support_quote=citation.support_quote,
                )
            )

    currencies = {
        audit.usage.cost_currency
        for audit in state.model_call_audits
        if audit.usage.cost_amount != 0
    }
    if len(currencies) > 1:
        raise ValueError("non-zero model costs use different currencies")
    currency = next(iter(currencies), state.budget.cost_currency)
    total_cost = sum(
        (audit.usage.cost_amount for audit in state.model_call_audits),
        Decimal("0"),
    )
    failures = [
        result.failure.code
        for result in state.tool_results
        if result.failure is not None
    ]
    # A safe stop is not automatically a completed research task.  In
    # particular, budget exhaustion and human-review handoff must remain
    # distinguishable from either a supported result or a valid abstention.
    task_completed = state.phase in {
        AgentPhase.COMPLETED,
        AgentPhase.INSUFFICIENT_EVIDENCE,
    }
    admitted_target_claim = _has_admitted_target_claim(state)
    return V07AgentActual(
        phase=state.phase,
        task_state=task_state,
        task_completed=task_completed,
        target_claim_abstained=not admitted_target_claim,
        admission_status=(
            "admitted" if admitted_target_claim else "blocked_no_direct_evidence"
        ),
        retrieved_ids=retrieved,
        evidence=state.evidence_ledger.items,
        citations=tuple(citations),
        claim_ids=tuple(claim.claim_id for claim in state.claims),
        failed_batch_count=len(failed_pubmed),
        replay_request_count=len(pubmed_results),
        partial_results_preserved=partial,
        error_type=failures[0] if failures else None,
        analysis_type=analysis_output.get("analysis_type"),
        analysis_valid=analysis_output.get("valid"),
        analysis_admitted=analysis_output.get("analysis_admitted"),
        valid_row_count=analysis_output.get("valid_row_count"),
        invalid_row_count=analysis_output.get("invalid_row_count"),
        report_generated=report_generated,
        model_calls=len(state.model_call_audits),
        real_model_calls=state.real_model_calls,
        network_calls=(
            sum(audit.network_used for audit in state.model_call_audits)
            + sum(result.network_used for result in state.tool_results)
        ),
        external_actions=sum(result.external_actions for result in state.tool_results),
        input_tokens=sum(audit.usage.input_tokens for audit in state.model_call_audits),
        output_tokens=sum(audit.usage.output_tokens for audit in state.model_call_audits),
        cost_amount=total_cost,
        cost_currency=currency,
        latency_ms=latency_ms,
        answer=state.answer,
    )


project_v07_agent_actual = project_agent_state


class V07AgentMetricObservation(_FrozenModel):
    value: float
    numerator: float | None = None
    denominator: float | None = None
    unit: str


class V07AgentCaseScore(_FrozenModel):
    id: str
    category: str
    passed: bool
    mismatches: tuple[str, ...] = ()
    applicable_metrics: tuple[str, ...]
    actual: V07AgentActual
    metric_observations: dict[str, V07AgentMetricObservation]

    @property
    def canonical_sha256(self) -> str:
        return sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class V07AgentAggregate(_FrozenModel):
    total: int
    passed: int
    failed: int
    by_category: dict[str, V07CategorySummary]
    metrics: dict[str, V07MetricResult]

    @property
    def canonical_sha256(self) -> str:
        return sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def _citation_support(
    actual: V07AgentActual, gold: V07AgentGold
) -> dict[tuple[str, str, str], bool]:
    evidence = {
        (item.source_id, item.chunk_id): item.content for item in actual.evidence
    }
    support: dict[tuple[str, str, str], bool] = {}
    for citation in actual.citations:
        key = (citation.claim_id, citation.source_id, citation.chunk_id)
        content = evidence.get((citation.source_id, citation.chunk_id))
        terms = gold.support_terms.get(citation.source_id)
        if terms:
            source_quote = (
                content.split("\nSource quote: ", 1)[1]
                if content and "\nSource quote: " in content
                else ""
            )
            quote_valid = bool(
                source_quote and citation.support_quote in source_quote
            )
            claim_text = citation.claim_text.casefold()
            quote = citation.support_quote.casefold()
            semantic = all(
                term.casefold() in claim_text and term.casefold() in quote
                for term in terms
            )
        else:
            quote_valid = bool(content and citation.support_quote in content)
            semantic = citation.source_id in {
                *gold.relevant_source_ids,
                *gold.direct_source_ids,
            }
        support[key] = quote_valid and semantic
    return support


def _literature_conflict_observations(
    case: V07EvaluationCase,
    fixture: V07AgentFixture,
    actual: V07AgentActual,
    citation_support: Mapping[tuple[str, str, str], bool],
) -> tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]:
    """Derive open literature conflicts from sources the Agent actually cited.

    The same deterministic qualification and conflict pipeline used by the
    rules baseline is applied only to cited source aliases.  Merely retrieving
    both sides into the evidence ledger is therefore insufficient.
    """

    if actual.phase != AgentPhase.COMPLETED:
        return (), {}, ()
    citation_quotes: dict[str, list[str]] = {}
    for citation in actual.citations:
        key = (citation.claim_id, citation.source_id, citation.chunk_id)
        if not citation_support.get(key, False):
            continue
        original = fixture.original_by_alias.get(citation.source_id)
        if original is not None:
            citation_quotes.setdefault(original, []).append(citation.support_quote)
    cited_originals = set(citation_quotes)
    raw_articles = case.input.get("articles", [])
    if not cited_originals or not isinstance(raw_articles, list):
        return (), {}, ()
    articles = [
        PubMedArticle.model_validate(raw)
        for raw in raw_articles
        if isinstance(raw, Mapping) and _source_id(raw) in cited_originals
    ]
    if not articles:
        return (), {}, ()
    provider = RuleBasedEvidenceProvider()
    research = ResearchResult(
        query=case.question.text,
        articles=articles,
        evidence=[provider.extract(article) for article in articles],
        answer=CitedAnswer(question=case.question.text, answer_markdown=""),
        provider_name=provider.name,
    )
    conditions = build_experiment_conditions(research, question=case.question)
    supported_conditions = []
    for condition in conditions:
        if condition.pmid is None:
            continue
        candidate_texts = tuple(
            text
            for text in (
                condition.qualification.supporting_quote,
                condition.key_result,
                condition.source_quote,
            )
            if text
        )
        if any(
            quote in candidate or candidate in quote
            for quote in citation_quotes.get(condition.pmid, [])
            for candidate in candidate_texts
        ):
            supported_conditions.append(condition)
    assessment = assess_evidence(supported_conditions, question=case.question)
    aliases = fixture.alias_by_original
    direct_ids = tuple(
        sorted(
            aliases[condition.pmid]
            for condition in supported_conditions
            if condition.pmid in aliases
            and condition.qualification.grade.value == "direct_interaction"
        )
    )
    outcomes = {
        aliases[condition.pmid]: condition.qualification.interaction_outcome.value
        for condition in supported_conditions
        if condition.pmid in aliases
        and condition.qualification.grade.value == "direct_interaction"
        and condition.qualification.interaction_outcome is not None
    }
    conflict_ids = ()
    if _states_open_conflict(actual):
        conflict_ids = tuple(
            sorted(
                conflict.id
                for conflict in assessment.conflicts
                if conflict.resolution_status.value == "open"
            )
        )
    return direct_ids, outcomes, conflict_ids


def _states_open_conflict(actual: V07AgentActual) -> bool:
    output = "\n".join(
        [actual.answer or "", *(citation.claim_text for citation in actual.citations)]
    )
    if _NEGATED_UNRESOLVED_PATTERN.search(output):
        return False
    if _UNRESOLVED_CONFLICT_PATTERN.search(output):
        return True
    return bool(_OPEN_CONFLICT_PATTERN.search(output)) and not bool(
        _NEGATED_CONFLICT_PATTERN.search(output)
    )


def _experiment_conflict_ids(actual: V07AgentActual) -> tuple[str, ...]:
    """Recognize a FICI conflict only from internally consistent tool facts."""

    if actual.phase != AgentPhase.COMPLETED:
        return ()
    for evidence in actual.evidence:
        if evidence.source_type != "frozen_experiment_summary":
            continue
        try:
            summary = json.loads(evidence.content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(summary, Mapping) or summary.get("analysis_type") != "fici":
            continue
        counts = summary.get("classification_counts")
        detected = summary.get("conflict_detected")
        if not isinstance(counts, Mapping) or not isinstance(detected, bool):
            continue
        synergy = counts.get("synergy", 0)
        antagonism = counts.get("antagonism", 0)
        cites_summary_verbatim = any(
            citation.source_id == evidence.source_id
            and citation.chunk_id == evidence.chunk_id
            and citation.support_quote in evidence.content
            for citation in actual.citations
        )
        if (
            isinstance(synergy, int)
            and not isinstance(synergy, bool)
            and synergy >= 0
            and isinstance(antagonism, int)
            and not isinstance(antagonism, bool)
            and antagonism >= 0
            and detected == bool(synergy and antagonism)
            and detected
            and cites_summary_verbatim
            and _states_open_conflict(actual)
        ):
            return ("conflict-fici",)
    return ()


def _conflict_observations(
    case: V07EvaluationCase,
    fixture: V07AgentFixture,
    actual: V07AgentActual,
    citation_support: Mapping[tuple[str, str, str], bool],
) -> tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]:
    direct_ids, outcomes, literature_conflicts = _literature_conflict_observations(
        case, fixture, actual, citation_support
    )
    conflict_ids = tuple(
        sorted({*literature_conflicts, *_experiment_conflict_ids(actual)})
    )
    return direct_ids, outcomes, conflict_ids


def score_v07_agent_case(
    case: V07EvaluationCase,
    fixture: V07AgentFixture,
    gold: V07AgentGold,
    actual: V07AgentActual,
) -> V07AgentCaseScore:
    """Score one projected run with no model or reviewer judgment."""

    if case.id != fixture.case_id or case.id != gold.case_id:
        raise ValueError("case, fixture, and gold IDs must match")
    applicable = tuple(case.applicable_metrics)
    observations: dict[str, V07AgentMetricObservation] = {}
    mismatches: list[str] = []

    if "retrieval_recall_at_k" in applicable and gold.relevant_source_ids:
        k = int(case.input.get("retrieval_k", case.input.get("max_results", 3)))
        retrieved = set(actual.retrieved_ids[:k])
        numerator = float(len(retrieved & set(gold.relevant_source_ids)))
        denominator = float(len(gold.relevant_source_ids))
        value = numerator / denominator
        observations["retrieval_recall_at_k"] = V07AgentMetricObservation(
            value=value, numerator=numerator, denominator=denominator, unit="ratio"
        )
        if value != 1.0:
            mismatches.append("retrieval_recall_at_k")

    citation_support = _citation_support(actual, gold)
    if "citation_precision" in applicable and actual.citations:
        numerator = float(sum(citation_support.values()))
        denominator = float(len(actual.citations))
        value = numerator / denominator
        observations["citation_precision"] = V07AgentMetricObservation(
            value=value, numerator=numerator, denominator=denominator, unit="ratio"
        )
        if value != 1.0:
            mismatches.append("citation_precision")

    if "unsupported_claim_rate" in applicable and actual.claim_ids:
        supported_claims = {
            claim_id
            for (claim_id, _, _), supported in citation_support.items()
            if supported
        }
        numerator = float(
            sum(claim_id not in supported_claims for claim_id in actual.claim_ids)
        )
        denominator = float(len(actual.claim_ids))
        value = numerator / denominator
        observations["unsupported_claim_rate"] = V07AgentMetricObservation(
            value=value, numerator=numerator, denominator=denominator, unit="ratio"
        )
        if value != 0.0:
            mismatches.append("unsupported_claim_rate")

    if "abstention_accuracy" in applicable and gold.abstention_field is not None:
        if gold.abstention_field in {"target_claim_abstained", "answer_abstained"}:
            actual_abstention: bool | str | None = actual.target_claim_abstained
        elif gold.abstention_field == "analysis_admitted":
            actual_abstention = actual.analysis_admitted
        else:
            actual_abstention = actual.admission_status
        matched = actual_abstention == gold.abstention_expected
        observations["abstention_accuracy"] = V07AgentMetricObservation(
            value=float(matched), numerator=float(matched), denominator=1.0, unit="ratio"
        )
        if not matched:
            mismatches.append("abstention_accuracy")

    if "task_completion_rate" in applicable:
        expected_completion = bool(gold.expected.get("task_completed", True))
        matched = actual.task_completed == expected_completion
        observations["task_completion_rate"] = V07AgentMetricObservation(
            value=float(matched), numerator=float(matched), denominator=1.0, unit="ratio"
        )
        if not matched:
            mismatches.append("task_completion_rate")

    if "cost" in applicable:
        observations["cost"] = V07AgentMetricObservation(
            value=float(actual.cost_amount), unit=actual.cost_currency
        )
    if "latency" in applicable:
        observations["latency"] = V07AgentMetricObservation(
            value=actual.latency_ms, unit="ms"
        )

    expected_checks = {
        "task_state": actual.task_state,
        "partial_results_preserved": actual.partial_results_preserved,
        "failed_batch_count": actual.failed_batch_count,
        "replay_request_count": actual.replay_request_count,
        "retrieved_ids": list(actual.retrieved_ids),
        "error_type": actual.error_type,
        "admission_status": actual.admission_status,
        "analysis_type": actual.analysis_type,
        "analysis_valid": actual.analysis_valid,
        "analysis_admitted": actual.analysis_admitted,
        "valid_row_count": actual.valid_row_count,
        "invalid_row_count": actual.invalid_row_count,
        "report_generated": actual.report_generated,
        "external_actions": actual.external_actions,
    }
    if "conflict_ids" in gold.expected:
        direct_ids, interaction_outcomes, conflict_ids = _conflict_observations(
            case, fixture, actual, citation_support
        )
        if "direct_source_ids" in gold.expected and set(
            gold.expected["direct_source_ids"]
        ) != set(direct_ids):
            mismatches.append("direct_source_ids")
        if (
            "interaction_outcomes" in gold.expected
            and gold.expected["interaction_outcomes"] != interaction_outcomes
        ):
            mismatches.append("interaction_outcomes")
        if set(gold.expected["conflict_ids"]) != set(conflict_ids):
            mismatches.append("conflict_ids")
    for field, observed in expected_checks.items():
        if field in gold.expected and gold.expected[field] != observed:
            mismatches.append(field)
    output = "\n".join(
        [actual.answer or "", *(citation.claim_text for citation in actual.citations)]
    ).casefold()
    if any(marker.casefold() in output for marker in gold.forbidden_markers):
        mismatches.append("forbidden_markers_present")
    unique_mismatches = tuple(dict.fromkeys(mismatches))
    return V07AgentCaseScore(
        id=case.id,
        category=case.category,
        passed=not unique_mismatches,
        mismatches=unique_mismatches,
        applicable_metrics=applicable,
        actual=actual,
        metric_observations=observations,
    )


def aggregate_v07_agent_scores(
    scores: Sequence[V07AgentCaseScore],
) -> V07AgentAggregate:
    """Micro-aggregate rates, sum cost, and report median local latency."""

    total = len(scores)
    passed = sum(score.passed for score in scores)
    categories = Counter(score.category for score in scores)
    passed_categories = Counter(score.category for score in scores if score.passed)
    by_category = {
        category: V07CategorySummary(
            total=count,
            passed=passed_categories[category],
            failed=count - passed_categories[category],
            pass_rate=(passed_categories[category] / count if count else 0.0),
        )
        for category, count in sorted(categories.items())
    }
    metrics: dict[str, V07MetricResult] = {}
    scopes = {
        "retrieval_recall_at_k": "micro across declared gold source IDs",
        "citation_precision": "micro across emitted claim-citation pairs",
        "unsupported_claim_rate": "micro across emitted atomic claims",
        "abstention_accuracy": "micro across applicable abstention decisions",
        "task_completion_rate": (
            "micro across applicable substantive Research outcomes; "
            "human-review and budget handoffs are not automatic completion"
        ),
        "cost": "sum across model-call audit costs",
        "latency": "median per-case wall-clock diagnostic",
    }
    for metric in _RATE_METRICS:
        values = [
            score.metric_observations[metric]
            for score in scores
            if metric in score.metric_observations
        ]
        numerator = sum(item.numerator or 0.0 for item in values)
        denominator = sum(item.denominator or 0.0 for item in values)
        metrics[metric] = V07MetricResult(
            status="measured" if denominator else "not_applicable",
            value=(numerator / denominator if denominator else None),
            numerator=(numerator if denominator else None),
            denominator=(denominator if denominator else None),
            unit="ratio",
            scope=scopes[metric],
            not_applicable_cases=total - len(values),
        )

    cost_values = [
        score.metric_observations["cost"]
        for score in scores
        if "cost" in score.metric_observations
    ]
    non_zero_currencies = {
        item.unit for item in cost_values if item.value != 0.0
    }
    if len(non_zero_currencies) > 1:
        raise ValueError("cannot aggregate non-zero costs in multiple currencies")
    currency = next(
        iter(non_zero_currencies), cost_values[0].unit if cost_values else "CNY"
    )
    metrics["cost"] = V07MetricResult(
        status="measured" if cost_values else "not_applicable",
        value=(sum(item.value for item in cost_values) if cost_values else None),
        unit=currency,
        scope=scopes["cost"],
        not_applicable_cases=total - len(cost_values),
    )
    latency_values = [
        score.metric_observations["latency"].value
        for score in scores
        if "latency" in score.metric_observations
    ]
    metrics["latency"] = V07MetricResult(
        status="measured" if latency_values else "not_applicable",
        value=(float(median(latency_values)) if latency_values else None),
        unit="ms",
        scope=scopes["latency"],
        not_applicable_cases=total - len(latency_values),
    )
    if set(metrics) != V07_METRICS:
        raise AssertionError("aggregate must contain the fixed seven metrics")
    return V07AgentAggregate(
        total=total,
        passed=passed,
        failed=total - passed,
        by_category=by_category,
        metrics=metrics,
    )


__all__ = [
    "V07AgentActual",
    "V07AgentAggregate",
    "V07AgentCaseScore",
    "V07AgentCitationObservation",
    "V07AgentFixture",
    "V07AgentGold",
    "V07AgentMetricObservation",
    "V07AgentRunContext",
    "V07FrozenAnalysisFixture",
    "V07FrozenPubMedBatch",
    "V07FrozenToolExecutor",
    "V07QuestionScope",
    "V07SourceAlias",
    "aggregate_v07_agent_scores",
    "build_v07_agent_fixture",
    "build_v07_agent_fixtures",
    "project_agent_state",
    "project_v07_agent_actual",
    "project_v07_agent_gold",
    "score_v07_agent_case",
]
