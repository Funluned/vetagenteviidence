from __future__ import annotations

import importlib
import json
import os
from decimal import Decimal
from hashlib import sha256

import httpx
import pytest

import vetevidence.deepseek_provider as deepseek_module
from vetevidence.agent_providers import GenerationRequest, LLMProvider
from vetevidence.deepseek_provider import (
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DEEPSEEK_DEFAULT_TIMEOUT_SECONDS,
    DEEPSEEK_MAX_TIMEOUT_SECONDS,
    DEEPSEEK_MIN_TIMEOUT_SECONDS,
    DEEPSEEK_PRICING_SNAPSHOT,
    DEEPSEEK_PRICING_SNAPSHOT_ID,
    DeepSeekProvider,
    DeepSeekRunBudget,
)


def _success_payload(
    *,
    text: str | None = '{"answer":"supported"}',
    finish_reason: str = "stop",
    model: str = "deepseek-v4-pro",
) -> dict[str, object]:
    return {
        "id": "chatcmpl-test-001",
        "object": "chat.completion",
        "created": 1785600000,
        "model": model,
        "system_fingerprint": "fp_test_v4",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 300,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 200,
            "completion_tokens": 50,
            "total_tokens": 350,
            "completion_tokens_details": {
                "reasoning_tokens": 20,
                "future_detail": 7,
            },
            "future_top_level_counter": 11,
        },
    }


def _json_response(request: httpx.Request, payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload, request=request)


def test_module_import_does_not_read_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_getenv(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("module import read an environment value")

    monkeypatch.setattr(os, "getenv", forbidden_getenv)

    importlib.reload(deepseek_module)


def test_success_uses_official_endpoint_system_role_and_full_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unit-test-key-never-log"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL
        assert request.headers["Authorization"] == f"Bearer {secret}"
        captured_body.update(json.loads(request.content))
        return _json_response(request, _success_payload())

    budget = DeepSeekRunBudget("1")
    provider = DeepSeekProvider(
        budget=budget,
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
    )
    request = GenerationRequest(
        prompt="只根据证据回答。",
        request_id="case-001",
        generation_parameters={
            "system_prompt": "输出 json，禁止使用模型记忆。",
            "max_tokens": 128,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
    )

    response = provider.generate(request)

    assert isinstance(provider, LLMProvider)
    assert response.succeeded is True
    assert response.text == '{"answer":"supported"}'
    assert response.model_name == "deepseek-v4-pro"
    assert response.model_version == "DeepSeek-V4-Pro"
    assert response.request_id == "case-001"
    assert response.network_used is True
    assert response.usage.input_tokens == 300
    assert response.usage.cache_hit_input_tokens == 100
    assert response.usage.cache_miss_input_tokens == 200
    assert response.usage.output_tokens == 50
    assert response.usage.reasoning_tokens == 20
    assert response.usage.cost_currency == "CNY"

    assert captured_body == {
        "model": "deepseek-v4-pro",
        "messages": [
            {
                "role": "system",
                "content": "输出 json，禁止使用模型记忆。",
            },
            {"role": "user", "content": "只根据证据回答。"},
        ],
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "stream": False,
    }

    expected_body_json = json.dumps(
        captured_body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    audit = provider.last_audit
    assert audit is not None
    assert audit.request_body_sha256 == sha256(
        expected_body_json.encode("utf-8")
    ).hexdigest()
    assert audit.response_id == "chatcmpl-test-001"
    assert audit.created == 1785600000
    assert audit.actual_model == "deepseek-v4-pro"
    assert audit.system_fingerprint == "fp_test_v4"
    assert audit.finish_reason == "stop"
    assert audit.http_status == 200
    assert audit.attempts == 1
    assert audit.timeout_seconds == DEEPSEEK_DEFAULT_TIMEOUT_SECONDS
    assert audit.failure_code is None
    assert audit.usage is not None
    assert audit.usage.pricing_snapshot_id == DEEPSEEK_PRICING_SNAPSHOT_ID
    assert audit.usage.exact_cost_cny == Decimal("0.0009025")
    assert json.loads(audit.usage.raw_usage_json) == _success_payload()["usage"]
    assert budget.spent_cny == Decimal("0.0009025")
    assert budget.reserved_cny == 0
    assert secret not in repr(provider.audit_records)
    assert secret not in repr(response)


def test_configured_timeout_is_used_by_httpx_and_recorded_in_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "timeout-config-key")
    configured_timeout = 75.5

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {
            "connect": configured_timeout,
            "read": configured_timeout,
            "write": configured_timeout,
            "pool": configured_timeout,
        }
        return _json_response(request, _success_payload())

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
        timeout_seconds=configured_timeout,
    )

    response = provider.generate(GenerationRequest(prompt="bounded timeout"))

    assert response.succeeded is True
    assert provider.last_audit is not None
    assert provider.last_audit.timeout_seconds == configured_timeout


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        DEEPSEEK_MIN_TIMEOUT_SECONDS - 0.1,
        DEEPSEEK_MAX_TIMEOUT_SECONDS + 0.1,
        float("nan"),
        float("inf"),
    ],
)
def test_provider_rejects_unbounded_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="between 30 and 300"):
        DeepSeekProvider(timeout_seconds=timeout_seconds)


