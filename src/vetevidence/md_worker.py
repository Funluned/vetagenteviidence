"""Optional OpenMM worker and atomic JSON queue for the v0.6 MD pilot.

Scientific parameterization is intentionally outside this worker.  The real
execution path accepts only a pre-parameterized OpenMM ``System`` XML together
with a topology PDB containing matching coordinates.  If those artifacts are
not available, callers may create a dry-run plan but no trajectory or energy
claim is fabricated.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from vetevidence.md_workflow import (
    MDAnalysisResult,
    MDArtifactReference,
    MDEvidenceGrade,
    MDExecutionAudit,
    MDReplicaAnalysis,
    MDReplicateSummary,
    MDRunResult,
    MDTaskManifest,
    MDTimeSeries,
    MDValidationStatus,
    canonical_md_manifest_sha256,
    canonical_md_result_sha256,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_EXECUTION_MODULES = ("openmm", "openmm.app")
_PARAMETERIZATION_MODULES = (
    "pdbfixer",
    "openff.toolkit",
    "openmmforcefields",
)
_DISTRIBUTION_NAMES = {
    "openmm": "openmm",
    "openmm.app": "openmm",
    "pdbfixer": "pdbfixer",
    "openff.toolkit": "openff-toolkit",
    "openmmforcefields": "openmmforcefields",
}
_GPU_PLATFORMS = {"CUDA", "HIP", "OpenCL"}
_MAX_SYSTEM_XML_BYTES = 25 * 1024 * 1024
_MAX_TOPOLOGY_PDB_BYTES = 25 * 1024 * 1024
_MAX_PARTICLES = 100_000
_MAX_FORCES = 128
_MAX_CONSTRAINTS = 2_000_000
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 128 * 1024 * 1024
_MAX_PORTABLE_STATE_BYTES = 128 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_CHARS = 4000

ModuleImporter = Callable[[str], ModuleType]
PopenFactory = Callable[..., Any]
_WINDOWS_CUDA_DLL_HANDLES: list[Any] = []


class MDWorkerModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class MDBackendUnavailable(RuntimeError):
    """Raised when a requested OpenMM capability is unavailable."""


class MDWorkerExecutionError(RuntimeError):
    """Raised when a prepared OpenMM smoke task fails safely."""


class MDWorkerCancelled(RuntimeError):
    """Raised after a cooperative cancellation checkpoint is persisted."""


class MDHardwareSnapshot(MDWorkerModel):
    operating_system: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    processor: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    cpu_count: int = Field(ge=1)
    openmm_platforms: list[str] = Field(default_factory=list)
    gpu_platforms: list[str] = Field(default_factory=list)
    fingerprint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )


class MDBackendPreflight(MDWorkerModel):
    backend: Literal["OpenMM"] = "OpenMM"
    execution_available: bool
    parameterization_available: bool
    reason: str | None = None
    missing_execution_modules: list[str] = Field(default_factory=list)
    missing_parameterization_modules: list[str] = Field(default_factory=list)
    package_versions: dict[str, str] = Field(default_factory=dict)
    hardware: MDHardwareSnapshot

    @model_validator(mode="after")
    def explain_unavailable_backend(self) -> "MDBackendPreflight":
        if not self.execution_available and not self.reason:
            raise ValueError("不可用的 OpenMM 后端必须提供原因。")
        if self.execution_available and self.missing_execution_modules:
            raise ValueError("执行后端可用时不能声明执行模块缺失。")
        return self


class MDReplicaPlan(MDWorkerModel):
    replica_index: int = Field(ge=1)
    seed: int = Field(ge=1, le=2_147_483_647)
    integration_steps: int = Field(ge=1, le=100_000)


class OpenMMDryRunPlan(MDWorkerModel):
    schema_version: Literal[1] = 1
    execution_mode: Literal["dry_run"] = "dry_run"
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    preflight: MDBackendPreflight
    replica_plans: list[MDReplicaPlan] = Field(min_length=1)
    planned_artifacts: list[str] = Field(min_length=1)
    automatic_parameterization_planned: Literal[False] = False
    binding_free_energy_planned: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)
    evidence_grade: MDEvidenceGrade = (
        MDEvidenceGrade.COMPUTATIONAL_PREDICTION
    )


class MDOriginalInputReference(MDWorkerModel):
    role: Literal["receptor", "ligand"]
    source_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    source_size_bytes: int = Field(ge=1)
    stored_path: str = Field(min_length=1)
    element_sequence: list[str] = Field(min_length=1)
    atom_identity_signatures: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def identity_count_matches(self) -> "MDOriginalInputReference":
        if len(self.atom_identity_signatures) != len(self.element_sequence):
            raise ValueError("原始输入原子身份数量必须与元素序列一致。")
        return self


class MDOriginalInputsReference(MDWorkerModel):
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    receptor: MDOriginalInputReference
    ligand: MDOriginalInputReference


class MDAtomMappingProof(MDWorkerModel):
    source_role: Literal["receptor", "ligand"]
    source_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    topology_atom_indices: list[int] = Field(min_length=1)
    mapping_method: str = Field(min_length=2)
    mapping_evidence_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )

    @field_validator("topology_atom_indices")
    @classmethod
    def mapping_indices_are_unique(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("拓扑原子索引不能为负数。")
        if len(value) != len(set(value)):
            raise ValueError("拓扑原子映射索引不能重复。")
        return value


class MDSystemSummary(MDWorkerModel):
    particle_count: int = Field(ge=1, le=_MAX_PARTICLES)
    force_count: int = Field(ge=0, le=_MAX_FORCES)
    constraint_count: int = Field(ge=0, le=_MAX_CONSTRAINTS)
    force_types: list[str] = Field(default_factory=list)
    uses_periodic_boundary_conditions: bool

    @model_validator(mode="after")
    def force_type_count_matches(self) -> "MDSystemSummary":
        if len(self.force_types) != self.force_count:
            raise ValueError("System force 类型数量必须与 force_count 一致。")
        if self.uses_periodic_boundary_conditions:
            raise ValueError(
                "v0.6 technical_smoke 仅开放非周期系统；周期盒向量尚未完成"
                " System/topology 双向绑定。"
            )
        return self


class MDPreparedSystemReference(MDWorkerModel):
    """Immutable references to user-supplied, pre-parameterized OpenMM input."""

    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    system_xml_path: str = Field(min_length=1)
    system_xml_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    system_xml_size_bytes: int = Field(ge=1)
    topology_pdb_path: str = Field(min_length=1)
    topology_pdb_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    topology_pdb_size_bytes: int = Field(ge=1)
    receptor_source_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    ligand_source_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    receptor_mapping: MDAtomMappingProof
    ligand_mapping: MDAtomMappingProof
    mapping_evidence_path: str = Field(min_length=1)
    declared_system_summary: MDSystemSummary
    parameterization_backend: str = Field(min_length=1)
    parameterization_version: str = Field(min_length=1)
    preparation_command: list[str] = Field(min_length=1)
    prepared_by: str = Field(min_length=1)
    forcefield_file_sha256: dict[str, str] = Field(min_length=1)
    forcefield_directory: str = Field(min_length=1)
    prepared_at: datetime
    notes: list[str] = Field(default_factory=list)

    @field_validator("prepared_at")
    @classmethod
    def prepared_time_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("参数化输入时间必须包含时区。")
        return value

    @field_validator("forcefield_file_sha256")
    @classmethod
    def validate_forcefield_hashes(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        pattern = re.compile(_SHA256_PATTERN)
        if any(
            not name.strip() or not pattern.fullmatch(digest)
            for name, digest in value.items()
        ):
            raise ValueError("力场文件必须记录名称和小写 SHA-256。")
        return value

    @model_validator(mode="after")
    def mapping_sources_match(self) -> "MDPreparedSystemReference":
        if (
            self.receptor_mapping.source_role != "receptor"
            or self.receptor_mapping.source_sha256
            != self.receptor_source_sha256
            or self.ligand_mapping.source_role != "ligand"
            or self.ligand_mapping.source_sha256
            != self.ligand_source_sha256
        ):
            raise ValueError("参数化清单的来源映射身份不一致。")
        if set(self.receptor_mapping.topology_atom_indices) & set(
            self.ligand_mapping.topology_atom_indices
        ):
            raise ValueError("受体和配体拓扑原子映射不能重叠。")
        if (
            self.receptor_mapping.mapping_method
            != self.ligand_mapping.mapping_method
            or self.receptor_mapping.mapping_evidence_sha256
            != self.ligand_mapping.mapping_evidence_sha256
        ):
            raise ValueError("受体与配体必须绑定同一份 canonical 原子映射证据。")
        return self


class MDCheckpointReference(MDWorkerModel):
    checkpoint_path: str = Field(min_length=1)
    checkpoint_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    checkpoint_size_bytes: int = Field(ge=1)
    portable_state_path: str | None = None
    portable_state_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    system_xml_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    topology_pdb_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    replica_index: Literal[1] = 1
    seed: int = Field(ge=1, le=2_147_483_647)
    step: int = Field(ge=0)
    backend_version: str = Field(min_length=1)
    hardware_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def checkpoint_time_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("检查点时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def portable_state_fields_match(self) -> "MDCheckpointReference":
        if (self.portable_state_path is None) != (
            self.portable_state_sha256 is None
        ):
            raise ValueError("portable state 路径和 SHA-256 必须同时提供。")
        return self


class MDJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MDJobRecord(MDWorkerModel):
    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1, max_length=64)
    manifest: MDTaskManifest
    state: MDJobState
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=0)
    attempts: int = Field(ge=0)
    resume_count: int = Field(ge=0)
    worker_pid: int | None = Field(default=None, ge=1)
    original_inputs: MDOriginalInputsReference
    prepared_system: MDPreparedSystemReference | None = None
    checkpoint: MDCheckpointReference | None = None
    dry_run_plan: OpenMMDryRunPlan | None = None
    run_result: MDRunResult | None = None
    error: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def job_times_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MD 队列时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def validate_state_payload(self) -> "MDJobRecord":
        if self.updated_at < self.created_at:
            raise ValueError("MD 队列更新时间不能早于创建时间。")
        if self.state in {
            MDJobState.RUNNING,
            MDJobState.CANCEL_REQUESTED,
        } and self.worker_pid is None:
            raise ValueError("运行中任务必须记录 worker PID。")
        if self.state not in {
            MDJobState.RUNNING,
            MDJobState.CANCEL_REQUESTED,
        } and self.worker_pid is not None:
            raise ValueError("非运行任务不能保留 worker PID。")
        if self.state is MDJobState.SUCCEEDED and (
            (self.dry_run_plan is None) == (self.run_result is None)
        ):
            raise ValueError(
                "成功任务必须且只能包含 dry-run 计划或真实 MD 结果之一。"
            )
        if self.state is MDJobState.FAILED and not self.error:
            raise ValueError("失败任务必须记录错误。")
        if self.state is not MDJobState.FAILED and self.error is not None:
            raise ValueError("只有失败任务可以记录错误。")
        return self


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size_bytes(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file()
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_file_lock(path: Path, *, timeout_seconds: float):
    """Small cross-process lock used around every job state transition."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待 MD job 文件锁超时。")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _normalized_element(value: str) -> str:
    letters = "".join(char for char in value if char.isalpha())
    if not letters:
        raise ValueError("结构中存在无法识别元素的原子。")
    return letters[:2].upper()


