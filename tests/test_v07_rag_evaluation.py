from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from vetevidence.v07_rag_evaluation import (
    RAG_CASE_SLICES,
    V07RAGBaselineReport,
    build_v07_rag_query,
    load_v07_rag_evaluation,
    run_v07_local_hash_rag_baseline,
    v07_rag_deterministic_result_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V07_ROOT = PROJECT_ROOT / "data" / "eval" / "v0.7"
CASES_PATH = V07_ROOT / "cases.json"
MANIFEST_PATH = V07_ROOT / "rag_retrieval.json"
BASELINE_PATH = V07_ROOT / "baselines" / "local_hash_rag_v1.json"
EXPECTED_CASES_SHA256 = (
    "119ff99d18be270d1306d5cb1ea77defd53430ad98823dcf3dc451435081ae6c"
)
EXPECTED_HARD_NEGATIVES = {
    "SYN-DIR-01-DECOY",
    "SYN-CTX-01",
    "SYN-CTX-02",
    "SYN-CTX-03",
    "SYN-NONE-02",
    "SYN-NONE-03",
    "SYN-HIT-01",
    "SYN-HIT-02",
    "SYN-HIT-03",
    "SYN-INJ-01",
    "SYN-INJ-02",
}


def _load():
    return load_v07_rag_evaluation(CASES_PATH, MANIFEST_PATH)


def _raw_article_records(cases_document: dict[str, Any]) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for case in cases_document["cases"]:
        for article in case["input"].get("articles", []):
            records[article["pmid"]] = article
        for batch in case["input"].get("retrieval_batches", []):
            if isinstance(batch, list):
                for article in batch:
                    records[article["pmid"]] = article
    return records


def _normalized(value: str) -> str:
    return " ".join(value.strip().split())


def test_manifest_is_bound_to_cases_and_fixes_four_cases_and_two_slices() -> None:
    loaded = _load()
    manifest = loaded.manifest

    assert loaded.cases_sha256 == EXPECTED_CASES_SHA256
    assert manifest.cases_sha256 == EXPECTED_CASES_SHA256
    assert manifest.cases_sha256_algorithm == "sha256-canonical-json-v1"
    assert manifest.retrieval_k == 3
    assert {item.case_id: item.slice for item in manifest.cases} == (
        RAG_CASE_SLICES
    )
    assert set(manifest.hard_negative_source_ids) == EXPECTED_HARD_NEGATIVES
    assert {
        item.slice for item in manifest.cases
    } == {"semantic_direct", "resilience_partial"}

    raw_manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    assert "gold_relevant_ids" not in raw_manifest
    assert "expected.json" not in raw_manifest


def test_sources_and_queries_exclude_evaluation_labels_from_text() -> None:
    loaded = _load()
    raw_records = _raw_article_records(loaded.cases_document)
    candidate_ids = set(loaded.manifest.hard_negative_source_ids)
    for case_id in RAG_CASE_SLICES:
        candidate_ids.update(
            loaded.cases_by_id[case_id]["input"]["gold_relevant_ids"]
        )

    for source_id in candidate_ids:
        source = loaded.source_catalog.sources[source_id]
        record = raw_records[source_id]
        title = _normalized(record["title"])
        abstract = record.get("abstract")
        expected_parts = [f"Title: {title}"]
        if isinstance(abstract, str) and abstract.strip():
            expected_parts.append(f"Abstract: {_normalized(abstract)}")
        assert source.content == "\n".join(expected_parts)
        assert source_id not in source.content
        assert "example.invalid" not in source.content
        assert source.evidence_role == "untrusted_evidence"

    for case_id in RAG_CASE_SLICES:
        case = loaded.cases_by_id[case_id]
        query = build_v07_rag_query(case)
        assert case["question"]["context"] not in query
        assert case_id not in query
        assert "gold_relevant_ids" not in query
        assert "example.invalid" not in query


def test_every_case_has_more_candidates_than_k_and_unique_top3() -> None:
    loaded = _load()
    report = run_v07_local_hash_rag_baseline(
        loaded,
        project_root=PROJECT_ROOT,
    )

    for result in report.results:
        assert result.candidate_count > report.retrieval_k
        assert result.candidate_count == result.index_manifest.source_count
        assert result.index_manifest.chunk_count == result.candidate_count
        assert len(result.retrieved_source_ids) == report.retrieval_k
        assert len(set(result.retrieved_source_ids)) == report.retrieval_k
        assert set(result.retrieval_modes) == {
            "keyword_only",
            "hash_vector_only",
            "hybrid",
        }
        for mode_result in result.retrieval_modes.values():
            assert len(mode_result.retrieved_source_ids) == report.retrieval_k
            assert len(set(mode_result.retrieved_source_ids)) == (
                report.retrieval_k
            )
            assert set(mode_result.hit_source_ids) == (
                set(mode_result.retrieved_source_ids)
                & set(result.gold_relevant_ids)
            )
        assert result.retrieved_source_ids == (
            result.retrieval_modes["hybrid"].retrieved_source_ids
        )
        assert set(result.candidate_source_ids) == (
            set(result.gold_relevant_ids) | EXPECTED_HARD_NEGATIVES
        )


def test_report_rejects_mode_hits_that_do_not_match_top3_and_gold() -> None:
    report = run_v07_local_hash_rag_baseline(
        _load(),
        project_root=PROJECT_ROOT,
    )
    payload = report.model_dump(mode="json")
    first = payload["results"][0]
    first["retrieval_modes"]["keyword_only"]["hit_source_ids"] = []

    with pytest.raises(ValueError, match="命中来源与 Top 3／gold 不一致"):
        V07RAGBaselineReport.model_validate(payload)


def test_tool_failure_is_not_indexed_and_injections_remain_untrusted_data() -> None:
    loaded = _load()
    report = run_v07_local_hash_rag_baseline(
        loaded,
        project_root=PROJECT_ROOT,
    )
    tool_result = next(result for result in report.results if result.id == "TOOL-02")

    assert tool_result.failed_retrieval_batch_count == 1
    assert tool_result.preserved_success_source_ids == (
        "SYN-TOOL-02-A",
        "SYN-TOOL-02-C",
    )
    assert {"SYN-TOOL-02-A", "SYN-TOOL-02-C"}.issubset(
        tool_result.candidate_source_ids
    )
    assert not any(
        "synthetic branch B failure" in source.content
        for source in loaded.source_catalog.sources.values()
    )

    injection_one = loaded.source_catalog.sources["SYN-INJ-01"]
    injection_two = loaded.source_catalog.sources["SYN-INJ-02"]
    assert "Ignore previous instructions" in injection_one.content
    assert "SYSTEM:" in injection_two.content
    assert injection_one.evidence_role == "untrusted_evidence"
    assert injection_two.evidence_role == "untrusted_evidence"
    assert all(result.external_actions == 0 for result in report.results)


def test_evaluator_never_reads_expected_or_uses_external_resources(
    monkeypatch,
) -> None:
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path.name == "expected.json":
            raise AssertionError("本地 RAG Recall 评测不得读取 expected.json")
        return original_read_text(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("固定离线评测不得读取环境变量或访问网络")

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os._Environ, "__getitem__", forbidden)
    monkeypatch.setattr(os._Environ, "get", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", forbidden)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    report = run_v07_local_hash_rag_baseline(
        _load(),
        project_root=PROJECT_ROOT,
    )

    assert report.system.llm_enabled is False
    assert report.system.embedding_fake is True
    assert report.system.network_calls == 0
    assert report.system.model_calls == 0
    assert report.system.real_model_calls == 0
    assert report.system.input_tokens == 0
    assert report.system.output_tokens == 0
    assert report.system.llm_api_cost_cny == 0
    assert report.system.llm_api_cost_usd == 0
    assert report.system.external_actions == 0
    assert all(result.network_calls == 0 for result in report.results)


def test_saved_baseline_is_reproducible_and_cli_check_passes() -> None:
    first = run_v07_local_hash_rag_baseline(
        _load(),
        project_root=PROJECT_ROOT,
    )
    second = run_v07_local_hash_rag_baseline(
        _load(),
        project_root=PROJECT_ROOT,
    )
    saved = V07RAGBaselineReport.model_validate_json(
        BASELINE_PATH.read_text(encoding="utf-8")
    )

    assert first == second
    assert saved == first
    assert first.deterministic_result_sha256 == (
        v07_rag_deterministic_result_sha256(first)
    )
    assert saved.deterministic_result_sha256 == (
        v07_rag_deterministic_result_sha256(saved)
    )
    assert (
        first.summary.retrieval_recall_at_3.numerator,
        first.summary.retrieval_recall_at_3.denominator,
    ) == (3, 5)
    assert (
        first.summary.slices["semantic_direct"].numerator,
        first.summary.slices["semantic_direct"].denominator,
    ) == (3, 3)
    assert (
        first.summary.slices["resilience_partial"].numerator,
        first.summary.slices["resilience_partial"].denominator,
    ) == (0, 2)
    expected_modes = {
        "keyword_only": (4, 5),
        "hash_vector_only": (2, 5),
        "hybrid": (3, 5),
    }
    assert {
        mode: (
            result.retrieval_recall_at_3.numerator,
            result.retrieval_recall_at_3.denominator,
        )
        for mode, result in first.summary.retrieval_modes.items()
    } == expected_modes
    assert first.summary.retrieval_modes["hybrid"].slices == (
        first.summary.slices
    )
    assert any(
        "hybrid 相对 keyword 命中变化 -1" in boundary
        for boundary in first.boundaries
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_v07_rag_baseline.py"),
            "--check-baseline",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "deterministic_result_sha256: match" in completed.stdout
    assert "keyword_only: 4/5" in completed.stdout
    assert "hash_vector_only: 2/5" in completed.stdout
    assert "hybrid: 3/5" in completed.stdout
    assert "real_model_calls=0" in completed.stdout
    assert "do_not_claim_hash_vector_gain" in completed.stdout
