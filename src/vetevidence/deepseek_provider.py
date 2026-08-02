"""Audited DeepSeek Chat Completions provider with an explicit CNY budget.

The module does not read credentials at import time.  A real request is only
possible when ``DEEPSEEK_API_KEY`` is present *and* the caller supplies a
positive :class:`DeepSeekRunBudget`.  Tests inject ``httpx.MockTransport``;
there is intentionally no SDK dependency or configurable third-party URL.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Callable, Literal, Mapping

import httpx

from .agent_providers import (
    GenerationRequest,
    GenerationResponse,
    ProviderFailure,
    ProviderUsage,
)
from .deepseek_contract import (
    DEEPSEEK_DEFAULT_TIMEOUT_SECONDS,
    DEEPSEEK_MAX_TIMEOUT_SECONDS,
    DEEPSEEK_MIN_TIMEOUT_SECONDS,
)


DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_COMPLETIONS_URL = (
    f"{DEEPSEEK_API_BASE_URL}/chat/completions"
)
DEEPSEEK_PRICING_SOURCE_URL = (
    "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
)
DEEPSEEK_PRICING_SNAPSHOT_ID = "deepseek-cny-2026-08-01"
DEEPSEEK_PRICING_SNAPSHOT_DATE = "2026-08-01"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"

_ONE_MILLION = Decimal("1000000")
_SUPPORTED_MODELS = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-pro"}
)
_LEGACY_MODELS = frozenset({"deepseek-chat", "deepseek-reasoner"})
_MODEL_VERSIONS = MappingProxyType(
    {
        "deepseek-v4-flash": "DeepSeek-V4-Flash-0731",
        "deepseek-v4-pro": "DeepSeek-V4-Pro",
    }
)
_ALLOWED_GENERATION_PARAMETERS = frozenset(
    {
        "max_tokens",
        "response_format",
        "system_prompt",
        "temperature",
        "top_p",
    }
)


@dataclass(frozen=True, slots=True)
class DeepSeekModelPrice:
    """Official CNY prices per one million tokens at the snapshot date."""

    cache_hit_input_cny_per_million: Decimal
    cache_miss_input_cny_per_million: Decimal
    output_cny_per_million: Decimal


DEEPSEEK_PRICING_SNAPSHOT: Mapping[str, DeepSeekModelPrice] = (
    MappingProxyType(
        {
            "deepseek-v4-flash": DeepSeekModelPrice(
                cache_hit_input_cny_per_million=Decimal("0.02"),
                cache_miss_input_cny_per_million=Decimal("1"),
                output_cny_per_million=Decimal("2"),
            ),
            "deepseek-v4-pro": DeepSeekModelPrice(
                cache_hit_input_cny_per_million=Decimal("0.025"),
                cache_miss_input_cny_per_million=Decimal("3"),
                output_cny_per_million=Decimal("6"),
            ),
        }
    )
)


def _decimal_amount(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be Decimal, int, or string")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field_name} must be Decimal, int, or string")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return amount


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(slots=True)
class DeepSeekRunBudget:
    """Thread-safe, run-scoped CNY ceiling shared by provider calls."""

    limit_cny: Decimal | int | str
    _spent_cny: Decimal = field(default=Decimal("0"), init=False, repr=False)
    _reserved_cny: Decimal = field(
        default=Decimal("0"), init=False, repr=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.limit_cny = _decimal_amount(
            self.limit_cny,
            field_name="limit_cny",
        )

    @property
    def spent_cny(self) -> Decimal:
        with self._lock:
            return self._spent_cny

    @property
    def reserved_cny(self) -> Decimal:
        with self._lock:
            return self._reserved_cny

    @property
    def remaining_cny(self) -> Decimal:
        with self._lock:
            return self.limit_cny - self._spent_cny - self._reserved_cny

    def _reserve(self, amount: Decimal) -> bool:
        with self._lock:
            if self._spent_cny + self._reserved_cny + amount > self.limit_cny:
                return False
            self._reserved_cny += amount
            return True

    def _settle(
        self,
        reserved: Decimal,
        *,
        actual: Decimal | None,
        charge_reservation: bool = False,
    ) -> None:
        with self._lock:
            self._reserved_cny -= reserved
            if actual is not None:
                self._spent_cny += actual
            elif charge_reservation:
                # A successful HTTP response without auditable usage may have
                # been billed.  Counting the conservative reservation prevents
                # a later request from silently exceeding the run ceiling.
                self._spent_cny += reserved


@dataclass(frozen=True, slots=True)
class DeepSeekUsageAudit:
    """Exact usage and cost for one successful HTTP response."""

    prompt_tokens: int
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    completion_tokens: int
    reasoning_tokens: int
    total_tokens: int
    raw_usage_json: str
    exact_cost_cny: Decimal
    cost_basis: Literal["reported_cache_split", "conservative_all_cache_miss"]
    pricing_snapshot_id: str = DEEPSEEK_PRICING_SNAPSHOT_ID
    pricing_snapshot_date: str = DEEPSEEK_PRICING_SNAPSHOT_DATE
    pricing_source_url: str = DEEPSEEK_PRICING_SOURCE_URL


@dataclass(frozen=True, slots=True)
class DeepSeekAuditRecord:
    """Credential-free provenance for one logical provider call."""

    request_id: str
    requested_model: str
    actual_model: str | None
    request_body_sha256: str
    response_id: str | None
    created: int | None
    system_fingerprint: str | None
    finish_reason: str | None
    usage: DeepSeekUsageAudit | None
    attempts: int
    http_status: int | None
    estimated_max_cost_cny_per_attempt: Decimal
    timeout_seconds: float = DEEPSEEK_DEFAULT_TIMEOUT_SECONDS
    conservative_unverified_cost_cny: Decimal = Decimal("0")
    settled_cost_cny: Decimal = Decimal("0")
    attempt_outcomes: tuple[str, ...] = ()
    failure_code: str | None = None


class DeepSeekProvider:
    """Minimal synchronous ``LLMProvider`` implementation for DeepSeek V4."""

    name = "deepseek"
    fake = False
    network_used = True

    def __init__(
        self,
        *,
        model_name: str = "deepseek-v4-pro",
        budget: DeepSeekRunBudget | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEEPSEEK_DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.25,
        default_max_tokens: int = 2048,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if model_name in _LEGACY_MODELS:
            raise ValueError(
                f"legacy DeepSeek model is not supported: {model_name}"
            )
        if model_name not in _SUPPORTED_MODELS:
            raise ValueError(f"unsupported DeepSeek model: {model_name}")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise TypeError("max_retries must be an integer")
        if not 0 <= max_retries <= 1:
            raise ValueError("max_retries must be 0 or 1")
        if (
            isinstance(default_max_tokens, bool)
            or not isinstance(default_max_tokens, int)
        ):
            raise TypeError("default_max_tokens must be an integer")
        if not 1 <= default_max_tokens <= 384_000:
            raise ValueError(
                "default_max_tokens must be between 1 and 384000"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a number")
        if (
            not math.isfinite(timeout_seconds)
            or not DEEPSEEK_MIN_TIMEOUT_SECONDS
            <= timeout_seconds
            <= DEEPSEEK_MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "timeout_seconds must be between "
                f"{DEEPSEEK_MIN_TIMEOUT_SECONDS:g} and "
                f"{DEEPSEEK_MAX_TIMEOUT_SECONDS:g}"
            )
        if isinstance(retry_backoff_seconds, bool) or not isinstance(
            retry_backoff_seconds, (int, float)
        ):
            raise TypeError("retry_backoff_seconds must be a number")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")

        self.model_name = model_name
        self.model_version = _MODEL_VERSIONS[model_name]
        # A zero budget is deliberately safer than an implicit open budget.
        self.budget = budget or DeepSeekRunBudget("0")
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max_retries
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._default_max_tokens = default_max_tokens
        self._sleep = sleep
        self._audit_records: list[DeepSeekAuditRecord] = []

    @property
    def audit_records(self) -> tuple[DeepSeekAuditRecord, ...]:
        return tuple(self._audit_records)

    @property
    def last_audit(self) -> DeepSeekAuditRecord | None:
        if not self._audit_records:
            return None
        return self._audit_records[-1]

    def _build_request_body(
        self,
        request: GenerationRequest,
    ) -> dict[str, object]:
        parameters = json.loads(request.generation_parameters_json)
        if not isinstance(parameters, dict):  # defensive; contract says mapping
            raise ValueError("generation_parameters must be a JSON object")
        unsupported = sorted(set(parameters) - _ALLOWED_GENERATION_PARAMETERS)
        if unsupported:
            raise ValueError(
                "unsupported generation parameters: " + ", ".join(unsupported)
            )
        for reserved in ("model", "messages", "stream"):
            if reserved in parameters:
                raise ValueError(
                    f"generation parameter is provider-controlled: {reserved}"
                )

        system_prompt = parameters.pop("system_prompt", None)
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt
        ):
            raise ValueError("system_prompt must be a non-empty string")

        max_tokens = parameters.get("max_tokens", self._default_max_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError("max_tokens must be an integer")
        if not 1 <= max_tokens <= 384_000:
            raise ValueError("max_tokens must be between 1 and 384000")
        parameters["max_tokens"] = max_tokens

        messages: list[dict[str, str]] = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        return {
            "model": self.model_name,
            "messages": messages,
            **parameters,
            "stream": False,
        }

    def _estimated_max_cost(
        self,
        request_body_json: str,
        *,
        max_tokens: int,
    ) -> Decimal:
        # DeepSeek tokenization is not available locally.  UTF-8 byte length
        # plus protocol headroom is a conservative input-token upper bound.
        input_token_upper_bound = len(request_body_json.encode("utf-8")) + 256
        price = DEEPSEEK_PRICING_SNAPSHOT[self.model_name]
        return (
            Decimal(input_token_upper_bound)
            * price.cache_miss_input_cny_per_million
            + Decimal(max_tokens) * price.output_cny_per_million
        ) / _ONE_MILLION

    def _failure_response(
        self,
        request: GenerationRequest,
        *,
        request_body_sha256: str,
        estimated_cost: Decimal,
        code: str,
        message: str,
        retryable: bool,
        attempts: int,
        http_status: int | None,
        network_used: bool,
        charged_cost_cny: Decimal = Decimal("0"),
        attempt_outcomes: tuple[str, ...] = (),
    ) -> GenerationResponse:
        audit = DeepSeekAuditRecord(
            request_id=request.request_id or "missing-request-id",
            requested_model=self.model_name,
            actual_model=None,
            request_body_sha256=request_body_sha256,
            response_id=None,
            created=None,
            system_fingerprint=None,
            finish_reason=None,
            usage=None,
            attempts=attempts,
            http_status=http_status,
            estimated_max_cost_cny_per_attempt=estimated_cost,
            timeout_seconds=self._timeout_seconds,
            conservative_unverified_cost_cny=charged_cost_cny,
            settled_cost_cny=charged_cost_cny,
            attempt_outcomes=attempt_outcomes,
            failure_code=code,
        )
        self._audit_records.append(audit)
        return GenerationResponse(
            text="",
            provider_name=self.name,
            model_name=self.model_name,
            model_version=self.model_version,
            prompt_sha256=request.prompt_sha256,
            generation_parameters_sha256=(
                request.generation_parameters_sha256
            ),
            usage=ProviderUsage(
                model_calls=attempts,
                cost_amount=float(charged_cost_cny),
                cost_currency="CNY",
            ),
            latency_ms=0.0,
            request_id=request.request_id,
            failure=ProviderFailure(
                code=code,
                message=message,
                retryable=retryable,
            ),
            fake=False,
            network_used=network_used,
        )

    def _parse_usage(
        self,
        raw_usage: object,
        *,
        actual_model: str,
    ) -> DeepSeekUsageAudit:
        if not isinstance(raw_usage, Mapping):
            raise ValueError("usage must be an object")
        prompt_tokens = _require_non_negative_int(
            raw_usage.get("prompt_tokens"),
            field_name="usage.prompt_tokens",
        )
        raw_cache_hit = raw_usage.get("prompt_cache_hit_tokens")
        raw_cache_miss = raw_usage.get("prompt_cache_miss_tokens")
        if raw_cache_hit is None and raw_cache_miss is None:
            cache_hit = None
            cache_miss = None
            billed_cache_hit = 0
            billed_cache_miss = prompt_tokens
            cost_basis = "conservative_all_cache_miss"
        elif raw_cache_hit is None or raw_cache_miss is None:
            raise ValueError("cache token fields must both be present or absent")
        else:
            cache_hit = _require_non_negative_int(
                raw_cache_hit,
                field_name="usage.prompt_cache_hit_tokens",
            )
            cache_miss = _require_non_negative_int(
                raw_cache_miss,
                field_name="usage.prompt_cache_miss_tokens",
            )
            billed_cache_hit = cache_hit
            billed_cache_miss = cache_miss
            cost_basis = "reported_cache_split"
        completion_tokens = _require_non_negative_int(
            raw_usage.get("completion_tokens"),
            field_name="usage.completion_tokens",
        )
        total_tokens = _require_non_negative_int(
            raw_usage.get("total_tokens"),
            field_name="usage.total_tokens",
        )
        if (
            cache_hit is not None
            and cache_miss is not None
            and cache_hit + cache_miss != prompt_tokens
        ):
            raise ValueError("cache token counts do not equal prompt_tokens")
        if prompt_tokens + completion_tokens != total_tokens:
            raise ValueError("prompt and completion tokens do not equal total_tokens")

        details = raw_usage.get("completion_tokens_details", {})
        if details is None:
            details = {}
        if not isinstance(details, Mapping):
            raise ValueError("usage.completion_tokens_details must be an object")
        reasoning_tokens = _require_non_negative_int(
            details.get("reasoning_tokens", 0),
            field_name="usage.completion_tokens_details.reasoning_tokens",
        )
        if reasoning_tokens > completion_tokens:
            raise ValueError("reasoning_tokens exceed completion_tokens")

        price = DEEPSEEK_PRICING_SNAPSHOT[actual_model]
        exact_cost = (
            Decimal(billed_cache_hit) * price.cache_hit_input_cny_per_million
            + Decimal(billed_cache_miss) * price.cache_miss_input_cny_per_million
            + Decimal(completion_tokens) * price.output_cny_per_million
        ) / _ONE_MILLION
        try:
            raw_usage_json = _canonical_json(raw_usage)
        except (TypeError, ValueError) as exc:
            raise ValueError("usage contains a non-JSON value") from exc
        return DeepSeekUsageAudit(
            prompt_tokens=prompt_tokens,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            raw_usage_json=raw_usage_json,
            exact_cost_cny=exact_cost,
            cost_basis=cost_basis,
        )

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if not isinstance(request, GenerationRequest):
            raise TypeError("request must be GenerationRequest")
        try:
            request_body = self._build_request_body(request)
            request_body_json = _canonical_json(request_body)
        except (TypeError, ValueError) as exc:
            return self._failure_response(
                request,
                request_body_sha256=sha256(b"").hexdigest(),
                estimated_cost=Decimal("0"),
                code="invalid_request",
                message=str(exc),
                retryable=False,
                attempts=0,
                http_status=None,
                network_used=False,
            )
        body_hash = sha256(request_body_json.encode("utf-8")).hexdigest()
        estimated_cost = self._estimated_max_cost(
            request_body_json,
            max_tokens=int(request_body["max_tokens"]),
        )

        api_key = os.environ.get(DEEPSEEK_API_KEY_ENV)
        if not api_key:
            return self._failure_response(
                request,
                request_body_sha256=body_hash,
                estimated_cost=estimated_cost,
                code="missing_api_key",
                message=f"{DEEPSEEK_API_KEY_ENV} is not configured",
                retryable=False,
                attempts=0,
                http_status=None,
                network_used=False,
            )

        started = time.perf_counter()
        retryable_statuses = {408, 409, 429, 500, 502, 503, 504}
        last_status: int | None = None
        attempts = 0
        conservative_unverified_cost = Decimal("0")
        attempt_outcomes: list[str] = []
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(
            transport=self._transport,
            timeout=self._timeout_seconds,
        ) as client:
            for attempt in range(1, self._max_retries + 2):
                attempts = attempt
                if not self.budget._reserve(estimated_cost):
                    return self._failure_response(
                        request,
                        request_body_sha256=body_hash,
                        estimated_cost=estimated_cost,
                        code="budget_exceeded",
                        message="DeepSeek run budget would be exceeded",
                        retryable=True,
                        attempts=attempt - 1,
                        http_status=last_status,
                        network_used=attempt > 1,
                        charged_cost_cny=conservative_unverified_cost,
                        attempt_outcomes=tuple(attempt_outcomes),
                    )
                try:
                    response = client.post(
                        DEEPSEEK_CHAT_COMPLETIONS_URL,
                        content=request_body_json.encode("utf-8"),
                        headers=headers,
                    )
                except (httpx.TimeoutException, httpx.TransportError):
                    attempt_outcomes.append("transport_error")
                    # A transport timeout can occur after the server accepted and
                    # billed the request.  Charge the conservative reservation so
                    # a retry cannot silently cross the hard run ceiling.
                    self.budget._settle(
                        estimated_cost,
                        actual=None,
                        charge_reservation=True,
                    )
                    conservative_unverified_cost += estimated_cost
                    if attempt <= self._max_retries:
                        self._sleep(self._retry_backoff_seconds * attempt)
                        continue
                    latency_ms = (time.perf_counter() - started) * 1000
                    result = self._failure_response(
                        request,
                        request_body_sha256=body_hash,
                        estimated_cost=estimated_cost,
                        code="transport_error",
                        message="DeepSeek request failed before a response was received",
                        retryable=True,
                        attempts=attempts,
                        http_status=None,
                        network_used=True,
                        charged_cost_cny=conservative_unverified_cost,
                        attempt_outcomes=tuple(attempt_outcomes),
                    )
                    return _with_latency(result, latency_ms)

                last_status = response.status_code
                if response.status_code != 200:
                    attempt_outcomes.append(f"http_{response.status_code}")
                    self.budget._settle(estimated_cost, actual=None)
                    if (
                        response.status_code in retryable_statuses
                        and attempt <= self._max_retries
                    ):
                        self._sleep(self._retry_backoff_seconds * attempt)
                        continue
                    code, message, retryable = _http_failure(
                        response.status_code
                    )
                    latency_ms = (time.perf_counter() - started) * 1000
                    result = self._failure_response(
                        request,
                        request_body_sha256=body_hash,
                        estimated_cost=estimated_cost,
                        code=code,
                        message=message,
                        retryable=retryable,
                        attempts=attempts,
                        http_status=response.status_code,
                        network_used=True,
                        charged_cost_cny=conservative_unverified_cost,
                        attempt_outcomes=tuple(attempt_outcomes),
                    )
                    return _with_latency(result, latency_ms)

                try:
                    payload = response.json()
                    parsed = self._parse_success_payload(payload)
                    usage = self._parse_usage(
                        parsed["usage"],
                        actual_model=parsed["actual_model"],
                    )
                except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                    attempt_outcomes.append("invalid_response")
                    self.budget._settle(
                        estimated_cost,
                        actual=None,
                        charge_reservation=True,
                    )
                    conservative_unverified_cost += estimated_cost
                    latency_ms = (time.perf_counter() - started) * 1000
                    result = self._failure_response(
                        request,
                        request_body_sha256=body_hash,
                        estimated_cost=estimated_cost,
                        code="invalid_json_response",
                        message="DeepSeek returned an invalid or incomplete JSON response",
                        retryable=True,
                        attempts=attempts,
                        http_status=200,
                        network_used=True,
                        charged_cost_cny=conservative_unverified_cost,
                        attempt_outcomes=tuple(attempt_outcomes),
                    )
                    return _with_latency(result, latency_ms)

                self.budget._settle(
                    estimated_cost,
                    actual=usage.exact_cost_cny,
                )
                attempt_outcomes.append("success")
                latency_ms = (time.perf_counter() - started) * 1000
                failure: ProviderFailure | None = None
                text = parsed["text"]
                finish_reason = parsed["finish_reason"]
                if finish_reason == "length":
                    failure = ProviderFailure(
                        code="truncated_output",
                        message="DeepSeek output was truncated at max_tokens",
                        retryable=False,
                    )
                elif finish_reason == "content_filter":
                    failure = ProviderFailure(
                        code="content_filtered",
                        message="DeepSeek filtered the model output",
                        retryable=False,
                    )
                elif finish_reason == "tool_calls":
                    failure = ProviderFailure(
                        code="unexpected_tool_calls",
                        message="DeepSeek returned unsupported native tool calls",
                        retryable=False,
                    )
                elif finish_reason == "insufficient_system_resource":
                    failure = ProviderFailure(
                        code="insufficient_system_resource",
                        message="DeepSeek reported insufficient system resources",
                        retryable=True,
                    )
                elif not text:
                    failure = ProviderFailure(
                        code="empty_output",
                        message="DeepSeek returned an empty response",
                        retryable=True,
                    )
                elif _expects_json_object(request) and not _is_json_object(text):
                    failure = ProviderFailure(
                        code="invalid_model_json",
                        message="DeepSeek model output was not a JSON object",
                        retryable=True,
                    )

                audit = DeepSeekAuditRecord(
                    request_id=request.request_id or "missing-request-id",
                    requested_model=self.model_name,
                    actual_model=parsed["actual_model"],
                    request_body_sha256=body_hash,
                    response_id=parsed["response_id"],
                    created=parsed["created"],
                    system_fingerprint=parsed["system_fingerprint"],
                    finish_reason=finish_reason,
                    usage=usage,
                    attempts=attempts,
                    http_status=200,
                    estimated_max_cost_cny_per_attempt=estimated_cost,
                    timeout_seconds=self._timeout_seconds,
                    conservative_unverified_cost_cny=(
                        conservative_unverified_cost
                    ),
                    settled_cost_cny=(
                        conservative_unverified_cost + usage.exact_cost_cny
                    ),
                    attempt_outcomes=tuple(attempt_outcomes),
                    failure_code=failure.code if failure else None,
                )
                self._audit_records.append(audit)
                return GenerationResponse(
                    text=text,
                    provider_name=self.name,
                    model_name=parsed["actual_model"],
                    model_version=_MODEL_VERSIONS[parsed["actual_model"]],
                    prompt_sha256=request.prompt_sha256,
                    generation_parameters_sha256=(
                        request.generation_parameters_sha256
                    ),
                    usage=ProviderUsage(
                        input_tokens=usage.prompt_tokens,
                        cache_hit_input_tokens=(
                            usage.prompt_cache_hit_tokens or 0
                        ),
                        cache_miss_input_tokens=(
                            usage.prompt_cache_miss_tokens
                            if usage.prompt_cache_miss_tokens is not None
                            else usage.prompt_tokens
                        ),
                        output_tokens=usage.completion_tokens,
                        reasoning_tokens=usage.reasoning_tokens,
                        model_calls=attempts,
                        cost_amount=float(
                            conservative_unverified_cost + usage.exact_cost_cny
                        ),
                        cost_currency="CNY",
                    ),
                    latency_ms=latency_ms,
                    request_id=request.request_id,
                    failure=failure,
                    fake=False,
                    network_used=True,
                )

        raise AssertionError("unreachable")

    def _parse_success_payload(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise ValueError("response must be an object")
        response_id = payload.get("id")
        created = payload.get("created")
        actual_model = payload.get("model")
        system_fingerprint = payload.get("system_fingerprint")
        choices = payload.get("choices")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError("response id is missing")
        if isinstance(created, bool) or not isinstance(created, int):
            raise ValueError("created is missing")
        if actual_model not in _SUPPORTED_MODELS:
            raise ValueError("response model is unsupported")
        if actual_model != self.model_name:
            raise ValueError("response model does not match requested model")
        if not isinstance(system_fingerprint, str) or not system_fingerprint:
            raise ValueError("system_fingerprint is missing")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("response must contain exactly one choice")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ValueError("choice must be an object")
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {
            "stop",
            "length",
            "content_filter",
            "tool_calls",
            "insufficient_system_resource",
        }:
            raise ValueError("finish_reason is invalid")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("message must be an object")
        text = message.get("content")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("message content must be a string or null")
        return {
            "response_id": response_id,
            "created": created,
            "actual_model": actual_model,
            "system_fingerprint": system_fingerprint,
            "finish_reason": finish_reason,
            "text": text,
            "usage": payload.get("usage"),
        }


def _expects_json_object(request: GenerationRequest) -> bool:
    parameters = json.loads(request.generation_parameters_json)
    response_format = parameters.get("response_format", {})
    return (
        isinstance(response_format, dict)
        and response_format.get("type") == "json_object"
    )


def _is_json_object(text: str) -> bool:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, dict)


def _http_failure(status_code: int) -> tuple[str, str, bool]:
    if status_code in {401, 403}:
        return (
            "authentication_failed",
            "DeepSeek rejected the configured credential",
            False,
        )
    if status_code == 402:
        return (
            "insufficient_balance",
            "DeepSeek reported insufficient account balance",
            False,
        )
    if status_code in {400, 404, 422}:
        return (
            "invalid_request",
            "DeepSeek rejected the request",
            False,
        )
    if status_code == 429:
        return ("rate_limited", "DeepSeek rate limit was reached", True)
    if status_code >= 500 or status_code in {408, 409}:
        return (
            "provider_unavailable",
            "DeepSeek was temporarily unavailable",
            True,
        )
    return ("http_error", f"DeepSeek returned HTTP {status_code}", False)


def _with_latency(
    response: GenerationResponse,
    latency_ms: float,
) -> GenerationResponse:
    return GenerationResponse(
        text=response.text,
        provider_name=response.provider_name,
        model_name=response.model_name,
        model_version=response.model_version,
        prompt_sha256=response.prompt_sha256,
        generation_parameters_sha256=(
            response.generation_parameters_sha256
        ),
        usage=response.usage,
        latency_ms=latency_ms,
        request_id=response.request_id,
        failure=response.failure,
        fake=response.fake,
        network_used=response.network_used,
    )


__all__ = [
    "DEEPSEEK_API_BASE_URL",
    "DEEPSEEK_CHAT_COMPLETIONS_URL",
    "DEEPSEEK_DEFAULT_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_TIMEOUT_SECONDS",
    "DEEPSEEK_MIN_TIMEOUT_SECONDS",
    "DEEPSEEK_PRICING_SNAPSHOT",
    "DEEPSEEK_PRICING_SNAPSHOT_DATE",
    "DEEPSEEK_PRICING_SNAPSHOT_ID",
    "DEEPSEEK_PRICING_SOURCE_URL",
    "DeepSeekAuditRecord",
    "DeepSeekModelPrice",
    "DeepSeekProvider",
    "DeepSeekRunBudget",
    "DeepSeekUsageAudit",
]
