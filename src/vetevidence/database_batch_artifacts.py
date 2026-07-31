"""Immutable, hash-verified bundles for multi-database query batches."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import stat
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self, Sequence
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from vetevidence.connector_artifacts import (
    ConnectorArchiveManifest,
    ConnectorArtifactStore,
)
from vetevidence.database_connectors import canonical_json, sha256_bytes


DATABASE_BATCH_SCHEMA_VERSION = "vetevidence-database-batch-v1"
DATABASE_BATCH_RESULT_SCHEMA_VERSION = "vetevidence-database-batch-result-v1"
DATABASE_RAW_AUDIT_SCHEMA_VERSION = "vetevidence-database-raw-audit-v1"
MAX_BATCH_MEMBERS = 50
MAX_BATCH_RECORDS = 100_000
MAX_QUERY_RESULT_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_VERIFIED_RAW_BYTES = 256 * 1024 * 1024
MAX_NORMALIZED_ZIP_BYTES = 128 * 1024 * 1024
MAX_RAW_AUDIT_ZIP_BYTES = 320 * 1024 * 1024
MAX_ZIP_MEMBERS = 4096

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_RESPONSE_FILENAME = re.compile(
    r"^response-[0-9]{3}\.(?:json|xml|cif|sdf|csv|xlsx|txt|bin)$"
)
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
_FORMULA_PREFIX = re.compile(r"^[\t\r\n ]*[=+\-@]")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|password|secret|token)"
    r"\b(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_QUERY_PARAM = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|password|secret|token)=)[^&#\s]+"
)
_EMAIL_ADDRESS = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "email",
        "password",
        "secret",
        "token",
    }
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_BATCH_MANIFEST_FILENAME = "batch-manifest.json"
_CHECKSUM_FILENAME = "SHA256SUMS.txt"


class DatabaseBatchArtifactError(RuntimeError):
    """Raised when a database batch cannot be stored or verified."""


class RestrictedRawExportError(PermissionError):
    """Raised unless a caller explicitly confirms restricted raw export."""


class DatabaseBatchMemberStatus(StrEnum):
    ARCHIVED = "archived"
    FAILED = "failed"


class DatabaseBatchStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class _BatchModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


def _safe_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not _SAFE_IDENTIFIER.fullmatch(cleaned):
        raise ValueError(
            f"{label} 只能包含字母、数字、点、下划线和连字符。"
        )
    return cleaned


def sanitize_batch_error(value: str) -> str:
    """Remove common credentials and personal email from an error message."""

    cleaned = value.replace("\x00", "\ufffd")
    cleaned = _SECRET_QUERY_PARAM.sub(r"\1<redacted>", cleaned)
    cleaned = _SECRET_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}<redacted>"
        ),
        cleaned,
    )
    cleaned = _BEARER_TOKEN.sub("Bearer <redacted>", cleaned)
    cleaned = _EMAIL_ADDRESS.sub("<redacted-email>", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "查询未归档，未提供错误详情。"
    return cleaned[:500]


class DatabaseBatchMember(_BatchModel):
    """A frozen outcome for one planned source operation."""

    source: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=500)
    query_id: str | None = Field(default=None, min_length=1, max_length=200)
    status: DatabaseBatchMemberStatus
    error: str | None = Field(default=None, max_length=500)

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        return _safe_identifier(value, "source")

    @field_validator("query_id")
    @classmethod
    def _validate_query_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_identifier(value, "query_id")

    @field_validator("error", mode="before")
    @classmethod
    def _sanitize_error(cls, value: object) -> object:
        if value is None:
            return None
        return sanitize_batch_error(str(value))

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if self.status is DatabaseBatchMemberStatus.ARCHIVED:
            if self.query_id is None:
                raise ValueError("archived 成员必须包含 query_id。")
            if self.error is not None:
                raise ValueError("archived 成员不能包含 error。")
        elif self.error is None:
            raise ValueError("failed 成员必须包含脱敏后的 error。")
        return self


def _batch_status(
    members: Sequence[DatabaseBatchMember],
) -> DatabaseBatchStatus:
    archived = sum(
        member.status is DatabaseBatchMemberStatus.ARCHIVED
        for member in members
    )
    if archived == len(members):
        return DatabaseBatchStatus.COMPLETE
    if archived:
        return DatabaseBatchStatus.PARTIAL
    return DatabaseBatchStatus.FAILED


class DatabaseBatchManifest(_BatchModel):
    """Immutable identity and frozen membership of one database batch."""

    schema_version: Literal["vetevidence-database-batch-v1"] = (
        DATABASE_BATCH_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1, max_length=200)
    batch_id: str = Field(min_length=1, max_length=200)
    created_at_utc: datetime
    status: DatabaseBatchStatus
    members: tuple[DatabaseBatchMember, ...] = Field(
        min_length=1,
        max_length=MAX_BATCH_MEMBERS,
    )

    @field_validator("run_id", "batch_id")
    @classmethod
    def _validate_identifier(cls, value: str, info: Any) -> str:
        return _safe_identifier(value, info.field_name)

    @field_validator("created_at_utc")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at_utc 必须包含时区。")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_members(self) -> Self:
        identities: set[tuple[str, str, str | None]] = set()
        query_ids: set[str] = set()
        for member in self.members:
            identity = (
                member.source.casefold(),
                member.operation.casefold(),
                member.query_id,
            )
            if identity in identities:
                raise ValueError("批次成员重复。")
            identities.add(identity)
            if member.query_id is not None:
                if member.query_id in query_ids:
                    raise ValueError("批次中的 query_id 必须唯一。")
                query_ids.add(member.query_id)
        expected_status = _batch_status(self.members)
        if self.status is not expected_status:
            raise ValueError(
                f"批次状态应为 {expected_status.value}，"
                f"而不是 {self.status.value}。"
            )
        return self


class DatabaseBatchRestoreReport(_BatchModel):
    """Valid persisted batches plus non-fatal recovery warnings."""

    manifests: tuple[DatabaseBatchManifest, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _VerifiedQueryArchive:
    directory: Path
    manifest: ConnectorArchiveManifest
    manifest_bytes: bytes
    result_bytes: bytes
    checksum_bytes: bytes
    result: dict[str, Any]
    response_filenames: tuple[str, ...]
    raw_byte_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _direct_file(
    directory: Path,
    filename: str,
    *,
    max_bytes: int,
) -> Path:
    path = directory / filename
    try:
        resolved_directory = directory.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DatabaseBatchArtifactError(
            f"归档文件缺失或无法解析：{filename}"
        ) from exc
    if (
        _is_link_or_reparse(directory)
        or _is_link_or_reparse(path)
        or resolved.parent != resolved_directory
        or not resolved.is_file()
    ):
        raise DatabaseBatchArtifactError(
            f"归档文件越界、不是普通文件或使用了链接：{filename}"
        )
    if resolved.stat().st_size > max_bytes:
        raise DatabaseBatchArtifactError(
            f"归档文件超过大小上限：{filename}"
        )
    return resolved


def _parse_checksums(
    payload: bytes,
    *,
    expected_names: set[str],
) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatabaseBatchArtifactError(
            "SHA256SUMS.txt 不是 UTF-8 文本。"
        ) from exc
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        digest, separator, filename = line.partition("  ")
        if (
            not separator
            or not _CHECKSUM.fullmatch(digest)
            or not filename
            or filename in checksums
        ):
            raise DatabaseBatchArtifactError(
                "SHA256SUMS.txt 包含无效或重复条目。"
            )
        checksums[filename] = digest
    if set(checksums) != expected_names:
        raise DatabaseBatchArtifactError(
            "SHA256SUMS.txt 的成员集合与归档清单不一致。"
        )
    return checksums


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _checksum_bytes(files: dict[str, bytes]) -> bytes:
    return (
        "\n".join(
            f"{sha256_bytes(files[name])}  {name}"
            for name in sorted(files)
        )
        + "\n"
    ).encode("utf-8")


def _safe_zip_name(name: str) -> str:
    if (
        not name
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or name.startswith("/")
        or name.endswith("/")
    ):
        raise DatabaseBatchArtifactError(f"ZIP 成员路径不安全：{name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise DatabaseBatchArtifactError(f"ZIP 成员路径不安全：{name!r}")
    return name


def _deterministic_zip(
    files: dict[str, bytes],
    *,
    max_payload_bytes: int,
) -> bytes:
    if len(files) > MAX_ZIP_MEMBERS:
        raise DatabaseBatchArtifactError("ZIP 成员数量超过上限。")
    total = sum(len(payload) for payload in files.values())
    if total > max_payload_bytes:
        raise DatabaseBatchArtifactError("ZIP 未压缩内容超过大小上限。")
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(files):
            safe_name = _safe_zip_name(name)
            info = zipfile.ZipInfo(safe_name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])
    payload = output.getvalue()
    if len(payload) > max_payload_bytes:
        raise DatabaseBatchArtifactError("ZIP 压缩包超过大小上限。")
    return payload


def verify_database_batch_zip(
    payload: bytes,
    *,
    max_uncompressed_bytes: int = MAX_RAW_AUDIT_ZIP_BYTES,
) -> tuple[str, ...]:
    """Verify safe paths, unique members and root checksums in a batch ZIP."""

    if len(payload) > MAX_RAW_AUDIT_ZIP_BYTES:
        raise DatabaseBatchArtifactError("ZIP 压缩包超过大小上限。")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ZIP_MEMBERS:
                raise DatabaseBatchArtifactError("ZIP 成员数量无效。")
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len(names) != len(
                {name.casefold() for name in names}
            ):
                raise DatabaseBatchArtifactError("ZIP 包含重复成员。")
            for entry, name in zip(entries, names, strict=True):
                _safe_zip_name(name)
                file_type = (entry.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    raise DatabaseBatchArtifactError(
                        f"ZIP 成员不能是符号链接：{name}"
                    )
            if _CHECKSUM_FILENAME not in names:
                raise DatabaseBatchArtifactError(
                    "ZIP 缺少 SHA256SUMS.txt。"
                )
            total = sum(entry.file_size for entry in entries)
            if total > max_uncompressed_bytes:
                raise DatabaseBatchArtifactError(
                    "ZIP 未压缩内容超过大小上限。"
                )
            checksum_payload = archive.read(_CHECKSUM_FILENAME)
            expected_names = set(names) - {_CHECKSUM_FILENAME}
            checksums = _parse_checksums(
                checksum_payload,
                expected_names=expected_names,
            )
            for name, expected in checksums.items():
                if sha256_bytes(archive.read(name)) != expected:
                    raise DatabaseBatchArtifactError(
                        f"ZIP 成员哈希校验失败：{name}"
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, DatabaseBatchArtifactError):
            raise
        raise DatabaseBatchArtifactError("无法读取数据库批次 ZIP。") from exc
    return tuple(sorted(names))


def _formula_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = canonical_json(value)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = text.replace("\x00", "\ufffd")
    if _FORMULA_PREFIX.match(text):
        return f"'{text}"
    return text


def _csv_bytes(
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(fieldnames),
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writerow(
        {fieldname: _formula_safe(fieldname) for fieldname in fieldnames}
    )
    for row in rows:
        writer.writerow(
            {
                fieldname: _formula_safe(row.get(fieldname))
                for fieldname in fieldnames
            }
        )
    return output.getvalue().encode("utf-8-sig")


def _normalized_result(result: dict[str, Any]) -> dict[str, Any]:
    """Copy only normalized fields; raw response-like fields never pass."""

    artifacts = result.get("artifacts")
    provenance: list[dict[str, Any]] = []
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            item = artifact.get("provenance")
            if isinstance(item, dict):
                provenance.append(item)
    return {
        key: result.get(key)
        for key in (
            "status",
            "acquisition_mode",
            "evidence_class",
            "records",
            "mappings",
            "warnings",
            "offline_request",
            "export_metadata",
        )
    } | {"provenance": provenance}


def _assert_no_unredacted_sensitive_data(
    value: Any,
    *,
    path: str = "$",
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if (
                name.casefold() in _SENSITIVE_FIELD_NAMES
                and item is not None
                and not (
                    isinstance(item, str)
                    and item in {"", "<redacted>", "<redacted-email>"}
                )
            ):
                raise DatabaseBatchArtifactError(
                    f"规范化下载仍含未脱敏敏感字段：{path}.{name}"
                )
            _assert_no_unredacted_sensitive_data(
                item,
                path=f"{path}.{name}",
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_unredacted_sensitive_data(
                item,
                path=f"{path}[{index}]",
            )
        return
    if not isinstance(value, str):
        return
    if (
        _SECRET_QUERY_PARAM.search(value)
        or _SECRET_ASSIGNMENT.search(value)
        or _BEARER_TOKEN.search(value)
        or _EMAIL_ADDRESS.search(value)
    ):
        raise DatabaseBatchArtifactError(
            f"规范化下载仍含疑似未脱敏敏感值：{path}"
        )


class DatabaseBatchArtifactStore:
    """Persist final batch membership once and generate verified downloads."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        connector_store: ConnectorArtifactStore | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = (
            root
            if root is not None
            else project_root / ".workbench" / "connector-batches"
        )
        self.connector_store = connector_store or ConnectorArtifactStore()

    @staticmethod
    def new_batch_id() -> str:
        return f"batch-{uuid4().hex}"

    def path_for(self, run_id: str, batch_id: str) -> Path:
        return self.root / _safe_identifier(
            run_id,
            "run_id",
        ) / _safe_identifier(batch_id, "batch_id")

    def save(
        self,
        run_id: str,
        batch_id: str,
        members: Sequence[DatabaseBatchMember],
        *,
        created_at_utc: datetime | None = None,
    ) -> Path:
        """Atomically persist a completed batch; existing batches are immutable."""

        frozen_members = tuple(members)
        manifest = DatabaseBatchManifest(
            run_id=run_id,
            batch_id=batch_id,
            created_at_utc=created_at_utc or datetime.now(UTC),
            status=_batch_status(frozen_members),
            members=frozen_members,
        )
        target = self.path_for(manifest.run_id, manifest.batch_id)
        if target.exists():
            raise FileExistsError(
                f"数据库批次已存在，不能覆盖：{target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".tmp-{uuid4().hex}"
        temporary.mkdir()
        try:
            manifest_bytes = _json_bytes(manifest.model_dump(mode="json"))
            (temporary / _BATCH_MANIFEST_FILENAME).write_bytes(
                manifest_bytes
            )
            checksum = (
                f"{sha256_bytes(manifest_bytes)}  "
                f"{_BATCH_MANIFEST_FILENAME}\n"
            ).encode("utf-8")
            (temporary / _CHECKSUM_FILENAME).write_bytes(checksum)
            temporary.replace(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def _load_manifest_and_bytes(
        self,
        run_id: str,
        batch_id: str,
    ) -> tuple[DatabaseBatchManifest, bytes]:
        target = self.path_for(run_id, batch_id)
        manifest_path = _direct_file(
            target,
            _BATCH_MANIFEST_FILENAME,
            max_bytes=1024 * 1024,
        )
        checksum_path = _direct_file(
            target,
            _CHECKSUM_FILENAME,
            max_bytes=16 * 1024,
        )
        actual_names = {item.name for item in target.iterdir()}
        expected_names = {
            _BATCH_MANIFEST_FILENAME,
            _CHECKSUM_FILENAME,
        }
        if actual_names != expected_names:
            raise DatabaseBatchArtifactError(
                "数据库批次目录包含未声明、缺失或链接文件。"
            )
        manifest_bytes = manifest_path.read_bytes()
        checksums = _parse_checksums(
            checksum_path.read_bytes(),
            expected_names={_BATCH_MANIFEST_FILENAME},
        )
        if (
            sha256_bytes(manifest_bytes)
            != checksums[_BATCH_MANIFEST_FILENAME]
        ):
            raise DatabaseBatchArtifactError("数据库批次清单哈希不一致。")
        try:
            manifest = DatabaseBatchManifest.model_validate_json(
                manifest_bytes
            )
        except ValueError as exc:
            raise DatabaseBatchArtifactError(
                "数据库批次清单无法解析。"
            ) from exc
        if manifest.run_id != run_id or manifest.batch_id != batch_id:
            raise DatabaseBatchArtifactError("数据库批次身份不一致。")
        return manifest, manifest_bytes

    def load_manifest(
        self,
        run_id: str,
        batch_id: str,
    ) -> DatabaseBatchManifest:
        return self._load_manifest_and_bytes(run_id, batch_id)[0]

    def restore_manifests(
        self,
        run_id: str,
        *,
        limit: int = 20,
    ) -> DatabaseBatchRestoreReport:
        """Restore recent valid batches without trusting directory entries."""

        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间。")
        safe_run_id = _safe_identifier(run_id, "run_id")
        run_directory = self.root / safe_run_id
        if not run_directory.exists():
            return DatabaseBatchRestoreReport()
        if (
            not run_directory.is_dir()
            or _is_link_or_reparse(run_directory)
        ):
            return DatabaseBatchRestoreReport(
                warnings=("批次恢复目录不是可信普通目录。",)
            )
        try:
            resolved_run = run_directory.resolve(strict=True)
            candidates = list(run_directory.iterdir())
        except OSError:
            return DatabaseBatchRestoreReport(
                warnings=("批次恢复目录无法读取。",)
            )
        manifests: list[DatabaseBatchManifest] = []
        warnings: list[str] = []
        for candidate in candidates[:500]:
            try:
                batch_id = _safe_identifier(candidate.name, "batch_id")
                if (
                    not candidate.is_dir()
                    or _is_link_or_reparse(candidate)
                    or candidate.resolve(strict=True).parent != resolved_run
                ):
                    raise DatabaseBatchArtifactError(
                        "不是可信普通批次目录"
                    )
                manifests.append(
                    self.load_manifest(safe_run_id, batch_id)
                )
            except (OSError, ValueError, DatabaseBatchArtifactError) as exc:
                warnings.append(
                    f"{candidate.name}: "
                    f"{sanitize_batch_error(str(exc))}"
                )
        manifests.sort(
            key=lambda item: item.created_at_utc,
            reverse=True,
        )
        return DatabaseBatchRestoreReport(
            manifests=tuple(manifests[:limit]),
            warnings=tuple(warnings),
        )

    def _verify_query_archive(
        self,
        run_id: str,
        query_id: str,
    ) -> _VerifiedQueryArchive:
        directory = self.connector_store.path_for(run_id, query_id)
        manifest_path = _direct_file(
            directory,
            "manifest.json",
            max_bytes=1024 * 1024,
        )
        result_path = _direct_file(
            directory,
            "result.json",
            max_bytes=MAX_QUERY_RESULT_BYTES,
        )
        checksum_path = _direct_file(
            directory,
            _CHECKSUM_FILENAME,
            max_bytes=1024 * 1024,
        )
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = ConnectorArchiveManifest.model_validate_json(
                manifest_bytes
            )
        except ValueError as exc:
            raise DatabaseBatchArtifactError(
                f"查询归档清单无法解析：{query_id}"
            ) from exc
        if manifest.run_id != run_id or manifest.query_id != query_id:
            raise DatabaseBatchArtifactError(
                f"查询归档身份不一致：{query_id}"
            )
        response_names = tuple(
            response.filename for response in manifest.responses
        )
        if (
            len(response_names) != len(set(response_names))
            or any(
                not _SAFE_RESPONSE_FILENAME.fullmatch(name)
                for name in response_names
            )
        ):
            raise DatabaseBatchArtifactError(
                f"查询归档包含重复或不安全的响应文件名：{query_id}"
            )
        expected_names = {
            "manifest.json",
            "result.json",
            *response_names,
        }
        checksum_bytes = checksum_path.read_bytes()
        checksums = _parse_checksums(
            checksum_bytes,
            expected_names=expected_names,
        )
        files = {
            "manifest.json": manifest_path,
            "result.json": result_path,
        }
        raw_byte_count = 0
        for response in manifest.responses:
            response_path = _direct_file(
                directory,
                response.filename,
                max_bytes=MAX_RESPONSE_BYTES,
            )
            if response_path.stat().st_size != response.byte_count:
                raise DatabaseBatchArtifactError(
                    f"响应文件大小与清单不一致：{response.filename}"
                )
            raw_byte_count += response.byte_count
            if raw_byte_count > MAX_VERIFIED_RAW_BYTES:
                raise DatabaseBatchArtifactError(
                    f"查询归档原始响应累计超过上限：{query_id}"
                )
            files[response.filename] = response_path
        for filename, path in files.items():
            actual = _sha256_file(path)
            if actual != checksums[filename]:
                raise DatabaseBatchArtifactError(
                    f"查询归档 SHA256SUMS 校验失败：{filename}"
                )
            if filename.startswith("response-"):
                response = next(
                    item
                    for item in manifest.responses
                    if item.filename == filename
                )
                if actual != response.sha256:
                    raise DatabaseBatchArtifactError(
                        f"响应文件清单哈希不一致：{filename}"
                    )
        result_bytes = result_path.read_bytes()
        if sha256_bytes(result_bytes) != manifest.result_json_sha256:
            raise DatabaseBatchArtifactError(
                f"查询归档 result.json 清单哈希不一致：{query_id}"
            )
        try:
            result = json.loads(result_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DatabaseBatchArtifactError(
                f"查询归档 result.json 无法解析：{query_id}"
            ) from exc
        if not isinstance(result, dict) or not isinstance(
            result.get("records"),
            list,
        ):
            raise DatabaseBatchArtifactError(
                f"查询归档 result.json 结构无效：{query_id}"
            )
        records_digest = sha256_bytes(
            canonical_json(result["records"]).encode("utf-8")
        )
        if records_digest != manifest.normalized_records_sha256:
            raise DatabaseBatchArtifactError(
                f"查询归档规范化记录哈希不一致：{query_id}"
            )
        export_metadata = result.get("export_metadata")
        if (
            isinstance(export_metadata, dict)
            and export_metadata.get("records_sha256") != records_digest
        ):
            raise DatabaseBatchArtifactError(
                f"查询归档导出元数据哈希不一致：{query_id}"
            )
        if result.get("status") != manifest.connector_status:
            raise DatabaseBatchArtifactError(
                f"查询归档状态与清单不一致：{query_id}"
            )
        return _VerifiedQueryArchive(
            directory=directory,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            result_bytes=result_bytes,
            checksum_bytes=checksum_bytes,
            result=result,
            response_filenames=response_names,
            raw_byte_count=raw_byte_count,
        )

    def _verified_archived_members(
        self,
        manifest: DatabaseBatchManifest,
    ) -> dict[str, _VerifiedQueryArchive]:
        verified: dict[str, _VerifiedQueryArchive] = {}
        raw_total = 0
        record_total = 0
        for member in manifest.members:
            if member.status is not DatabaseBatchMemberStatus.ARCHIVED:
                continue
            assert member.query_id is not None
            query = self._verify_query_archive(
                manifest.run_id,
                member.query_id,
            )
            raw_total += query.raw_byte_count
            record_total += len(query.result["records"])
            if raw_total > MAX_VERIFIED_RAW_BYTES:
                raise DatabaseBatchArtifactError(
                    "批次原始响应累计超过校验上限。"
                )
            if record_total > MAX_BATCH_RECORDS:
                raise DatabaseBatchArtifactError(
                    "批次规范化记录数量超过上限。"
                )
            verified[member.query_id] = query
        return verified

    def build_normalized_zip(self, run_id: str, batch_id: str) -> bytes:
        """Build a normalized-only ZIP; raw response files are never included."""

        manifest, manifest_bytes = self._load_manifest_and_bytes(
            run_id,
            batch_id,
        )
        verified = self._verified_archived_members(manifest)
        result_members: list[dict[str, Any]] = []
        summary_rows: list[dict[str, Any]] = []
        source_members: dict[
            str,
            list[tuple[DatabaseBatchMember, _VerifiedQueryArchive | None]],
        ] = defaultdict(list)
        for member in manifest.members:
            query = (
                verified.get(member.query_id)
                if member.query_id is not None
                else None
            )
            source_members[member.source].append((member, query))
            connector_status = (
                query.manifest.connector_status if query is not None else ""
            )
            records = query.result["records"] if query is not None else []
            warnings = (
                query.result.get("warnings", []) if query is not None else []
            )
            summary_rows.append(
                {
                    "batch_id": manifest.batch_id,
                    "source": member.source,
                    "operation": member.operation,
                    "query_id": member.query_id,
                    "batch_member_status": member.status.value,
                    "connector_status": connector_status,
                    "record_count": len(records),
                    "warning_count": (
                        len(warnings) if isinstance(warnings, list) else 0
                    ),
                    "error": member.error,
                }
            )
            result_members.append(
                {
                    "source": member.source,
                    "operation": member.operation,
                    "query_id": member.query_id,
                    "batch_member_status": member.status.value,
                    "error": member.error,
                    "result": (
                        _normalized_result(query.result)
                        if query is not None
                        else None
                    ),
                }
            )
            _assert_no_unredacted_sensitive_data(result_members[-1])

        files: dict[str, bytes] = {
            _BATCH_MANIFEST_FILENAME: manifest_bytes,
            "batch-result.json": _json_bytes(
                {
                    "schema_version": DATABASE_BATCH_RESULT_SCHEMA_VERSION,
                    "run_id": manifest.run_id,
                    "batch_id": manifest.batch_id,
                    "created_at_utc": manifest.created_at_utc.isoformat(),
                    "status": manifest.status.value,
                    "members": result_members,
                }
            ),
            "summary.csv": _csv_bytes(
                (
                    "batch_id",
                    "source",
                    "operation",
                    "query_id",
                    "batch_member_status",
                    "connector_status",
                    "record_count",
                    "warning_count",
                    "error",
                ),
                summary_rows,
            ),
        }
        table_metadata = (
            "source",
            "operation",
            "query_id",
            "batch_member_status",
            "connector_status",
            "record_index",
            "error",
        )
        for source, items in sorted(source_members.items()):
            record_columns = sorted(
                {
                    f"record.{key}"
                    for _, query in items
                    if query is not None
                    for record in query.result["records"]
                    if isinstance(record, dict)
                    for key in record
                }
            )
            rows: list[dict[str, Any]] = []
            for member, query in items:
                connector_status = (
                    query.manifest.connector_status if query else ""
                )
                records = query.result["records"] if query else []
                if not records:
                    rows.append(
                        {
                            "source": source,
                            "operation": member.operation,
                            "query_id": member.query_id,
                            "batch_member_status": member.status.value,
                            "connector_status": connector_status,
                            "record_index": "",
                            "error": member.error,
                        }
                    )
                    continue
                for index, record in enumerate(records, start=1):
                    row = {
                        "source": source,
                        "operation": member.operation,
                        "query_id": member.query_id,
                        "batch_member_status": member.status.value,
                        "connector_status": connector_status,
                        "record_index": index,
                        "error": member.error,
                    }
                    if isinstance(record, dict):
                        row.update(
                            {
                                f"record.{key}": value
                                for key, value in record.items()
                            }
                        )
                    else:
                        row["record.value"] = record
                        if "record.value" not in record_columns:
                            record_columns.append("record.value")
                    rows.append(row)
            fields = (*table_metadata, *sorted(record_columns))
            files[f"tables/{source}.csv"] = _csv_bytes(fields, rows)

        files[_CHECKSUM_FILENAME] = _checksum_bytes(files)
        payload = _deterministic_zip(
            files,
            max_payload_bytes=MAX_NORMALIZED_ZIP_BYTES,
        )
        verify_database_batch_zip(
            payload,
            max_uncompressed_bytes=MAX_NORMALIZED_ZIP_BYTES,
        )
        return payload

    def build_raw_audit_zip(
        self,
        run_id: str,
        batch_id: str,
        *,
        allow_restricted_raw: bool = False,
    ) -> bytes:
        """Build a raw audit ZIP only after explicit redistribution warning."""

        if allow_restricted_raw is not True:
            raise RestrictedRawExportError(
                "原始响应或导入文件可能受许可限制；"
                "必须显式设置 allow_restricted_raw=True。"
            )
        manifest, manifest_bytes = self._load_manifest_and_bytes(
            run_id,
            batch_id,
        )
        verified = self._verified_archived_members(manifest)
        files: dict[str, bytes] = {
            _BATCH_MANIFEST_FILENAME: manifest_bytes,
        }
        audit_members: list[dict[str, Any]] = []
        for member in manifest.members:
            if member.query_id is None:
                continue
            query = verified[member.query_id]
            prefix = (
                f"queries/{member.source}/{member.query_id}"
            )
            files[f"{prefix}/manifest.json"] = query.manifest_bytes
            files[f"{prefix}/result.json"] = query.result_bytes
            files[f"{prefix}/{_CHECKSUM_FILENAME}"] = query.checksum_bytes
            responses: list[dict[str, Any]] = []
            response_by_name = {
                response.filename: response
                for response in query.manifest.responses
            }
            for filename in query.response_filenames:
                path = _direct_file(
                    query.directory,
                    filename,
                    max_bytes=MAX_RESPONSE_BYTES,
                )
                payload = path.read_bytes()
                files[f"{prefix}/{filename}"] = payload
                response = response_by_name[filename]
                responses.append(
                    {
                        "filename": filename,
                        "sha256": response.sha256,
                        "byte_count": response.byte_count,
                        "content_type": response.content_type,
                        "license_url": response.provenance.license_url,
                    }
                )
            audit_members.append(
                {
                    "source": member.source,
                    "operation": member.operation,
                    "query_id": member.query_id,
                    "responses": responses,
                }
            )
        files["raw-audit-manifest.json"] = _json_bytes(
            {
                "schema_version": DATABASE_RAW_AUDIT_SCHEMA_VERSION,
                "run_id": manifest.run_id,
                "batch_id": manifest.batch_id,
                "notice": (
                    "仅供已确认许可条件的内部科研审计；"
                    "不得据此假定可重新分发原始响应或导入文件。"
                ),
                "members": audit_members,
            }
        )
        files[_CHECKSUM_FILENAME] = _checksum_bytes(files)
        payload = _deterministic_zip(
            files,
            max_payload_bytes=MAX_RAW_AUDIT_ZIP_BYTES,
        )
        verify_database_batch_zip(payload)
        return payload


__all__ = [
    "DATABASE_BATCH_RESULT_SCHEMA_VERSION",
    "DATABASE_BATCH_SCHEMA_VERSION",
    "DATABASE_RAW_AUDIT_SCHEMA_VERSION",
    "MAX_BATCH_MEMBERS",
    "DatabaseBatchArtifactError",
    "DatabaseBatchArtifactStore",
    "DatabaseBatchManifest",
    "DatabaseBatchMember",
    "DatabaseBatchMemberStatus",
    "DatabaseBatchRestoreReport",
    "DatabaseBatchStatus",
    "RestrictedRawExportError",
    "sanitize_batch_error",
    "verify_database_batch_zip",
]
