"""Pure helpers for the Streamlit molecular-dynamics technical-smoke UI.

The UI deliberately accepts an already parameterized OpenMM ``System`` and
its matching topology.  These helpers only validate and bind user-provided
metadata; they never parameterize chemistry or run molecular dynamics.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.md_worker import MDJobRecord, MDJobStore


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_FORCEFIELD_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_INDEX_TOKEN = re.compile(r"^(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MAPPING_ATOMS = 500_000
_MAX_FORCEFIELD_FILES = 64
_MAX_FORCEFIELD_BYTES = 25 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class MDUISupportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class MDSeriesSummary(MDUISupportModel):
    sample_count: int = Field(ge=1)
    latest: float
    minimum: float
    maximum: float
    mean: float
    unit: str = Field(min_length=1)


class MDJobProgress(MDUISupportModel):
    job_id: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1)
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    requested_platform: str = Field(min_length=1)
    actual_platform: str | None = None
    selected_device: str | None = None
    driver_version: str | None = None
    temperature: MDSeriesSummary | None = None
    potential_energy: MDSeriesSummary | None = None
    reserved_metrics_not_produced: tuple[str, ...] = ()


class MDArtifactDownload(MDUISupportModel):
    role: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    mime: str = Field(min_length=1)
    payload: bytes = Field(min_length=1)


@dataclass(frozen=True)
class MDJobListing:
    records: tuple[MDJobRecord, ...] = ()
    invalid_files: tuple[str, ...] = ()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_task_id(value: str) -> str:
    """Return a safe task ID accepted by both manifest and job storage."""

    normalized = value.strip()
    if not _SAFE_TASK_ID.fullmatch(normalized):
        raise ValueError(
            "任务 ID 只能包含 1–64 位字母、数字、点、下划线或连字符，"
            "且必须以字母或数字开头。"
        )
    return normalized


def default_md_task_id(run_id: str) -> str:
    """Build a stable, editable default without leaking arbitrary run text."""

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-_")
    if not safe:
        safe = "run"
    return validate_task_id(f"md-{safe[:48]}-01")


def infer_source_format(filename: str, *, role: str) -> str:
    """Infer only the formats explicitly supported by the v0.6 manifest."""

    suffix = PurePath(filename).suffix.casefold().lstrip(".")
    allowed = (
        {"pdb"}
        if role == "receptor"
        else {"sdf"}
        if role == "ligand"
        else set()
    )
    if suffix not in allowed:
        label = "受体" if role == "receptor" else "配体"
        raise ValueError(f"{label}文件格式不受支持：{suffix or '无扩展名'}。")
    return suffix


def parse_nonempty_lines(
    value: str,
    *,
    label: str,
    maximum_count: int = 256,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if maximum_count < 1:
        raise ValueError("maximum_count 必须大于 0。")
    items = tuple(line.strip() for line in value.splitlines() if line.strip())
    if not items and not allow_empty:
        raise ValueError(f"{label}不能为空。")
    if len(items) > maximum_count:
        raise ValueError(f"{label}最多允许 {maximum_count} 项。")
    if any("\x00" in item or len(item) > 2048 for item in items):
        raise ValueError(f"{label}包含 NUL 或超长条目。")
    return items


def parse_preparation_command(value: str) -> tuple[str, ...]:
    """Parse an exact argv list: one argument per line, with no shell parsing."""

    return parse_nonempty_lines(
        value,
        label="参数化命令",
        maximum_count=128,
    )


def parse_atom_indices(
    value: str,
    *,
    label: str,
    allow_empty: bool = False,
    maximum_count: int = _MAX_MAPPING_ATOMS,
) -> tuple[int, ...]:
    """Parse zero-based integer indices and inclusive ranges such as ``0-99``."""

    tokens = tuple(
        token
        for token in re.split(r"[\s,;，；]+", value.strip())
        if token
    )
    if not tokens:
        if allow_empty:
            return ()
        raise ValueError(f"{label}不能为空。")
    values: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        match = _INDEX_TOKEN.fullmatch(token)
        if match is None:
            raise ValueError(
                f"{label}包含无效条目 {token!r}；请使用整数或闭区间 start-end。"
            )
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end < start:
            raise ValueError(f"{label}区间 {token!r} 的终点小于起点。")
        if end >= _MAX_MAPPING_ATOMS:
            raise ValueError(
                f"{label}索引必须小于 {_MAX_MAPPING_ATOMS}。"
            )
        if len(values) + (end - start + 1) > maximum_count:
            raise ValueError(f"{label}展开后最多允许 {maximum_count} 个索引。")
        for index in range(start, end + 1):
            if index in seen:
                raise ValueError(f"{label}包含重复索引 {index}。")
            seen.add(index)
            values.append(index)
    return tuple(values)


def normalize_forcefield_files(
    files: Mapping[str, bytes],
) -> dict[str, bytes]:
    """Validate force-field/parameter filenames before immutable storage."""

    if not files:
        raise ValueError("至少上传一个实际使用的力场或参数文件。")
    if len(files) > _MAX_FORCEFIELD_FILES:
        raise ValueError(f"力场/参数文件最多 {_MAX_FORCEFIELD_FILES} 个。")
    normalized: dict[str, bytes] = {}
    total = 0
    for raw_name, raw_payload in files.items():
        name = PurePath(raw_name).name
        if name != raw_name or not _SAFE_FORCEFIELD_NAME.fullmatch(name):
            raise ValueError(f"力场文件名不安全：{raw_name!r}。")
        if name in normalized:
            raise ValueError(f"力场文件名重复：{name}。")
        payload = bytes(raw_payload)
        if not payload:
            raise ValueError(f"力场文件为空：{name}。")
        total += len(payload)
        if total > _MAX_FORCEFIELD_BYTES:
            raise ValueError("力场/参数文件总量超过 25 MB。")
        normalized[name] = payload
    return normalized


def build_mapping_evidence(
    *,
    manifest_sha256: str,
    receptor_source_sha256: str,
    ligand_source_sha256: str,
    topology_pdb: bytes,
    receptor_indices: Sequence[int],
    ligand_indices: Sequence[int],
    mapping_method: str,
    prepared_by: str,
    preparation_command: Sequence[str],
    recorded_at: datetime | None = None,
) -> bytes:
    """Create submitted review evidence later wrapped by worker canonical v2."""

    hashes = (
        manifest_sha256,
        receptor_source_sha256,
        ligand_source_sha256,
    )
    if any(not _SHA256.fullmatch(item) for item in hashes):
        raise ValueError("映射证据中的 manifest/source SHA-256 无效。")
    method = mapping_method.strip()
    reviewer = prepared_by.strip()
    command = tuple(item.strip() for item in preparation_command)
    if not method or not reviewer or not command or any(not item for item in command):
        raise ValueError("映射方法、prepared_by 和参数化命令均不能为空。")
    timestamp = recorded_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("映射证据时间必须包含时区。")
    payload = {
        "schema": "vetevidence-md-atom-mapping-v1",
        "manifest_sha256": manifest_sha256,
        "receptor_source_sha256": receptor_source_sha256,
        "ligand_source_sha256": ligand_source_sha256,
        "topology_pdb_sha256": sha256_bytes(topology_pdb),
        "mapping_method": method,
        "prepared_by": reviewer,
        "preparation_command": list(command),
        "recorded_at": timestamp.isoformat(),
        "zero_based_indices": {
            "receptor": list(receptor_indices),
            "ligand": list(ligand_indices),
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def summarize_series(
    values: Sequence[float],
    *,
    unit: str,
) -> MDSeriesSummary | None:
    if not values:
        return None
    numbers = [float(value) for value in values]
    return MDSeriesSummary(
        sample_count=len(numbers),
        latest=numbers[-1],
        minimum=min(numbers),
        maximum=max(numbers),
        mean=sum(numbers) / len(numbers),
        unit=unit,
    )


def build_job_progress(record: MDJobRecord) -> MDJobProgress:
    """Extract only actually persisted progress and metrics from a job."""

    completed_steps = record.checkpoint.step if record.checkpoint else 0
    actual_platform: str | None = None
    selected_device: str | None = None
    driver_version: str | None = None
    temperature = None
    potential = None
    reserved: tuple[str, ...] = ()
    if record.run_result is not None:
        audit = record.run_result.execution_audit
        actual_platform = audit.platform_name
        selected_device = audit.selected_device
        driver_version = audit.driver_version
        completed_steps = record.manifest.protocol.integration_steps
        analysis = record.run_result.analysis
        reserved = tuple(analysis.reserved_metrics_not_produced)
        if analysis.replicas:
            replica = analysis.replicas[0]
            if replica.temperature_kelvin is not None:
                temperature = summarize_series(
                    replica.temperature_kelvin.values,
                    unit=replica.temperature_kelvin.unit,
                )
            if replica.potential_energy_kj_mol is not None:
                potential = summarize_series(
                    replica.potential_energy_kj_mol.values,
                    unit=replica.potential_energy_kj_mol.unit,
                )
    return MDJobProgress(
        job_id=record.job_id,
        state=record.state.value,
        completed_steps=min(
            completed_steps,
            record.manifest.protocol.integration_steps,
        ),
        total_steps=record.manifest.protocol.integration_steps,
        requested_platform=record.manifest.hardware_request.platform,
        actual_platform=actual_platform,
        selected_device=selected_device,
        driver_version=driver_version,
        temperature=temperature,
        potential_energy=potential,
        reserved_metrics_not_produced=reserved,
    )


def list_md_jobs(store: MDJobStore) -> MDJobListing:
    """List verified jobs newest first while reporting corrupt state files."""

    if not store.jobs_root.is_dir():
        return MDJobListing()
    records: list[MDJobRecord] = []
    invalid: list[str] = []
    for path in sorted(store.jobs_root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            job_id = str(raw["job_id"])
            records.append(store.load(job_id))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            invalid.append(path.name)
    records.sort(key=lambda item: item.updated_at, reverse=True)
    return MDJobListing(records=tuple(records), invalid_files=tuple(invalid))


def _artifact_mime(filename: str) -> str:
    suffix = PurePath(filename).suffix.casefold()
    return {
        ".json": "application/json",
        ".xml": "application/xml",
        ".csv": "text/csv",
        ".pdb": "chemical/x-pdb",
        ".pml": "text/plain",
        ".dcd": "application/octet-stream",
        ".chk": "application/octet-stream",
    }.get(suffix, "application/octet-stream")


def verified_artifact_downloads(
    store: MDJobStore,
    record: MDJobRecord,
) -> tuple[MDArtifactDownload, ...]:
    """Read successful artifacts only after store and SHA-256 verification."""

    current = store.load(record.job_id)
    result = current.run_result
    if result is None:
        raise ValueError("当前任务没有实际 MD 工件；dry-run 计划不生成轨迹。")
    attempt_directory = (
        store.artifact_directory(current.job_id) / result.attempt_id
    ).resolve()
    if attempt_directory.parent != store.artifact_directory(current.job_id):
        raise ValueError("MD attempt 工件目录越界。")
    downloads: list[MDArtifactDownload] = []
    total = 0
    for artifact in result.artifacts:
        path = (attempt_directory / artifact.filename).resolve()
        if path.parent != attempt_directory:
            raise ValueError(f"MD 工件路径越界：{artifact.filename}。")
        payload = path.read_bytes()
        total += len(payload)
        if total > _MAX_ARTIFACT_BYTES:
            raise ValueError("MD 工件下载总量超过 512 MB 安全上限。")
        if (
            len(payload) != artifact.size_bytes
            or sha256_bytes(payload) != artifact.sha256
        ):
            raise ValueError(f"MD 工件校验失败：{artifact.filename}。")
        downloads.append(
            MDArtifactDownload(
                role=artifact.role,
                filename=artifact.filename,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                mime=_artifact_mime(artifact.filename),
                payload=payload,
            )
        )
    return tuple(downloads)


__all__ = [
    "MDArtifactDownload",
    "MDJobListing",
    "MDJobProgress",
    "MDSeriesSummary",
    "build_job_progress",
    "build_mapping_evidence",
    "default_md_task_id",
    "infer_source_format",
    "list_md_jobs",
    "normalize_forcefield_files",
    "parse_atom_indices",
    "parse_nonempty_lines",
    "parse_preparation_command",
    "sha256_bytes",
    "summarize_series",
    "validate_task_id",
    "verified_artifact_downloads",
]
