"""Closed, typed tool boundary for the research-agent experiment.

This module deliberately does not implement networking, shell access, arbitrary
file access, or arbitrary HTTP.  It validates calls against a five-tool
allowlist and provides an exact-match frozen replay executor for offline tests
and versioned evaluations.
"""

from __future__ import annotations

import json
import re
import unicodedata
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class AgentToolName(StrEnum):
    PUBMED_SEARCH = "pubmed.search"
    LOCAL_RAG_SEARCH = "local_rag.search"
    EXPERIMENT_FICI = "experiment.fici"
    EXPERIMENT_GROWTH_CURVE = "experiment.growth_curve"
    REPORT_BUILD = "report.build"


TOOL_ALLOWLIST = frozenset(item.value for item in AgentToolName)
EVIDENCE_TOOL_ALLOWLIST = frozenset(
    {
        AgentToolName.PUBMED_SEARCH,
        AgentToolName.LOCAL_RAG_SEARCH,
        AgentToolName.EXPERIMENT_FICI,
        AgentToolName.EXPERIMENT_GROWTH_CURVE,
    }
)


class ToolValidationError(ValueError):
    """A fail-closed validation error raised before a tool can execute."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PubMedSearchArguments(_ToolModel):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: int = Field(default=5, ge=1, le=20, strict=True)


class LocalRAGSearchArguments(_ToolModel):
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=3, ge=1, le=10, strict=True)


class FICIArguments(_ToolModel):
    """Reference an input already admitted to the current run.

    The model is not allowed to choose a file path or inject raw executable
    content.  A concrete adapter resolves this opaque identifier inside the
    run's already-authorized input registry.
    """

    dataset_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class GrowthCurveArguments(_ToolModel):
    dataset_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class ReportBuildArguments(_ToolModel):
    report_input_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


ToolArguments = (
    PubMedSearchArguments
    | LocalRAGSearchArguments
    | FICIArguments
    | GrowthCurveArguments
    | ReportBuildArguments
)


_ARGUMENT_MODELS: dict[AgentToolName, type[_ToolModel]] = {
    AgentToolName.PUBMED_SEARCH: PubMedSearchArguments,
    AgentToolName.LOCAL_RAG_SEARCH: LocalRAGSearchArguments,
    AgentToolName.EXPERIMENT_FICI: FICIArguments,
    AgentToolName.EXPERIMENT_GROWTH_CURVE: GrowthCurveArguments,
    AgentToolName.REPORT_BUILD: ReportBuildArguments,
}

_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?m)^\s*(?:system|developer|assistant|tool)\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"<\s*/?\s*(?:system|developer|assistant|tool)(?:\s|>)",
        re.IGNORECASE,
    ),
    re.compile(r"忽略(?:之前|以上|前面|所有).{0,12}(?:指令|提示|要求)"),
    re.compile(r"(?:系统提示|开发者消息|越狱提示)"),
)


def contains_prompt_injection(value: str) -> bool:
    """Conservatively identify control-language in a control-channel value."""

    normalized = unicodedata.normalize("NFKC", value)
    return any(pattern.search(normalized) for pattern in _PROMPT_INJECTION_PATTERNS)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validated_tool_name(value: str | AgentToolName) -> AgentToolName:
    try:
        return AgentToolName(value)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(
            "unknown_tool",
            "Tool name is not in the research-agent allowlist.",
        ) from exc


def validate_tool_arguments(
    tool_name: str | AgentToolName,
    arguments: Mapping[str, Any],
) -> ToolArguments:
    """Validate one model-proposed argument object without coercing extra keys."""

    name = _validated_tool_name(tool_name)
    if not isinstance(arguments, Mapping):
        raise ToolValidationError(
            "invalid_arguments",
            "Tool arguments must be a JSON object.",
        )
    try:
        validated = _ARGUMENT_MODELS[name].model_validate(dict(arguments))
    except ValidationError as exc:
        raise ToolValidationError(
            "invalid_arguments",
            "Tool arguments do not match the allowlisted schema.",
        ) from exc

    for value in validated.model_dump(mode="json").values():
        if isinstance(value, str) and contains_prompt_injection(value):
            raise ToolValidationError(
                "prompt_injection",
                "Control-language is forbidden in tool arguments.",
            )
    return validated  # type: ignore[return-value]


class ValidatedToolCall(_ToolModel):
    call_id: str = Field(min_length=1, max_length=128)
    tool_name: AgentToolName
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def arguments_match_tool(self) -> "ValidatedToolCall":
        validated = validate_tool_arguments(self.tool_name, self.arguments)
        object.__setattr__(
            self,
            "arguments",
            validated.model_dump(mode="json"),
        )
        return self

    @property
    def arguments_json(self) -> str:
        return _canonical_json(self.arguments)

    @property
    def signature_sha256(self) -> str:
        payload = {
            "arguments": self.arguments,
            "tool_name": self.tool_name.value,
        }
        return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def validate_tool_call(
    *,
    call_id: str,
    tool_name: str | AgentToolName,
    arguments: Mapping[str, Any],
) -> ValidatedToolCall:
    name = _validated_tool_name(tool_name)
    validated_arguments = validate_tool_arguments(name, arguments)
    try:
        return ValidatedToolCall(
            call_id=call_id,
            tool_name=name,
            arguments=validated_arguments.model_dump(mode="json"),
        )
    except ValidationError as exc:
        raise ToolValidationError(
            "invalid_call",
            "Tool call metadata is invalid.",
        ) from exc


class ToolEvidence(_ToolModel):
    """One immutable evidence chunk emitted by an allowlisted tool."""

    source_id: str = Field(min_length=1, max_length=256)
    chunk_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, repr=False)
    source_type: str = Field(default="frozen_replay", min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=1_000)
    locator: str | None = Field(default=None, max_length=2_000)
    evidence_role: str = Field(default="untrusted_evidence", pattern="^untrusted_evidence$")


class ToolFailure(_ToolModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False


class FrozenToolResponse(_ToolModel):
    """Data returned by an exact frozen replay; it cannot execute a callback."""

    status: str = Field(default="succeeded", pattern="^(succeeded|partial|failed)$")
    evidence: tuple[ToolEvidence, ...] = ()
    output: dict[str, Any] = Field(default_factory=dict)
    failure: ToolFailure | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "FrozenToolResponse":
        if self.status == "succeeded" and self.failure is not None:
            raise ValueError("succeeded response cannot contain failure")
        if self.status == "partial" and (
            self.failure is None or not self.evidence
        ):
            raise ValueError("partial response requires evidence and failure")
        if self.status == "failed" and self.failure is None:
            raise ValueError("failed response requires failure")
        try:
            _canonical_json(self.output)
        except (TypeError, ValueError) as exc:
            raise ValueError("output must contain finite JSON values") from exc
        return self


class ToolExecutionResult(_ToolModel):
    call_id: str = Field(min_length=1, max_length=128)
    tool_name: AgentToolName
    call_signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(pattern="^(succeeded|partial|failed)$")
    evidence: tuple[ToolEvidence, ...] = ()
    output: dict[str, Any] = Field(default_factory=dict)
    failure: ToolFailure | None = None
    frozen_replay: bool = False
    network_used: bool = False
    external_actions: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_status(self) -> "ToolExecutionResult":
        FrozenToolResponse(
            status=self.status,
            evidence=self.evidence,
            output=self.output,
            failure=self.failure,
        )
        return self

    @property
    def succeeded(self) -> bool:
        return self.status in {"succeeded", "partial"}


class FrozenToolReplay(_ToolModel):
    tool_name: AgentToolName
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: FrozenToolResponse

    @classmethod
    def for_call(
        cls,
        tool_name: str | AgentToolName,
        arguments: Mapping[str, Any],
        *,
        evidence: Sequence[ToolEvidence] = (),
        output: Mapping[str, Any] | None = None,
        status: str = "succeeded",
        failure: ToolFailure | None = None,
    ) -> "FrozenToolReplay":
        call = validate_tool_call(
            call_id="frozen-signature",
            tool_name=tool_name,
            arguments=arguments,
        )
        return cls(
            tool_name=call.tool_name,
            signature_sha256=call.signature_sha256,
            response=FrozenToolResponse(
                status=status,
                evidence=tuple(evidence),
                output=dict(output or {}),
                failure=failure,
            ),
        )


@runtime_checkable
class AgentToolExecutor(Protocol):
    def execute(self, call: ValidatedToolCall) -> ToolExecutionResult: ...


class FrozenReplayToolExecutor:
    """Execute only exact, pre-registered calls from immutable replay data."""

    def __init__(self, replays: Sequence[FrozenToolReplay]) -> None:
        by_signature: dict[str, FrozenToolReplay] = {}
        for replay in replays:
            if not isinstance(replay, FrozenToolReplay):
                raise TypeError("replays must contain FrozenToolReplay values")
            if replay.signature_sha256 in by_signature:
                raise ValueError("duplicate frozen tool replay signature")
            by_signature[replay.signature_sha256] = replay
        self._replays = by_signature
        self._calls: list[ValidatedToolCall] = []

    @property
    def calls(self) -> tuple[ValidatedToolCall, ...]:
        return tuple(self._calls)

    def execute(self, call: ValidatedToolCall) -> ToolExecutionResult:
        if not isinstance(call, ValidatedToolCall):
            raise TypeError("call must be ValidatedToolCall")
        self._calls.append(call)
        replay = self._replays.get(call.signature_sha256)
        if replay is None or replay.tool_name != call.tool_name:
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                call_signature_sha256=call.signature_sha256,
                status="failed",
                failure=ToolFailure(
                    code="frozen_replay_missing",
                    message="No exact frozen replay exists for this call.",
                ),
                frozen_replay=True,
            )
        response = replay.response
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            call_signature_sha256=call.signature_sha256,
            status=response.status,
            evidence=response.evidence,
            output=response.output,
            failure=response.failure,
            frozen_replay=True,
            network_used=False,
            external_actions=0,
        )


def tool_argument_schemas() -> dict[str, dict[str, Any]]:
    """Return JSON schemas suitable for the planning prompt and audit output."""

    return {
        name.value: _ARGUMENT_MODELS[name].model_json_schema()
        for name in AgentToolName
    }


__all__ = [
    "AgentToolExecutor",
    "AgentToolName",
    "FICIArguments",
    "FrozenReplayToolExecutor",
    "FrozenToolReplay",
    "FrozenToolResponse",
    "GrowthCurveArguments",
    "LocalRAGSearchArguments",
    "PubMedSearchArguments",
    "ReportBuildArguments",
    "TOOL_ALLOWLIST",
    "EVIDENCE_TOOL_ALLOWLIST",
    "ToolEvidence",
    "ToolExecutionResult",
    "ToolFailure",
    "ToolValidationError",
    "ValidatedToolCall",
    "contains_prompt_injection",
    "tool_argument_schemas",
    "validate_tool_arguments",
    "validate_tool_call",
]
