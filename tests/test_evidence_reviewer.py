from __future__ import annotations

import json
from decimal import Decimal

import pytest

from vetevidence.agent_providers import (
    GenerationRequest,
    GenerationResponse,
    LLMProvider,
    ProviderFailure,
    ProviderUsage,
)
from vetevidence.agent_runtime import (
    AgentCitation,
    AgentClaim,
    AgentError,
    AgentPlan,
    AgentPhase,
    AgentState,
    AgentStopReason,
    EvidenceLedger,
)
from vetevidence.agent_tools import (
    AgentEvidenceGrade,
    ToolEvidence,
    ToolExecutionResult,
    ToolFailure,
)
from vetevidence.evidence_reviewer import (
    EVIDENCE_REVIEWER_SYSTEM_PROMPT,
    RESEARCH_REVISION_SYSTEM_PROMPT,
    SAFE_REFUSAL,
    EvidenceReviewBudget,
    EvidenceReviewStatus,
    run_evidence_review,
)


APPROVED = json.dumps(
    {
        "decision": "approved",
        "rationale": "Every current claim has a ledger-bound verbatim quote.",
        "flagged_claim_ids": [],
    }
)

CHANGES_REQUESTED = json.dumps(
    {
        "decision": "changes_requested",
        "rationale": "Narrow the wording of claim-1.",
        "flagged_claim_ids": ["claim-1"],
    }
)

REVISED_DRAFT = json.dumps(
    {
        "refusal": False,
        "refusal_reason": None,
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "The frozen fixture reports FICI 0.4.",
                "scope": "Limited to the cited frozen checkerboard fixture.",
                "citations": [
                    {
                        "source_id": "SYN-DIR-01",
                        "chunk_id": "SYN-DIR-01#abstract",
                        "support_quote": "reported FICI 0.4",
                    }
                ],
            }
        ],
    }
)


class ScriptedReviewerProvider:
    name = "scripted_reviewer_fake"
    model_name = "scripted-reviewer-v1"
    model_version = "test-v1"
    fake = True
    network_used = False

    def __init__(self, responses: list[str | ProviderFailure]) -> None:
        self.responses = list(responses)
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected fake model call")
        scripted = self.responses.pop(0)
        failure = scripted if isinstance(scripted, ProviderFailure) else None
        return GenerationResponse(
            text="" if failure else scripted,
            provider_name=self.name,
            model_name=self.model_name,
            model_version=self.model_version,
            prompt_sha256=request.prompt_sha256,
            generation_parameters_sha256=(
                request.generation_parameters_sha256
            ),
            usage=ProviderUsage(
                input_tokens=20,
                output_tokens=10,
                model_calls=1,
                cost_amount=0.01,
                cost_currency="CNY",
            ),
            latency_ms=12.5,
            request_id=request.request_id,
            failure=failure,
            fake=True,
            network_used=False,
        )


def _research_state() -> AgentState:
    evidence = ToolEvidence(
        source_id="SYN-DIR-01",
        chunk_id="SYN-DIR-01#abstract",
        content="The checkerboard assay reported FICI 0.4 in the frozen fixture.",
        source_type="synthetic_fixture",
    )
    claim = AgentClaim(
        claim_id="claim-1",
        text="The assay reported FICI 0.4.",
        scope="Limited to the cited frozen checkerboard fixture.",
        citations=(
            AgentCitation(
                source_id=evidence.source_id,
                chunk_id=evidence.chunk_id,
                support_quote="reported FICI 0.4",
            ),
        ),
    )
    return AgentState(
        run_id="research-1",
        question="What did the checkerboard assay report?",
        phase=AgentPhase.COMPLETED,
        stop_reason=AgentStopReason.COMPLETED,
        evidence_ledger=EvidenceLedger(items=(evidence,)),
        claims=(claim,),
        answer=claim.text,
        tool_results=(
            ToolExecutionResult(
                call_id="research-1:tool:1",
                tool_name="local_rag.search",
                call_signature_sha256="0" * 64,
                status="succeeded",
                evidence=(evidence,),
                frozen_replay=True,
            ),
        ),
    )


