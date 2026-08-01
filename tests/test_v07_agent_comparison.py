from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from vetevidence.agent_providers import (
    GenerationRequest,
    GenerationResponse,
    ProviderUsage,
)
from vetevidence.agent_runtime import AgentPhase, AgentStopReason
from vetevidence.evidence_reviewer import SAFE_REFUSAL, EvidenceReviewStatus
from vetevidence.v07_agent_comparison import (
    V07AgentComparisonReport,
    build_rules_baseline_reference,
    run_v07_agent_comparison,
)
from vetevidence.v07_agent_evaluation import build_v07_agent_fixture
from vetevidence.v07_agent_fake import V07ContractSmokeProvider
from vetevidence.v07_evaluation import (
    V07BaselineReport,
    load_v07_evaluation,
)


ROOT = Path(__file__).resolve().parents[1]
V07_ROOT = ROOT / "data" / "eval" / "v0.7"

PLAN = json.dumps(
    {
        "items": [
            {
                "tool_name": "pubmed.search",
                "arguments": {
                    "query": "quercetin amoxicillin FICI",
                    "max_results": 2,
                },
            }
        ]
    }
)

DRAFT = json.dumps(
    {
        "refusal": False,
        "refusal_reason": None,
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "The combination showed synergistic activity with FICI 0.4.",
                "scope": "Only the cited synthetic checkerboard experiment.",
                "citations": [
                    {
                        "source_id": "source-001",
                        "chunk_id": "source-001:abstract",
                        "support_quote": (
                            "showed synergistic activity of quercetin and amoxicillin"
                        ),
                    }
                ],
            }
        ],
    }
)

APPROVED = json.dumps(
    {
        "decision": "approved",
        "rationale": "The claim uses a ledger-bound verbatim quote and narrow scope.",
        "flagged_claim_ids": [],
    }
)

REJECTED = json.dumps(
    {
        "decision": "rejected",
        "rationale": "The candidate answer is not safe to expose.",
        "flagged_claim_ids": ["claim-1"],
    }
)


class ScriptedFakeProvider:
    name = "comparison-scripted-fake"
    model_name = "comparison-model"
    model_version = "test-v1"
    fake = True
    network_used = False

    def __init__(
        self,
        responses: list[str],
        *,
        cost_cny: float,
        model_version: str = "test-v1",
    ) -> None:
        self.responses = list(responses)
        self.cost_cny = cost_cny
        self.model_version = model_version
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected fake provider call")
        return GenerationResponse(
            text=self.responses.pop(0),
            provider_name=self.name,
            model_name=self.model_name,
            model_version=self.model_version,
            prompt_sha256=request.prompt_sha256,
            generation_parameters_sha256=request.generation_parameters_sha256,
            usage=ProviderUsage(
                input_tokens=10,
                output_tokens=5,
                model_calls=1,
                cost_amount=self.cost_cny,
                cost_currency="CNY",
            ),
            latency_ms=3.0,
            request_id=request.request_id,
            fake=True,
            network_used=False,
        )


class AuditedScriptedFakeProvider(ScriptedFakeProvider):
    """Offline fake that exposes DeepSeek-shaped settlement audit records."""

    def __init__(
        self,
        responses: list[str],
        *,
        settled_cost_cny: Decimal,
    ) -> None:
        super().__init__(responses, cost_cny=0.99)
        self.settled_cost_cny = settled_cost_cny
        self._audit_records: list[dict[str, object]] = []

    @property
    def audit_records(self) -> tuple[dict[str, object], ...]:
        return tuple(self._audit_records)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        response = super().generate(request)
        self._audit_records.append(
            {
                "attempts": 1,
                "settled_cost_cny": self.settled_cost_cny,
                "request_body_sha256": request.request_sha256,
            }
        )
        return response


@pytest.fixture(scope="module")
def comparison_inputs():
    loaded = load_v07_evaluation(
        V07_ROOT / "cases.json",
        V07_ROOT / "expected.json",
    )
    case = loaded.dataset.cases[0]
    fixture = build_v07_agent_fixture(case)
    baseline = V07BaselineReport.model_validate_json(
        (V07_ROOT / "baselines" / "rules_v1.json").read_text(encoding="utf-8")
    )
    return loaded, case, fixture, baseline


def _run(
    comparison_inputs,
    *,
    verdict: str = APPROVED,
    generated_at: datetime | None = None,
):
    loaded, case, fixture, baseline = comparison_inputs
    research = ScriptedFakeProvider([PLAN, DRAFT], cost_cny=0.10)
    reviewer = ScriptedFakeProvider([verdict], cost_cny=0.05)

    # Exercise both accepted provider forms: Research is an instance, Reviewer
    # is a two-argument factory.  The revision role reuses Research by default.
    def reviewer_factory(selected_case, role):
        assert selected_case.id == case.id
        assert role == "reviewer"
        return reviewer

    report = run_v07_agent_comparison(
        (case,),
        {case.id: loaded.expected[case.id]},
        (fixture,),
        rules_baseline=baseline,
        research_provider=research,
        reviewer_provider=reviewer_factory,
        execution_mode="fake",
        generated_at=generated_at,
    )
    return report, research, reviewer


