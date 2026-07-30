from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from io import BytesIO

import pytest

from vetevidence.connector_artifacts import (
    ConnectorArchiveError,
    ConnectorArtifactStore,
)
from vetevidence.database_connectors import (
    CONNECTOR_PARSER_VERSION,
    ConnectorResult,
    ConnectorStatus,
    ProvenanceRecord,
    ResponseArtifact,
)


def _result() -> ConnectorResult:
    raw = b'{"cid":5280343}'
    provenance = ProvenanceRecord(
        source_name="PubChem",
        endpoint_url="https://example.test/pubchem",
        method="GET",
        normalized_request=(
            '{"method":"GET","params":{"api_key":"<redacted>"}}'
        ),
        request_sha256="a" * 64,
        raw_response_sha256=hashlib.sha256(raw).hexdigest(),
        retrieved_at_utc=datetime(2026, 7, 30, tzinfo=UTC),
        http_status=200,
        content_type="application/json",
        source_version="2026-07-01",
        stable_ids=("PubChem:CID:5280343",),
    )
    return ConnectorResult(
        status=ConnectorStatus.OK,
        records=(
            {
                "record_type": "compound",
                "cid": 5280343,
                "inchikey": "REFJWTPEDVJJIY-UHFFFAOYSA-N",
            },
        ),
        artifacts=(
            ResponseArtifact(
                provenance=provenance,
                raw_response=raw,
                response_headers={
                    "etag": '"abc"',
                    "last-modified": "Wed, 01 Jul 2026 00:00:00 GMT",
                },
            ),
        ),
    )


def test_store_preserves_raw_response_and_builds_verified_zip(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    target = store.save("run-1", "query-1", _result())
    (target / "response-999.bin").write_bytes(b"not declared by manifest")

    manifest = store.load_manifest("run-1", "query-1")
    assert manifest.responses[0].filename == "response-001.json"
    assert manifest.responses[0].response_etag == '"abc"'
    assert (target / "response-001.json").read_bytes() == b'{"cid":5280343}'
    assert manifest.parser_version == CONNECTOR_PARSER_VERSION
    assert manifest.parser_version == "vetevidence-database-connectors-0.5"
    assert len(manifest.normalized_records_sha256) == 64

    with zipfile.ZipFile(BytesIO(store.build_zip("run-1", "query-1"))) as zf:
        assert set(zf.namelist()) == {
            "manifest.json",
            "result.json",
            "response-001.json",
            "SHA256SUMS.txt",
        }
        exported = json.loads(zf.read("result.json"))
        assert exported["records"][0]["cid"] == 5280343
        assert "raw_response" not in exported["artifacts"][0]


def test_build_zip_rejects_manifest_response_path_traversal(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    target = store.save("run-1", "query-1", _result())
    outside = target.parent / "outside.bin"
    outside.write_bytes(b'{"cid":5280343}')
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["responses"][0]["filename"] = "../outside.bin"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorArchiveError, match="不安全"):
        store.build_zip("run-1", "query-1")


def test_store_uses_short_staging_name_for_long_windows_query_ids(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path / "connectors")
    run_id = "run-" + "b" * 32
    query_id = "swiss-target-prediction-" + "a" * 32

    target = store.save(run_id, query_id, _result())

    assert target.name == query_id
    assert store.load_manifest(run_id, query_id).query_id == query_id
    assert not list(target.parent.glob(".tmp-*"))


def test_store_refuses_overwrite_and_detects_tampering(tmp_path) -> None:
    store = ConnectorArtifactStore(tmp_path)
    target = store.save("run-1", "query-1", _result())

    with pytest.raises(FileExistsError):
        store.save("run-1", "query-1", _result())

    (target / "response-001.json").write_bytes(b"tampered")
    with pytest.raises(ConnectorArchiveError, match="校验失败"):
        store.load_manifest("run-1", "query-1")


@pytest.mark.parametrize("unsafe", ["../escape", "a/b", "", "含空格"])
def test_store_rejects_unsafe_identifiers(tmp_path, unsafe: str) -> None:
    store = ConnectorArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for(unsafe, "query-1")