def _refusal_state() -> AgentState:
    return AgentState(
        run_id="research-refusal",
        question="Is there evidence for the unsupported question?",
        phase=AgentPhase.INSUFFICIENT_EVIDENCE,
        stop_reason=AgentStopReason.INSUFFICIENT_EVIDENCE,
        answer="insufficient_evidence",
    )


def _two_claim_research_state() -> AgentState:
    first = _research_state()
    second_evidence = ToolEvidence(
        source_id="SYN-DIR-02",
        chunk_id="SYN-DIR-02#abstract",
        content="The frozen growth fixture reported no regrowth at 24 hours.",
        source_type="synthetic_fixture",
    )
    second_claim = AgentClaim(
        claim_id="claim-2",
        text="The growth fixture reported no regrowth at 24 hours.",
        scope="Limited to the cited frozen growth fixture.",
        citations=(
            AgentCitation(
                source_id=second_evidence.source_id,
                chunk_id=second_evidence.chunk_id,
                support_quote="reported no regrowth at 24 hours",
            ),
        ),
    )
    all_evidence = first.evidence_ledger.items + (second_evidence,)
    all_claims = first.claims + (second_claim,)
    return AgentState(
        run_id="research-2",
        question=first.question,
        phase=AgentPhase.COMPLETED,
        stop_reason=AgentStopReason.COMPLETED,
        evidence_ledger=EvidenceLedger(items=all_evidence),
        claims=all_claims,
        answer="\n".join(claim.text for claim in all_claims),
        tool_results=(
            ToolExecutionResult(
                call_id="research-2:tool:1",
                tool_name="local_rag.search",
                call_signature_sha256="1" * 64,
                status="succeeded",
                evidence=all_evidence,
                frozen_replay=True,
            ),
        ),
    )


def test_reviewer_approves_same_read_only_research_draft_and_ledger() -> None:
    research = _research_state()
    before = research.model_dump_json()
    reviewer = ScriptedReviewerProvider([APPROVED])

    result = run_evidence_review(
        research,
        reviewer_provider=reviewer,
        run_id="dual-approved",
    )

    assert isinstance(reviewer, LLMProvider)
    assert result.status == EvidenceReviewStatus.APPROVED
    assert result.safe_refusal is False
    assert result.final_answer == (
        "The assay reported FICI 0.4.\n"
        "Scope: Limited to the cited frozen checkerboard fixture.\n"
        'Evidence: [SYN-DIR-01 | SYN-DIR-01#abstract] "reported FICI 0.4"'
    )
    assert result.initial_draft == result.final_draft
    assert result.shared_research_state_sha256 == research.canonical_sha256
    assert result.shared_evidence_ledger_sha256 == (
        research.evidence_ledger.canonical_sha256
    )
    assert len(result.shared_draft_sha256) == 64
    assert len(result.shared_tool_trace_sha256) == 64
    assert research.model_dump_json() == before
    assert result.budget.review_calls_used == 1
    assert result.budget.revision_calls_used == 0
    assert result.budget.retries_used == 0


