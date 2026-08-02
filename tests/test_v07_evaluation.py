from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vetevidence import workbench_pipeline
from vetevidence.v07_evaluation import (
    V07BaselineReport,
    V07_CATEGORIES,
    V07_METRICS,
    load_v07_evaluation,
    run_v07_rule_baseline,
    v07_deterministic_result_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V07_ROOT = PROJECT_ROOT / "data" / "eval" / "v0.7"
CASES_PATH = V07_ROOT / "cases.json"
EXPECTED_PATH = V07_ROOT / "expected.json"
BASELINE_PATH = V07_ROOT / "baselines" / "rules_v1.json"
DOCUMENTATION_PATH = PROJECT_ROOT / "docs" / "V0.7_EVALUATION.md"

EXPECTED_FAILURE_IDS = {
    "DIR-02",
    "CIT-01",
    "CIT-02",
    "CIT-03",
    "INJ-01",
    "INJ-02",
    "TOOL-02",
}


def _load_v07():
    return load_v07_evaluation(CASES_PATH, EXPECTED_PATH)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _fixture_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "pmid" and isinstance(item, str):
                found.append(item)
            elif key == "gold_relevant_ids" and isinstance(item, list):
                found.extend(str(identifier) for identifier in item)
            found.extend(_fixture_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_fixture_ids(item))
    return found


def test_v07_dataset_is_balanced_versioned_and_gold_aligned() -> None:
    loaded = _load_v07()
    cases = loaded.dataset.cases

    assert loaded.dataset.schema_version == "1.0"
    assert loaded.dataset.dataset_version == "v0.7.0"
    assert loaded.review_status == "engineering_gold_pending_domain_expert_review"
    assert len(cases) == 27
    assert Counter(case.category for case in cases) == {
        category: 3 for category in V07_CATEGORIES
    }
    assert set(loaded.dataset.metric_definitions) == V07_METRICS
    assert {
        metric
        for case in cases
        for metric in case.applicable_metrics
    } == V07_METRICS
    assert {
        case.input["retrieval_k"]
        for case in cases
        if "retrieval_recall_at_k" in case.applicable_metrics
    } == {3}
    assert {case.id for case in cases} == set(loaded.expected)


def test_v07_fixtures_are_synthetic_and_offline_only() -> None:
    raw_dataset = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = raw_dataset["cases"]

    assert all(
        case["data_status"] == "synthetic_evaluation_only"
        for case in cases
    )

    identifiers = _fixture_ids(cases)
    assert identifiers
    assert all(identifier.startswith("SYN-") for identifier in identifiers)
    assert not any(identifier.isdecimal() for identifier in identifiers)

    all_strings = [item for item in _walk(raw_dataset) if isinstance(item, str)]
    urls = [
        match.group(0)
        for text in all_strings
        for match in re.finditer(r"https?://[^\s\"'\\]+", text)
    ]
    assert urls
    assert all(urlparse(url).hostname == "example.invalid" for url in urls)
    assert not any("pubmed.ncbi.nlm.nih.gov" in text for text in all_strings)
    assert not any(
        isinstance(item, dict) and "doi" in item
        for item in _walk(raw_dataset)
    )


def test_rules_v1_baseline_records_expected_capability_gaps() -> None:
    report = run_v07_rule_baseline(
        _load_v07(),
        project_root=PROJECT_ROOT,
    )

    assert report.summary.total == 27
    assert report.summary.passed == 20
    assert report.summary.failed == 7
    assert report.summary.evaluation_errors == 0
    assert {
        result.id for result in report.results if not result.passed
    } == EXPECTED_FAILURE_IDS
    assert not any(
        result.error_type == "evaluation_error" for result in report.results
    )
    assert all(
        result.actual.get("model_calls") == 0
        and result.actual.get("network_calls") == 0
        and result.actual.get("external_actions") == 0
        for result in report.results
    )

    assert report.system.provider == "rules_v1"
    assert report.system.llm_enabled is False
    assert report.system.model_calls == 0
    assert report.system.network_calls == 0
    assert report.system.input_tokens == 0
    assert report.system.output_tokens == 0
    assert report.system.llm_api_cost_usd == 0
    assert (
        report.metrics["retrieval_recall_at_k"].numerator,
        report.metrics["retrieval_recall_at_k"].denominator,
    ) == (3, 5)
    assert (
        report.metrics["citation_precision"].numerator,
        report.metrics["citation_precision"].denominator,
    ) == (4, 7)
    assert (
        report.metrics["unsupported_claim_rate"].numerator,
        report.metrics["unsupported_claim_rate"].denominator,
    ) == (3, 7)
    assert (
        report.metrics["abstention_accuracy"].numerator,
        report.metrics["abstention_accuracy"].denominator,
    ) == (20, 25)
    assert (
        report.metrics["task_completion_rate"].numerator,
        report.metrics["task_completion_rate"].denominator,
    ) == (26, 27)
    assert report.metrics["cost"].value == 0
    assert report.metrics["cost"].numerator is None
    assert report.metrics["cost"].denominator is None


def test_rules_v1_deterministic_hash_ignores_time_and_latency() -> None:
    loaded = _load_v07()
    first = run_v07_rule_baseline(loaded, project_root=PROJECT_ROOT)
    second = run_v07_rule_baseline(loaded, project_root=PROJECT_ROOT)

    assert first.deterministic_result_sha256 == (
        second.deterministic_result_sha256
    )
    assert first.deterministic_result_sha256 == (
        v07_deterministic_result_sha256(first)
    )
    assert second.deterministic_result_sha256 == (
        v07_deterministic_result_sha256(second)
    )

    changed_diagnostics = first.model_copy(deep=True)
    changed_diagnostics.generated_at += timedelta(days=1)
    for result in changed_diagnostics.results:
        result.latency_ms += 1000
    latency = changed_diagnostics.metrics["latency"]
    changed_diagnostics.metrics["latency"] = latency.model_copy(
        update={"value": (latency.value or 0) + 1000}
    )
    assert v07_deterministic_result_sha256(changed_diagnostics) == (
        first.deterministic_result_sha256
    )


def test_saved_rules_v1_baseline_is_intact_and_matches_fresh_run() -> None:
    saved = V07BaselineReport.model_validate_json(
        BASELINE_PATH.read_text(encoding="utf-8")
    )
    fresh = run_v07_rule_baseline(
        _load_v07(),
        project_root=PROJECT_ROOT,
    )

    recomputed_saved_hash = v07_deterministic_result_sha256(saved)
    recomputed_fresh_hash = v07_deterministic_result_sha256(fresh)
    assert saved.deterministic_result_sha256 == recomputed_saved_hash
    assert fresh.deterministic_result_sha256 == recomputed_fresh_hash
    assert saved.dataset_sha256 == fresh.dataset_sha256
    assert saved.system.implementation_sha256 == (
        fresh.system.implementation_sha256
    )
    assert recomputed_saved_hash == recomputed_fresh_hash


def test_rules_v1_baseline_never_constructs_real_external_clients(
    monkeypatch,
) -> None:
    def reject_external_client(*args, **kwargs):
        del args, kwargs
        raise AssertionError("v0.7 离线基线不得创建真实外部客户端")

    monkeypatch.setattr(
        workbench_pipeline,
        "PubMedClient",
        reject_external_client,
    )
    monkeypatch.setattr(
        workbench_pipeline.LetPubJournalRankingProvider,
        "default",
        reject_external_client,
    )

    report = run_v07_rule_baseline(
        _load_v07(),
        project_root=PROJECT_ROOT,
    )

    assert report.summary.passed == 20
    assert report.summary.evaluation_errors == 0
    assert report.system.network_calls == 0


def test_legacy_30_case_evaluation_remains_unchanged() -> None:
    legacy_cases = json.loads(
        (PROJECT_ROOT / "data" / "eval" / "cases.json").read_text(
            encoding="utf-8"
        )
    )

    assert isinstance(legacy_cases, list)
    assert len(legacy_cases) == 30


def test_v07_documentation_states_the_non_claim_boundaries() -> None:
    documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")

    assert "完全离线、合成的工程评测场景" in documentation
    assert "不是科研证据" in documentation
    assert "不是实时 PubMed 或 RAG 召回" in documentation
    assert "LLM／网络调用：`0 / 0`" in documentation
    assert "规则基线允许失败" in documentation
    assert "单次实时查询的字段回归" in documentation
    assert "回答器信任边界测试" in documentation