def test_price_snapshot_uses_exact_decimal_cny_rates() -> None:
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-flash"]
        .cache_hit_input_cny_per_million
        == Decimal("0.02")
    )
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-flash"]
        .cache_miss_input_cny_per_million
        == Decimal("1")
    )
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-flash"]
        .output_cny_per_million
        == Decimal("2")
    )
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-pro"]
        .cache_hit_input_cny_per_million
        == Decimal("0.025")
    )
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-pro"]
        .cache_miss_input_cny_per_million
        == Decimal("3")
    )
    assert (
        DEEPSEEK_PRICING_SNAPSHOT["deepseek-v4-pro"]
        .output_cny_per_million
        == Decimal("6")
    )


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner"])
def test_legacy_models_are_rejected(model: str) -> None:
    with pytest.raises(ValueError, match="legacy"):
        DeepSeekProvider(model_name=model)


def test_unknown_model_and_configurable_url_are_not_available() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        DeepSeekProvider(model_name="some-proxy-model")
    with pytest.raises(TypeError, match="base_url"):
        DeepSeekProvider(base_url="https://example.invalid")  # type: ignore[call-arg]


def test_unknown_generation_parameter_is_blocked_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "parameter-test-key")

    def forbidden_transport(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(forbidden_transport),
    )
    response = provider.generate(
        GenerationRequest(
            prompt="blocked",
            generation_parameters={"unknown_parameter": True},
        )
    )

    assert response.failure is not None
    assert response.failure.code == "invalid_request"
    assert response.network_used is False


def test_missing_key_blocks_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def forbidden_transport(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(forbidden_transport),
    )
    response = provider.generate(GenerationRequest(prompt="offline"))

    assert response.failure is not None
    assert response.failure.code == "missing_api_key"
    assert response.network_used is False
    assert provider.last_audit is not None
    assert provider.last_audit.attempts == 0


def test_zero_or_insufficient_budget_blocks_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "budget-test-key")

    def forbidden_transport(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("0"),
        transport=httpx.MockTransport(forbidden_transport),
    )
    response = provider.generate(
        GenerationRequest(
            prompt="must stay offline",
            generation_parameters={"max_tokens": 1},
        )
    )

    assert response.failure is not None
    assert response.failure.code == "budget_exceeded"
    assert response.network_used is False
    assert provider.budget.spent_cny == 0
    assert provider.budget.reserved_cny == 0


def test_budget_rejects_float_to_avoid_binary_money_values() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        DeepSeekRunBudget(0.1)  # type: ignore[arg-type]


def test_retry_is_bounded_and_only_success_is_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "retry-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(429, text="secret-ish body", request=request)
        return _json_response(request, _success_payload())

    budget = DeepSeekRunBudget("1")
    provider = DeepSeekProvider(
        budget=budget,
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_backoff_seconds=0,
    )

    response = provider.generate(GenerationRequest(prompt="retry"))

    assert response.succeeded is True
    assert calls == 2
    assert provider.last_audit is not None
    assert provider.last_audit.attempts == 2
    assert budget.spent_cny == Decimal("0.0009025")
    assert budget.reserved_cny == 0


def test_final_http_failure_is_structured_and_does_not_echo_body_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-echo-this-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            text=f"server echoed {secret}",
            request=request,
        )

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_backoff_seconds=0,
    )
    response = provider.generate(GenerationRequest(prompt="retry then stop"))

    assert calls == 2
    assert response.failure is not None
    assert response.failure.code == "rate_limited"
    assert response.failure.retryable is True
    assert secret not in response.failure.message
    assert secret not in repr(provider.last_audit)
    assert provider.last_audit is not None
    assert provider.last_audit.attempts == 2
    assert provider.last_audit.http_status == 429


