"""Offline provider contracts used by the v0.7 RAG and agent experiments.

The implementations in this module are deliberately incapable of contacting a
model service.  They provide deterministic seams for exercising orchestration,
auditing and retrieval code before any real provider is explicitly enabled.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence, runtime_checkable


def _require_string(value: object, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")


def _require_non_negative_integer(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _freeze_json_value(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path} keys must be strings")
        frozen: dict[str, object] = {}
        for key in sorted(value):
            frozen[key] = _freeze_json_value(
                value[key],
                path=f"{path}.{key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _thaw_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A fully materialized prompt ready for a generation provider."""

    prompt: str
    request_id: str | None = None
    generation_parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_string(self.prompt, field_name="prompt")
        if self.request_id is not None:
            _require_string(self.request_id, field_name="request_id")
            if not self.request_id:
                raise ValueError("request_id must not be empty")
        if not isinstance(self.generation_parameters, Mapping):
            raise TypeError("generation_parameters must be a mapping")
        frozen_parameters = _freeze_json_value(
            self.generation_parameters,
            path="generation_parameters",
        )
        object.__setattr__(self, "generation_parameters", frozen_parameters)

    @property
    def prompt_sha256(self) -> str:
        """Return a stable hash of the exact UTF-8 prompt sent to a provider."""

        return sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def generation_parameters_json(self) -> str:
        """Canonical JSON for the immutable generation parameters."""

        return _canonical_json(self.generation_parameters)

    @property
    def generation_parameters_sha256(self) -> str:
        return sha256(
            self.generation_parameters_json.encode("utf-8")
        ).hexdigest()

    @property
    def request_sha256(self) -> str:
        payload = {
            "generation_parameters": self.generation_parameters,
            "prompt": self.prompt,
        }
        return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Auditable usage counters for one provider response."""

    input_tokens: int = 0
    cache_hit_input_tokens: int = 0
    cache_miss_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    model_calls: int = 0
    cost_amount: float = 0.0
    cost_currency: str = "USD"

    def __post_init__(self) -> None:
        _require_non_negative_integer(
            self.input_tokens,
            field_name="input_tokens",
        )
        _require_non_negative_integer(
            self.cache_hit_input_tokens,
            field_name="cache_hit_input_tokens",
        )
        _require_non_negative_integer(
            self.cache_miss_input_tokens,
            field_name="cache_miss_input_tokens",
        )
        _require_non_negative_integer(
            self.output_tokens,
            field_name="output_tokens",
        )
        _require_non_negative_integer(
            self.reasoning_tokens,
            field_name="reasoning_tokens",
        )
        _require_non_negative_integer(
            self.model_calls,
            field_name="model_calls",
        )
        if (
            self.cache_hit_input_tokens + self.cache_miss_input_tokens
            > self.input_tokens
        ):
            raise ValueError("cache input token counts exceed input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens must not exceed output_tokens")
        if isinstance(self.cost_amount, bool) or not isinstance(
            self.cost_amount,
            (int, float),
        ):
            raise TypeError("cost_amount must be a number")
        if not math.isfinite(float(self.cost_amount)) or self.cost_amount < 0:
            raise ValueError("cost_amount must be finite and non-negative")
        _require_string(self.cost_currency, field_name="cost_currency")
        if not re.fullmatch(r"[A-Z]{3}", self.cost_currency):
            raise ValueError("cost_currency must be a three-letter ISO code")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        """Compatibility view for callers that specifically report USD."""

        if self.cost_currency != "USD":
            return None
        return float(self.cost_amount)


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """Structured provider failure that callers can audit without parsing text."""

    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        _require_string(self.code, field_name="code")
        _require_string(self.message, field_name="message")
        if not self.code:
            raise ValueError("code must not be empty")
        if not self.message:
            raise ValueError("message must not be empty")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """Provider output plus the provenance required for later evaluation."""

    text: str
    provider_name: str
    model_name: str
    prompt_sha256: str
    generation_parameters_sha256: str
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    model_version: str | None = None
    latency_ms: float = 0.0
    request_id: str | None = None
    failure: ProviderFailure | None = None
    fake: bool = False
    network_used: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("text", self.text),
            ("provider_name", self.provider_name),
            ("model_name", self.model_name),
            ("prompt_sha256", self.prompt_sha256),
            (
                "generation_parameters_sha256",
                self.generation_parameters_sha256,
            ),
        ):
            _require_string(value, field_name=field_name)
        if not self.provider_name:
            raise ValueError("provider_name must not be empty")
        if not self.model_name:
            raise ValueError("model_name must not be empty")
        if not re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256):
            raise ValueError("prompt_sha256 must be a lowercase SHA-256 digest")
        if not re.fullmatch(
            r"[0-9a-f]{64}",
            self.generation_parameters_sha256,
        ):
            raise ValueError(
                "generation_parameters_sha256 must be a lowercase SHA-256 digest"
            )
        if not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage")
        if self.model_version is not None:
            _require_string(self.model_version, field_name="model_version")
            if not self.model_version:
                raise ValueError("model_version must not be empty")
        if isinstance(self.latency_ms, bool) or not isinstance(
            self.latency_ms,
            (int, float),
        ):
            raise TypeError("latency_ms must be a number")
        if not math.isfinite(float(self.latency_ms)) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        if self.request_id is not None:
            _require_string(self.request_id, field_name="request_id")
            if not self.request_id:
                raise ValueError("request_id must not be empty")
        if self.failure is not None and not isinstance(
            self.failure,
            ProviderFailure,
        ):
            raise TypeError("failure must be ProviderFailure or None")
        if not isinstance(self.fake, bool):
            raise TypeError("fake must be a boolean")
        if not isinstance(self.network_used, bool):
            raise TypeError("network_used must be a boolean")

    @property
    def succeeded(self) -> bool:
        return self.failure is None


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal generation boundary consumed by the local RAG pipeline."""

    name: str
    model_name: str
    model_version: str | None
    fake: bool
    network_used: bool

    def generate(self, request: GenerationRequest) -> GenerationResponse: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Batch embedding boundary consumed by the local RAG pipeline."""

    name: str
    model_name: str
    model_version: str
    dimensions: int
    fake: bool
    network_used: bool

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class DeterministicFakeLLMProvider:
    """Return one configured response without reading credentials or networking."""

    name = "deterministic_fake_llm"
    fake = True
    network_used = False

    def __init__(
        self,
        response_text: str = "{}",
        *,
        model_name: str = "deterministic-fake-v1",
        model_version: str | None = "v1",
        usage: ProviderUsage | None = None,
        failure: ProviderFailure | None = None,
    ) -> None:
        _require_string(response_text, field_name="response_text")
        _require_string(model_name, field_name="model_name")
        if not model_name:
            raise ValueError("model_name must not be empty")
        if model_version is not None:
            _require_string(model_version, field_name="model_version")
            if not model_version:
                raise ValueError("model_version must not be empty")
        if usage is not None and not isinstance(usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage or None")
        if failure is not None and not isinstance(failure, ProviderFailure):
            raise TypeError("failure must be ProviderFailure or None")
        self._response_text = response_text
        self.model_name = model_name
        self.model_version = model_version
        self._usage = usage or ProviderUsage()
        self._failure = failure

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if not isinstance(request, GenerationRequest):
            raise TypeError("request must be GenerationRequest")
        return GenerationResponse(
            text=self._response_text,
            provider_name=self.name,
            model_name=self.model_name,
            prompt_sha256=request.prompt_sha256,
            generation_parameters_sha256=(
                request.generation_parameters_sha256
            ),
            usage=self._usage,
            model_version=self.model_version,
            latency_ms=0.0,
            request_id=request.request_id,
            failure=self._failure,
            fake=self.fake,
            network_used=self.network_used,
        )


_ENGLISH_OR_NUMBER = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_MIXED_TOKEN = re.compile(
    r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)


def _ngrams(items: Sequence[str], size: int) -> list[str]:
    if len(items) < size:
        return []
    return ["\x1f".join(items[index : index + size]) for index in range(len(items) - size + 1)]


def _text_features(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    features: list[str] = []

    mixed_tokens = _MIXED_TOKEN.findall(normalized)
    for size in (1, 2):
        features.extend(
            f"token:{size}:{gram}" for gram in _ngrams(mixed_tokens, size)
        )

    for word in _ENGLISH_OR_NUMBER.findall(normalized):
        characters = list(word)
        features.append(f"word:{word}")
        for size in (2, 3):
            features.extend(
                f"latin-char:{size}:{gram}"
                for gram in _ngrams(characters, size)
            )

    for run in _CJK_RUN.findall(normalized):
        characters = list(run)
        for size in (1, 2, 3):
            features.extend(
                f"cjk-char:{size}:{gram}"
                for gram in _ngrams(characters, size)
            )

    return features


class DeterministicHashEmbeddingProvider:
    """Create normalized multilingual feature-hash vectors without a model."""

    name = "deterministic_hash_embedding"
    model_name = "feature-hash-v1"
    model_version = "1.0"
    fake = True
    network_used = False

    def __init__(self, dimensions: int = 256) -> None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int):
            raise TypeError("dimensions must be an integer")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise TypeError("texts must be a sequence of strings")
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        _require_string(text, field_name="text")
        vector = [0.0] * self.dimensions
        for feature in _text_features(text):
            digest = sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector


__all__ = [
    "DeterministicFakeLLMProvider",
    "DeterministicHashEmbeddingProvider",
    "EmbeddingProvider",
    "GenerationRequest",
    "GenerationResponse",
    "LLMProvider",
    "ProviderFailure",
    "ProviderUsage",
]