def test_fair_shared_state_cost_accounting_and_fake_hash_are_stable(
    comparison_inputs,
) -> None:
    first, first_research, first_reviewer = _run(
        comparison_inputs,
        generated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    second, _, _ = _run(
        comparison_inputs,
        generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    case = first.cases[0]
    assert len(first_research.requests) == 2
    assert len(first_reviewer.requests) == 1
    assert case.review.shared_research_state_sha256 == (
        case.research_state.canonical_sha256
    )
    assert case.shared_hashes.research_state_sha256 == (
        case.review.shared_research_state_sha256
    )
    assert case.shared_hashes.initial_draft_sha256 == case.review.shared_draft_sha256
    assert case.shared_hashes.evidence_ledger_sha256 == (
        case.review.shared_evidence_ledger_sha256
    )
    assert case.shared_hashes.tool_trace_sha256 == (
        case.review.shared_tool_trace_sha256
    )
    assert case.single.actual.retrieved_ids == case.dual.actual.retrieved_ids
    assert case.single.actual.evidence == case.dual.actual.evidence

    assert case.single.usage.costs_by_currency == {"CNY": Decimal("0.2")}
    assert case.dual.usage.costs_by_currency == {"CNY": Decimal("0.25")}
    assert first.actual_spend.total_actual.costs_by_currency == {
        "CNY": Decimal("0.25")
    }
    assert first.actual_spend.total_actual.logical_model_calls == 3
    assert first.rules_baseline.total == 27
    assert first.rules_baseline.passed == 20
    assert first.rules_baseline.failed == 7
    assert first.gold_review_status == (
        "engineering_gold_pending_domain_expert_review"
    )
    assert len(first.boundaries) >= 3
    assert "synthetic" in first.boundaries[0]
    assert first.result_sha256 == second.result_sha256
    assert first.generated_at != second.generated_at


def test_nonapproved_review_projects_only_safe_refusal(comparison_inputs) -> None:
    report, _, _ = _run(comparison_inputs, verdict=REJECTED)
    case = report.cases[0]

    assert case.review.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert case.review.audit_candidate_draft.claims
    assert case.review.final_draft.refusal is True
    assert case.dual.actual.phase == AgentPhase.HUMAN_REVIEW_REQUIRED
    assert case.research_state.stop_reason == AgentStopReason.COMPLETED
    assert case.dual.actual.claim_ids == ()
    assert case.dual.actual.citations == ()
    assert case.dual.actual.answer == SAFE_REFUSAL
    assert case.single.actual.claim_ids == ("claim-1",)
    assert case.single.actual.retrieved_ids == case.dual.actual.retrieved_ids


def test_model_name_and_version_must_match_across_roles(comparison_inputs) -> None:
    loaded, case, fixture, baseline = comparison_inputs
    research = ScriptedFakeProvider([PLAN, DRAFT], cost_cny=0.0)
    reviewer = ScriptedFakeProvider(
        [APPROVED], cost_cny=0.0, model_version="different-version"
    )

    with pytest.raises(ValueError, match="one model name/version"):
        run_v07_agent_comparison(
            (case,),
            {case.id: loaded.expected[case.id]},
            (fixture,),
            rules_baseline=build_rules_baseline_reference(baseline),
            research_provider=research,
            reviewer_provider=reviewer,
        )
    assert research.requests == []
    assert reviewer.requests == []


def test_result_hash_detects_nonvolatile_report_tampering(comparison_inputs) -> None:
    report, _, _ = _run(comparison_inputs)
    payload = report.model_dump(mode="json")
    payload["rules_baseline"]["baseline_path"] = "tampered.json"

    with pytest.raises(ValidationError, match="result hash"):
        V07AgentComparisonReport.model_validate(payload)


def test_provider_settlement_audit_overrides_compatibility_float_cost(
    comparison_inputs,
) -> None:
    loaded, case, fixture, baseline = comparison_inputs
    research = AuditedScriptedFakeProvider(
        [PLAN, DRAFT], settled_cost_cny=Decimal("0.012345678901")
    )
    reviewer = AuditedScriptedFakeProvider(
        [APPROVED], settled_cost_cny=Decimal("0.004000000009")
    )

    report = run_v07_agent_comparison(
        (case,),
        {case.id: loaded.expected[case.id]},
        (fixture,),
        rules_baseline=baseline,
        research_provider=research,
        reviewer_provider=reviewer,
    )

    assert report.actual_spend.shared_research.costs_by_currency == {
        "CNY": Decimal("0.024691357802")
    }
    assert report.actual_spend.total_actual.costs_by_currency == {
        "CNY": Decimal("0.028691357811")
    }
    assert report.actual_spend.total_actual.actual_http_attempts == 3
    assert report.cases[0].single.usage.cost_accounting_source == (
        "provider_settled_audit"
    )
    assert report.cases[0].single.actual.cost_amount == Decimal("0.024691357802")


def test_tool_resilience_runs_success_failure_success_through_full_agent() -> None:
    loaded = load_v07_evaluation(
        V07_ROOT / "cases.json",
        V07_ROOT / "expected.json",
    )
    case = next(item for item in loaded.dataset.cases if item.id == "TOOL-02")
    fixture = build_v07_agent_fixture(case)
    baseline = V07BaselineReport.model_validate_json(
        (V07_ROOT / "baselines" / "rules_v1.json").read_text(encoding="utf-8")
    )

    def provider_factory(*_args):
        return V07ContractSmokeProvider(fixture)

    report = run_v07_agent_comparison(
        (case,),
        {case.id: loaded.expected[case.id]},
        (fixture,),
        rules_baseline=baseline,
        research_provider=provider_factory,
        reviewer_provider=provider_factory,
        execution_mode="fake",
    )

    result = report.cases[0]
    assert len(result.research_state.plan.items) == 3
    assert [item.status for item in result.research_state.tool_results] == [
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert result.single.actual.failed_batch_count == 1
    assert result.single.actual.replay_request_count == 3
    assert result.single.actual.partial_results_preserved is True
    assert result.single.actual.retrieved_ids == ("source-001", "source-002")
    assert result.single.score.metric_observations[
        "retrieval_recall_at_k"
    ].value == 1.0
    assert result.single.score.passed is True
    assert result.dual.score.passed is True
