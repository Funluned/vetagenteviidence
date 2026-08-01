from __future__ import annotations

import math
import os
from hashlib import sha256

import pytest

from vetevidence.agent_providers import (
    DeterministicFakeLLMProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingProvider,
    GenerationRequest,
    LLMProvider,
    ProviderUsage,
)


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_fake_llm_is_repeatable_and_audits_exact_prompt() -> None:
    request = GenerationRequest(
        prompt="仅依据给定证据回答。\nEvidence: FICI = 0.4",
        request_id="case-DIR-01",
        generation_parameters={
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "stop": ["END"],
        },
    )
    provider = DeterministicFakeLLMProvider(
        '{"answer":"synergy"}',
    )

    first = provider.generate(request)
    second = provider.generate(request)

    assert isinstance(provider, LLMProvider)
    assert first == second
    assert first.text == '{"answer":"synergy"}'
    assert first.prompt_sha256 == sha256(
        request.prompt.encode("utf-8")
    ).hexdigest()
    assert (
        first.generation_parameters_sha256
        == request.generation_parameters_sha256
    )
    assert first.model_version == "v1"
    assert first.latency_ms == 0
    assert first.request_id == "case-DIR-01"
    assert first.fake is True
    assert first.network_used is False
    assert first.succeeded is True


def test_fake_llm_default_usage_and_cost_are_zero() -> None:
    response = DeterministicFakeLLMProvider().generate(
        GenerationRequest(prompt="offline smoke")
    )

    assert response.usage.input_tokens == 0
    assert response.usage.cache_hit_input_tokens == 0
    assert response.usage.cache_miss_input_tokens == 0
    assert response.usage.output_tokens == 0
    assert response.usage.reasoning_tokens == 0
    assert response.usage.total_tokens == 0
    assert response.usage.model_calls == 0
    assert response.usage.cost_usd == 0
    assert response.usage.cost_amount == 0
    assert response.usage.cost_currency == "USD"
    assert response.latency_ms == 0


def test_generation_parameter_hash_is_canonical_and_parameters_are_read_only() -> None:
    first = GenerationRequest(
        prompt="same",
        generation_parameters={
            "temperature": 0,
            "response_format": {"schema": ["claim", "source_id"]},
        },
    )
    second = GenerationRequest(
        prompt="same",
        generation_parameters={
            "response_format": {"schema": ["claim", "source_id"]},
            "temperature": 0,
        },
    )

    assert first.generation_parameters_json == second.generation_parameters_json
    assert (
        first.generation_parameters_sha256
        == second.generation_parameters_sha256
    )
    assert first.request_sha256 == second.request_sha256
    with pytest.raises(TypeError):
        first.generation_parameters["temperature"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "parameters, error_type",
    [
        ({"temperature": float("nan")}, ValueError),
        ({"temperature": float("inf")}, ValueError),
        ({"unsupported": {"set"}}, TypeError),
        ({1: "non-string key"}, TypeError),
    ],
)
def test_generation_parameters_reject_non_canonical_values(
    parameters: dict[object, object],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        GenerationRequest(
            prompt="invalid parameters",
            generation_parameters=parameters,  # type: ignore[arg-type]
        )


def test_provider_usage_records_full_audit_fields_and_rejects_invalid_values() -> None:
    usage = ProviderUsage(
        input_tokens=100,
        cache_hit_input_tokens=60,
        cache_miss_input_tokens=40,
        output_tokens=20,
        reasoning_tokens=8,
        model_calls=1,
        cost_amount=0.25,
        cost_currency="CNY",
    )

    assert usage.total_tokens == 120
    assert usage.cost_usd is None
    with pytest.raises(ValueError, match="cache input"):
        ProviderUsage(input_tokens=1, cache_hit_input_tokens=2)
    with pytest.raises(ValueError, match="reasoning_tokens"):
        ProviderUsage(output_tokens=1, reasoning_tokens=2)
    with pytest.raises(ValueError, match="cost_currency"):
        ProviderUsage(cost_currency="usd")
    with pytest.raises(ValueError, match="cost_amount"):
        ProviderUsage(cost_amount=-0.01)


def test_fake_providers_do_not_read_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_getenv(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("provider attempted to read an environment value")

    def forbidden_environ_get(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("provider attempted to read os.environ")

    monkeypatch.setattr(os, "getenv", forbidden_getenv)
    monkeypatch.setattr(type(os.environ), "get", forbidden_environ_get)

    DeterministicFakeLLMProvider().generate(
        GenerationRequest(prompt="must remain offline")
    )
    DeterministicHashEmbeddingProvider().embed(["本地证据"])


def test_hash_embeddings_are_stable_normalized_and_configurable() -> None:
    provider = DeterministicHashEmbeddingProvider(dimensions=512)
    texts = [
        "槲皮素与阿莫西林的 FICI 为 0.4。",
        "quercetin and amoxicillin showed synergy, FICI 0.4",
    ]

    first = provider.embed(texts)
    second = provider.embed(texts)

    assert isinstance(provider, EmbeddingProvider)
    assert first == second
    assert all(len(vector) == 512 for vector in first)
    assert all(
        math.isclose(_cosine(vector, vector), 1.0, abs_tol=1e-12)
        for vector in first
    )
    assert provider.fake is True
    assert provider.network_used is False
    assert provider.model_name == "feature-hash-v1"
    assert provider.model_version == "1.0"


def test_hash_embeddings_preserve_basic_similarity() -> None:
    provider = DeterministicHashEmbeddingProvider(dimensions=1024)
    query, related, unrelated = provider.embed(
        [
            "quercetin amoxicillin synergy FICI",
            "amoxicillin and quercetin had a synergistic FICI result",
            "feline nutrition vitamin intake",
        ]
    )

    assert _cosine(query, related) > _cosine(query, unrelated)
    assert _cosine(query, related) > 0


def test_hash_embedding_accepts_empty_text_as_zero_vector() -> None:
    provider = DeterministicHashEmbeddingProvider(dimensions=32)

    assert provider.embed([""]) == [[0.0] * 32]