def test_invalid_http_json_is_safely_reported_and_reservation_is_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "invalid-json-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=f"not json {secret}".encode(),
            request=request,
        )

    budget = DeepSeekRunBudget("1")
    provider = DeepSeekProvider(
        budget=budget,
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(GenerationRequest(prompt="invalid json"))

    assert response.failure is not None
    assert response.failure.code == "invalid_json_response"
    assert secret not in response.failure.message
    assert budget.spent_cny > 0
    assert budget.spent_cny == provider.last_audit.estimated_max_cost_cny_per_attempt  # type: ignore[union-attr]
    assert budget.reserved_cny == 0


@pytest.mark.parametrize(
    ("payload", "parameters", "expected_code"),
    [
        (
            _success_payload(text="", finish_reason="stop"),
            {},
            "empty_output",
        ),
        (
            _success_payload(text='{"partial":', finish_reason="length"),
            {"response_format": {"type": "json_object"}},
            "truncated_output",
        ),
        (
            _success_payload(text="not-json", finish_reason="stop"),
            {"response_format": {"type": "json_object"}},
            "invalid_model_json",
        ),
        (
            _success_payload(finish_reason="content_filter"),
            {},
            "content_filtered",
        ),
        (
            _success_payload(finish_reason="tool_calls"),
            {},
            "unexpected_tool_calls",
        ),
        (
            _success_payload(finish_reason="insufficient_system_resource"),
            {},
            "insufficient_system_resource",
        ),
    ],
)
def test_empty_truncated_and_invalid_model_output_are_structured(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    parameters: dict[str, object],
    expected_code: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-output-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, payload)

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(
        GenerationRequest(prompt="json please", generation_parameters=parameters)
    )

    assert response.failure is not None
    assert response.failure.code == expected_code
    assert provider.last_audit is not None
    assert provider.last_audit.failure_code == expected_code
    assert provider.last_audit.usage is not None
    assert provider.budget.spent_cny == Decimal("0.0009025")


def test_invalid_usage_is_a_redacted_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "usage-key")
    payload = _success_payload()
    assert isinstance(payload["usage"], dict)
    payload["usage"].pop("prompt_cache_miss_tokens")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, payload)

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(GenerationRequest(prompt="bad usage"))

    assert response.failure is not None
    assert response.failure.code == "invalid_json_response"
    assert "prompt_cache_miss_tokens" not in response.failure.message


def test_missing_cache_breakdown_is_distinct_and_costed_as_all_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "cache-accounting-key")
    payload = _success_payload()
    assert isinstance(payload["usage"], dict)
    payload["usage"].pop("prompt_cache_hit_tokens")
    payload["usage"].pop("prompt_cache_miss_tokens")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, payload)

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(GenerationRequest(prompt="missing cache split"))

    assert response.succeeded is True
    assert response.usage.cache_hit_input_tokens == 0
    assert response.usage.cache_miss_input_tokens == 300
    assert provider.last_audit is not None
    assert provider.last_audit.usage is not None
    assert provider.last_audit.usage.prompt_cache_hit_tokens is None
    assert provider.last_audit.usage.prompt_cache_miss_tokens is None
    assert (
        provider.last_audit.usage.cost_basis
        == "conservative_all_cache_miss"
    )
    assert provider.last_audit.usage.exact_cost_cny == Decimal("0.0012")


def test_actual_flash_model_uses_flash_price_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "flash-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            _success_payload(model="deepseek-v4-flash"),
        )

    provider = DeepSeekProvider(
        model_name="deepseek-v4-flash",
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(GenerationRequest(prompt="flash"))

    assert response.succeeded is True
    assert response.model_version == "DeepSeek-V4-Flash-0731"
    assert provider.last_audit is not None
    assert provider.last_audit.usage is not None
    assert provider.last_audit.usage.exact_cost_cny == Decimal("0.000302")


def test_transport_timeout_is_conservatively_charged_and_retry_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "timeout-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous timeout", request=request)

    budget = DeepSeekRunBudget("1")
    provider = DeepSeekProvider(
        budget=budget,
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_backoff_seconds=0,
    )
    response = provider.generate(GenerationRequest(prompt="ambiguous timeout"))

    assert calls == 2
    assert response.failure is not None
    assert response.failure.code == "transport_error"
    assert provider.last_audit is not None
    reservation = provider.last_audit.estimated_max_cost_cny_per_attempt
    assert budget.spent_cny == reservation * 2
    assert provider.last_audit.conservative_unverified_cost_cny == reservation * 2
    assert Decimal(str(response.usage.cost_amount)) == reservation * 2
    assert response.usage.model_calls == 2


def test_provider_default_has_no_hidden_http_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "single-attempt-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous timeout", request=request)

    provider = DeepSeekProvider(
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
    )
    response = provider.generate(GenerationRequest(prompt="one attempt"))

    assert calls == 1
    assert response.failure is not None
    assert response.usage.model_calls == 1


def test_response_model_must_match_the_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-mismatch-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            _success_payload(model="deepseek-v4-flash"),
        )

    provider = DeepSeekProvider(
        model_name="deepseek-v4-pro",
        budget=DeepSeekRunBudget("1"),
        transport=httpx.MockTransport(handler),
    )
    response = provider.generate(GenerationRequest(prompt="strict model"))

    assert response.failure is not None
    assert response.failure.code == "invalid_json_response"
    assert provider.last_audit is not None
    assert provider.last_audit.actual_model is None
