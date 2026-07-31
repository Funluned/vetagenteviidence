from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vetevidence.connector_artifacts import ConnectorArtifactStore
from vetevidence.database_batch_artifacts import (
    DatabaseBatchArtifactError,
    DatabaseBatchArtifactStore,
    DatabaseBatchMember,
    DatabaseBatchMemberStatus,
    DatabaseBatchStatus,
    RestrictedRawExportError,
    verify_database_batch_zip,
)
from vetevidence.database_connectors import (
    ConnectorResult,
    ConnectorStatus,
    ProvenanceRecord,
    ResponseArtifact,
)


def _result(
    *,
    records: tuple[dict[str, object], ...] | None = None,
) -> ConnectorResult:
    raw = b'{"cid":5280343,"secret":"raw-only"}'
    provenance = ProvenanceRecord(
        source_name="PubChem",
        endpoint_url="https://example.test/pubchem",
        method="GET",
        normalized_request='{"api_key":"<redacted>","method":"GET"}',
        request_sha256="a" * 64,
        raw_response_sha256=hashlib.sha256(raw).hexdigest(),
        retrieved_at_utc=datetime(2026, 7, 31, tzinfo=UTC),
        http_status=200,
        content_type="application/json",
        stable_ids=("PubChem:CID:5280343",),
    )
    return ConnectorResult(
        status=ConnectorStatus.OK,
        records=records
        or (
            {
                "record_type": "compound",
                "cid": 5280343,
                "label": "=2+2",
            },
        ),
        artifacts=(
            ResponseArtifact(
                provenance=provenance,
                raw_response=raw,
            ),
        ),
    )


def _stores(tmp_path):
    connector_store = ConnectorArtifactStore(tmp_path / "connectors")
    batch_store = DatabaseBatchArtifactStore(
        tmp_path / "batches",
        connector_store=connector_store,
    )
    return connector_store, batch_store


def _archived(query_id: str = "query-1") -> DatabaseBatchMember:
    return DatabaseBatchMember(
        source="pubchem",
        operation="compound lookup",
        query_id=query_id,
        status=DatabaseBatchMemberStatus.ARCHIVED,
    )


def test_manifest_is_frozen_immutable_and_computes_partial_status(tmp_path) -> None:
    connector_store, store = _stores(tmp_path)
    connector_store.save("run-1", "query-1", _result())
    failed = DatabaseBatchMember(
        source="drugbank",
        operation="licensed import",
        status=DatabaseBatchMemberStatus.FAILED,
        error="token=super-secret user@example.test",
    )

    store.save("run-1", "batch-1", (_archived(), failed))
    manifest = store.load_manifest("run-1", "batch-1")

    assert manifest.status is DatabaseBatchStatus.PARTIAL
    assert "super-secret" not in manifest.members[1].error
    assert "user@example.test" not in manifest.members[1].error
    with pytest.raises(ValidationError):
        manifest.status = DatabaseBatchStatus.COMPLETE
    with pytest.raises(FileExistsError):
        store.save("run-1", "batch-1", (_archived(),))


def test_normalized_zip_is_self_verifying_formula_safe_and_excludes_raw(
    tmp_path,
) -> None:
    connector_store, store = _stores(tmp_path)
    connector_store.save("run-1", "query-1", _result())
    store.save("run-1", "batch-1", (_archived(),))

    payload = store.build_normalized_zip("run-1", "batch-1")
    names = verify_database_batch_zip(payload)

    assert set(names) == {
        "SHA256SUMS.txt",
        "batch-manifest.json",
        "batch-result.json",
        "summary.csv",
        "tables/pubchem.csv",
    }
    assert b"raw-only" not in payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        result = json.loads(archive.read("batch-result.json"))
        assert result["members"][0]["result"]["records"][0]["cid"] == 5280343
        assert all(
            "raw_response" not in item
            for item in result["members"][0]["result"]["provenance"]
        )
        table = archive.read("tables/pubchem.csv").decode("utf-8-sig")
        assert "'=2+2" in table


def test_normalized_zip_rejects_tampered_query_archive(tmp_path) -> None:
    connector_store, store = _stores(tmp_path)
    target = connector_store.save("run-1", "query-1", _result())
    store.save("run-1", "batch-1", (_archived(),))
    (target / "result.json").write_bytes(b'{"records":[]}')

    with pytest.raises(DatabaseBatchArtifactError, match="SHA256SUMS"):
        store.build_normalized_zip("run-1", "batch-1")


def test_query_manifest_path_traversal_is_rejected(tmp_path) -> None:
    connector_store, store = _stores(tmp_path)
    target = connector_store.save("run-1", "query-1", _result())
    store.save("run-1", "batch-1", (_archived(),))
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["responses"][0]["filename"] = "../outside.bin"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(DatabaseBatchArtifactError, match="不安全"):
        store.build_normalized_zip("run-1", "batch-1")


def test_raw_audit_zip_requires_explicit_confirmation(tmp_path) -> None:
    connector_store, store = _stores(tmp_path)
    connector_store.save("run-1", "query-1", _result())
    store.save("run-1", "batch-1", (_archived(),))

    with pytest.raises(RestrictedRawExportError):
        store.build_raw_audit_zip("run-1", "batch-1")

    payload = store.build_raw_audit_zip(
        "run-1",
        "batch-1",
        allow_restricted_raw=True,
    )
    names = verify_database_batch_zip(payload)
    assert "queries/pubchem/query-1/response-001.json" in names
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert b"raw-only" in archive.read(
            "queries/pubchem/query-1/response-001.json"
        )


def test_restore_returns_valid_batches_and_warns_about_bad_directories(
    tmp_path,
) -> None:
    _, store = _stores(tmp_path)
    failed = DatabaseBatchMember(
        source="drugbank",
        operation="licensed import",
        status=DatabaseBatchMemberStatus.FAILED,
        error="offline",
    )
    store.save("run-1", "batch-1", (failed,))
    (store.root / "run-1" / "broken").mkdir()
    (store.root / "run-1" / "broken" / "junk.txt").write_text(
        "bad",
        encoding="utf-8",
    )

    report = store.restore_manifests("run-1", limit=10)

    assert [item.batch_id for item in report.manifests] == ["batch-1"]
    assert report.warnings and report.warnings[0].startswith("broken:")


def test_normalized_export_rejects_unredacted_sensitive_fields(tmp_path) -> None:
    connector_store, store = _stores(tmp_path)
    connector_store.save(
        "run-1",
        "query-1",
        _result(records=({"api_key": "should-not-export"},)),
    )
    store.save("run-1", "batch-1", (_archived(),))

    with pytest.raises(DatabaseBatchArtifactError, match="敏感字段"):
        store.build_normalized_zip("run-1", "batch-1")


def test_zip_verifier_rejects_duplicate_and_traversal_members() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape.txt", b"x")
        archive.writestr(
            "SHA256SUMS.txt",
            f"{hashlib.sha256(b'x').hexdigest()}  ../escape.txt\n",
        )

    with pytest.raises(DatabaseBatchArtifactError, match="路径不安全"):
        verify_database_batch_zip(output.getvalue())