def _canonical_atom_identity(fields: Mapping[str, object]) -> str:
    return json.dumps(
        dict(fields),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pdb_atom_identities(payload: bytes) -> list[dict[str, object]]:
    identities: list[dict[str, object]] = []
    for line in payload.decode("utf-8", errors="strict").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        explicit = line[76:78].strip() if len(line) >= 78 else ""
        atom_name = line[12:16].strip() if len(line) >= 16 else ""
        identities.append(
            {
                "record": line[0:6].strip().upper(),
                "atom_name": atom_name,
                "altloc": line[16:17].strip(),
                "residue_name": line[17:20].strip().upper(),
                "chain_id": line[21:22].strip(),
                "residue_number": line[22:26].strip(),
                "insertion_code": line[26:27].strip(),
                "element": _normalized_element(explicit or atom_name),
            }
        )
    return identities


def _pdb_elements(payload: bytes) -> list[str]:
    return [
        str(identity["element"])
        for identity in _pdb_atom_identities(payload)
    ]


def _mmcif_elements(payload: bytes) -> list[str]:
    lines = payload.decode("utf-8", errors="strict").splitlines()
    for start, line in enumerate(lines):
        if line.strip() != "loop_":
            continue
        headers: list[str] = []
        cursor = start + 1
        while cursor < len(lines) and lines[cursor].lstrip().startswith("_"):
            headers.append(lines[cursor].strip())
            cursor += 1
        if "_atom_site.type_symbol" not in headers:
            continue
        type_index = headers.index("_atom_site.type_symbol")
        elements: list[str] = []
        while cursor < len(lines):
            row = lines[cursor].strip()
            if not row or row == "#" or row.startswith(("loop_", "_")):
                break
            values = shlex.split(row)
            if len(values) < len(headers):
                raise ValueError("mmCIF atom_site 行不完整，不能建立原子映射。")
            elements.append(_normalized_element(values[type_index]))
            cursor += 1
        if elements:
            return elements
    raise ValueError("mmCIF 缺少可审计的 atom_site.type_symbol 映射。")


def _sdf_elements(payload: bytes) -> list[str]:
    lines = payload.decode("utf-8", errors="strict").splitlines()
    if len(lines) < 4:
        raise ValueError("SDF 过短，不能建立原子映射。")
    try:
        atom_count = int(lines[3][0:3])
    except ValueError as exc:
        raise ValueError("SDF 原子计数无效。") from exc
    atom_lines = lines[4 : 4 + atom_count]
    if len(atom_lines) != atom_count:
        raise ValueError("SDF 原子表不完整。")
    return [_normalized_element(line[31:34]) for line in atom_lines]


def _sdf_atom_identities(payload: bytes) -> list[dict[str, object]]:
    return [
        {
            "source_atom_index": index,
            "element": element,
        }
        for index, element in enumerate(_sdf_elements(payload))
    ]


def _mol2_elements(payload: bytes) -> list[str]:
    lines = payload.decode("utf-8", errors="strict").splitlines()
    try:
        start = lines.index("@<TRIPOS>ATOM") + 1
    except ValueError as exc:
        raise ValueError("MOL2 缺少 ATOM 区段。") from exc
    elements: list[str] = []
    for line in lines[start:]:
        if line.startswith("@<TRIPOS>"):
            break
        fields = line.split()
        if fields:
            if len(fields) < 6:
                raise ValueError("MOL2 原子行不完整。")
            elements.append(_normalized_element(fields[5].split(".")[0]))
    return elements


def _source_elements(payload: bytes, source_format: str) -> list[str]:
    parser = {
        "pdb": _pdb_elements,
        "mmcif": _mmcif_elements,
        "cif": _mmcif_elements,
        "sdf": _sdf_elements,
        "mol2": _mol2_elements,
    }.get(source_format)
    if parser is None:
        raise ValueError(
            "technical smoke 的来源映射仅支持 PDB/mmCIF 与 SDF/MOL2；"
            "SMILES 需要先生成并审核带原子身份的结构文件。"
        )
    elements = parser(payload)
    if not elements:
        raise ValueError("原始结构没有可映射原子。")
    return elements


def _source_atom_identity_signatures(
    payload: bytes,
    source_format: str,
) -> list[str]:
    parser = {
        "pdb": _pdb_atom_identities,
        "sdf": _sdf_atom_identities,
    }.get(source_format)
    if parser is None:
        raise ValueError(
            "v0.6 原子身份冻结只支持单模型 PDB 与单记录 SDF。"
        )
    identities = parser(payload)
    if not identities:
        raise ValueError("原始结构没有可冻结的原子身份。")
    return [_canonical_atom_identity(item) for item in identities]


def _canonical_mapping_proof(
    *,
    manifest_sha256: str,
    original_inputs: MDOriginalInputsReference,
    topology_payload: bytes,
    receptor_indices: Sequence[int],
    ligand_indices: Sequence[int],
    mapping_method: str,
    prepared_by: str,
    preparation_command: Sequence[str],
    submitted_evidence_sha256: str,
    submitted_evidence_size_bytes: int,
) -> bytes:
    """Build and validate the executable source-to-topology atom proof."""

    topology_identities = _pdb_atom_identities(topology_payload)
    if not topology_identities:
        raise ValueError("topology PDB 不包含可冻结的原子身份。")

    def rows_for(
        *,
        role: str,
        original: MDOriginalInputReference,
        indices: Sequence[int],
        require_exact_identity: bool,
    ) -> list[dict[str, object]]:
        if len(indices) != len(original.atom_identity_signatures):
            raise ValueError(f"{role}原子身份数量与映射数量不一致。")
        rows: list[dict[str, object]] = []
        for source_index, (signature, topology_index) in enumerate(
            zip(original.atom_identity_signatures, indices, strict=True)
        ):
            if topology_index < 0 or topology_index >= len(topology_identities):
                raise ValueError(f"{role}映射索引超出 topology 原子范围。")
            try:
                source_identity = json.loads(signature)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{role}原子身份签名不是 canonical JSON。") from exc
            topology_identity = topology_identities[topology_index]
            if require_exact_identity:
                if source_identity != topology_identity:
                    raise ValueError(
                        f"{role} source_atom_index={source_index} 与 "
                        f"topology_atom_index={topology_index} 的链、残基、"
                        "原子名、altloc 或元素身份不一致。"
                    )
            elif source_identity.get("element") != topology_identity.get(
                "element"
            ):
                raise ValueError(
                    f"{role} source_atom_index={source_index} 与 "
                    f"topology_atom_index={topology_index} 的元素不一致。"
                )
            rows.append(
                {
                    "source_atom_index": source_index,
                    "topology_atom_index": topology_index,
                    "source_identity": source_identity,
                    "topology_identity": topology_identity,
                }
            )
        return rows

    document = {
        "schema": "vetevidence-md-atom-mapping-v2",
        "manifest_sha256": manifest_sha256,
        "source_sha256": {
            "receptor": original_inputs.receptor.source_sha256,
            "ligand": original_inputs.ligand.source_sha256,
        },
        "topology_pdb_sha256": _sha256_bytes(topology_payload),
        "mapping_method": mapping_method,
        "prepared_by": prepared_by,
        "preparation_command": list(preparation_command),
        "submitted_evidence": {
            "sha256": submitted_evidence_sha256,
            "size_bytes": submitted_evidence_size_bytes,
        },
        "atoms": {
            "receptor": rows_for(
                role="受体",
                original=original_inputs.receptor,
                indices=receptor_indices,
                require_exact_identity=True,
            ),
            "ligand": rows_for(
                role="配体",
                original=original_inputs.ligand,
                indices=ligand_indices,
                require_exact_identity=False,
            ),
        },
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _module_version(module: ModuleType, module_name: str) -> str:
    direct = getattr(module, "__version__", None)
    if direct:
        return str(direct)
    version_object = getattr(module, "version", None)
    nested = getattr(version_object, "version", None)
    if nested:
        return str(nested)
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAMES[module_name])
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _openmm_platform_names(openmm_module: ModuleType | None) -> list[str]:
    if openmm_module is None:
        return []
    platform_class = getattr(openmm_module, "Platform", None)
    if platform_class is None:
        return []
    try:
        return [
            str(platform_class.getPlatform(index).getName())
            for index in range(platform_class.getNumPlatforms())
        ]
    except Exception:
        return []


def _preload_windows_openmm_cuda_dependencies() -> None:
    """Load the CUDA wheel DLLs before OpenMM scans its Windows plugins."""

    if os.name != "nt" or _WINDOWS_CUDA_DLL_HANDLES:
        return
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    nvidia_root = (purelib / "nvidia").resolve()
    if not nvidia_root.is_dir() or nvidia_root.parent != purelib:
        return
    relative_dlls = (
        Path("cuda_runtime") / "bin" / "cudart64_12.dll",
        Path("cufft") / "bin" / "cufft64_11.dll",
        Path("nvjitlink") / "bin" / "nvJitLink_120_0.dll",
        Path("cuda_nvrtc") / "bin" / "nvrtc-builtins64_129.dll",
        Path("cuda_nvrtc") / "bin" / "nvrtc64_120_0.dll",
    )
    loaded: list[Any] = []
    try:
        for relative in relative_dlls:
            candidate = (nvidia_root / relative).resolve(strict=True)
            candidate.relative_to(nvidia_root)
            loaded.append(ctypes.WinDLL(str(candidate)))
    except (OSError, ValueError):
        loaded.clear()
        return
    _WINDOWS_CUDA_DLL_HANDLES.extend(loaded)


def _hardware_snapshot(openmm_platforms: Sequence[str]) -> MDHardwareSnapshot:
    raw = {
        "operating_system": (
            f"{platform.system()} {platform.release()}".strip() or "unknown"
        ),
        "machine": platform.machine() or "unknown",
        "processor": platform.processor() or "unknown",
        "python_version": platform.python_version(),
        "cpu_count": max(1, os.cpu_count() or 1),
        "openmm_platforms": list(openmm_platforms),
        "gpu_platforms": [
            name for name in openmm_platforms if name in _GPU_PLATFORMS
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return MDHardwareSnapshot(
        **raw,
        fingerprint_sha256=fingerprint,
    )


def preflight_openmm(
    *,
    importer: ModuleImporter | None = None,
) -> MDBackendPreflight:
    """Inspect optional execution and parameterization dependencies."""

    if importer is None:
        _preload_windows_openmm_cuda_dependencies()
    active_importer = importer or importlib.import_module
    loaded: dict[str, ModuleType] = {}
    missing_execution: list[str] = []
    missing_parameterization: list[str] = []
    for module_name in [*_EXECUTION_MODULES, *_PARAMETERIZATION_MODULES]:
        try:
            loaded[module_name] = active_importer(module_name)
        except (ImportError, ModuleNotFoundError):
            if module_name in _EXECUTION_MODULES:
                missing_execution.append(module_name)
            else:
                missing_parameterization.append(module_name)
    package_versions = {
        name: _module_version(module, name)
        for name, module in loaded.items()
    }
    platforms = _openmm_platform_names(loaded.get("openmm"))
    execution_available = not missing_execution
    reason = None
    if not execution_available:
        reason = (
            "OpenMM 执行后端不可用，缺少模块："
            + "、".join(missing_execution)
            + "。仍可保存 MD 清单，但不能生成轨迹或能量。"
        )
    return MDBackendPreflight(
        execution_available=execution_available,
        parameterization_available=not missing_parameterization,
        reason=reason,
        missing_execution_modules=missing_execution,
        missing_parameterization_modules=missing_parameterization,
        package_versions=package_versions,
        hardware=_hardware_snapshot(platforms),
    )


def build_openmm_dry_run(
    manifest: MDTaskManifest,
    *,
    preflight: MDBackendPreflight | None = None,
) -> OpenMMDryRunPlan:
    """Build a truthful plan without creating trajectories or scientific data."""

    verified = MDTaskManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    active_preflight = preflight or preflight_openmm()
    if not active_preflight.execution_available:
        raise MDBackendUnavailable(
            active_preflight.reason or "OpenMM 执行后端不可用。"
        )
    requested = verified.hardware_request.platform
    if (
        requested != "auto"
        and requested not in active_preflight.hardware.openmm_platforms
    ):
        raise MDBackendUnavailable(
            f"请求的 OpenMM 平台 {requested} 不可用；"
            f"当前平台：{active_preflight.hardware.openmm_platforms or ['无']}。"
        )
    if (
        verified.hardware_request.gpu_required
        and not active_preflight.hardware.gpu_platforms
    ):
        raise MDBackendUnavailable("任务要求 GPU，但 OpenMM 未发现 GPU 平台。")
    protocol = verified.protocol
    warnings = [
        "dry-run 不会生成轨迹、能量或科研结论。",
        "只有已参数化的 OpenMM System XML 和匹配 topology PDB "
        "才能进入真实 smoke 执行。",
        "不会计算或计划结合自由能。",
    ]
    if not active_preflight.parameterization_available:
        warnings.append(
            "自动参数化栈不可用，缺少："
            + "、".join(active_preflight.missing_parameterization_modules)
            + "；本试点不会尝试猜测参数。"
        )
    return OpenMMDryRunPlan(
        manifest_sha256=verified.manifest_sha256 or "",
        preflight=active_preflight,
        replica_plans=[
            MDReplicaPlan(
                replica_index=index,
                seed=seed,
                integration_steps=protocol.integration_steps,
            )
            for index, seed in enumerate(protocol.seeds, start=1)
        ],
        planned_artifacts=[
            "manifest.json",
            "topology.pdb",
            "system.xml",
            "portable-state.xml",
            "checkpoint.chk",
            "trajectory.dcd",
            "state.csv",
            "analysis.json",
            "representative.pdb",
            "view_md.pml",
        ],
        warnings=warnings,
    )


class MDJobStore:
    """One immutable manifest per atomically updated local JSON job."""

    def __init__(self, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = (
            root or project_root / ".workbench" / "md"
        ).resolve()
        self.jobs_root = self.root / "jobs"
        self.inputs_root = self.root / "inputs"
        self.checkpoints_root = self.root / "checkpoints"
        self.artifacts_root = self.root / "artifacts"
        self.locks_root = self.root / "locks"

    @staticmethod
    def _safe_id(value: str) -> str:
        reserved_stem = value.split(".", maxsplit=1)[0].upper()
        if (
            not _SAFE_JOB_ID.fullmatch(value)
            or value.endswith(".")
            or reserved_stem in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(
                "MD job ID 格式不安全；请使用不超过 64 位的字母、"
                "数字、点、下划线和连字符。"
            )
        return value

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def job_path(self, job_id: str) -> Path:
        safe = self._safe_id(job_id).casefold()
        target = (self.jobs_root / f"{safe}.json").resolve()
        if target.parent != self.jobs_root.resolve():
            raise ValueError("MD job 路径超出允许目录。")
        return target

    def artifact_directory(self, job_id: str) -> Path:
        safe = self._safe_id(job_id).casefold()
        target = (self.artifacts_root / safe).resolve()
        if target.parent != self.artifacts_root.resolve():
            raise ValueError("MD 产物路径超出允许目录。")
        return target

    def attempt_directory(self, job_id: str, attempt: int) -> Path:
        if attempt < 1 or attempt > 9999:
            raise ValueError("MD attempt 必须位于 1..9999。")
        target = (
            self.artifact_directory(job_id) / f"attempt-{attempt:04d}"
        ).resolve()
        if target.parent != self.artifact_directory(job_id):
            raise ValueError("MD attempt 路径超出允许目录。")
        return target

    def _lock_path(self, job_id: str) -> Path:
        safe = self._safe_id(job_id).casefold()
        return (self.locks_root / f"{safe}.lock").resolve()

    def _save(self, record: MDJobRecord, *, create_only: bool = False) -> None:
        target = self.job_path(record.job_id)
        with _exclusive_file_lock(
            self._lock_path(record.job_id),
            timeout_seconds=_LOCK_TIMEOUT_SECONDS,
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            if create_only and target.exists():
                raise ValueError("MD job 已存在，拒绝覆盖。")
            if not create_only:
                if not target.is_file():
                    raise ValueError("MD job 状态文件缺失，拒绝盲写。")
                current = MDJobRecord.model_validate_json(
                    target.read_text(encoding="utf-8")
                )
                if record.revision != current.revision + 1:
                    raise ValueError(
                        "MD job revision 已变化，拒绝并发覆盖。"
                    )
            payload = json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            _atomic_write_bytes(target, payload)

    def load(self, job_id: str) -> MDJobRecord:
        target = self.job_path(job_id)
        try:
            record = MDJobRecord.model_validate_json(
                target.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ValueError("未找到 MD job。") from exc
        if record.job_id != job_id:
            raise ValueError("MD job 文件中的 ID 与请求不一致。")
        if (
            record.manifest.manifest_sha256
            != canonical_md_manifest_sha256(record.manifest)
        ):
            raise ValueError("MD job 中的 manifest SHA-256 校验失败。")
        expected_input = (
            self.inputs_root / self._safe_id(job_id).casefold()
        ).resolve()
        original_directory = (expected_input / "original").resolve()
        for reference, source in (
            (record.original_inputs.receptor, record.manifest.receptor_source),
            (record.original_inputs.ligand, record.manifest.ligand_source),
        ):
            path = Path(reference.stored_path).resolve()
            if path.parent != original_directory:
                raise ValueError("MD 原始输入路径超出 job 专属目录。")
            if (
                not path.is_file()
                or path.stat().st_size != reference.source_size_bytes
                or _sha256_file(path) != reference.source_sha256
                or reference.source_sha256 != source.sha256
                or reference.source_size_bytes != source.size_bytes
            ):
                raise ValueError("MD 原始输入缺失或 SHA-256 校验失败。")
        if (
            record.original_inputs.manifest_sha256
            != record.manifest.manifest_sha256
        ):
            raise ValueError("MD 原始输入未绑定当前 manifest。")
        if record.prepared_system is not None:
            expected_prepared = (expected_input / "prepared").resolve()
            prepared_paths = {
                Path(record.prepared_system.system_xml_path).resolve(),
                Path(record.prepared_system.topology_pdb_path).resolve(),
                Path(record.prepared_system.mapping_evidence_path).resolve(),
            }
            if any(path.parent != expected_prepared for path in prepared_paths):
                raise ValueError("MD 参数化输入路径超出 job 专属目录。")
            forcefield_directory = Path(
                record.prepared_system.forcefield_directory
            ).resolve()
            if forcefield_directory.parent != expected_prepared:
                raise ValueError("MD 力场文件路径超出 job 专属目录。")
            evidence_path = Path(
                record.prepared_system.mapping_evidence_path
            )
            evidence_sha = _sha256_file(evidence_path)
            if (
                evidence_sha
                != record.prepared_system.receptor_mapping.mapping_evidence_sha256
                or evidence_sha
                != record.prepared_system.ligand_mapping.mapping_evidence_sha256
            ):
                raise ValueError("MD 原子映射证据 SHA-256 校验失败。")
            for name, digest in record.prepared_system.forcefield_file_sha256.items():
                forcefield_path = (forcefield_directory / name).resolve()
                if forcefield_path.parent != forcefield_directory:
                    raise ValueError("MD 力场文件名导致目录穿越。")
                if (
                    not forcefield_path.is_file()
                    or _sha256_file(forcefield_path) != digest
                ):
                    raise ValueError("MD 力场文件缺失或 SHA-256 校验失败。")
            try:
                _, topology_path = _verify_prepared_system(
                    record.prepared_system
                )
                _verify_canonical_mapping_proof(
                    record.prepared_system,
                    original_inputs=record.original_inputs,
                    topology_payload=topology_path.read_bytes(),
                )
            except MDWorkerExecutionError as exc:
                raise ValueError(str(exc)) from exc
        if record.checkpoint is not None:
            expected_checkpoint = (
                self.checkpoints_root / self._safe_id(job_id).casefold()
            ).resolve()
            checkpoint_paths = {
                Path(record.checkpoint.checkpoint_path).resolve(),
                *(
                    [Path(record.checkpoint.portable_state_path).resolve()]
                    if record.checkpoint.portable_state_path is not None
                    else []
                ),
            }
            if any(
                path.parent != expected_checkpoint
                for path in checkpoint_paths
            ):
                raise ValueError("MD checkpoint 路径超出 job 专属目录。")
        if record.run_result is not None:
            if (
                record.run_result.result_manifest_sha256
                != canonical_md_result_sha256(record.run_result)
            ):
                raise ValueError("MD 结果清单 SHA-256 校验失败。")
            attempt_directory = (
                self.artifact_directory(job_id)
                / record.run_result.attempt_id
            ).resolve()
            if attempt_directory.parent != self.artifact_directory(job_id):
                raise ValueError("MD 结果 attempt 路径超出允许目录。")
            for artifact in record.run_result.artifacts:
                path = (attempt_directory / artifact.filename).resolve()
                if path.parent != attempt_directory:
                    raise ValueError("MD 结果产物路径超出 attempt 目录。")
                if (
                    not path.is_file()
                    or path.stat().st_size != artifact.size_bytes
                    or _sha256_file(path) != artifact.sha256
                ):
                    raise ValueError(
                        f"MD 产物 {artifact.role} 缺失或 SHA-256 校验失败。"
                    )
        return record

    def enqueue(
        self,
        manifest: MDTaskManifest,
        *,
        receptor_payload: bytes | str,
        ligand_payload: bytes | str,
        job_id: str | None = None,
    ) -> MDJobRecord:
        verified = MDTaskManifest.model_validate(
            manifest.model_dump(mode="python")
        )
        active_job_id = job_id or verified.task_id
        self._safe_id(active_job_id)
        receptor_bytes = (
            receptor_payload
            if isinstance(receptor_payload, bytes)
            else receptor_payload.encode("utf-8")
        )
        ligand_bytes = (
            ligand_payload
            if isinstance(ligand_payload, bytes)
            else ligand_payload.encode("utf-8")
        )
        for payload, source, label in (
            (receptor_bytes, verified.receptor_source, "受体"),
            (ligand_bytes, verified.ligand_source, "配体"),
        ):
            if (
                _sha256_bytes(payload) != source.sha256
                or len(payload) != source.size_bytes
            ):
                raise ValueError(f"MD {label}原始字节与 manifest 不一致。")
        receptor_elements = _source_elements(
            receptor_bytes,
            verified.receptor_source.format,
        )
        receptor_signatures = _source_atom_identity_signatures(
            receptor_bytes,
            verified.receptor_source.format,
        )
        ligand_elements = _source_elements(
            ligand_bytes,
            verified.ligand_source.format,
        )
        ligand_signatures = _source_atom_identity_signatures(
            ligand_bytes,
            verified.ligand_source.format,
        )
        input_directory = (
            self.inputs_root / self._safe_id(active_job_id).casefold()
        ).resolve()
        if input_directory.parent != self.inputs_root.resolve():
            raise ValueError("MD 输入路径超出允许目录。")
        if input_directory.exists():
            raise ValueError("MD job 输入目录已存在，拒绝覆盖。")
        temporary = input_directory.with_name(
            f".{input_directory.name}.{uuid4().hex}.tmp"
        )
        original = temporary / "original"
        receptor_name = f"receptor.{verified.receptor_source.format}"
        ligand_name = f"ligand.{verified.ligand_source.format}"
        try:
            original.mkdir(parents=True)
            (original / receptor_name).write_bytes(receptor_bytes)
            (original / ligand_name).write_bytes(ligand_bytes)
            temporary.replace(input_directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        original_inputs = MDOriginalInputsReference(
            manifest_sha256=verified.manifest_sha256 or "",
            receptor=MDOriginalInputReference(
                role="receptor",
                source_sha256=verified.receptor_source.sha256 or "",
                source_size_bytes=len(receptor_bytes),
                stored_path=str(
                    input_directory / "original" / receptor_name
                ),
                element_sequence=receptor_elements,
                atom_identity_signatures=receptor_signatures,
            ),
            ligand=MDOriginalInputReference(
                role="ligand",
                source_sha256=verified.ligand_source.sha256 or "",
                source_size_bytes=len(ligand_bytes),
                stored_path=str(input_directory / "original" / ligand_name),
                element_sequence=ligand_elements,
                atom_identity_signatures=ligand_signatures,
            ),
        )
        now = _utc_now()
        record = MDJobRecord(
            job_id=active_job_id,
            manifest=verified,
            state=MDJobState.QUEUED,
            created_at=now,
            updated_at=now,
            revision=0,
            attempts=0,
            resume_count=0,
            original_inputs=original_inputs,
        )
        try:
            self._save(record, create_only=True)
        except Exception:
            if not self.job_path(active_job_id).exists():
                shutil.rmtree(input_directory, ignore_errors=True)
            raise
        return record

    def reconcile_stale_jobs(self) -> list[MDJobRecord]:
        """Resolve jobs whose recorded worker PID no longer exists."""

        reconciled: list[MDJobRecord] = []
        if not self.jobs_root.is_dir():
            return reconciled
        for path in sorted(self.jobs_root.glob("*.json")):
            try:
                raw = MDJobRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if raw.state not in {
                    MDJobState.RUNNING,
                    MDJobState.CANCEL_REQUESTED,
                }:
                    continue
                if raw.worker_pid is not None and self._pid_is_alive(
                    raw.worker_pid
                ):
                    continue
                current = self.load(raw.job_id)
                if current.state is MDJobState.CANCEL_REQUESTED:
                    reconciled.append(self.mark_cancelled(raw.job_id))
                elif current.state is MDJobState.RUNNING:
                    reconciled.append(
                        self.mark_failed(
                            raw.job_id,
                            "启动恢复发现 worker PID 已不存在；"
                            "可在核验 checkpoint 后恢复。",
                        )
                    )
            except (OSError, ValueError):
                continue
        return reconciled

    def _updated(
        self,
        record: MDJobRecord,
        **updates: object,
    ) -> MDJobRecord:
        payload = record.model_dump(mode="python")
        payload.update(updates)
        payload["updated_at"] = _utc_now()
        payload["revision"] = record.revision + 1
        return MDJobRecord.model_validate(payload)

    def claim(
        self,
        job_id: str,
        *,
        worker_pid: int | None = None,
    ) -> MDJobRecord:
        record = self.load(job_id)
        if record.state is not MDJobState.QUEUED:
            raise ValueError(f"只有 queued job 可认领，当前为 {record.state}。")
        claimed = self._updated(
            record,
            state=MDJobState.RUNNING,
            worker_pid=worker_pid or os.getpid(),
            attempts=record.attempts + 1,
            dry_run_plan=None,
            run_result=None,
            error=None,
        )
        self._save(claimed)
        return claimed

    def request_cancel(self, job_id: str) -> MDJobRecord:
        record = self.load(job_id)
        if record.state is MDJobState.QUEUED:
            updated = self._updated(
                record,
                state=MDJobState.CANCELLED,
                worker_pid=None,
            )
        elif record.state is MDJobState.RUNNING:
            updated = self._updated(
                record,
                state=MDJobState.CANCEL_REQUESTED,
            )
        elif record.state in {
            MDJobState.CANCEL_REQUESTED,
            MDJobState.CANCELLED,
        }:
            return record
        else:
            raise ValueError(f"当前状态 {record.state} 不能取消。")
        self._save(updated)
        return updated

    def mark_cancelled(self, job_id: str) -> MDJobRecord:
        record = self.load(job_id)
        if record.state not in {
            MDJobState.RUNNING,
            MDJobState.CANCEL_REQUESTED,
        }:
            raise ValueError("只有运行中或待取消 job 可标记 cancelled。")
        updated = self._updated(
            record,
            state=MDJobState.CANCELLED,
            worker_pid=None,
            error=None,
        )
        self._save(updated)
        return updated

    def mark_failed(self, job_id: str, error: str) -> MDJobRecord:
        record = self.load(job_id)
        if record.state not in {
            MDJobState.RUNNING,
            MDJobState.CANCEL_REQUESTED,
        }:
            raise ValueError("只有运行中的 job 可标记失败。")
        bounded = error.strip()[-_MAX_ERROR_CHARS:] or "未知 MD worker 错误"
        updated = self._updated(
            record,
            state=MDJobState.FAILED,
            worker_pid=None,
            error=bounded,
        )
        self._save(updated)
        return updated

    def mark_dry_run_succeeded(
        self,
        job_id: str,
        plan: OpenMMDryRunPlan,
    ) -> MDJobRecord:
        record = self.load(job_id)
        if record.state is not MDJobState.RUNNING:
            raise ValueError("只有 running job 可完成 dry-run。")
        if plan.manifest_sha256 != record.manifest.manifest_sha256:
            raise ValueError("dry-run 计划与 job manifest 不匹配。")
        updated = self._updated(
            record,
            state=MDJobState.SUCCEEDED,
            worker_pid=None,
            dry_run_plan=plan,
            run_result=None,
            error=None,
        )
        self._save(updated)
        return updated

    def mark_run_succeeded(
        self,
        job_id: str,
        result: MDRunResult,
    ) -> MDJobRecord:
        record = self.load(job_id)
        if record.state is not MDJobState.RUNNING:
            raise ValueError("只有 running job 可保存真实 MD 结果。")
        if (
            result.manifest.manifest_sha256
            != record.manifest.manifest_sha256
        ):
            raise ValueError("MD 结果与 job manifest 不匹配。")
        updated = self._updated(
            record,
            state=MDJobState.SUCCEEDED,
            worker_pid=None,
            dry_run_plan=None,
            run_result=result,
            error=None,
        )
        self._save(updated)
        return updated

    def save_prepared_system(
        self,
        job_id: str,
        *,
        system_xml: bytes | str,
        topology_pdb: bytes | str,
        parameterization_backend: str,
        parameterization_version: str,
        forcefield_files: Mapping[str, bytes],
        preparation_command: Sequence[str],
        prepared_by: str,
        declared_system_summary: MDSystemSummary,
        receptor_topology_atom_indices: Sequence[int],
        ligand_topology_atom_indices: Sequence[int],
        mapping_method: str,
        mapping_evidence: bytes | str,
        notes: Sequence[str] = (),
    ) -> MDJobRecord:
        record = self.load(job_id)
        if record.state not in {
            MDJobState.QUEUED,
            MDJobState.FAILED,
            MDJobState.CANCELLED,
        }:
            raise ValueError("运行中的 MD job 不能替换参数化输入。")
        system_payload = (
            system_xml
            if isinstance(system_xml, bytes)
            else system_xml.encode("utf-8")
        )
        topology_payload = (
            topology_pdb
            if isinstance(topology_pdb, bytes)
            else topology_pdb.encode("utf-8")
        )
        if (
            not system_payload.strip()
            or len(system_payload) > _MAX_SYSTEM_XML_BYTES
        ):
            raise ValueError("OpenMM System XML 为空或超过大小限制。")
        if b"<System" not in system_payload:
            raise ValueError("输入不包含 OpenMM <System> XML。")
        xml_upper = system_payload.upper()
        if any(
            marker in xml_upper
            for marker in (b"<!DOCTYPE", b"<!ENTITY", b" SYSTEM ", b" PUBLIC ")
        ):
            raise ValueError("OpenMM System XML 不允许 DOCTYPE 或外部实体。")
        if system_payload.count(b"<Particle") > _MAX_PARTICLES:
            raise ValueError("OpenMM System XML 粒子声明超过安全上限。")
        if system_payload.count(b"<Force") > _MAX_FORCES + 1:
            raise ValueError("OpenMM System XML force 声明超过安全上限。")
        if system_payload.count(b"<Constraint") > _MAX_CONSTRAINTS + 1:
            raise ValueError("OpenMM System XML constraint 声明超过安全上限。")
        if max((len(line) for line in system_payload.splitlines()), default=0) > (
            1 * 1024 * 1024
        ):
            raise ValueError("OpenMM System XML 单行表达式超过 1 MB 上限。")
        if (
            not topology_payload.strip()
            or len(topology_payload) > _MAX_TOPOLOGY_PDB_BYTES
        ):
            raise ValueError("topology PDB 为空或超过大小限制。")
        if not any(
            line.startswith((b"ATOM  ", b"HETATM"))
            for line in topology_payload.splitlines()
        ):
            raise ValueError("topology PDB 不包含坐标记录。")
        if any(
            line.startswith(b"CRYST1")
            for line in topology_payload.splitlines()
        ):
            raise ValueError(
                "v0.6 technical_smoke 不接受带 CRYST1 周期盒的 topology PDB。"
            )
        topology_elements = _pdb_elements(topology_payload)
        if declared_system_summary.particle_count != len(topology_elements):
            raise ValueError(
                "声明的 System 粒子数与 topology PDB 原子数不一致。"
            )
        receptor_indices = list(receptor_topology_atom_indices)
        ligand_indices = list(ligand_topology_atom_indices)
        if set(receptor_indices) & set(ligand_indices):
            raise ValueError("受体与配体原子映射不能重叠。")
        for indices, original, label in (
            (
                receptor_indices,
                record.original_inputs.receptor.element_sequence,
                "受体",
            ),
            (
                ligand_indices,
                record.original_inputs.ligand.element_sequence,
                "配体",
            ),
        ):
            if len(indices) != len(original):
                raise ValueError(f"{label}映射数量与原始原子数量不一致。")
            if any(index >= len(topology_elements) for index in indices):
                raise ValueError(f"{label}映射索引超出 topology 原子范围。")
            mapped = [topology_elements[index] for index in indices]
            if mapped != original:
                raise ValueError(f"{label}映射的元素序列与原始输入不一致。")
        evidence_payload = (
            mapping_evidence
            if isinstance(mapping_evidence, bytes)
            else mapping_evidence.encode("utf-8")
        )
        if not evidence_payload.strip() or len(evidence_payload) > 5 * 1024 * 1024:
            raise ValueError("原子映射证据为空或超过 5 MB。")
        if not preparation_command or any(
            not item.strip() for item in preparation_command
        ):
            raise ValueError("必须记录非空参数化命令参数列表。")
        if not prepared_by.strip():
            raise ValueError("必须记录参数化审核人。")
        if not mapping_method.strip():
            raise ValueError("必须记录原子映射方法。")
        submitted_evidence_sha = _sha256_bytes(evidence_payload)
        canonical_evidence_payload = _canonical_mapping_proof(
            manifest_sha256=record.manifest.manifest_sha256 or "",
            original_inputs=record.original_inputs,
            topology_payload=topology_payload,
            receptor_indices=receptor_indices,
            ligand_indices=ligand_indices,
            mapping_method=mapping_method,
            prepared_by=prepared_by,
            preparation_command=preparation_command,
            submitted_evidence_sha256=submitted_evidence_sha,
            submitted_evidence_size_bytes=len(evidence_payload),
        )
        normalized_forcefields: dict[str, bytes] = {}
        for name, payload in forcefield_files.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
                or not payload
            ):
                raise ValueError("力场文件名不安全或内容为空。")
            normalized_forcefields[name] = bytes(payload)
        if not normalized_forcefields:
            raise ValueError("必须保存至少一个实际力场/参数文件。")
        if sum(map(len, normalized_forcefields.values())) > 25 * 1024 * 1024:
            raise ValueError("力场/参数文件总量超过 25 MB。")
        input_directory = (
            self.inputs_root / self._safe_id(job_id).casefold()
        ).resolve()
        if input_directory.parent != self.inputs_root.resolve():
            raise ValueError("MD 输入路径超出允许目录。")
        if not input_directory.is_dir():
            raise ValueError("MD 原始输入目录缺失。")
        prepared_directory = (input_directory / "prepared").resolve()
        if prepared_directory.exists():
            raise ValueError("该 MD job 已保存参数化输入，拒绝覆盖。")
        temporary = input_directory / f".prepared.{uuid4().hex}.tmp"
        temporary.mkdir()
        try:
            (temporary / "system.xml").write_bytes(system_payload)
            (temporary / "topology.pdb").write_bytes(topology_payload)
            (temporary / "mapping-evidence.json").write_bytes(
                canonical_evidence_payload
            )
            (temporary / "submitted-mapping-evidence.bin").write_bytes(
                evidence_payload
            )
            forcefield_directory = temporary / "forcefields"
            forcefield_directory.mkdir()
            for name, payload in normalized_forcefields.items():
                (forcefield_directory / name).write_bytes(payload)
            temporary.replace(prepared_directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        mapping_sha = _sha256_bytes(canonical_evidence_payload)
        prepared = MDPreparedSystemReference(
            manifest_sha256=record.manifest.manifest_sha256 or "",
            system_xml_path=str(prepared_directory / "system.xml"),
            system_xml_sha256=_sha256_bytes(system_payload),
            system_xml_size_bytes=len(system_payload),
            topology_pdb_path=str(prepared_directory / "topology.pdb"),
            topology_pdb_sha256=_sha256_bytes(topology_payload),
            topology_pdb_size_bytes=len(topology_payload),
            receptor_source_sha256=(
                record.original_inputs.receptor.source_sha256
            ),
            ligand_source_sha256=record.original_inputs.ligand.source_sha256,
            receptor_mapping=MDAtomMappingProof(
                source_role="receptor",
                source_sha256=record.original_inputs.receptor.source_sha256,
                topology_atom_indices=receptor_indices,
                mapping_method=mapping_method,
                mapping_evidence_sha256=mapping_sha,
            ),
            ligand_mapping=MDAtomMappingProof(
                source_role="ligand",
                source_sha256=record.original_inputs.ligand.source_sha256,
                topology_atom_indices=ligand_indices,
                mapping_method=mapping_method,
                mapping_evidence_sha256=mapping_sha,
            ),
            mapping_evidence_path=str(
                prepared_directory / "mapping-evidence.json"
            ),
            declared_system_summary=declared_system_summary,
            parameterization_backend=parameterization_backend,
            parameterization_version=parameterization_version,
            preparation_command=list(preparation_command),
            prepared_by=prepared_by,
            forcefield_file_sha256={
                name: _sha256_bytes(payload)
                for name, payload in normalized_forcefields.items()
            },
            forcefield_directory=str(prepared_directory / "forcefields"),
            prepared_at=_utc_now(),
            notes=list(notes),
        )
        updates: dict[str, object] = {"prepared_system": prepared}
        if record.state is MDJobState.FAILED and record.checkpoint is None:
            # A pre-execution setup failure can be retried after the missing
            # parameterized inputs are supplied.  This is a retry, not a
            # checkpoint resume.
            updates.update(
                {
                    "state": MDJobState.QUEUED,
                    "error": None,
                    "worker_pid": None,
                }
            )
        updated = self._updated(record, **updates)
        try:
            self._save(updated)
        except Exception:
            shutil.rmtree(prepared_directory, ignore_errors=True)
            raise
        return updated

    def save_checkpoint(
        self,
        job_id: str,
        *,
        checkpoint_payload: bytes,
        portable_state_payload: bytes | None,
        step: int,
        backend_version: str,
        hardware_fingerprint: str,
    ) -> MDJobRecord:
        record = self.load(job_id)
        if record.state not in {
            MDJobState.RUNNING,
            MDJobState.CANCEL_REQUESTED,
            MDJobState.FAILED,
            MDJobState.CANCELLED,
        }:
            raise ValueError("当前 MD job 状态不能保存检查点。")
        if not checkpoint_payload:
            raise ValueError("OpenMM checkpoint 为空。")
        if len(checkpoint_payload) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("OpenMM checkpoint 超过 128 MB 安全上限。")
        if record.prepared_system is None:
            raise ValueError("没有绑定参数化 System，不能保存 checkpoint。")
        if step > record.manifest.protocol.integration_steps:
            raise ValueError("checkpoint step 超过 technical_smoke 总步数。")
        directory = (
            self.checkpoints_root / self._safe_id(job_id).casefold()
        ).resolve()
        if directory.parent != self.checkpoints_root.resolve():
            raise ValueError("MD 检查点路径超出允许目录。")
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint_digest = _sha256_bytes(checkpoint_payload)
        checkpoint_path = directory / (
            f"state-{step:09d}-{checkpoint_digest[:12]}.chk"
        )
        _atomic_write_bytes(checkpoint_path, checkpoint_payload)
        portable_path: Path | None = None
        portable_sha: str | None = None
        if portable_state_payload is not None:
            if not portable_state_payload:
                raise ValueError("portable state 不能为空。")
            if len(portable_state_payload) > _MAX_PORTABLE_STATE_BYTES:
                raise ValueError("OpenMM portable state 超过 128 MB 安全上限。")
            portable_digest = _sha256_bytes(portable_state_payload)
            portable_path = directory / (
                f"state-{step:09d}-{portable_digest[:12]}.xml"
            )
            _atomic_write_bytes(portable_path, portable_state_payload)
            portable_sha = portable_digest
        reference = MDCheckpointReference(
            checkpoint_path=str(checkpoint_path),
            checkpoint_sha256=checkpoint_digest,
            checkpoint_size_bytes=len(checkpoint_payload),
            portable_state_path=(
                str(portable_path) if portable_path is not None else None
            ),
            portable_state_sha256=portable_sha,
            manifest_sha256=record.manifest.manifest_sha256 or "",
            system_xml_sha256=record.prepared_system.system_xml_sha256,
            topology_pdb_sha256=record.prepared_system.topology_pdb_sha256,
            replica_index=1,
            seed=record.manifest.protocol.seeds[0],
            step=step,
            backend_version=backend_version,
            hardware_fingerprint=hardware_fingerprint,
            created_at=_utc_now(),
        )
        updated = self._updated(record, checkpoint=reference)
        self._save(updated)
        return updated

    @staticmethod
    def _verify_checkpoint(
        reference: MDCheckpointReference,
        *,
        manifest: MDTaskManifest | None = None,
        prepared_system: MDPreparedSystemReference | None = None,
    ) -> None:
        checkpoint_path = Path(reference.checkpoint_path)
        if (
            not checkpoint_path.is_file()
            or checkpoint_path.stat().st_size != reference.checkpoint_size_bytes
            or _sha256_file(checkpoint_path) != reference.checkpoint_sha256
        ):
            raise ValueError("MD checkpoint 缺失或 SHA-256 校验失败。")
        if reference.portable_state_path is not None:
            portable_path = Path(reference.portable_state_path)
            if (
                not portable_path.is_file()
                or _sha256_file(portable_path)
                != reference.portable_state_sha256
            ):
                raise ValueError("MD portable state 缺失或 SHA-256 校验失败。")
        if manifest is not None:
            if (
                reference.manifest_sha256 != manifest.manifest_sha256
                or reference.seed != manifest.protocol.seeds[0]
            ):
                raise ValueError("MD checkpoint 与 manifest/seed 不匹配。")
            if reference.step > manifest.protocol.integration_steps:
                raise ValueError("MD checkpoint step 超过 manifest 总步数。")
        if prepared_system is not None:
            if (
                reference.system_xml_sha256
                != prepared_system.system_xml_sha256
                or reference.topology_pdb_sha256
                != prepared_system.topology_pdb_sha256
            ):
                raise ValueError("MD checkpoint 与 System/topology 不匹配。")

    def resume(self, job_id: str) -> MDJobRecord:
        record = self.load(job_id)
        if record.state not in {
            MDJobState.FAILED,
            MDJobState.CANCELLED,
        }:
            raise ValueError("只有 failed 或 cancelled MD job 可恢复。")
        if record.checkpoint is None:
            raise ValueError("MD job 没有可核验的 checkpoint，不能恢复。")
        if (
            record.checkpoint.step
            >= record.manifest.protocol.integration_steps
        ):
            raise ValueError(
                "checkpoint 已达到 technical_smoke 总步数，不能作为恢复起点。"
            )
        self._verify_checkpoint(
            record.checkpoint,
            manifest=record.manifest,
            prepared_system=record.prepared_system,
        )
        updated = self._updated(
            record,
            state=MDJobState.QUEUED,
            worker_pid=None,
            error=None,
            dry_run_plan=None,
            run_result=None,
            resume_count=record.resume_count + 1,
        )
        self._save(updated)
        return updated


def _verify_prepared_system(
    reference: MDPreparedSystemReference,
) -> tuple[Path, Path]:
    system_path = Path(reference.system_xml_path).resolve()
    topology_path = Path(reference.topology_pdb_path).resolve()
    if (
        not system_path.is_file()
        or system_path.stat().st_size != reference.system_xml_size_bytes
        or _sha256_file(system_path) != reference.system_xml_sha256
    ):
        raise MDWorkerExecutionError(
            "OpenMM System XML 缺失或 SHA-256 校验失败。"
        )
    if (
        not topology_path.is_file()
        or topology_path.stat().st_size != reference.topology_pdb_size_bytes
        or _sha256_file(topology_path) != reference.topology_pdb_sha256
    ):
        raise MDWorkerExecutionError(
            "OpenMM topology PDB 缺失或 SHA-256 校验失败。"
        )
    evidence_path = Path(reference.mapping_evidence_path).resolve()
    if (
        not evidence_path.is_file()
        or _sha256_file(evidence_path)
        != reference.receptor_mapping.mapping_evidence_sha256
        or _sha256_file(evidence_path)
        != reference.ligand_mapping.mapping_evidence_sha256
    ):
        raise MDWorkerExecutionError("原子映射证据缺失或 SHA-256 校验失败。")
    forcefield_directory = Path(reference.forcefield_directory).resolve()
    for name, expected in reference.forcefield_file_sha256.items():
        path = (forcefield_directory / name).resolve()
        if (
            path.parent != forcefield_directory
            or not path.is_file()
            or _sha256_file(path) != expected
        ):
            raise MDWorkerExecutionError(
                f"力场/参数文件 {name} 缺失或 SHA-256 校验失败。"
            )
    return system_path, topology_path


def _verify_canonical_mapping_proof(
    reference: MDPreparedSystemReference,
    *,
    original_inputs: MDOriginalInputsReference,
    topology_payload: bytes,
) -> None:
    evidence_path = Path(reference.mapping_evidence_path).resolve()
    evidence_payload = evidence_path.read_bytes()
    try:
        document = json.loads(evidence_payload)
        submitted = document["submitted_evidence"]
        submitted_sha = str(submitted["sha256"])
        submitted_size = int(submitted["size_bytes"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MDWorkerExecutionError(
            "原子映射证据不是有效的 v0.6 canonical JSON。"
        ) from exc
    if document.get("schema") != "vetevidence-md-atom-mapping-v2":
        raise MDWorkerExecutionError("原子映射证据 schema 不受支持。")
    submitted_path = (
        evidence_path.parent / "submitted-mapping-evidence.bin"
    ).resolve()
    if (
        submitted_path.parent != evidence_path.parent
        or not submitted_path.is_file()
        or submitted_path.stat().st_size != submitted_size
        or _sha256_file(submitted_path) != submitted_sha
    ):
        raise MDWorkerExecutionError(
            "提交的原子映射支持证据缺失或 SHA-256 校验失败。"
        )
    try:
        expected = _canonical_mapping_proof(
            manifest_sha256=reference.manifest_sha256,
            original_inputs=original_inputs,
            topology_payload=topology_payload,
            receptor_indices=(
                reference.receptor_mapping.topology_atom_indices
            ),
            ligand_indices=reference.ligand_mapping.topology_atom_indices,
            mapping_method=reference.receptor_mapping.mapping_method,
            prepared_by=reference.prepared_by,
            preparation_command=reference.preparation_command,
            submitted_evidence_sha256=submitted_sha,
            submitted_evidence_size_bytes=submitted_size,
        )
    except ValueError as exc:
        raise MDWorkerExecutionError(str(exc)) from exc
    if evidence_payload != expected:
        raise MDWorkerExecutionError(
            "原子映射证据与 manifest、来源、topology 或逐原子身份不一致。"
        )


def _artifact_reference(path: Path, role: str) -> MDArtifactReference:
    return MDArtifactReference(
        role=role,
        filename=path.name,
        sha256=_sha256_file(path),
        size_bytes=path.stat().st_size,
    )


def _close_openmm_reporters(simulation: Any | None) -> None:
    if simulation is None:
        return
    for reporter in list(getattr(simulation, "reporters", ())):
        output = getattr(reporter, "_out", None)
        close = getattr(output, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass
    reporters = getattr(simulation, "reporters", None)
    if hasattr(reporters, "clear"):
        reporters.clear()


def _state_series_from_csv(
    path: Path,
) -> tuple[MDTimeSeries | None, MDTimeSeries | None]:
    """Read only temperature and potential energy from OpenMM's own CSV."""

    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return None, None
    if not rows:
        return None, None
    fieldnames = list(rows[0])
    time_key = next(
        (name for name in fieldnames if "Time" in name and "ps" in name),
        None,
    )
    temperature_key = next(
        (name for name in fieldnames if "Temperature" in name),
        None,
    )
    potential_key = next(
        (name for name in fieldnames if "Potential Energy" in name),
        None,
    )
    if time_key is None:
        return None, None
    try:
        times = [float(row[time_key]) for row in rows]
        temperature = (
            MDTimeSeries(
                times_ps=times,
                values=[float(row[temperature_key]) for row in rows],
                unit="K",
            )
            if temperature_key is not None
            else None
        )
        potential = (
            MDTimeSeries(
                times_ps=times,
                values=[float(row[potential_key]) for row in rows],
                unit="kJ/mol",
            )
            if potential_key is not None
            else None
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    return temperature, potential


def _actual_system_summary(system: Any) -> MDSystemSummary:
    force_types = [
        type(system.getForce(index)).__name__
        for index in range(system.getNumForces())
    ]
    return MDSystemSummary(
        particle_count=system.getNumParticles(),
        force_count=system.getNumForces(),
        constraint_count=system.getNumConstraints(),
        force_types=force_types,
        uses_periodic_boundary_conditions=bool(
            system.usesPeriodicBoundaryConditions()
        ),
    )


def _seed_system_random_sources(system: Any, seed: int) -> dict[str, int]:
    assignments = {"LangevinMiddleIntegrator": seed}
    for index in range(system.getNumForces()):
        force = system.getForce(index)
        setter = getattr(force, "setRandomNumberSeed", None)
        if not callable(setter):
            continue
        derived = ((seed + index + 1) % 2_147_483_647) or 1
        setter(derived)
        assignments[f"{type(force).__name__}[{index}]"] = derived
    return assignments


def _context_platform_properties(context: Any) -> dict[str, str]:
    active = context.getPlatform()
    result: dict[str, str] = {}
    try:
        names = list(active.getPropertyNames())
    except Exception:
        names = []
    for name in names:
        try:
            result[str(name)] = str(active.getPropertyValue(context, name))
        except Exception:
            continue
    return result


def _execution_environment_fingerprint(
    *,
    preflight: MDBackendPreflight,
    backend_version: str,
    platform_name: str,
    platform_properties: Mapping[str, str],
) -> str:
    payload = {
        "backend": preflight.backend,
        "backend_version": backend_version,
        "hardware_snapshot": preflight.hardware.model_dump(mode="json"),
        "actual_platform": platform_name,
        "actual_platform_properties": dict(platform_properties),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _technical_smoke_qc(
    temperature: MDTimeSeries | None,
    potential: MDTimeSeries | None,
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    if (
        temperature is None
        or potential is None
        or not temperature.values
        or not potential.values
    ):
        return False, ["状态日志缺少非空温度或势能序列。"]
    values = [*temperature.values, *potential.values]
    if any(not math.isfinite(value) for value in values):
        return False, ["状态日志包含 NaN 或无穷值。"]
    if any(value <= 0 or value > 10_000 for value in temperature.values):
        return False, ["温度超出 technical smoke 的宽松数值安全范围。"]
    if any(abs(value) > 1e12 for value in potential.values):
        return False, ["势能绝对值超过 technical smoke 数值安全上限。"]
    return True, warnings


def execute_prepared_openmm_smoke(
    manifest: MDTaskManifest,
    prepared_system: MDPreparedSystemReference,
    *,
    original_inputs: MDOriginalInputsReference,
    output_directory: Path,
    attempt_id: str,
    preflight: MDBackendPreflight | None = None,
    checkpoint: MDCheckpointReference | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    checkpoint_callback: (
        Callable[[int, bytes, bytes, str], None] | None
    ) = None,
) -> MDRunResult:
    """Run a tiny, auditable technical smoke on a pre-parameterized system."""

    verified = MDTaskManifest.model_validate(
        manifest.model_dump(mode="python")
    )
    if not verified.protocol_approved_by_user:
        raise MDWorkerExecutionError(
            "真实 OpenMM technical_smoke 必须先由用户明确批准协议；"
            "未批准任务只能 dry-run。"
        )
    if verified.protocol.preset.value != "technical_smoke":
        raise MDWorkerExecutionError(
            "v0.6 真实执行仅支持 technical_smoke；"
            "探索性和科研模拟仍需人工复核后续实现。"
        )
    if prepared_system.manifest_sha256 != verified.manifest_sha256:
        raise MDWorkerExecutionError(
            "参数化 OpenMM 输入未绑定当前 MD manifest。"
        )
    if (
        original_inputs.manifest_sha256 != verified.manifest_sha256
        or original_inputs.receptor.source_sha256
        != verified.receptor_source.sha256
        or original_inputs.ligand.source_sha256 != verified.ligand_source.sha256
        or prepared_system.receptor_source_sha256
        != original_inputs.receptor.source_sha256
        or prepared_system.ligand_source_sha256
        != original_inputs.ligand.source_sha256
    ):
        raise MDWorkerExecutionError("原始输入、参数化输入与 manifest 身份链不一致。")
    active_preflight = preflight or preflight_openmm()
    if not active_preflight.execution_available:
        raise MDBackendUnavailable(
            active_preflight.reason or "OpenMM 执行后端不可用。"
        )
    backend_version = active_preflight.package_versions.get(
        "openmm",
        "unknown",
    )
    if checkpoint is not None:
        MDJobStore._verify_checkpoint(
            checkpoint,
            manifest=verified,
            prepared_system=prepared_system,
        )
        if checkpoint.backend_version != backend_version:
            raise MDWorkerExecutionError(
                "checkpoint OpenMM 版本与当前后端不一致。"
            )
    system_path, topology_path = _verify_prepared_system(prepared_system)
    for reference, source in (
        (original_inputs.receptor, verified.receptor_source),
        (original_inputs.ligand, verified.ligand_source),
    ):
        source_path = Path(reference.stored_path).resolve()
        if (
            not source_path.is_file()
            or source_path.stat().st_size != reference.source_size_bytes
            or _sha256_file(source_path) != reference.source_sha256
        ):
            raise MDWorkerExecutionError(
                f"{reference.role} 原始输入缺失或 SHA-256 校验失败。"
            )
        source_payload = source_path.read_bytes()
        if _source_elements(source_payload, source.format) != (
            reference.element_sequence
        ):
            raise MDWorkerExecutionError(
                f"{reference.role} 原始输入元素序列不一致。"
            )
        if _source_atom_identity_signatures(
            source_payload,
            source.format,
        ) != reference.atom_identity_signatures:
            raise MDWorkerExecutionError(
                f"{reference.role} 原始输入逐原子身份签名不一致。"
            )
    _verify_canonical_mapping_proof(
        prepared_system,
        original_inputs=original_inputs,
        topology_payload=topology_path.read_bytes(),
    )
    topology_elements = _pdb_elements(topology_path.read_bytes())
    for mapping, original in (
        (
            prepared_system.receptor_mapping,
            original_inputs.receptor.element_sequence,
        ),
        (
            prepared_system.ligand_mapping,
            original_inputs.ligand.element_sequence,
        ),
    ):
        if [
            topology_elements[index]
            for index in mapping.topology_atom_indices
        ] != original:
            raise MDWorkerExecutionError("source→topology 原子映射复核失败。")
    try:
        openmm = importlib.import_module("openmm")
        app = importlib.import_module("openmm.app")
        unit = importlib.import_module("openmm.unit")
    except (ImportError, ModuleNotFoundError) as exc:
        raise MDBackendUnavailable(
            "OpenMM 在 preflight 后不可导入，拒绝执行。"
        ) from exc
    requested_platform = verified.hardware_request.platform
    if (
        verified.hardware_request.gpu_required
        and not active_preflight.hardware.gpu_platforms
    ):
        raise MDBackendUnavailable("任务要求 GPU，但 OpenMM 未发现 GPU 平台。")
    if (
        requested_platform != "auto"
        and requested_platform
        not in active_preflight.hardware.openmm_platforms
    ):
        raise MDBackendUnavailable(
            f"请求的 OpenMM 平台 {requested_platform} 不可用。"
        )
    output_directory = output_directory.resolve()
    if output_directory.exists():
        raise MDWorkerExecutionError("MD 产物目录已存在，拒绝覆盖。")
    temporary = output_directory.with_name(
        f".{output_directory.name}.{uuid4().hex}.tmp"
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    started_at = _utc_now()
    started = time.perf_counter()
    simulation = None
    actual_platform_name = "unknown"
    actual_platform_properties: dict[str, str] = {}
    execution_fingerprint = ""
    random_seed_assignments: dict[str, int] = {}
    try:
        system_xml = system_path.read_text(encoding="utf-8")
        system = openmm.XmlSerializer.deserialize(system_xml)
        pdb = app.PDBFile(str(topology_path))
        topology_atom_count = sum(1 for _ in pdb.topology.atoms())
        if system.getNumParticles() != topology_atom_count:
            raise MDWorkerExecutionError(
                "System 粒子数与 topology PDB 原子数不一致。"
            )
        actual_summary = _actual_system_summary(system)
        if actual_summary != prepared_system.declared_system_summary:
            raise MDWorkerExecutionError(
                "OpenMM System 实际摘要与参数化清单不一致。"
            )
        if actual_summary.particle_count > _MAX_PARTICLES:
            raise MDWorkerExecutionError("OpenMM System 粒子数超过安全上限。")
        forcefield = verified.forcefield
        integrator = openmm.LangevinMiddleIntegrator(
            forcefield.temperature_kelvin * unit.kelvin,
            forcefield.friction_per_ps / unit.picosecond,
            verified.protocol.timestep_fs * unit.femtoseconds,
        )
        seed = verified.protocol.seeds[0]
        integrator.setRandomNumberSeed(seed)
        random_seed_assignments = _seed_system_random_sources(system, seed)
        if requested_platform == "auto":
            simulation = app.Simulation(
                pdb.topology,
                system,
                integrator,
            )
        else:
            selected_platform = openmm.Platform.getPlatformByName(
                requested_platform
            )
            properties: dict[str, str] = {}
            if requested_platform in _GPU_PLATFORMS:
                if verified.hardware_request.device_indices:
                    properties["DeviceIndex"] = ",".join(
                        str(item)
                        for item in verified.hardware_request.device_indices
                    )
                properties["Precision"] = verified.hardware_request.precision
            simulation = app.Simulation(
                pdb.topology,
                system,
                integrator,
                selected_platform,
                properties,
            )
        simulation.context.setPositions(pdb.positions)
        actual_platform_name = simulation.context.getPlatform().getName()
        if (
            verified.hardware_request.gpu_required
            and actual_platform_name not in _GPU_PLATFORMS
        ):
            raise MDBackendUnavailable(
                "任务要求 GPU，但实际 OpenMM Context 未使用 GPU 平台。"
            )
        actual_platform_properties = _context_platform_properties(
            simulation.context
        )
        execution_fingerprint = _execution_environment_fingerprint(
            preflight=active_preflight,
            backend_version=backend_version,
            platform_name=actual_platform_name,
            platform_properties=actual_platform_properties,
        )
        if (
            checkpoint is not None
            and checkpoint.hardware_fingerprint != execution_fingerprint
        ):
            raise MDWorkerExecutionError(
                "checkpoint 的实际平台、设备、精度或硬件指纹与当前 "
                "OpenMM Context 不一致。"
            )
        if checkpoint is None:
            simulation.context.setVelocitiesToTemperature(
                forcefield.temperature_kelvin * unit.kelvin,
                seed,
            )
            simulation.minimizeEnergy(
                maxIterations=(
                    verified.protocol.energy_minimization_max_iterations
                )
            )
        else:
            simulation.loadCheckpoint(checkpoint.checkpoint_path)
            if int(simulation.currentStep) != checkpoint.step:
                raise MDWorkerExecutionError(
                    "checkpoint 二进制内部 currentStep 与审计元数据不一致。"
                )
        total_steps = verified.protocol.integration_steps
        completed_steps = checkpoint.step if checkpoint is not None else 0
        remaining_steps = total_steps - completed_steps
        if remaining_steps <= 0:
            raise MDWorkerExecutionError(
                "checkpoint 已达到或超过 technical_smoke 总步数，"
                "无需恢复执行。"
            )
        estimated_frames = math.ceil(
            remaining_steps / verified.protocol.report_interval_steps
        )
        estimated_dcd_bytes = (
            1024
            + estimated_frames
            * (128 + actual_summary.particle_count * 3 * 4)
        )
        if estimated_dcd_bytes > _MAX_OUTPUT_BYTES:
            raise MDWorkerExecutionError(
                "预计 DCD 轨迹超过 technical_smoke 产物上限。"
            )
        trajectory_path = temporary / "trajectory.dcd"
        state_log_path = temporary / "state.csv"
        checkpoint_path = temporary / "checkpoint.chk"
        portable_state_path = temporary / "portable-state.xml"
        final_pdb_path = temporary / "representative.pdb"
        copied_system_path = temporary / "system.xml"
        copied_topology_path = temporary / "topology.pdb"
        manifest_path = temporary / "manifest.json"
        analysis_path = temporary / "analysis.json"
        pymol_path = temporary / "view_md.pml"
        shutil.copyfile(system_path, copied_system_path)
        shutil.copyfile(topology_path, copied_topology_path)
        simulation.reporters.append(
            app.DCDReporter(
                str(trajectory_path),
                verified.protocol.report_interval_steps,
            )
        )
        simulation.reporters.append(
            app.StateDataReporter(
                str(state_log_path),
                verified.protocol.report_interval_steps,
                step=True,
                time=True,
                potentialEnergy=True,
                temperature=True,
                progress=True,
                remainingTime=True,
                speed=True,
                totalSteps=total_steps,
            )
        )
        simulation.reporters.append(
            app.CheckpointReporter(
                str(checkpoint_path),
                verified.protocol.checkpoint_interval_steps,
            )
        )
        steps_done = completed_steps
        simulation.saveCheckpoint(str(checkpoint_path))
        simulation.saveState(str(portable_state_path))
        checkpoint_payload = checkpoint_path.read_bytes()
        portable_state_payload = portable_state_path.read_bytes()
        if (
            len(checkpoint_payload) > _MAX_CHECKPOINT_BYTES
            or len(portable_state_payload) > _MAX_PORTABLE_STATE_BYTES
        ):
            raise MDWorkerExecutionError(
                "OpenMM checkpoint 或 portable state 超过安全上限。"
            )
        if checkpoint_callback is not None:
            checkpoint_callback(
                steps_done,
                checkpoint_payload,
                portable_state_payload,
                execution_fingerprint,
            )
        while steps_done < total_steps:
            if cancel_requested is not None and cancel_requested():
                raise MDWorkerCancelled("MD technical smoke 已收到取消请求。")
            chunk = min(
                verified.protocol.chunk_steps,
                total_steps - steps_done,
            )
            simulation.step(chunk)
            steps_done += chunk
            simulation.saveCheckpoint(str(checkpoint_path))
            simulation.saveState(str(portable_state_path))
            checkpoint_payload = checkpoint_path.read_bytes()
            portable_state_payload = portable_state_path.read_bytes()
            if (
                len(checkpoint_payload) > _MAX_CHECKPOINT_BYTES
                or len(portable_state_payload) > _MAX_PORTABLE_STATE_BYTES
            ):
                raise MDWorkerExecutionError(
                    "OpenMM checkpoint 或 portable state 超过安全上限。"
                )
            if checkpoint_callback is not None:
                checkpoint_callback(
                    steps_done,
                    checkpoint_payload,
                    portable_state_payload,
                    execution_fingerprint,
                )
            if _directory_size_bytes(temporary) > _MAX_OUTPUT_BYTES:
                raise MDWorkerExecutionError(
                    "MD technical_smoke 临时产物超过安全上限。"
                )
            if (
                time.perf_counter() - started
                > verified.protocol.walltime_limit_seconds
            ):
                raise MDWorkerExecutionError(
                    "MD technical smoke 超过清单 walltime 上限。"
                )
            if cancel_requested is not None and cancel_requested():
                raise MDWorkerCancelled(
                    "MD technical smoke 已保存 checkpoint 并取消。"
                )
        simulation.saveState(str(portable_state_path))
        if not checkpoint_path.is_file():
            simulation.saveCheckpoint(str(checkpoint_path))
        state = simulation.context.getState(positions=True)
        with final_pdb_path.open("w", encoding="utf-8") as handle:
            app.PDBFile.writeFile(
                simulation.topology,
                state.getPositions(),
                handle,
            )
        manifest_path.write_text(
            json.dumps(
                verified.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        pymol_path.write_text(
            "load representative.pdb, md_complex\n"
            "load_traj trajectory.dcd, md_complex\n"
            "hide everything\n"
            "show cartoon, polymer\n"
            "show sticks, organic\n",
            encoding="utf-8",
        )
        temperature, potential = _state_series_from_csv(state_log_path)
        qc_passed, qc_warnings = _technical_smoke_qc(
            temperature,
            potential,
        )
        if not qc_passed:
            raise MDWorkerExecutionError(
                "OpenMM technical smoke QC 失败：" + "；".join(qc_warnings)
            )
        analysis = MDAnalysisResult(
            replicas=[
                MDReplicaAnalysis(
                    replica_index=1,
                    seed=seed,
                    qc_passed=qc_passed,
                    temperature_kelvin=temperature,
                    potential_energy_kj_mol=potential,
                    warnings=[
                        "technical_smoke 只验证数值执行链，"
                        "不能解释结合稳定性。",
                        *qc_warnings,
                        *(
                            ["本段由已核验 checkpoint 恢复执行。"]
                            if checkpoint is not None
                            else []
                        ),
                    ],
                )
            ],
            replicate_summary=MDReplicateSummary(
                total_replicas=1,
                successful_replicas=1,
                convergence_checked=False,
                between_replica_consistent=False,
            ),
            produced_metrics=[
                "temperature_kelvin",
                "potential_energy_kj_mol",
            ],
        )
        analysis_path.write_text(
            json.dumps(
                analysis.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        required_paths = [
            trajectory_path,
            state_log_path,
            checkpoint_path,
            portable_state_path,
            final_pdb_path,
            copied_system_path,
            copied_topology_path,
            manifest_path,
            analysis_path,
            pymol_path,
        ]
        if any(
            not path.is_file() or path.stat().st_size <= 0
            for path in required_paths
        ):
            raise MDWorkerExecutionError("OpenMM smoke 缺少必需产物。")
        if sum(path.stat().st_size for path in required_paths) > _MAX_OUTPUT_BYTES:
            raise MDWorkerExecutionError("OpenMM smoke 产物总量超过安全上限。")
        # Reporters keep file handles open.  Close them before the atomic
        # directory move, which is required on Windows.
        _close_openmm_reporters(simulation)
        temporary.replace(output_directory)
    except Exception:
        _close_openmm_reporters(simulation)
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        simulation = None
    duration = max(0.0, time.perf_counter() - started)
    completed_at = _utc_now()
    # Paths must be rebuilt after the temporary directory is atomically moved.
    trajectory_path = output_directory / "trajectory.dcd"
    state_log_path = output_directory / "state.csv"
    checkpoint_path = output_directory / "checkpoint.chk"
    portable_state_path = output_directory / "portable-state.xml"
    final_pdb_path = output_directory / "representative.pdb"
    copied_system_path = output_directory / "system.xml"
    copied_topology_path = output_directory / "topology.pdb"
    manifest_path = output_directory / "manifest.json"
    analysis_path = output_directory / "analysis.json"
    pymol_path = output_directory / "view_md.pml"
    artifacts = [
        _artifact_reference(manifest_path, "manifest"),
        _artifact_reference(copied_system_path, "system"),
        _artifact_reference(copied_topology_path, "topology"),
        _artifact_reference(trajectory_path, "trajectory"),
        _artifact_reference(state_log_path, "state_log"),
        _artifact_reference(checkpoint_path, "checkpoint"),
        _artifact_reference(portable_state_path, "portable_state"),
        _artifact_reference(analysis_path, "analysis"),
        _artifact_reference(final_pdb_path, "representative_structure"),
        _artifact_reference(pymol_path, "pymol_script"),
    ]
    return MDRunResult(
        manifest=verified,
        validation_status=MDValidationStatus.TECHNICAL_SMOKE_PASSED,
        analysis=analysis,
        execution_audit=MDExecutionAudit(
            execution_mode="openmm_local",
            backend_version=backend_version,
            package_versions=active_preflight.package_versions,
            hardware_fingerprint=execution_fingerprint,
            platform_name=actual_platform_name,
            precision=actual_platform_properties.get(
                "Precision",
                (
                    "not_applicable"
                    if actual_platform_name == "CPU"
                    else "not_reported"
                ),
            ),
            platform_properties=actual_platform_properties,
            selected_device=actual_platform_properties.get("DeviceIndex"),
            driver_version=actual_platform_properties.get(
                "CudaDriverVersion"
            ),
            forcefield_file_sha256=(
                prepared_system.forcefield_file_sha256
            ),
            seeds=verified.protocol.seeds,
            random_seed_assignments=random_seed_assignments,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
        ),
        artifacts=artifacts,
        attempt_id=attempt_id,
    )


def process_queued_job(
    store: MDJobStore,
    job_id: str,
    *,
    dry_run: bool,
    preflight: MDBackendPreflight | None = None,
    raise_on_error: bool = False,
) -> MDJobRecord:
    """Claim and process one job, always persisting a terminal state."""

    claimed = store.claim(job_id)
    try:
        active_preflight = preflight or preflight_openmm()
        if not active_preflight.execution_available:
            raise MDBackendUnavailable(
                active_preflight.reason or "OpenMM 执行后端不可用。"
            )
        latest = store.load(job_id)
        if latest.state is MDJobState.CANCEL_REQUESTED:
            return store.mark_cancelled(job_id)
        if dry_run:
            plan = build_openmm_dry_run(
                claimed.manifest,
                preflight=active_preflight,
            )
            return store.mark_dry_run_succeeded(job_id, plan)
        if not claimed.manifest.protocol_approved_by_user:
            raise MDWorkerExecutionError(
                "真实 OpenMM technical_smoke 必须先由用户明确批准协议；"
                "未批准任务只能 dry-run。"
            )
        if claimed.prepared_system is None:
            parameterization_note = (
                "自动 OpenFF 参数化栈不可用："
                + "、".join(
                    active_preflight.missing_parameterization_modules
                )
                if not active_preflight.parameterization_available
                else "v0.6 尚未开放自动参数化"
            )
            raise MDWorkerExecutionError(
                "真实执行必须提供已参数化的 OpenMM System XML "
                f"和匹配 topology PDB；{parameterization_note}，"
                "不会猜测或伪造参数。"
            )
        attempt_directory = store.attempt_directory(
            job_id,
            claimed.attempts,
        )

        def persist_checkpoint(
            step: int,
            checkpoint_payload: bytes,
            portable_state_payload: bytes,
            execution_fingerprint: str,
        ) -> None:
            store.save_checkpoint(
                job_id,
                checkpoint_payload=checkpoint_payload,
                portable_state_payload=portable_state_payload,
                step=step,
                backend_version=active_preflight.package_versions.get(
                    "openmm",
                    "unknown",
                ),
                hardware_fingerprint=execution_fingerprint,
            )

        result = execute_prepared_openmm_smoke(
            claimed.manifest,
            claimed.prepared_system,
            original_inputs=claimed.original_inputs,
            output_directory=attempt_directory,
            attempt_id=f"attempt-{claimed.attempts:04d}",
            preflight=active_preflight,
            checkpoint=claimed.checkpoint,
            cancel_requested=lambda: (
                store.load(job_id).state is MDJobState.CANCEL_REQUESTED
            ),
            checkpoint_callback=persist_checkpoint,
        )
        return store.mark_run_succeeded(job_id, result)
    except MDWorkerCancelled:
        current = store.load(job_id)
        if current.state is MDJobState.CANCEL_REQUESTED:
            return store.mark_cancelled(job_id)
        return store.mark_failed(job_id, "MD worker 在未请求时取消。")
    except Exception as exc:
        current = store.load(job_id)
        if current.state is MDJobState.CANCEL_REQUESTED:
            terminal = store.mark_cancelled(job_id)
        else:
            terminal = store.mark_failed(job_id, str(exc))
        if raise_on_error:
            raise
        return terminal


def _hidden_window_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def launch_md_worker(
    store: MDJobStore,
    job_id: str,
    *,
    dry_run: bool = True,
    python_executable: str | os.PathLike[str] | None = None,
    popen_factory: PopenFactory | None = None,
) -> Any:
    """Launch the queue worker with an argument list and ``shell=False``."""

    store.load(job_id)
    executable = os.fspath(python_executable or sys.executable)
    if not executable.strip():
        raise ValueError("Python 可执行文件路径为空。")
    command = [
        executable,
        "-m",
        "vetevidence.md_worker",
        "run-job",
        "--root",
        str(store.root),
        "--job-id",
        job_id,
    ]
    if dry_run:
        command.append("--dry-run")
    active_factory = popen_factory or subprocess.Popen
    return active_factory(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **_hidden_window_options(),
    )


def cancel_md_worker(
    store: MDJobStore,
    job_id: str,
    process: Any,
    *,
    timeout_seconds: float = 10.0,
) -> MDJobRecord:
    """Request cancellation, terminate the child, and persist cancellation."""

    if timeout_seconds <= 0:
        raise ValueError("取消等待时间必须大于 0。")
    requested = store.request_cancel(job_id)
    if requested.state is MDJobState.CANCELLED:
        return requested
    if process.poll() is None:
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout_seconds)
    latest = store.load(job_id)
    if latest.state is MDJobState.CANCELLED:
        return latest
    if latest.state is MDJobState.CANCEL_REQUESTED:
        return store.mark_cancelled(job_id)
    return latest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VetEvidence MD worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-job")
    run.add_argument("--root", required=True)
    run.add_argument("--job-id", required=True)
    run.add_argument("--dry-run", action="store_true")
    return parser


def _start_worker_deadline(
    store: MDJobStore,
    job_id: str,
    *,
    timeout_seconds: float,
) -> threading.Event:
    """Hard-stop the dedicated worker if OpenMM does not return in time."""

    completed = threading.Event()

    def enforce() -> None:
        if completed.wait(timeout_seconds):
            return
        try:
            current = store.load(job_id)
            if current.state is MDJobState.CANCEL_REQUESTED:
                store.mark_cancelled(job_id)
            elif current.state is MDJobState.RUNNING:
                store.mark_failed(
                    job_id,
                    "后台 worker 超过清单硬截止时间并被终止；"
                    "Context 创建、最小化或单步计算可能未返回。",
                )
            elif current.state is MDJobState.QUEUED:
                store.request_cancel(job_id)
            else:
                return
        except Exception:
            pass
        os._exit(124)

    threading.Thread(
        target=enforce,
        name=f"md-hard-deadline-{job_id}",
        daemon=True,
    ).start()
    return completed


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    store = MDJobStore(Path(arguments.root))
    queued = store.load(arguments.job_id)
    deadline = _start_worker_deadline(
        store,
        arguments.job_id,
        timeout_seconds=float(
            queued.manifest.protocol.walltime_limit_seconds
        ),
    )
    try:
        record = process_queued_job(
            store,
            arguments.job_id,
            dry_run=bool(arguments.dry_run),
        )
    finally:
        deadline.set()
    print(
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if record.state is MDJobState.SUCCEEDED else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MDBackendPreflight",
    "MDBackendUnavailable",
    "MDAtomMappingProof",
    "MDCheckpointReference",
    "MDHardwareSnapshot",
    "MDJobRecord",
    "MDJobState",
    "MDJobStore",
    "MDOriginalInputReference",
    "MDOriginalInputsReference",
    "MDPreparedSystemReference",
    "MDReplicaPlan",
    "MDWorkerExecutionError",
    "MDWorkerCancelled",
    "MDSystemSummary",
    "OpenMMDryRunPlan",
    "build_openmm_dry_run",
    "cancel_md_worker",
    "execute_prepared_openmm_smoke",
    "launch_md_worker",
    "main",
    "preflight_openmm",
    "process_queued_job",
]
