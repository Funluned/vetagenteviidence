"""Fair v0.7 rules/single-agent/dual-agent comparison orchestration.

The comparison runs the Research Agent exactly once per case.  The single
branch scores that immutable state directly, while the dual branch hands the
same state to the Evidence Reviewer.  Consequently retrieval, evidence, and
tool traces are shared rather than re-run.  This module performs no file,
credential, or network discovery; callers supply frozen fixtures, gold
records, providers, budgets, and the read-only rules baseline reference.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.agent_providers import LLMProvider
from vetevidence.agent_runtime import (
    AgentBudget,
    AgentDraft,
    AgentModelCallAudit,
    AgentPhase,
    AgentState,
    AgentStopReason,
    render_agent_claims,
    run_research_agent,
)
from vetevidence.evidence_reviewer import (
    SAFE_REFUSAL,
    EvidenceReviewBudget,
    EvidenceReviewCallAudit,
    EvidenceReviewResult,
    EvidenceReviewStatus,
    run_evidence_review,
)
from vetevidence.v07_agent_evaluation import (
    V07AgentActual,
    V07AgentAggregate,
    V07AgentCaseScore,
    V07AgentFixture,
    V07FrozenToolExecutor,
    aggregate_v07_agent_scores,
    project_agent_state,
    project_v07_agent_gold,
    score_v07_agent_case,
)
from vetevidence.v07_evaluation import (
    V07BaselineReport,
    V07EvaluationCase,
    V07ExpectedCase,
)


ExecutionMode = Literal["fake", "real"]
ProviderRole = Literal["research", "reviewer", "research_revision"]
ProviderFactory: TypeAlias = Callable[..., LLMProvider]
ProviderSource: TypeAlias = LLMProvider | ProviderFactory
ResearchBudgetSource: TypeAlias = AgentBudget | Callable[..., AgentBudget] | None
ReviewBudgetSource: TypeAlias = (
    EvidenceReviewBudget | Callable[..., EvidenceReviewBudget] | None
)

_HIGHER_IS_BETTER = {
    "retrieval_recall_at_k",
    "citation_precision",
    "abstention_accuracy",
    "task_completion_rate",
}
_LOWER_IS_BETTER = {"unsupported_claim_rate", "cost", "latency"}
_RULES_BASELINE_PATH = "data/eval/v0.7/baselines/rules_v1.json"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert provider-owned audit records without guessing their schema."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _strip_fake_volatility(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_fake_volatility(item)
            for key, item in value.items()
            if key not in {"generated_at", "latency", "latency_ms"}
        }
    if isinstance(value, list):
        return [_strip_fake_volatility(item) for item in value]
    return value


def _result_hash(payload: Mapping[str, Any], mode: ExecutionMode) -> str:
    hash_payload: Any = payload
    if mode == "fake":
        hash_payload = _strip_fake_volatility(payload)
    return _sha256_json(hash_payload)


class _ComparisonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class V07RulesBaselineReference(_ComparisonModel):
    """Read-only pointer and score summary for the existing rules_v1 branch."""

    baseline_path: str = Field(min_length=1)
    provider: Literal["rules_v1"] = "rules_v1"
    deterministic_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total: int = Field(ge=1, strict=True)
    passed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    pass_rate: float = Field(ge=0.0, le=1.0)
    system_profile: dict[str, Any]

    @model_validator(mode="after")
    def validate_summary(self) -> "V07RulesBaselineReference":
        if self.passed + self.failed != self.total:
            raise ValueError("rules baseline passed/failed counts must equal total")
        expected_rate = self.passed / self.total
        if abs(self.pass_rate - expected_rate) > 1e-12:
            raise ValueError("rules baseline pass_rate does not match its counts")
        return self


def build_rules_baseline_reference(
    baseline: V07RulesBaselineReference | V07BaselineReport | Mapping[str, Any],
    *,
    baseline_path: str = _RULES_BASELINE_PATH,
) -> V07RulesBaselineReference:
    """Normalize a saved rules report or an already-minimized reference."""

    if isinstance(baseline, V07RulesBaselineReference):
        return baseline
    if isinstance(baseline, Mapping) and "baseline_path" in baseline:
        return V07RulesBaselineReference.model_validate(baseline)
    report = (
        baseline
        if isinstance(baseline, V07BaselineReport)
        else V07BaselineReport.model_validate(baseline)
    )
    return V07RulesBaselineReference(
        baseline_path=baseline_path,
        deterministic_result_sha256=report.deterministic_result_sha256,
        implementation_sha256=report.system.implementation_sha256,
        total=report.summary.total,
        passed=report.summary.passed,
        failed=report.summary.failed,
        pass_rate=report.summary.pass_rate,
        system_profile=report.system.model_dump(mode="json"),
    )


class V07ProviderAuditSlice(_ComparisonModel):
    """Credential-free provider audit records captured during one stage."""

    binding: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_version: str | None = None
    provider_audit_available: bool
    http_attempts: int = Field(ge=0, strict=True)
    settled_cost_cny: Decimal | None = Field(default=None, ge=0)
    records: tuple[dict[str, Any], ...] = ()


class V07BranchUsage(_ComparisonModel):
    logical_model_calls: int = Field(ge=0, strict=True)
    provider_reported_model_calls: int = Field(ge=0, strict=True)
    real_model_calls: int = Field(ge=0, strict=True)
    logical_network_calls: int = Field(ge=0, strict=True)
    actual_http_attempts: int = Field(ge=0, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    reasoning_tokens: int = Field(ge=0, strict=True)
    costs_by_currency: dict[str, Decimal] = Field(default_factory=dict)
    cost_accounting_source: Literal[
        "provider_settled_audit", "runtime_compatibility", "mixed"
    ]
    latency_ms: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_costs(self) -> "V07BranchUsage":
        for currency, amount in self.costs_by_currency.items():
            if not isinstance(currency, str) or not currency.isalpha() or len(currency) != 3:
                raise ValueError("usage currencies must be three letters")
            if currency != currency.upper():
                raise ValueError("usage currencies must be uppercase")
            if not amount.is_finite() or amount < 0:
                raise ValueError("usage costs must be finite and non-negative")
        return self


def _combine_usage(*items: V07BranchUsage) -> V07BranchUsage:
    costs: dict[str, Decimal] = {}
    for item in items:
        for currency, amount in item.costs_by_currency.items():
            costs[currency] = costs.get(currency, Decimal("0")) + amount
    sources = {item.cost_accounting_source for item in items}
    cost_source: Literal[
        "provider_settled_audit", "runtime_compatibility", "mixed"
    ] = next(iter(sources)) if len(sources) == 1 else "mixed"
    return V07BranchUsage(
        logical_model_calls=sum(item.logical_model_calls for item in items),
        provider_reported_model_calls=sum(
            item.provider_reported_model_calls for item in items
        ),
        real_model_calls=sum(item.real_model_calls for item in items),
        logical_network_calls=sum(item.logical_network_calls for item in items),
        actual_http_attempts=sum(item.actual_http_attempts for item in items),
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=sum(item.output_tokens for item in items),
        reasoning_tokens=sum(item.reasoning_tokens for item in items),
        costs_by_currency=costs,
        cost_accounting_source=cost_source,
        latency_ms=sum(item.latency_ms for item in items),
    )


class V07SharedCaseHashes(_ComparisonModel):
    research_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class V07AgentBranchResult(_ComparisonModel):
    actual: V07AgentActual
    score: V07AgentCaseScore
    usage: V07BranchUsage

    @model_validator(mode="after")
    def score_matches_actual(self) -> "V07AgentBranchResult":
        if self.score.actual != self.actual:
            raise ValueError("branch score must contain the same projected actual")
        non_zero = {
            currency
            for currency, amount in self.usage.costs_by_currency.items()
            if amount != 0
        }
        if len(non_zero) > 1:
            raise ValueError("one scored branch cannot use multiple non-zero currencies")
        expected_cost = sum(
            self.usage.costs_by_currency.values(), Decimal("0")
        )
        expected_currency = next(
            iter(non_zero),
            next(iter(self.usage.costs_by_currency), self.actual.cost_currency),
        )
        if (
            self.actual.cost_amount != expected_cost
            or self.actual.cost_currency != expected_currency
        ):
            raise ValueError("scored branch cost must match its audited usage")
        return self


class V07AgentComparisonCase(_ComparisonModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    shared_hashes: V07SharedCaseHashes
    research_state: AgentState = Field(repr=False)
    single: V07AgentBranchResult
    dual: V07AgentBranchResult
    review_incremental_usage: V07BranchUsage
    review: EvidenceReviewResult = Field(repr=False)
    research_call_audits: tuple[AgentModelCallAudit, ...] = ()
    review_call_audits: tuple[EvidenceReviewCallAudit, ...] = ()
    provider_audit_slices: tuple[V07ProviderAuditSlice, ...] = ()
    retrieval_reused: Literal[True] = True

    @model_validator(mode="after")
    def validate_shared_branch(self) -> "V07AgentComparisonCase":
        hashes = self.shared_hashes
        if self.research_state.canonical_sha256 != hashes.research_state_sha256:
            raise ValueError("shared research-state hash does not match state")
        if (
            self.research_state.evidence_ledger.canonical_sha256
            != hashes.evidence_ledger_sha256
        ):
            raise ValueError("shared evidence-ledger hash does not match state")
        if self.review.shared_research_state_sha256 != hashes.research_state_sha256:
            raise ValueError("review did not receive the shared research state")
        if self.review.shared_draft_sha256 != hashes.initial_draft_sha256:
            raise ValueError("review did not receive the shared initial draft")
        if self.review.shared_evidence_ledger_sha256 != hashes.evidence_ledger_sha256:
            raise ValueError("review did not receive the shared evidence ledger")
        if self.review.shared_tool_trace_sha256 != hashes.tool_trace_sha256:
            raise ValueError("review did not receive the shared tool trace")
        if self.research_call_audits != self.research_state.model_call_audits:
            raise ValueError("research audit copy does not match the shared state")
        if self.review_call_audits != self.review.call_audits:
            raise ValueError("review audit copy does not match the review result")
        if self.dual.usage != _combine_usage(
            self.single.usage, self.review_incremental_usage
        ):
            raise ValueError("dual usage must equal shared research plus review usage")
        single = self.single.actual
        dual = self.dual.actual
        shared_fields = (
            "retrieved_ids",
            "evidence",
            "failed_batch_count",
            "replay_request_count",
            "partial_results_preserved",
            "error_type",
            "analysis_type",
            "analysis_valid",
            "analysis_admitted",
            "valid_row_count",
            "invalid_row_count",
            "report_generated",
            "external_actions",
        )
        if any(getattr(single, field) != getattr(dual, field) for field in shared_fields):
            raise ValueError("reviewer changed retrieval or tool-derived observations")
        if self.review.status != EvidenceReviewStatus.APPROVED:
            if dual.claim_ids or dual.answer != SAFE_REFUSAL:
                raise ValueError("non-approved dual branch must expose only safe refusal")
        return self


class V07MetricDelta(_ComparisonModel):
    preferred_direction: Literal["higher", "lower"]
    single_value: float | None = None
    dual_value: float | None = None
    dual_minus_single: float | None = None
    unit: str


class V07ActualSpend(_ComparisonModel):
    shared_research: V07BranchUsage
    reviewer_and_revision: V07BranchUsage
    total_actual: V07BranchUsage
    accounting_rule: Literal[
        "shared Research is charged once; reviewer and revision are incremental"
    ] = "shared Research is charged once; reviewer and revision are incremental"

    @model_validator(mode="after")
    def validate_accounting(self) -> "V07ActualSpend":
        if self.total_actual != _combine_usage(
            self.shared_research, self.reviewer_and_revision
        ):
            raise ValueError("actual spend double-counted or omitted a branch")
        return self


class V07AgentComparisonReport(_ComparisonModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version: Literal["v0.7.0"] = "v0.7.0"
    evaluation_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_review_status: str = Field(min_length=1)
    boundaries: tuple[str, ...] = Field(min_length=3)
    generated_at: datetime
    execution_mode: ExecutionMode
    rules_baseline: V07RulesBaselineReference
    single_aggregate: V07AgentAggregate
    dual_aggregate: V07AgentAggregate
    metric_deltas: dict[str, V07MetricDelta]
    actual_spend: V07ActualSpend
    cases: tuple[V07AgentComparisonCase, ...]
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_policy: Literal[
        "deterministic_fake_excludes_generated_at_and_latency",
        "real_run_full_content_integrity_not_deterministic_replay",
    ]
    provider_budget_policy: Literal[
        "shared paid-run ceiling is enforced by caller-supplied providers"
    ] = "shared paid-run ceiling is enforced by caller-supplied providers"

    @model_validator(mode="after")
    def validate_report(self) -> "V07AgentComparisonReport":
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.single_aggregate.total != len(self.cases):
            raise ValueError("single aggregate count does not match cases")
        if self.dual_aggregate.total != len(self.cases):
            raise ValueError("dual aggregate count does not match cases")
        expected_policy = (
            "deterministic_fake_excludes_generated_at_and_latency"
            if self.execution_mode == "fake"
            else "real_run_full_content_integrity_not_deterministic_replay"
        )
        if self.hash_policy != expected_policy:
            raise ValueError("hash policy does not match execution mode")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != _result_hash(payload, self.execution_mode):
            raise ValueError("comparison result hash does not match report content")
        return self


class _AuditSnapshot:
    __slots__ = ("available", "records")

    def __init__(self, available: bool, records: tuple[Any, ...]) -> None:
        self.available = available
        self.records = records


def _provider_records(provider: LLMProvider) -> _AuditSnapshot:
    if not hasattr(provider, "audit_records"):
        return _AuditSnapshot(False, ())
    records = getattr(provider, "audit_records")
    if callable(records):
        records = records()
    if not isinstance(records, (tuple, list)):
        raise TypeError("provider audit_records must be a sequence")
    return _AuditSnapshot(True, tuple(records))


def _provider_identity(provider: LLMProvider) -> tuple[str, str | None]:
    model_name = getattr(provider, "model_name", None)
    model_version = getattr(provider, "model_version", None)
    if not isinstance(model_name, str) or not model_name:
        raise TypeError("provider model_name must be a non-empty string")
    if model_version is not None and (
        not isinstance(model_version, str) or not model_version
    ):
        raise TypeError("provider model_version must be a non-empty string or None")
    if not callable(getattr(provider, "generate", None)):
        raise TypeError("provider must expose generate(request)")
    return model_name, model_version


def _call_factory(factory: ProviderFactory, case: V07EvaluationCase, role: str) -> Any:
    """Support conventional zero-, one-, or two-argument provider factories."""

    import inspect

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(case, role)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
    ]
    variadic = any(
        parameter.kind == parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if variadic or len(positional) >= 2:
        return factory(case, role)
    if len(positional) == 1:
        return factory(case)
    return factory()


def _resolve_provider(
    source: ProviderSource,
    case: V07EvaluationCase,
    role: ProviderRole,
) -> LLMProvider:
    provider = source if callable(getattr(source, "generate", None)) else None
    if provider is None:
        if not callable(source):
            raise TypeError("provider source must be a provider or factory")
        provider = _call_factory(source, case, role)
    _provider_identity(provider)
    fake = getattr(provider, "fake", None)
    network_used = getattr(provider, "network_used", None)
    if not isinstance(fake, bool) or not isinstance(network_used, bool):
        raise TypeError("provider fake/network_used flags must be booleans")
    return provider


def _resolve_budget(
    source: ResearchBudgetSource | ReviewBudgetSource,
    case: V07EvaluationCase,
    expected_type: type[AgentBudget] | type[EvidenceReviewBudget],
) -> AgentBudget | EvidenceReviewBudget | None:
    if source is None:
        return None
    budget: Any = source
    if not isinstance(source, expected_type):
        if not callable(source):
            raise TypeError("budget source must be a budget or factory")
        budget = _call_factory(source, case, "budget")
    if not isinstance(budget, expected_type):
        raise TypeError(f"budget factory must return {expected_type.__name__}")
    usage_fields = (
        "normal_model_calls_used",
        "review_calls_used",
        "revision_calls_used",
        "retries_used",
        "tool_calls_used",
        "total_tokens_used",
        "cost_amount_used",
    )
    if any(getattr(budget, field, 0) != 0 for field in usage_fields):
        raise ValueError("comparison budgets must start with zero observed usage")
    return budget.model_copy(deep=True)


def _capture_slice(
    provider: LLMProvider,
    before: _AuditSnapshot,
    *,
    binding: str,
) -> V07ProviderAuditSlice:
    after = _provider_records(provider)
    if before.available != after.available:
        raise RuntimeError("provider audit availability changed during a run")
    if len(after.records) < len(before.records):
        raise RuntimeError("provider audit records were truncated during a run")
    records = after.records[len(before.records) :]
    attempts = 0
    settled_costs: list[Decimal] = []
    all_records_have_settled_cost = bool(records)
    for record in records:
        raw = record.get("attempts", 0) if isinstance(record, Mapping) else getattr(
            record, "attempts", 0
        )
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise TypeError("provider audit attempts must be a non-negative integer")
        attempts += raw
        if isinstance(record, Mapping):
            settled = record.get("settled_cost_cny")
            has_settled = "settled_cost_cny" in record
        else:
            settled = getattr(record, "settled_cost_cny", None)
            has_settled = hasattr(record, "settled_cost_cny")
        if not has_settled:
            all_records_have_settled_cost = False
        else:
            amount = Decimal(str(settled))
            if not amount.is_finite() or amount < 0:
                raise ValueError("provider settled cost must be finite and non-negative")
            settled_costs.append(amount)
    provider_name = getattr(provider, "name", None)
    if not isinstance(provider_name, str) or not provider_name:
        raise TypeError("provider name must be a non-empty string")
    model_name, model_version = _provider_identity(provider)
    return V07ProviderAuditSlice(
        binding=binding,
        provider_name=provider_name,
        model_name=model_name,
        model_version=model_version,
        provider_audit_available=after.available,
        http_attempts=attempts,
        settled_cost_cny=(
            sum(settled_costs, Decimal("0"))
            if all_records_have_settled_cost
            else None
        ),
        records=tuple(_jsonable(record) for record in records),
    )


def _usage_from_audits(
    audits: Sequence[AgentModelCallAudit | EvidenceReviewCallAudit],
    *,
    latency_ms: float,
    audit_slices: Sequence[V07ProviderAuditSlice],
    default_currency: str,
) -> V07BranchUsage:
    costs: dict[str, Decimal] = {}
    for audit in audits:
        amount = audit.usage.cost_amount
        if amount != 0:
            currency = audit.usage.cost_currency
            costs[currency] = costs.get(currency, Decimal("0")) + amount
    if not costs:
        costs[default_currency] = Decimal("0")
    nonempty_slices = [item for item in audit_slices if item.records]
    uses_settled_cost = bool(nonempty_slices) and all(
        item.settled_cost_cny is not None for item in nonempty_slices
    ) and sum(len(item.records) for item in nonempty_slices) == len(audits)
    if uses_settled_cost:
        costs = {
            "CNY": sum(
                (
                    item.settled_cost_cny or Decimal("0")
                    for item in nonempty_slices
                ),
                Decimal("0"),
            )
        }
    exact_available = any(item.provider_audit_available for item in audit_slices)
    http_attempts = sum(item.http_attempts for item in audit_slices)
    if not exact_available:
        http_attempts = sum(audit.network_used for audit in audits)
    return V07BranchUsage(
        logical_model_calls=len(audits),
        provider_reported_model_calls=sum(
            audit.usage.provider_reported_model_calls for audit in audits
        ),
        real_model_calls=sum(not audit.fake for audit in audits),
        logical_network_calls=sum(audit.network_used for audit in audits),
        actual_http_attempts=http_attempts,
        input_tokens=sum(audit.usage.input_tokens for audit in audits),
        output_tokens=sum(audit.usage.output_tokens for audit in audits),
        reasoning_tokens=sum(audit.usage.reasoning_tokens for audit in audits),
        costs_by_currency=costs,
        cost_accounting_source=(
            "provider_settled_audit"
            if uses_settled_cost
            else "runtime_compatibility"
        ),
        latency_ms=latency_ms,
    )


def _draft_from_state(state: AgentState) -> AgentDraft:
    if state.claims:
        return AgentDraft(refusal=False, refusal_reason=None, claims=state.claims)
    return AgentDraft(
        refusal=True,
        refusal_reason=state.answer or "insufficient_evidence",
        claims=(),
    )


def _shared_hashes(state: AgentState) -> V07SharedCaseHashes:
    draft = _draft_from_state(state)
    return V07SharedCaseHashes(
        research_state_sha256=state.canonical_sha256,
        initial_draft_sha256=_sha256_json(draft.model_dump(mode="json")),
        evidence_ledger_sha256=state.evidence_ledger.canonical_sha256,
        tool_trace_sha256=_sha256_json(
            [item.model_dump(mode="json") for item in state.tool_results]
        ),
    )


def _review_projection_state(
    state: AgentState,
    review: EvidenceReviewResult,
) -> AgentState:
    draft = review.final_draft
    has_claims = bool(draft.claims)
    if review.status != EvidenceReviewStatus.APPROVED:
        phase = AgentPhase.HUMAN_REVIEW_REQUIRED
        stop_reason = AgentStopReason.HUMAN_REVIEW_REQUIRED
        answer = SAFE_REFUSAL
    elif has_claims:
        phase = AgentPhase.COMPLETED
        stop_reason = AgentStopReason.COMPLETED
        answer = render_agent_claims(draft.claims)
    else:
        phase = AgentPhase.INSUFFICIENT_EVIDENCE
        stop_reason = AgentStopReason.INSUFFICIENT_EVIDENCE
        answer = review.final_answer
    return AgentState.model_validate(
        {
            **state.model_dump(mode="python"),
            "phase": phase,
            "stop_reason": stop_reason,
            "claims": draft.claims,
            "answer": answer,
        }
    )


def _dual_actual(
    base: V07AgentActual,
    review_audits: Sequence[EvidenceReviewCallAudit],
    *,
    total_latency_ms: float,
) -> V07AgentActual:
    non_zero = {
        base.cost_currency
        for _ in (0,)
        if base.cost_amount != 0
    } | {
        audit.usage.cost_currency
        for audit in review_audits
        if audit.usage.cost_amount != 0
    }
    if len(non_zero) > 1:
        raise ValueError("research and review costs use different currencies")
    currency = next(iter(non_zero), base.cost_currency)
    review_cost = sum(
        (audit.usage.cost_amount for audit in review_audits), Decimal("0")
    )
    return base.model_copy(
        update={
            "model_calls": base.model_calls + len(review_audits),
            "real_model_calls": base.real_model_calls
            + sum(not audit.fake for audit in review_audits),
            "network_calls": base.network_calls
            + sum(audit.network_used for audit in review_audits),
            "input_tokens": base.input_tokens
            + sum(audit.usage.input_tokens for audit in review_audits),
            "output_tokens": base.output_tokens
            + sum(audit.usage.output_tokens for audit in review_audits),
            "cost_amount": base.cost_amount + review_cost,
            "cost_currency": currency,
            "latency_ms": total_latency_ms,
        }
    )


def _actual_with_usage_cost(
    actual: V07AgentActual,
    usage: V07BranchUsage,
) -> V07AgentActual:
    non_zero = {
        currency
        for currency, amount in usage.costs_by_currency.items()
        if amount != 0
    }
    if len(non_zero) > 1:
        raise ValueError("one branch cannot be scored in multiple currencies")
    currency = next(
        iter(non_zero),
        next(iter(usage.costs_by_currency), actual.cost_currency),
    )
    amount = sum(usage.costs_by_currency.values(), Decimal("0"))
    return actual.model_copy(
        update={"cost_amount": amount, "cost_currency": currency}
    )


def _normalize_by_id(
    values: Mapping[str, Any] | Sequence[Any],
    *,
    id_attribute: str,
    label: str,
) -> dict[str, Any]:
    if isinstance(values, Mapping):
        normalized = dict(values)
        for identifier, value in normalized.items():
            if getattr(value, id_attribute) != identifier:
                raise ValueError(f"{label} mapping key does not match object ID")
        return normalized
    normalized: dict[str, Any] = {}
    for value in values:
        identifier = getattr(value, id_attribute)
        if identifier in normalized:
            raise ValueError(f"duplicate {label} ID: {identifier}")
        normalized[identifier] = value
    return normalized


def _metric_deltas(
    single: V07AgentAggregate,
    dual: V07AgentAggregate,
) -> dict[str, V07MetricDelta]:
    if set(single.metrics) != set(dual.metrics):
        raise ValueError("single and dual aggregates expose different metrics")
    deltas: dict[str, V07MetricDelta] = {}
    for metric in sorted(single.metrics):
        left = single.metrics[metric]
        right = dual.metrics[metric]
        if left.unit != right.unit:
            raise ValueError(f"single/dual metric units differ for {metric}")
        direction: Literal["higher", "lower"]
        if metric in _HIGHER_IS_BETTER:
            direction = "higher"
        elif metric in _LOWER_IS_BETTER:
            direction = "lower"
        else:
            raise ValueError(f"unknown metric direction: {metric}")
        difference = (
            right.value - left.value
            if left.value is not None and right.value is not None
            else None
        )
        deltas[metric] = V07MetricDelta(
            preferred_direction=direction,
            single_value=left.value,
            dual_value=right.value,
            dual_minus_single=difference,
            unit=left.unit,
        )
    return deltas


def _validate_call_models(
    expected_identity: tuple[str, str | None],
    audits: Sequence[AgentModelCallAudit | EvidenceReviewCallAudit],
) -> None:
    for audit in audits:
        if (audit.model_name, audit.model_version) != expected_identity:
            raise ValueError(
                "provider response model name/version differs across comparison roles"
            )


def _build_report(
    *,
    evaluation_input_sha256: str,
    gold_review_status: str,
    boundaries: tuple[str, ...],
    generated_at: datetime,
    execution_mode: ExecutionMode,
    rules_baseline: V07RulesBaselineReference,
    single_aggregate: V07AgentAggregate,
    dual_aggregate: V07AgentAggregate,
    metric_deltas: dict[str, V07MetricDelta],
    actual_spend: V07ActualSpend,
    cases: tuple[V07AgentComparisonCase, ...],
) -> V07AgentComparisonReport:
    hash_policy = (
        "deterministic_fake_excludes_generated_at_and_latency"
        if execution_mode == "fake"
        else "real_run_full_content_integrity_not_deterministic_replay"
    )
    fields: dict[str, Any] = {
        "evaluation_input_sha256": evaluation_input_sha256,
        "gold_review_status": gold_review_status,
        "boundaries": boundaries,
        "generated_at": generated_at,
        "execution_mode": execution_mode,
        "rules_baseline": rules_baseline,
        "single_aggregate": single_aggregate,
        "dual_aggregate": dual_aggregate,
        "metric_deltas": metric_deltas,
        "actual_spend": actual_spend,
        "cases": cases,
        "hash_policy": hash_policy,
    }
    provisional = V07AgentComparisonReport.model_construct(
        **fields, result_sha256="0" * 64
    )
    payload = provisional.model_dump(mode="json", exclude={"result_sha256"})
    fields["result_sha256"] = _result_hash(payload, execution_mode)
    return V07AgentComparisonReport.model_validate(fields)


def run_v07_agent_comparison(
    cases: Sequence[V07EvaluationCase],
    expected: Mapping[str, V07ExpectedCase] | Sequence[V07ExpectedCase],
    fixtures: Mapping[str, V07AgentFixture] | Sequence[V07AgentFixture],
    *,
    rules_baseline: V07RulesBaselineReference
    | V07BaselineReport
    | Mapping[str, Any],
    research_provider: ProviderSource,
    reviewer_provider: ProviderSource,
    revision_provider: ProviderSource | None = None,
    research_budget: ResearchBudgetSource = None,
    review_budget: ReviewBudgetSource = None,
    execution_mode: ExecutionMode | None = None,
    generated_at: datetime | None = None,
    gold_review_status: str = "engineering_gold_pending_domain_expert_review",
    boundaries: Sequence[str] | None = None,
    rules_baseline_path: str = _RULES_BASELINE_PATH,
) -> V07AgentComparisonReport:
    """Run one fair single-vs-dual comparison over caller-supplied fixtures.

    Provider factories may accept ``()``, ``(case)``, or ``(case, role)``.
    Instances are also accepted.  All three roles must advertise and return the
    same model name/version.  A paid-run ceiling spanning all instances remains
    the responsibility of the caller-supplied provider layer.
    """

    case_tuple = tuple(cases)
    if not case_tuple:
        raise ValueError("comparison requires at least one case")
    case_ids = [case.id for case in case_tuple]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("comparison case IDs must be unique")
    expected_by_id = _normalize_by_id(
        expected, id_attribute="id", label="expected"
    )
    fixtures_by_id = _normalize_by_id(
        fixtures, id_attribute="case_id", label="fixture"
    )
    if set(expected_by_id) != set(case_ids) or set(fixtures_by_id) != set(case_ids):
        raise ValueError("cases, expected records, and fixtures must match exactly")

    reference = build_rules_baseline_reference(
        rules_baseline, baseline_path=rules_baseline_path
    )
    input_sha256 = _sha256_json(
        {
            "cases": [case.model_dump(mode="json") for case in case_tuple],
            "expected": {
                identifier: expected_by_id[identifier].model_dump(mode="json")
                for identifier in sorted(expected_by_id)
            },
            "fixtures": {
                identifier: fixtures_by_id[identifier].canonical_sha256
                for identifier in sorted(fixtures_by_id)
            },
        }
    )

    comparison_cases: list[V07AgentComparisonCase] = []
    observed_modes: set[ExecutionMode] = set()
    research_usage_items: list[V07BranchUsage] = []
    review_usage_items: list[V07BranchUsage] = []

    for case in case_tuple:
        fixture = fixtures_by_id[case.id]
        gold = project_v07_agent_gold(case, expected_by_id[case.id], fixture)
        research = _resolve_provider(research_provider, case, "research")
        reviewer = _resolve_provider(reviewer_provider, case, "reviewer")
        revision = (
            research
            if revision_provider is None
            else _resolve_provider(revision_provider, case, "research_revision")
        )
        identity = _provider_identity(research)
        if _provider_identity(reviewer) != identity or _provider_identity(revision) != identity:
            raise ValueError(
                "research, reviewer, and revision providers must use one model name/version"
            )

        research_before = _provider_records(research)
        started = perf_counter()
        with V07FrozenToolExecutor(fixture) as executor:
            research_state = run_research_agent(
                fixture.provider_question,
                provider=research,
                tool_executor=executor,
                run_id=fixture.run_id,
                budget=_resolve_budget(research_budget, case, AgentBudget),
            )
        research_latency_ms = (perf_counter() - started) * 1_000.0
        research_slice = _capture_slice(
            research, research_before, binding="research"
        )
        _validate_call_models(identity, research_state.model_call_audits)

        single_actual = project_agent_state(
            case,
            research_state,
            fixture,
            latency_ms=research_latency_ms,
        )
        single_usage = _usage_from_audits(
            research_state.model_call_audits,
            latency_ms=research_latency_ms,
            audit_slices=(research_slice,),
            default_currency=research_state.budget.cost_currency,
        )
        single_actual = _actual_with_usage_cost(single_actual, single_usage)
        single_score = score_v07_agent_case(case, fixture, gold, single_actual)

        review_providers: dict[int, tuple[LLMProvider, set[str], _AuditSnapshot]] = {}
        for provider, binding in (
            (reviewer, "reviewer"),
            (revision, "research_revision"),
        ):
            key = id(provider)
            if key not in review_providers:
                review_providers[key] = (
                    provider,
                    {binding},
                    _provider_records(provider),
                )
            else:
                review_providers[key][1].add(binding)

        review_started = perf_counter()
        review = run_evidence_review(
            research_state,
            reviewer_provider=reviewer,
            research_provider=revision,
            run_id=f"{fixture.run_id}:review",
            budget=_resolve_budget(review_budget, case, EvidenceReviewBudget),
        )
        review_latency_ms = (perf_counter() - review_started) * 1_000.0
        review_slices = tuple(
            _capture_slice(
                provider,
                before,
                binding="+".join(sorted(bindings)),
            )
            for provider, bindings, before in review_providers.values()
        )
        _validate_call_models(identity, review.call_audits)

        projection_state = _review_projection_state(research_state, review)
        dual_base = project_agent_state(
            case,
            projection_state,
            fixture,
            latency_ms=research_latency_ms + review_latency_ms,
        )
        dual_actual = _dual_actual(
            dual_base,
            review.call_audits,
            total_latency_ms=research_latency_ms + review_latency_ms,
        )
        review_usage = _usage_from_audits(
            review.call_audits,
            latency_ms=review_latency_ms,
            audit_slices=review_slices,
            default_currency=review.budget.cost_currency,
        )
        dual_usage = _combine_usage(single_usage, review_usage)
        dual_actual = _actual_with_usage_cost(dual_actual, dual_usage)
        dual_score = score_v07_agent_case(case, fixture, gold, dual_actual)
        hashes = _shared_hashes(research_state)

        configured_flags = {
            bool(getattr(research, "fake")),
            bool(getattr(reviewer, "fake")),
            bool(getattr(revision, "fake")),
        }
        audit_flags = {
            audit.fake
            for audit in (*research_state.model_call_audits, *review.call_audits)
        }
        all_flags = configured_flags | audit_flags
        if len(all_flags) != 1:
            raise ValueError("fake and real providers cannot be mixed in one comparison")
        observed_modes.add("fake" if all_flags == {True} else "real")

        comparison_cases.append(
            V07AgentComparisonCase(
                id=case.id,
                category=case.category,
                shared_hashes=hashes,
                research_state=research_state,
                single=V07AgentBranchResult(
                    actual=single_actual,
                    score=single_score,
                    usage=single_usage,
                ),
                dual=V07AgentBranchResult(
                    actual=dual_actual,
                    score=dual_score,
                    usage=dual_usage,
                ),
                review_incremental_usage=review_usage,
                review=review,
                research_call_audits=research_state.model_call_audits,
                review_call_audits=review.call_audits,
                provider_audit_slices=(research_slice, *review_slices),
            )
        )
        research_usage_items.append(single_usage)
        review_usage_items.append(review_usage)

    if len(observed_modes) != 1:
        raise ValueError("all comparison cases must use the same execution mode")
    observed_mode = next(iter(observed_modes))
    if execution_mode is not None and execution_mode != observed_mode:
        raise ValueError("declared execution_mode does not match provider audits")

    single_aggregate = aggregate_v07_agent_scores(
        [item.single.score for item in comparison_cases]
    )
    dual_aggregate = aggregate_v07_agent_scores(
        [item.dual.score for item in comparison_cases]
    )
    shared_research = _combine_usage(*research_usage_items)
    review_incremental = _combine_usage(*review_usage_items)
    actual_spend = V07ActualSpend(
        shared_research=shared_research,
        reviewer_and_revision=review_incremental,
        total_actual=_combine_usage(shared_research, review_incremental),
    )
    selected_boundaries = tuple(boundaries) if boundaries is not None else (
        (
            f"All {len(case_tuple)} selected cases are synthetic engineering "
            "fixtures; no result establishes clinical or scientific truth."
        ),
        (
            "Fake mode validates orchestration and provider contracts only; "
            "it does not validate model quality."
        ),
        (
            "Real mode means real provider API calls only; it does not turn "
            "synthetic fixtures into scientific evidence."
        ),
    )
    return _build_report(
        evaluation_input_sha256=input_sha256,
        gold_review_status=gold_review_status,
        boundaries=selected_boundaries,
        generated_at=generated_at or datetime.now(timezone.utc),
        execution_mode=observed_mode,
        rules_baseline=reference,
        single_aggregate=single_aggregate,
        dual_aggregate=dual_aggregate,
        metric_deltas=_metric_deltas(single_aggregate, dual_aggregate),
        actual_spend=actual_spend,
        cases=tuple(comparison_cases),
    )


__all__ = [
    "V07ActualSpend",
    "V07AgentBranchResult",
    "V07AgentComparisonCase",
    "V07AgentComparisonReport",
    "V07BranchUsage",
    "V07MetricDelta",
    "V07ProviderAuditSlice",
    "V07RulesBaselineReference",
    "V07SharedCaseHashes",
    "build_rules_baseline_reference",
    "run_v07_agent_comparison",
]