def test_reviewer_blocks_context_only_claim_before_model_call() -> None:
    base = _research_state()
    contextual = base.evidence_ledger.items[0].model_copy(
        update={"evidence_grade": AgentEvidenceGrade.CONTEXTUAL}
    )
    research = base.model_copy(
        update={
            "evidence_ledger": EvidenceLedger(items=(contextual,)),
            "tool_results": tuple(
                result.model_copy(update={"evidence": (contextual,)})
                for result in base.tool_results
            ),
        }
    )
    reviewer = ScriptedReviewerProvider([])

    result = run_evidence_review(
        research,
        reviewer_provider=reviewer,
        run_id="review-context-only",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.safe_refusal is True
    assert result.errors[-1].code == "claim_without_direct_evidence"
    assert reviewer.requests == []


def test_reviewer_accepts_question_graded_direct_claim() -> None:
    base = _research_state()
    direct = base.evidence_ledger.items[0].model_copy(
        update={"evidence_grade": AgentEvidenceGrade.DIRECT_INTERACTION}
    )
    research = base.model_copy(
        update={
            "evidence_ledger": EvidenceLedger(items=(direct,)),
            "tool_results": tuple(
                result.model_copy(update={"evidence": (direct,)})
                for result in base.tool_results
            ),
        }
    )
    reviewer = ScriptedReviewerProvider([APPROVED])

    result = run_evidence_review(
        research,
        reviewer_provider=reviewer,
        run_id="review-graded-direct",
    )

    assert result.status == EvidenceReviewStatus.APPROVED
    assert result.safe_refusal is False
    assert len(reviewer.requests) == 1
    assert result.budget.model_calls_used == 1
    assert result.budget.total_tokens_used == 30
    assert result.budget.cost_amount_used == Decimal("0.01")
    assert result.budget.costs_by_currency == {"CNY": Decimal("0.01")}
    assert len(result.call_audits) == 1
    audit = result.call_audits[0]
    assert audit.role == "reviewer"
    assert audit.fake is True
    assert audit.network_used is False
    assert audit.latency_ms == 12.5
    assert audit.usage.input_tokens == 20
    assert audit.usage.output_tokens == 10
    assert len(audit.request_sha256) == 64
    assert reviewer.requests[0].generation_parameters["system_prompt"] == (
        EVIDENCE_REVIEWER_SYSTEM_PROMPT
    )
    assert EVIDENCE_REVIEWER_SYSTEM_PROMPT not in reviewer.requests[0].prompt
    review_payload = json.loads(
        reviewer.requests[0].prompt.partition("REVIEW_INPUT=")[2]
    )
    assert review_payload["research_phase"] == "completed"
    assert review_payload["research_stop_reason"] == "completed"
    assert review_payload["validated_plan"] is None
    assert review_payload["tool_trace"] == [
        {
            "call_id": "research-1:tool:1",
            "tool_name": "local_rag.search",
            "call_signature_sha256": "0" * 64,
            "status": "succeeded",
            "failure": None,
            "output": {},
            "evidence_refs": [
                {
                    "source_id": "SYN-DIR-01",
                    "chunk_id": "SYN-DIR-01#abstract",
                }
            ],
            "frozen_replay": True,
            "network_used": False,
            "external_actions": 0,
        }
    ]
    assert review_payload["research_errors"] == []
    assert review_payload["tool_trace_sha256"] == (
        result.shared_tool_trace_sha256
    )
    assert len(review_payload["review_tool_trace_sha256"]) == 64
    review_trace_json = json.dumps(review_payload["tool_trace"])
    assert "checkerboard assay reported" not in review_trace_json
    assert "content" not in review_trace_json
    assert "locator" not in review_trace_json
    assert "message" not in review_trace_json

    restored = type(result).model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.canonical_sha256 == result.canonical_sha256


def test_changes_trigger_one_revision_then_one_final_review() -> None:
    research = _research_state()
    reviewer = ScriptedReviewerProvider([CHANGES_REQUESTED, APPROVED])
    researcher = ScriptedReviewerProvider([REVISED_DRAFT])

    result = run_evidence_review(
        research,
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id="dual-revision",
    )

    assert result.status == EvidenceReviewStatus.APPROVED
    assert result.final_answer == (
        "The frozen fixture reports FICI 0.4.\n"
        "Scope: Limited to the cited frozen checkerboard fixture.\n"
        'Evidence: [SYN-DIR-01 | SYN-DIR-01#abstract] "reported FICI 0.4"'
    )
    assert [verdict.decision for verdict in result.verdicts] == [
        "changes_requested",
        "approved",
    ]
    assert [audit.role for audit in result.call_audits] == [
        "reviewer",
        "research_revision",
        "reviewer",
    ]
    assert result.budget.review_calls_used == 2
    assert result.budget.revision_calls_used == 1
    assert result.budget.retries_used == 0
    assert result.budget.model_calls_used == 3
    assert len(reviewer.requests) == 2
    assert len(researcher.requests) == 1
    assert researcher.requests[0].generation_parameters["system_prompt"] == (
        RESEARCH_REVISION_SYSTEM_PROMPT
    )
    assert all(
        request.generation_parameters["system_prompt"]
        == EVIDENCE_REVIEWER_SYSTEM_PROMPT
        for request in reviewer.requests
    )
    assert result.shared_draft_sha256 != result.final_draft_sha256
    assert {claim.claim_id for claim in result.final_draft.claims} == {"claim-1"}


@pytest.mark.parametrize("second_decision", ["changes_requested", "rejected"])
def test_second_non_approval_stops_with_safe_human_review(
    second_decision: str,
) -> None:
    second = json.dumps(
        {
            "decision": second_decision,
            "rationale": "The revised draft still does not pass.",
            "flagged_claim_ids": ["claim-1"],
        }
    )
    reviewer = ScriptedReviewerProvider([CHANGES_REQUESTED, second])
    researcher = ScriptedReviewerProvider([REVISED_DRAFT])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id=f"dual-second-{second_decision}",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.safe_refusal is True
    assert result.final_answer == SAFE_REFUSAL
    assert result.final_draft.refusal is True
    assert result.final_draft.claims == ()
    assert result.audit_candidate_draft.claims
    assert result.errors[-1].code == "second_review_not_approved"
    assert result.budget.review_calls_used == 2
    assert result.budget.revision_calls_used == 1
    assert result.budget.model_calls_used == 3


def test_rejected_first_draft_does_not_call_research_revision() -> None:
    rejected = json.dumps(
        {
            "decision": "rejected",
            "rationale": "The draft should not be released.",
            "flagged_claim_ids": ["claim-1"],
        }
    )
    reviewer = ScriptedReviewerProvider([rejected])
    researcher = ScriptedReviewerProvider([])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id="dual-rejected",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.final_answer == SAFE_REFUSAL
    assert result.errors[-1].code == "reviewer_rejected"
    assert researcher.requests == []


def test_reviewer_sees_sanitized_partial_failure_trace_and_rejects() -> None:
    first_evidence = ToolEvidence(
        source_id="source-001",
        chunk_id="source-001:abstract",
        content="A retained route returned a reviewable result.",
        source_type="synthetic_fixture",
    )
    third_evidence = ToolEvidence(
        source_id="source-002",
        chunk_id="source-002:abstract",
        content="A later route also returned a reviewable result.",
        source_type="synthetic_fixture",
    )
    state = AgentState(
        run_id="partial-failure",
        question="Did three routes preserve results after one failure?",
        phase=AgentPhase.INSUFFICIENT_EVIDENCE,
        stop_reason=AgentStopReason.INSUFFICIENT_EVIDENCE,
        plan=AgentPlan.model_validate(
            {
                "items": [
                    {
                        "step_id": f"partial-failure:tool:{index}",
                        "tool_name": "local_rag.search",
                        "arguments": {"query": f"route-{route}"},
                    }
                    for index, route in enumerate(("a", "b", "c"), start=1)
                ]
            }
        ),
        evidence_ledger=EvidenceLedger(items=(first_evidence, third_evidence)),
        answer="insufficient_evidence",
        tool_results=(
            ToolExecutionResult(
                call_id="partial-failure:tool:1",
                tool_name="local_rag.search",
                call_signature_sha256="2" * 64,
                status="succeeded",
                evidence=(first_evidence,),
                output={
                    "retrieved_count": 1,
                    "retrieval_mode": "keyword",
                    "private_path": "C:/must-not-enter-review-trace",
                },
                frozen_replay=True,
            ),
            ToolExecutionResult(
                call_id="partial-failure:tool:2",
                tool_name="local_rag.search",
                call_signature_sha256="3" * 64,
                status="failed",
                failure=ToolFailure(
                    code="TimeoutError",
                    message="private failure detail must not enter review trace",
                    retryable=True,
                ),
                output={"private_path": "C:/also-private"},
                frozen_replay=True,
            ),
            ToolExecutionResult(
                call_id="partial-failure:tool:3",
                tool_name="local_rag.search",
                call_signature_sha256="4" * 64,
                status="succeeded",
                evidence=(third_evidence,),
                output={"retrieved_count": 1, "retrieval_mode": "keyword"},
                frozen_replay=True,
            ),
        ),
        errors=(
            AgentError(
                stage=AgentPhase.EXECUTING_TOOLS,
                code="TimeoutError",
                message="private research error detail",
            ),
        ),
    )
    rejected = json.dumps(
        {
            "decision": "rejected",
            "rationale": "The requested route coverage was not completed.",
            "flagged_claim_ids": [],
        }
    )
    reviewer = ScriptedReviewerProvider([rejected])

    result = run_evidence_review(
        state,
        reviewer_provider=reviewer,
        run_id="partial-failure-review",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.final_answer == SAFE_REFUSAL
    payload = json.loads(reviewer.requests[0].prompt.partition("REVIEW_INPUT=")[2])
    assert payload["tool_trace"][0]["output"] == {
        "retrieval_mode": "keyword",
        "retrieved_count": 1,
    }
    assert payload["tool_trace"][1]["failure"] == {
        "code": "TimeoutError",
        "retryable": True,
    }
    assert len(payload["validated_plan"]["items"]) == 3
    assert [item["status"] for item in payload["tool_trace"]] == [
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert payload["tool_trace"][2]["evidence_refs"] == [
        {"chunk_id": "source-002:abstract", "source_id": "source-002"}
    ]
    assert payload["research_errors"] == [{"code": "TimeoutError"}]
    assert payload["tool_trace_sha256"] == result.shared_tool_trace_sha256
    rendered_trace = json.dumps(payload["tool_trace"])
    assert "private_path" not in rendered_trace
    assert "private failure detail" not in rendered_trace
    assert "C:/" not in rendered_trace
    assert "private research error detail" not in reviewer.requests[0].prompt


@pytest.mark.parametrize(
    ("untrusted_retrieval_mode", "untrusted_analysis_type"),
    [
        ("SECRET_TOKEN_123", "SECRET_TOKEN_123"),
        (["SECRET_TOKEN_123"], {"leak": "SECRET_TOKEN_123"}),
        ({"leak": "SECRET_TOKEN_123"}, ["SECRET_TOKEN_123"]),
    ],
)
def test_reviewer_trace_normalizes_untrusted_enum_and_error_codes(
    untrusted_retrieval_mode: object,
    untrusted_analysis_type: object,
) -> None:
    secret = "SECRET_TOKEN_123"
    state = AgentState(
        run_id="untrusted-trace-fields",
        question="Review an untrusted tool failure.",
        phase=AgentPhase.INSUFFICIENT_EVIDENCE,
        stop_reason=AgentStopReason.INSUFFICIENT_EVIDENCE,
        answer="insufficient_evidence",
        tool_results=(
            ToolExecutionResult(
                call_id="untrusted-trace-fields:tool:1",
                tool_name="local_rag.search",
                call_signature_sha256="5" * 64,
                status="failed",
                failure=ToolFailure(
                    code="C:/private/failure-code",
                    message="private failure detail",
                ),
                output={
                    "retrieval_mode": untrusted_retrieval_mode,
                    "analysis_type": untrusted_analysis_type,
                },
                frozen_replay=True,
            ),
        ),
        errors=(
            AgentError(
                stage=AgentPhase.EXECUTING_TOOLS,
                code=secret,
                message="private research detail",
            ),
        ),
    )
    reviewer = ScriptedReviewerProvider(
        [
            json.dumps(
                {
                    "decision": "rejected",
                    "rationale": "The tool route failed.",
                    "flagged_claim_ids": [],
                }
            )
        ]
    )

    run_evidence_review(
        state,
        reviewer_provider=reviewer,
        run_id="untrusted-trace-fields-review",
    )

    prompt = reviewer.requests[0].prompt
    payload = json.loads(prompt.partition("REVIEW_INPUT=")[2])
    assert payload["tool_trace"][0]["failure"] == {
        "code": "unclassified_tool_failure",
        "retryable": False,
    }
    assert payload["tool_trace"][0]["output"] == {}
    assert payload["research_errors"] == [
        {"code": "unclassified_research_error"}
    ]
    assert secret not in prompt
    assert "C:/private" not in prompt
    assert "private failure detail" not in prompt
    assert "private research detail" not in prompt


def test_unknown_flagged_claim_id_fails_closed_before_revision() -> None:
    invalid = json.dumps(
        {
            "decision": "changes_requested",
            "rationale": "Flag an invented claim.",
            "flagged_claim_ids": ["invented-claim"],
        }
    )
    reviewer = ScriptedReviewerProvider([invalid])
    researcher = ScriptedReviewerProvider([])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id="unknown-flag",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.errors[-1].code == "unknown_flagged_claim_id"
    assert researcher.requests == []


@pytest.mark.parametrize(
    ("revision", "error_code"),
    [
        (
            {
                "refusal": False,
                "refusal_reason": None,
                "claims": [
                    {
                        "claim_id": "new-claim",
                        "text": "New claim.",
                        "scope": "Invented scope.",
                        "citations": [
                            {
                                "source_id": "SYN-DIR-01",
                                "chunk_id": "SYN-DIR-01#abstract",
                                "support_quote": "reported FICI 0.4",
                            }
                        ],
                    }
                ],
            },
            "revision_added_claim_id",
        ),
        (
            {
                "refusal": False,
                "refusal_reason": None,
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Outside citation.",
                        "scope": "Limited to an outside source.",
                        "citations": [
                            {
                                "source_id": "OUTSIDE",
                                "chunk_id": "OUTSIDE#1",
                                "support_quote": "invented",
                            }
                        ],
                    }
                ],
            },
            "citation_outside_shared_ledger",
        ),
        (
            {
                "refusal": False,
                "refusal_reason": None,
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Wrong quote.",
                        "scope": "Limited to the frozen fixture.",
                        "citations": [
                            {
                                "source_id": "SYN-DIR-01",
                                "chunk_id": "SYN-DIR-01#abstract",
                                "support_quote": "reported FICI 0.2",
                            }
                        ],
                    }
                ],
            },
            "non_verbatim_support_quote",
        ),
    ],
)
def test_revision_cannot_add_claim_or_escape_shared_evidence(
    revision: dict[str, object],
    error_code: str,
) -> None:
    reviewer = ScriptedReviewerProvider([CHANGES_REQUESTED])
    researcher = ScriptedReviewerProvider([json.dumps(revision)])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id=f"unsafe-revision-{error_code}",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.final_answer == SAFE_REFUSAL
    assert result.errors[-1].code == error_code
    assert len(reviewer.requests) == 1
    assert len(researcher.requests) == 1


