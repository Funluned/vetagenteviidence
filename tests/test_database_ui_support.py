from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vetevidence.connector_artifacts import ConnectorArtifactStore
from vetevidence.database_connectors import (
    ConnectorResult,
    ConnectorStatus,
    DatabaseEvidenceClass,
    ProvenanceRecord,
    ResponseArtifact,
)
from vetevidence.database_ui_support import (
    CUSTOM_TAXON_LABEL,
    DATABASE_SOURCE_CONFIGS,
    DAVID_SUPPORTED_TAXON_IDS,
    SWISS_TARGET_SUPPORTED_TAXON_IDS,
    VETERINARY_SPECIES_TAX_IDS,
    DatabaseQueryExecution,
    DatabaseQueryGroupExecution,
    DatabaseSourceConfig,
    parse_taxon_selection,
    restore_connector_entries,
    summarize_connector_results,
)
from vetevidence.restricted_database_imports import (
    import_restricted_database_file,
)


def _result(
    *,
    status: ConnectorStatus = ConnectorStatus.OK,
    source_name: str = "PubChem",
    record_type: str = "compound",
    with_artifact: bool = True,
) -> ConnectorResult:
    raw = b'{"ok":true}'
    artifacts: tuple[ResponseArtifact, ...] = ()
    if with_artifact:
        artifacts = (
            ResponseArtifact(
                provenance=ProvenanceRecord(
                    source_name=source_name,
                    endpoint_url="https://example.test/query",
                    method="GET",
                    normalized_request=(
                        '{"params":{"email":"<redacted>"}}'
                    ),
                    request_sha256="a" * 64,
                    raw_response_sha256=hashlib.sha256(raw).hexdigest(),
                    retrieved_at_utc=datetime(2026, 7, 30, tzinfo=UTC),
                    http_status=200,
                    content_type="application/json",
                ),
                raw_response=raw,
                response_headers={"etag": '"fixture"'},
            ),
        )
    records = (
        ({"record_type": record_type, "source_url": "https://example.test"},)
        if status is ConnectorStatus.OK
        else ()
    )
    return ConnectorResult(
        status=status,
        records=records,
        artifacts=artifacts,
    )


def _set_created_at(path, value: str) -> None:
    manifest_path = path / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["created_at_utc"] = value
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def test_source_configs_cover_exactly_twelve_product_sources() -> None:
    assert [item.key for item in DATABASE_SOURCE_CONFIGS] == [
        "pubchem",
        "uniprot",
        "ncbi-gene",
        "genbank",
        "rcsb-pdb",
        "string",
        "david",
        "omim",
        "drugbank",
        "genecards",
        "malacards",
        "swiss-target-prediction",
    ]
    by_key = {item.key: item for item in DATABASE_SOURCE_CONFIGS}
    assert all(
        item.label and item.input_label and item.placeholder
        for item in DATABASE_SOURCE_CONFIGS
    )
    assert {
        key for key, item in by_key.items() if item.requires_taxon_id
    } == {
        "uniprot",
        "ncbi-gene",
        "genbank",
        "string",
        "david",
        "swiss-target-prediction",
    }
    assert {
        key for key, item in by_key.items() if item.requires_ncbi_email
    } == {"ncbi-gene", "genbank"}
    assert {
        key for key, item in by_key.items() if item.requires_david_email
    } == {"david"}
    assert {
        key
        for key, item in by_key.items()
        if item.requires_external_consent
    } == {"string", "david"}
    assert {
        key for key, item in by_key.items() if item.requires_omim_key
    } == {"omim"}
    assert {
        key for key, item in by_key.items() if item.requires_drugbank_key
    } == {"drugbank"}
    assert {
        key
        for key, item in by_key.items()
        if item.requires_license_confirmation
    } == {"drugbank", "genecards", "malacards"}
    assert {
        key for key, item in by_key.items() if item.requires_manual_import
    } == {"genecards", "malacards", "swiss-target-prediction"}


