"""Persistent, hash-verified archives for external database responses."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.database_connectors import (
    CONNECTOR_PARSER_VERSION,
    ConnectorResult,
    ProvenanceRecord,
    canonical_json,
    export_connector_result,
    sha256_bytes,
)


CONNECTOR_ARCHIVE_SCHEMA_VERSION = "vetevidence-connector-archive-v1"
NORMALIZED_RECORD_SCHEMA_VERSION = "vetevidence-external-record-v1"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")


class ConnectorArchiveError(RuntimeError):
    """Raised when an archive cannot be written or fails integrity checks."""


class ArchiveModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ArchivedResponse(ArchiveModel):
    index: int = Field(ge=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    byte_count: int = Field(ge=0)
    content_type: str | None = None
    response_etag: str | None = None
    response_last_modified: str | None = None
    provenance: ProvenanceRecord


class ConnectorArchiveManifest(ArchiveModel):
    schema_version: Literal["vetevidence-connector-archive-v1"] = (
        CONNECTOR_ARCHIVE_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    created_at_utc: datetime
    parser_version: str = CONNECTOR_PARSER_VERSION
    normalized_record_schema_version: str = NORMALIZED_RECORD_SCHEMA_VERSION
    connector_status: str = Field(min_length=1)
    normalized_records_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    result_json_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    responses: tuple[ArchivedResponse, ...] = ()
    warnings: tuple[str, ...] = ()


def _safe_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not _SAFE_IDENTIFIER.fullmatch(cleaned):
        raise ValueError(
            f"{label} 只能包含字母、数字、点、下划线和连字符。"
        )
    return cleaned


def _suffix_for_content_type(content_type: str | None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if media_type.endswith("+json") or media_type == "application/json":
        return ".json"
    if media_type.endswith("+xml") or media_type in {
        "application/xml",
        "text/xml",
    }:
        return ".xml"
    if "chemical/x-mmcif" in media_type or "cif" in media_type:
        return ".cif"
    if "sdf" in media_type:
        return ".sdf"
    if media_type.startswith("text/"):
        return ".txt"
    return ".bin"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ConnectorArtifactStore:
    """Write each query once and verify every payload again when loading."""

    def __init__(self, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = root or project_root / ".workbench" / "connectors"

    def path_for(self, run_id: str, query_id: str) -> Path:
        return self.root / _safe_identifier(
            run_id,
            "run_id",
        ) / _safe_identifier(query_id, "query_id")

    def save(
        self,
        run_id: str,
        query_id: str,
        result: ConnectorResult,
    ) -> Path:
        target = self.path_for(run_id, query_id)
        if target.exists():
            raise FileExistsError(
                f"连接器查询归档已存在，不能覆盖：{target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.mkdir()
        try:
            archived_responses: list[ArchivedResponse] = []
            for index, artifact in enumerate(result.artifacts, start=1):
                actual_sha256 = sha256_bytes(artifact.raw_response)
                expected_sha256 = artifact.provenance.raw_response_sha256
                if actual_sha256 != expected_sha256:
                    raise ConnectorArchiveError(
                        f"第 {index} 个原始响应与 provenance 哈希不一致。"
                    )
                suffix = _suffix_for_content_type(
                    artifact.provenance.content_type
                )
                filename = f"response-{index:03d}{suffix}"
                (temporary / filename).write_bytes(artifact.raw_response)
                archived_responses.append(
                    ArchivedResponse(
                        index=index,
                        filename=filename,
                        sha256=actual_sha256,
                        byte_count=len(artifact.raw_response),
                        content_type=artifact.provenance.content_type,
                        response_etag=artifact.response_headers.get("etag"),
                        response_last_modified=(
                            artifact.response_headers.get("last-modified")
                        ),
                        provenance=artifact.provenance,
                    )
                )

            result_export = export_connector_result(result)
            result_bytes = result_export.content.encode("utf-8")
            (temporary / "result.json").write_bytes(result_bytes)
            records_sha256 = sha256_bytes(
                canonical_json(result.records).encode("utf-8")
            )
            manifest = ConnectorArchiveManifest(
                run_id=_safe_identifier(run_id, "run_id"),
                query_id=_safe_identifier(query_id, "query_id"),
                created_at_utc=datetime.now(UTC),
                connector_status=result.status.value,
                normalized_records_sha256=records_sha256,
                result_json_sha256=sha256_bytes(result_bytes),
                responses=tuple(archived_responses),
                warnings=result.warnings,
            )
            manifest_bytes = json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            (temporary / "manifest.json").write_bytes(manifest_bytes)

            checksummed_files = [
                *[item.filename for item in archived_responses],
                "result.json",
                "manifest.json",
            ]
            checksum_lines = [
                f"{_sha256_file(temporary / filename)}  {filename}"
                for filename in checksummed_files
            ]
            (temporary / "SHA256SUMS.txt").write_text(
                "\n".join(checksum_lines) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def load_manifest(
        self,
        run_id: str,
        query_id: str,
    ) -> ConnectorArchiveManifest:
        target = self.path_for(run_id, query_id)
        try:
            manifest = ConnectorArchiveManifest.model_validate_json(
                (target / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ConnectorArchiveError(
                "连接器归档清单缺失或无法解析。"
            ) from exc
        if manifest.run_id != run_id or manifest.query_id != query_id:
            raise ConnectorArchiveError("连接器归档身份与请求不一致。")
        if _sha256_file(target / "result.json") != manifest.result_json_sha256:
            raise ConnectorArchiveError("连接器 result.json 哈希不一致。")
        for response in manifest.responses:
            path = target / response.filename
            if (
                not path.is_file()
                or path.stat().st_size != response.byte_count
                or _sha256_file(path) != response.sha256
            ):
                raise ConnectorArchiveError(
                    f"连接器原始响应校验失败：{response.filename}"
                )
        return manifest

    def build_zip(self, run_id: str, query_id: str) -> bytes:
        self.load_manifest(run_id, query_id)
        target = self.path_for(run_id, query_id)
        filenames = [
            "manifest.json",
            "result.json",
            "SHA256SUMS.txt",
            *[
                item.name
                for item in sorted(target.glob("response-*"))
                if item.is_file()
            ],
        ]
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for filename in filenames:
                archive.writestr(filename, (target / filename).read_bytes())
        return buffer.getvalue()


__all__ = [
    "CONNECTOR_ARCHIVE_SCHEMA_VERSION",
    "CONNECTOR_PARSER_VERSION",
    "NORMALIZED_RECORD_SCHEMA_VERSION",
    "ArchivedResponse",
    "ConnectorArchiveError",
    "ConnectorArchiveManifest",
    "ConnectorArtifactStore",
]