def test_refusal_draft_is_reviewed_and_invalid_change_request_fails_closed() -> None:
    reviewer = ScriptedReviewerProvider([APPROVED])
    approved_refusal = run_evidence_review(
        _refusal_state(),
        reviewer_provider=reviewer,
        run_id="review-refusal",
    )

    assert approved_refusal.status == EvidenceReviewStatus.APPROVED
    assert approved_refusal.safe_refusal is True
    assert approved_refusal.final_answer == "insufficient_evidence"
    assert approved_refusal.initial_draft.refusal is True
    assert approved_refusal.initial_draft.claims == ()
    assert len(reviewer.requests) == 1

    changes_without_claim = json.dumps(
        {
            "decision": "changes_requested",
            "rationale": "Clarify the refusal.",
            "flagged_claim_ids": [],
        }
    )
    researcher = ScriptedReviewerProvider([])
    blocked = run_evidence_review(
        _refusal_state(),
        reviewer_provider=ScriptedReviewerProvider(
            [changes_without_claim, changes_without_claim]
        ),
        research_provider=researcher,
        run_id="refusal-no-new-claim",
    )
    assert blocked.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert blocked.errors[-1].code == "invalid_reviewer_json"
    assert researcher.requests == []


def test_reviewer_cost_budget_fails_closed_after_accounting_usage() -> None:
    result = run_evidence_review(
        _research_state(),
        reviewer_provider=ScriptedReviewerProvider([APPROVED]),
        run_id="review-cost-cap",
        budget=EvidenceReviewBudget(max_cost_amount=Decimal("0.005")),
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.errors[-1].code == "review_usage_budget_exceeded"
    assert result.budget.cost_amount_used == Decimal("0.01")
    assert len(result.call_audits) == 1


def test_revision_cannot_change_or_remove_an_unflagged_claim() -> None:
    result = run_evidence_review(
        _two_claim_research_state(),
        reviewer_provider=ScriptedReviewerProvider([CHANGES_REQUESTED]),
        research_provider=ScriptedReviewerProvider([REVISED_DRAFT]),
        run_id="revision-unflagged-claim",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.errors[-1].code == "revision_changed_unflagged_claim"


def test_all_json_and_provider_retries_share_one_global_allowance() -> None:
    reviewer = ScriptedReviewerProvider(["not-json", CHANGES_REQUESTED])
    researcher = ScriptedReviewerProvider(["also-not-json"])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        research_provider=researcher,
        run_id="shared-retry",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.errors[-1].code == "review_retry_limit"
    assert result.budget.review_calls_used == 1
    assert result.budget.revision_calls_used == 1
    assert result.budget.retries_used == 1
    assert result.budget.model_calls_used == 3
    assert [audit.retry for audit in result.call_audits] == [False, True, False]
    assert len(reviewer.requests) == 2
    assert len(researcher.requests) == 1


def test_retryable_provider_failure_consumes_the_only_retry_and_is_audited() -> None:
    reviewer = ScriptedReviewerProvider(
        [
            ProviderFailure(
                code="temporary_unavailable",
                message="Temporary fake failure.",
                retryable=True,
            ),
            APPROVED,
        ]
    )

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        run_id="provider-retry",
    )

    assert result.status == EvidenceReviewStatus.APPROVED
    assert result.budget.retries_used == 1
    assert len(result.call_audits) == 2
    assert result.call_audits[0].failure_code == "temporary_unavailable"
    assert result.call_audits[0].response_succeeded is False


def test_strict_json_rejects_extra_tool_output_and_never_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "os.getenv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reviewer must not read credentials")
        ),
    )
    invalid = json.dumps(
        {
            "decision": "approved",
            "rationale": "Attempted tool output.",
            "flagged_claim_ids": [],
            "tool_call": {"name": "pubmed.search"},
        }
    )
    reviewer = ScriptedReviewerProvider([invalid, invalid])

    result = run_evidence_review(
        _research_state(),
        reviewer_provider=reviewer,
        run_id="no-tools",
    )

    assert result.status == EvidenceReviewStatus.HUMAN_REVIEW_REQUIRED
    assert result.errors[-1].code == "invalid_reviewer_json"
    assert len(reviewer.requests) == 2