def test_new_source_access_modes_evidence_and_taxon_constraints() -> None:
    by_key = {item.key: item for item in DATABASE_SOURCE_CONFIGS}

    assert {
        key: by_key[key].access_mode
        for key in (
            "omim",
            "drugbank",
            "genecards",
            "malacards",
            "swiss-target-prediction",
        )
    } == {
        "omim": "credentialed_api",
        "drugbank": "licensed_api",
        "genecards": "licensed_import",
        "malacards": "licensed_import",
        "swiss-target-prediction": "manual_prediction_import",
    }
    assert {
        key: by_key[key].fixed_taxon_id
        for key in ("omim", "genecards", "malacards")
    } == {
        "omim": 9606,
        "genecards": 9606,
        "malacards": 9606,
    }
    assert by_key["drugbank"].fixed_taxon_id is None
    assert "InChIKey" not in by_key["drugbank"].input_label
    assert (
        by_key["swiss-target-prediction"].evidence_class
        is DatabaseEvidenceClass.COMPUTATIONAL_PREDICTION
    )
    assert all(
        by_key[key].evidence_class is DatabaseEvidenceClass.CURATED_DATABASE
        for key in ("omim", "drugbank", "genecards", "malacards")
    )
    assert SWISS_TARGET_SUPPORTED_TAXON_IDS == frozenset(
        {9606, 10090, 10116}
    )

    with pytest.raises(ValidationError):
        DatabaseSourceConfig(
            key="omim",
            label="OMIM",
            input_label="MIM 编号",
            placeholder="例如：100100",
            fixed_taxon_id=0,
        )


def test_taxon_selection_supports_named_species_and_strict_custom_ids() -> None:
    assert VETERINARY_SPECIES_TAX_IDS["牛（Bos taurus）"] == 9913
    assert parse_taxon_selection("牛（Bos taurus）") == 9913
    assert parse_taxon_selection(CUSTOM_TAXON_LABEL, " 9615 ") == 9615
    assert parse_taxon_selection(CUSTOM_TAXON_LABEL, 9685) == 9685
    assert DAVID_SUPPORTED_TAXON_IDS == frozenset(
        {9031, 9606, 9615, 9685, 9823, 9913, 10090, 10116}
    )

    invalid = (
        ("不存在的物种", None),
        (CUSTOM_TAXON_LABEL, None),
        (CUSTOM_TAXON_LABEL, ""),
        (CUSTOM_TAXON_LABEL, "0"),
        (CUSTOM_TAXON_LABEL, "-1"),
        (CUSTOM_TAXON_LABEL, "1.5"),
        (CUSTOM_TAXON_LABEL, True),
    )
    for selection, custom in invalid:
        with pytest.raises(ValueError):
            parse_taxon_selection(selection, custom)


def test_connector_summary_is_complete_and_immutable() -> None:
    results = [
        _result(status=ConnectorStatus.OK),
        _result(status=ConnectorStatus.OK),
        _result(status=ConnectorStatus.NO_RESULTS),
        _result(status=ConnectorStatus.OFFLINE_EXPORT, with_artifact=False),
        _result(status=ConnectorStatus.DEGRADED),
    ]

    summary = summarize_connector_results(results)

    assert summary.model_dump() == {
        "total": 5,
        "online_available": 2,
        "no_results": 1,
        "offline_export": 1,
        "degraded": 1,
    }
    with pytest.raises(ValidationError):
        summary.total = 10


def test_query_group_exposes_frozen_membership_without_history_slicing() -> None:
    first = DatabaseQueryExecution(
        source="PubChem",
        input_summary="input_sha256=one",
        query_id="pubchem-one",
        result=_result(),
    )
    failed = DatabaseQueryExecution(
        source="PubChem",
        input_summary="input_sha256=two",
        error="timeout",
    )
    other_source = DatabaseQueryExecution(
        source="OMIM",
        input_summary="input_sha256=one",
        query_id="omim-one",
        result=_result(
            status=ConnectorStatus.OFFLINE_EXPORT,
            source_name="OMIM",
            with_artifact=False,
        ),
    )

    pubchem = DatabaseQueryGroupExecution(
        source="PubChem",
        operations=(first, failed),
    )
    omim = DatabaseQueryGroupExecution(
        source="OMIM",
        operations=(other_source,),
    )

    assert pubchem.planned_count == 2
    assert pubchem.archived_count == 1
    assert pubchem.failed_count == 1
    assert pubchem.query_ids == ("pubchem-one",)
    assert pubchem.results == (first.result,)
    assert omim.query_ids == ("omim-one",)
    assert first.archived is True
    assert failed.archived is False


def test_restore_verifies_archives_hydrates_raw_responses_and_sorts(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    newer = store.save(
        "run-1",
        "pubchem-newer",
        _result(),
    )
    older = store.save(
        "run-1",
        "ncbi-gene-older",
        _result(
            source_name="NCBI Gene/GenBank",
            record_type="gene",
        ),
    )
    _set_created_at(older, "2026-07-29T00:00:00Z")
    _set_created_at(newer, "2026-07-30T00:00:00Z")

    report = restore_connector_entries(store, "run-1")

    assert report.warnings == ()
    assert [entry.query_id for entry in report.entries] == [
        "ncbi-gene-older",
        "pubchem-newer",
    ]
    assert [entry.source for entry in report.entries] == [
        "NCBI Gene",
        "PubChem",
    ]
    assert report.entries[1].result.artifacts[0].raw_response == b'{"ok":true}'
    assert (
        report.entries[1].result.artifacts[0].response_headers["etag"]
        == '"fixture"'
    )
    assert report.entries[1].archive_path.endswith("/run-1/pubchem-newer")


def test_restore_infers_offline_source_from_verified_query_id(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    store.save(
        "run-1",
        "string-offline",
        _result(
            status=ConnectorStatus.OFFLINE_EXPORT,
            with_artifact=False,
        ),
    )

    report = restore_connector_entries(store, "run-1")

    assert len(report.entries) == 1
    assert report.entries[0].source == "STRING"
    assert report.entries[0].result.status is ConnectorStatus.OFFLINE_EXPORT


def test_restore_infers_manual_import_source_and_hydrates_csv(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    payload = b"Target,UniProt ID,Probability\nEGFR,P00533,0.9\n"
    result = import_restricted_database_file(
        "SwissTargetPrediction",
        "prediction.csv",
        payload,
        license_confirmed=True,
        query_context={"smiles": "CCO", "taxon_id": 9606},
    )
    store.save(
        "run-1",
        "swiss-target-prediction-import",
        result,
    )

    report = restore_connector_entries(store, "run-1")

    assert report.warnings == ()
    assert report.entries[0].source == "SwissTargetPrediction"
    restored = report.entries[0].result
    assert restored.acquisition_mode.value == "manual_import"
    assert restored.evidence_class.value == "computational_prediction"
    assert restored.artifacts[0].provenance.method == "IMPORT"
    assert restored.artifacts[0].provenance.http_status is None
    assert restored.artifacts[0].raw_response == payload


def test_restore_skips_tampering_bad_directories_and_identity_renames(
    tmp_path,
) -> None:
    store = ConnectorArtifactStore(tmp_path)
    tampered = store.save("run-1", "pubchem-tampered", _result())
    (tampered / "result.json").write_text(
        '{"email":"secret@example.test"}',
        encoding="utf-8",
    )
    renamed = store.save("run-1", "uniprot-original", _result())
    renamed.rename(renamed.with_name("uniprot-renamed"))
    bad_directory = tmp_path / "run-1" / "含空格 bad"
    bad_directory.mkdir()
    (bad_directory / "manifest.json").write_text(
        "secret@example.test",
        encoding="utf-8",
    )

    report = restore_connector_entries(store, "run-1")

    assert report.entries == ()
    assert len(report.warnings) == 3
    assert all("secret@example.test" not in item for item in report.warnings)
    assert all("uniprot-original" not in item for item in report.warnings)


def test_restore_rejects_hash_valid_result_with_sensitive_field(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    target = store.save("run-1", "pubchem-sensitive", _result())
    result_path = target / "result.json"
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    result_payload["records"][0]["api_key"] = "must-not-leak"
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["result_json_sha256"] = hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    report = restore_connector_entries(store, "run-1")

    assert report.entries == ()
    assert len(report.warnings) == 1
    assert "must-not-leak" not in report.warnings[0]


def test_restore_rejects_response_declared_above_size_limit(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    target = store.save("run-1", "pubchem-oversized", _result())
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["responses"][0]["byte_count"] = 64 * 1024 * 1024 + 1
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    report = restore_connector_entries(store, "run-1")

    assert report.entries == ()
    assert len(report.warnings) == 1


def test_restore_keeps_newest_entries_and_enforces_hard_limit(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    for index in range(3):
        target = store.save(
            "run-1",
            f"pubchem-{index:03d}",
            _result(status=ConnectorStatus.NO_RESULTS),
        )
        _set_created_at(
            target,
            f"2026-07-{index + 1:02d}T00:00:00Z",
        )

    report = restore_connector_entries(store, "run-1", limit=2)

    assert [entry.query_id for entry in report.entries] == [
        "pubchem-001",
        "pubchem-002",
    ]
    with pytest.raises(ValueError):
        restore_connector_entries(store, "run-1", limit=101)
