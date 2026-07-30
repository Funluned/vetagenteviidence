"""Auditable orchestration for receptor-gated, replicated Vina docking.

The module coordinates existing Open Babel and AutoDock Vina boundaries.  It
does not download structures, infer a binding pocket, or turn a computed score
into experimental evidence.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shlex
import statistics
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from vetevidence.mechanism_prediction import (
    SourceProvenance,
    VinaDockingRun,
    VinaParameters,
    VinaTaskManifest,
    build_vina_manifest,
    parse_vina_output,
    validate_pdbqt_bytes,
)
from vetevidence.openbabel_execution import OpenBabelPreparationArtifacts
from vetevidence.vina_execution import (
    VinaExecutionArtifacts,
    VinaLocalExecutionMetadata,
    execute_vina,
)


_MAX_RECEPTOR_BYTES = 50 * 1024 * 1024
_MAX_ERROR_CHARACTERS = 2000
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_INCHIKEY_PATTERN = r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
_PDB_ID_PATTERN = r"^[0-9][A-Za-z0-9]{3}$"
_UNIPROT_ID_PATTERN = r"^[A-Z0-9]{6,10}$"
_WATER_RESIDUES = frozenset({"DOD", "HOH", "WAT"})
_METAL_ELEMENTS = frozenset(
    {
        "AL",
        "BA",
        "BE",
        "CA",
        "CD",
        "CO",
        "CR",
        "CS",
        "CU",
        "FE",
        "HG",
        "K",
        "LI",
        "MG",
        "MN",
        "MO",
        "NA",
        "NI",
        "PB",
        "PT",
        "RB",
        "SR",
        "V",
        "W",
        "ZN",
    }
)
_BLANK_CHAIN = "__blank__"
_LIGAND_FORMATS = frozenset(
    {"mol", "mol2", "pdb", "pdbqt", "sdf", "smi", "smiles"}
)


class DockingWorkflowModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class ResidueIdentity(DockingWorkflowModel):
    """Unambiguous residue location used by receptor preparation decisions."""

    model_id: str = Field(min_length=1)
    chain_id: str = Field(min_length=1)
    residue_name: str = Field(min_length=1, max_length=3)
    residue_number: str = Field(min_length=1, max_length=8)
    insertion_code: str = Field(default="", max_length=1)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.model_id,
            self.chain_id,
            self.residue_name.upper(),
            self.residue_number,
            self.insertion_code,
        )


class ReceptorBounds(DockingWorkflowModel):
    minimum_x: float
    maximum_x: float
    minimum_y: float
    maximum_y: float
    minimum_z: float
    maximum_z: float

    @model_validator(mode="after")
    def ordered_axes(self) -> ReceptorBounds:
        if (
            self.minimum_x > self.maximum_x
            or self.minimum_y > self.maximum_y
            or self.minimum_z > self.maximum_z
        ):
            raise ValueError("受体坐标边界顺序无效。")
        return self

    def contains(
        self,
        point: tuple[float, float, float],
        *,
        margin: float = 0.0,
    ) -> bool:
        x, y, z = point
        return (
            self.minimum_x - margin <= x <= self.maximum_x + margin
            and self.minimum_y - margin <= y <= self.maximum_y + margin
            and self.minimum_z - margin <= z <= self.maximum_z + margin
        )


class ReceptorIdentity(DockingWorkflowModel):
    """Canonical receptor identity bound to one raw RCSB structure payload."""

    pdb_id: str = Field(pattern=_PDB_ID_PATTERN)
    ncbi_taxid: int = Field(ge=1)
    target_name: str = Field(min_length=1)
    organism: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    raw_structure_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    uniprot_ids: tuple[str, ...] = ()

    @field_validator("pdb_id", mode="before")
    @classmethod
    def normalize_pdb_id(cls, value: object) -> str:
        return str(value).strip().upper()

    @field_validator("source_url")
    @classmethod
    def source_must_be_rcsb_https(cls, value: str) -> str:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme.casefold() != "https" or not (
            hostname == "rcsb.org" or hostname.endswith(".rcsb.org")
        ):
            raise ValueError("受体来源必须是可追溯的 RCSB HTTPS 地址。")
        return value

    @field_validator("uniprot_ids", mode="before")
    @classmethod
    def normalize_uniprot_ids(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        normalized = tuple(
            dict.fromkeys(str(item).strip().upper() for item in value)  # type: ignore[arg-type]
        )
        if any(
            not re.fullmatch(_UNIPROT_ID_PATTERN, item)
            for item in normalized
        ):
            raise ValueError("UniProt ID 格式无效。")
        return normalized


class LigandIdentity(DockingWorkflowModel):
    """Canonical PubChem identity or an explicit user-controlled namespace."""

    namespace: Literal["pubchem", "user"]
    structure_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    pubchem_cid: int | None = Field(default=None, ge=1)
    inchikey: str | None = Field(
        default=None,
        min_length=27,
        max_length=27,
        pattern=_INCHIKEY_PATTERN,
    )
    user_namespace: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$",
    )
    user_accession: str | None = Field(
        default=None,
        max_length=64,
        pattern=_SAFE_ID_PATTERN,
    )
    source_url: str | None = None
    source_revision: str = Field(min_length=1)

    @model_validator(mode="after")
    def namespace_fields_match(self) -> LigandIdentity:
        if self.namespace == "pubchem":
            if self.pubchem_cid is None or self.inchikey is None:
                raise ValueError("PubChem 配体必须同时记录 CID 和 InChIKey。")
            if self.user_namespace is not None or self.user_accession is not None:
                raise ValueError("PubChem 配体不能混入 user namespace 字段。")
            if self.source_url is None:
                raise ValueError("PubChem 配体必须记录来源 URL。")
            parsed = urlparse(self.source_url)
            hostname = (parsed.hostname or "").casefold()
            if parsed.scheme.casefold() != "https" or not (
                hostname == "pubchem.ncbi.nlm.nih.gov"
                or hostname.endswith(".pubchem.ncbi.nlm.nih.gov")
            ):
                raise ValueError("PubChem 配体来源必须是 PubChem HTTPS 地址。")
        else:
            if self.user_namespace is None or self.user_accession is None:
                raise ValueError("用户配体必须记录 namespace 和 accession。")
            if self.pubchem_cid is not None or self.inchikey is not None:
                raise ValueError("用户命名空间配体不能冒用 PubChem 身份字段。")
        return self

    @property
    def canonical_accession(self) -> str:
        if self.namespace == "pubchem":
            return f"PubChem:CID{self.pubchem_cid}:{self.inchikey}"
        return f"{self.user_namespace}:{self.user_accession}"


class ReceptorPreparationAudit(DockingWorkflowModel):
    method: Literal["external_tool", "user_provided"]
    tool: str = Field(min_length=1)
    version: str = Field(min_length=1)
    arguments: tuple[str, ...] = ()
    executable_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def external_tool_requires_hash(self) -> ReceptorPreparationAudit:
        if self.method == "external_tool" and self.executable_sha256 is None:
            raise ValueError("外部受体准备工具必须记录可执行文件 SHA-256。")
        if self.method == "user_provided" and self.executable_sha256 is not None:
            raise ValueError("用户提供的受体 PDBQT 不能伪造工具可执行文件哈希。")
        return self


class ReceptorQCResult(DockingWorkflowModel):
    """Read-only structural checks performed before receptor preparation."""

    filename: str = Field(min_length=1)
    structure_format: Literal["pdb", "mmcif"]
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    size_bytes: int = Field(ge=1)
    atom_count: int = Field(ge=0)
    polymer_atom_count: int = Field(ge=0)
    hetero_atom_count: int = Field(ge=0)
    water_atom_count: int = Field(ge=0)
    model_count: int = Field(ge=0)
    model_ids: tuple[str, ...]
    chains: tuple[str, ...]
    alternate_location_atom_count: int = Field(ge=0)
    alternate_locations: tuple[str, ...]
    water_residues: tuple[ResidueIdentity, ...]
    heterogen_residues: tuple[ResidueIdentity, ...]
    metal_residues: tuple[ResidueIdentity, ...]
    bounds: ReceptorBounds | None = None
    malformed_coordinate_count: int = Field(ge=0)
    blocking_issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.blocking_issues


class DockingPocket(DockingWorkflowModel):
    """A user-confirmed rectangular Vina search region."""

    center_x: float
    center_y: float
    center_z: float
    size_x: float = Field(gt=0, le=60)
    size_y: float = Field(gt=0, le=60)
    size_z: float = Field(gt=0, le=60)
    basis_type: Literal["co_crystal", "residue_selection", "manual"]
    basis_residues: tuple[ResidueIdentity, ...] = ()
    selection_basis: str = Field(min_length=1)
    source_structure_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def evidence_based_pockets_need_residues(self) -> DockingPocket:
        if (
            self.basis_type in {"co_crystal", "residue_selection"}
            and not self.basis_residues
        ):
            raise ValueError("共晶或残基选择口袋必须记录依据残基。")
        return self


class ReceptorApproval(DockingWorkflowModel):
    """Immutable approval binding scientific choices to exact receptor files."""

    identity: ReceptorIdentity
    receptor_structure_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    receptor_pdbqt_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    selected_receptor_pdb: bytes = Field(min_length=1)
    selected_receptor_pdb_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    selected_model: str = Field(min_length=1)
    selected_chains: tuple[str, ...] = Field(min_length=1)
    alternate_location_policy: Literal[
        "not_present",
        "highest_occupancy",
        "explicit",
    ]
    selected_alternate_locations: tuple[str, ...] = ()
    water_policy: Literal["not_present", "remove_all", "retain_explicit"]
    retained_waters: tuple[ResidueIdentity, ...] = ()
    heterogen_policy: Literal["not_present", "remove_all", "retain_explicit"]
    retained_heterogens: tuple[ResidueIdentity, ...] = ()
    metal_policy: Literal["not_present", "remove_all", "retain_explicit"]
    retained_metals: tuple[ResidueIdentity, ...] = ()
    preparation_audit: ReceptorPreparationAudit
    heavy_atom_match_fraction: float = Field(ge=0.9, le=1.0)
    maximum_heavy_atom_coordinate_delta: float = Field(ge=0, le=0.5)
    pocket: DockingPocket
    reviewer: str = Field(min_length=1)
    confirmed: Literal[True] = True
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("受体人工确认时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def immutable_bindings_are_consistent(self) -> ReceptorApproval:
        if self.identity.raw_structure_sha256 != self.receptor_structure_sha256:
            raise ValueError("受体身份与人工审批的原始结构 SHA-256 不一致。")
        if _sha256(self.selected_receptor_pdb) != self.selected_receptor_pdb_sha256:
            raise ValueError("人工选择受体 PDB 内容 SHA-256 不一致。")
        if self.pocket.source_structure_sha256 != self.receptor_structure_sha256:
            raise ValueError("对接口袋未绑定人工审批的原始受体结构。")
        if len(self.selected_chains) != len(set(self.selected_chains)):
            raise ValueError("人工审批的受体链不能重复。")
        if any(
            chain != _BLANK_CHAIN
            and (
                len(chain) != 1
                or not chain.isascii()
                or not chain.isalnum()
            )
            for chain in self.selected_chains
        ):
            raise ValueError("人工审批的受体链必须是单字符 ASCII 链 ID。")
        if self.alternate_location_policy == "not_present":
            if self.selected_alternate_locations:
                raise ValueError("无 alternate location 时不能记录选择构象。")
        elif self.alternate_location_policy == "explicit":
            if not self.selected_alternate_locations:
                raise ValueError("explicit alternate location 必须记录选择构象。")
        elif self.selected_alternate_locations:
            raise ValueError("highest_occupancy 策略不能混入显式构象清单。")
        for label, policy, retained in (
            ("水分子", self.water_policy, self.retained_waters),
            ("异源物", self.heterogen_policy, self.retained_heterogens),
            ("金属", self.metal_policy, self.retained_metals),
        ):
            if len(retained) != len(_residue_keys(retained)):
                raise ValueError(f"{label}保留清单不能包含重复残基。")
            if policy in {"not_present", "remove_all"} and retained:
                raise ValueError(f"{label}策略为 {policy} 时不能保留残基。")
            if policy == "retain_explicit" and not retained:
                raise ValueError(f"{label} retain_explicit 策略必须列出残基。")
        return self


class DockingRunSettings(DockingWorkflowModel):
    exhaustiveness: int = Field(default=8, ge=1)
    num_modes: int = Field(default=9, ge=1)
    energy_range: float = Field(default=3.0, gt=0)


class LigandBatchItem(DockingWorkflowModel):
    ligand_id: str = Field(min_length=1, max_length=64, pattern=_SAFE_ID_PATTERN)
    compound_name: str = Field(min_length=1)
    identity: LigandIdentity
    filename: str = Field(min_length=1)
    input_format: str
    original_payload: bytes = Field(min_length=1)

    @field_validator("input_format", mode="before")
    @classmethod
    def normalize_input_format(cls, value: object) -> str:
        normalized = str(value).strip().casefold().removeprefix(".")
        if normalized not in _LIGAND_FORMATS:
            allowed = "、".join(sorted(_LIGAND_FORMATS))
            raise ValueError(f"不支持的配体格式；仅允许：{allowed}。")
        return normalized

    @model_validator(mode="after")
    def payload_matches_identity(self) -> LigandBatchItem:
        if _sha256(self.original_payload) != self.identity.structure_sha256:
            raise ValueError("配体原始文件 SHA-256 与 LigandIdentity 不一致。")
        return self


class LigandPreparationRecord(DockingWorkflowModel):
    ligand_id: str
    compound_name: str
    identity: LigandIdentity
    filename: str
    input_format: str
    original_payload: bytes
    original_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: Literal["succeeded", "failed"]
    prepared_pdbqt: bytes | None = None
    prepared_pdbqt_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    preparation_metadata: dict[str, object] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def payloads_match_status(self) -> LigandPreparationRecord:
        if _sha256(self.original_payload) != self.original_sha256:
            raise ValueError("配体准备记录的原始文件 SHA-256 不一致。")
        if self.identity.structure_sha256 != self.original_sha256:
            raise ValueError("配体准备记录与 LigandIdentity SHA-256 不一致。")
        if self.status == "succeeded":
            if self.prepared_pdbqt is None or self.prepared_pdbqt_sha256 is None:
                raise ValueError("成功的配体准备必须保留 PDBQT 及其 SHA-256。")
            if _sha256(self.prepared_pdbqt) != self.prepared_pdbqt_sha256:
                raise ValueError("准备后配体 PDBQT SHA-256 不一致。")
            if self.error is not None:
                raise ValueError("成功的配体准备不能包含错误。")
        else:
            if self.error is None:
                raise ValueError("失败的配体准备必须记录错误。")
            if self.prepared_pdbqt is not None or self.prepared_pdbqt_sha256 is not None:
                raise ValueError("失败的配体准备不能包含伪造的成功产物。")
        return self


class DockingAttempt(DockingWorkflowModel):
    ligand_id: str
    seed: int
    task_id: str = Field(min_length=1, max_length=64, pattern=_SAFE_ID_PATTERN)
    status: Literal["succeeded", "failed", "skipped"]
    score_label: Literal["Vina 预测评分"] = "Vina 预测评分"
    manifest: VinaTaskManifest | None = None
    docking_run: VinaDockingRun | None = None
    execution_metadata: VinaLocalExecutionMetadata | None = None
    bound_log: bytes | None = None
    output_pdbqt: bytes | None = None
    error: str | None = None

    @model_validator(mode="after")
    def artifacts_match_status(self) -> DockingAttempt:
        if self.status == "succeeded":
            if (
                self.manifest is None
                or self.docking_run is None
                or self.execution_metadata is None
                or self.bound_log is None
                or self.output_pdbqt is None
            ):
                raise ValueError("成功的 Vina 尝试必须保留清单、分数、日志和构象。")
            if self.error is not None:
                raise ValueError("成功的 Vina 尝试不能包含错误。")
            if (
                self.manifest.task_id != self.task_id
                or self.manifest.parameters.seed != self.seed
            ):
                raise ValueError("成功的 Vina 尝试与任务 ID/seed 不一致。")
        else:
            if self.error is None:
                raise ValueError("未成功的 Vina 尝试必须记录原因。")
            if any(
                item is not None
                for item in (
                    self.docking_run,
                    self.execution_metadata,
                    self.bound_log,
                    self.output_pdbqt,
                )
            ):
                raise ValueError("未成功的 Vina 尝试不能包含成功执行产物。")
            if self.status == "skipped" and self.manifest is not None:
                raise ValueError("跳过的 Vina 尝试不能包含未执行的任务清单。")
        return self


class DockingStabilitySummary(DockingWorkflowModel):
    ligand_id: str
    requested_seeds: tuple[int, ...] = Field(min_length=1)
    successful_seed_count: int = Field(ge=0)
    failed_or_skipped_seed_count: int = Field(ge=0)
    best_scores_kcal_mol: tuple[float, ...]
    minimum_score_kcal_mol: float | None = None
    maximum_score_kcal_mol: float | None = None
    mean_score_kcal_mol: float | None = None
    median_score_kcal_mol: float | None = None
    population_sd_kcal_mol: float | None = Field(default=None, ge=0)
    score_range_kcal_mol: float | None = Field(default=None, ge=0)
    assessment: Literal[
        "unavailable",
        "insufficient_replicates",
        "descriptive_only",
    ]
    cross_seed_pose_rmsd_available: Literal[False] = False
    note: str = (
        "跨 seed 的 Vina 预测评分仅作描述性稳定性摘要；"
        "未进行构象原子映射与结构对齐，因此不报告跨 seed 构象 RMSD。"
    )


class DockingBatchResult(DockingWorkflowModel):
    batch_id: str = Field(min_length=1, max_length=64, pattern=_SAFE_ID_PATTERN)
    receptor_original_filename: str = Field(min_length=1)
    receptor_original_payload: bytes = Field(min_length=1)
    receptor_pdbqt: bytes = Field(min_length=1)
    receptor_qc: ReceptorQCResult
    receptor_approval: ReceptorApproval
    preparations: tuple[LigandPreparationRecord, ...] = Field(min_length=1)
    attempts: tuple[DockingAttempt, ...] = Field(min_length=1)
    stability: tuple[DockingStabilitySummary, ...] = Field(min_length=1)


class OptionalExternalToolStatus(DockingWorkflowModel):
    tool: str = Field(min_length=1)
    available: bool
    executable_path: str | None = None
    version_output: str | None = None
    executable_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason: str | None = None

    @model_validator(mode="after")
    def details_match_availability(self) -> OptionalExternalToolStatus:
        if self.available:
            if (
                self.executable_path is None
                or self.version_output is None
                or self.executable_sha256 is None
            ):
                raise ValueError("可用外部工具必须记录路径、版本输出和 SHA-256。")
            if self.reason is not None:
                raise ValueError("可用外部工具不能包含不可用原因。")
        elif self.reason is None:
            raise ValueError("不可用外部工具必须记录原因。")
        return self


class LigandPreparer(Protocol):
    def __call__(
        self,
        ligand: LigandBatchItem,
    ) -> bytes | OpenBabelPreparationArtifacts: ...


class VinaExecutor(Protocol):
    def __call__(
        self,
        manifest: VinaTaskManifest,
        ligand_pdbqt: bytes,
        receptor_pdbqt: bytes,
    ) -> VinaExecutionArtifacts: ...


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _payload_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bounded_error(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}".strip()
    return value[-_MAX_ERROR_CHARACTERS:]


def _normalize_chain(value: str) -> str:
    normalized = value.strip()
    if normalized in {"", ".", "?"}:
        return _BLANK_CHAIN
    return normalized


@dataclass(frozen=True)
class _StructureAtom:
    record: Literal["ATOM", "HETATM"]
    model_id: str
    serial: str
    atom_name: str
    residue_name: str
    chain_id: str
    residue_number: str
    insertion_code: str
    alternate_location: str
    occupancy: float
    x: float
    y: float
    z: float
    element: str

    @property
    def residue(self) -> ResidueIdentity:
        return ResidueIdentity(
            model_id=self.model_id,
            chain_id=self.chain_id,
            residue_name=self.residue_name,
            residue_number=self.residue_number,
            insertion_code=self.insertion_code,
        )

    @property
    def correspondence_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.chain_id,
            self.residue_name.upper(),
            self.residue_number,
            self.insertion_code,
            self.atom_name.upper(),
        )

    @property
    def is_hydrogen(self) -> bool:
        return self.element.upper() in {"H", "HD", "HS"}

    @property
    def coordinates(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


def _infer_element(atom_name: str, explicit: str) -> str:
    value = "".join(character for character in explicit if character.isalpha())
    if value:
        return value[:2].upper()
    stripped = atom_name.strip()
    letters = "".join(character for character in stripped if character.isalpha())
    if not letters:
        return ""
    if len(letters) >= 2 and letters[:2].upper() in _METAL_ELEMENTS:
        return letters[:2].upper()
    return letters[0].upper()


def _pdb_structure_atoms(text: str) -> tuple[_StructureAtom, ...]:
    atoms: list[_StructureAtom] = []
    current_model = "1"
    for line in text.splitlines():
        record = line[:6].strip().upper()
        if record == "MODEL":
            parts = line.split()
            current_model = (
                line[10:14].strip()
                or (parts[1] if len(parts) > 1 else "")
                or str(len({atom.model_id for atom in atoms}) + 1)
            )
            continue
        if record not in {"ATOM", "HETATM"}:
            continue
        try:
            serial = line[6:11].strip() or str(len(atoms) + 1)
            atom_name = line[12:16].strip()
            alternate_location = line[16:17].strip()
            residue_name = line[17:20].strip()
            chain_id = _normalize_chain(line[21:22])
            residue_number = line[22:26].strip()
            insertion_code = line[26:27].strip()
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
            occupancy_text = line[54:60].strip()
            occupancy = float(occupancy_text) if occupancy_text else 1.0
            element = _infer_element(
                atom_name,
                line[76:78].strip() if len(line) >= 78 else line.split()[-1],
            )
            if (
                not atom_name
                or not residue_name
                or not residue_number
                or not all(
                    math.isfinite(value) for value in (x, y, z, occupancy)
                )
            ):
                raise ValueError
        except (IndexError, ValueError):
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                serial = fields[1]
                atom_name = fields[2]
                residue_name = fields[3]
                if re.fullmatch(r"[-+]?[0-9]+[A-Za-z]?", fields[4]):
                    chain_id = _BLANK_CHAIN
                    residue_number = fields[4]
                    coordinate_start = 5
                else:
                    chain_id = _normalize_chain(fields[4])
                    residue_number = fields[5]
                    coordinate_start = 6
                x, y, z = (
                    float(fields[coordinate_start]),
                    float(fields[coordinate_start + 1]),
                    float(fields[coordinate_start + 2]),
                )
                alternate_location = ""
                insertion_code = ""
                occupancy = 1.0
                element = _infer_element(atom_name, fields[-1])
                if not all(math.isfinite(value) for value in (x, y, z)):
                    raise ValueError
            except (IndexError, ValueError):
                continue
        atoms.append(
            _StructureAtom(
                record=record,  # type: ignore[arg-type]
                model_id=current_model,
                serial=serial,
                atom_name=atom_name,
                residue_name=residue_name.upper(),
                chain_id=chain_id,
                residue_number=residue_number,
                insertion_code=insertion_code,
                alternate_location=alternate_location,
                occupancy=occupancy,
                x=x,
                y=y,
                z=z,
                element=element,
            )
        )
    return tuple(atoms)


def _mmcif_structure_atoms(text: str) -> tuple[_StructureAtom, ...]:
    headers, rows = _atom_site_rows(text)
    index = {name.casefold(): offset for offset, name in enumerate(headers)}

    def column(*names: str) -> int | None:
        return next(
            (index[name.casefold()] for name in names if name.casefold() in index),
            None,
        )

    group = column("_atom_site.group_PDB")
    atom_name = column("_atom_site.auth_atom_id", "_atom_site.label_atom_id")
    residue_name = column("_atom_site.auth_comp_id", "_atom_site.label_comp_id")
    chain = column("_atom_site.auth_asym_id", "_atom_site.label_asym_id")
    residue_number = column("_atom_site.auth_seq_id", "_atom_site.label_seq_id")
    insertion = column("_atom_site.pdbx_PDB_ins_code")
    alternate = column("_atom_site.label_alt_id", "_atom_site.auth_alt_id")
    occupancy = column("_atom_site.occupancy")
    model = column("_atom_site.pdbx_PDB_model_num")
    serial = column("_atom_site.id")
    element = column("_atom_site.type_symbol")
    x_col = column("_atom_site.Cartn_x")
    y_col = column("_atom_site.Cartn_y")
    z_col = column("_atom_site.Cartn_z")
    required = (group, atom_name, residue_name, x_col, y_col, z_col)
    if any(item is None for item in required):
        return ()

    atoms: list[_StructureAtom] = []
    for row in rows:
        try:
            record = row[group].upper()  # type: ignore[index]
            if record not in {"ATOM", "HETATM"}:
                continue
            x = float(row[x_col])  # type: ignore[index]
            y = float(row[y_col])  # type: ignore[index]
            z = float(row[z_col])  # type: ignore[index]
            active_atom_name = row[atom_name]  # type: ignore[index]
            active_residue_name = row[residue_name]  # type: ignore[index]
            active_residue_number = (
                row[residue_number] if residue_number is not None else "1"
            )
            if active_residue_number in {"", ".", "?"}:
                active_residue_number = "1"
            active_occupancy = (
                float(row[occupancy])
                if occupancy is not None and row[occupancy] not in {"", ".", "?"}
                else 1.0
            )
            if not all(
                math.isfinite(value) for value in (x, y, z, active_occupancy)
            ):
                raise ValueError
        except (IndexError, ValueError):
            continue
        atoms.append(
            _StructureAtom(
                record=record,  # type: ignore[arg-type]
                model_id=(
                    row[model]
                    if model is not None and row[model] not in {"", ".", "?"}
                    else "1"
                ),
                serial=row[serial] if serial is not None else str(len(atoms) + 1),
                atom_name=active_atom_name,
                residue_name=active_residue_name.upper(),
                chain_id=(
                    _normalize_chain(row[chain])
                    if chain is not None
                    else _BLANK_CHAIN
                ),
                residue_number=active_residue_number,
                insertion_code=(
                    row[insertion]
                    if insertion is not None
                    and row[insertion] not in {"", ".", "?"}
                    else ""
                ),
                alternate_location=(
                    row[alternate]
                    if alternate is not None
                    and row[alternate] not in {"", ".", "?"}
                    else ""
                ),
                occupancy=active_occupancy,
                x=x,
                y=y,
                z=z,
                element=_infer_element(
                    active_atom_name,
                    row[element] if element is not None else "",
                ),
            )
        )
    return tuple(atoms)


def _structure_atoms(
    payload: bytes,
    structure_format: Literal["pdb", "mmcif"],
) -> tuple[_StructureAtom, ...]:
    text = payload.decode("utf-8-sig", errors="strict")
    return (
        _pdb_structure_atoms(text)
        if structure_format == "pdb"
        else _mmcif_structure_atoms(text)
    )


def _residue_inventory(
    atoms: Sequence[_StructureAtom],
) -> tuple[
    tuple[ResidueIdentity, ...],
    tuple[ResidueIdentity, ...],
    tuple[ResidueIdentity, ...],
]:
    water: dict[tuple[str, str, str, str, str], ResidueIdentity] = {}
    heterogen: dict[tuple[str, str, str, str, str], ResidueIdentity] = {}
    metal: dict[tuple[str, str, str, str, str], ResidueIdentity] = {}
    for atom in atoms:
        if atom.record != "HETATM":
            continue
        residue = atom.residue
        if atom.residue_name in _WATER_RESIDUES:
            water[residue.key] = residue
        elif atom.element.upper() in _METAL_ELEMENTS:
            metal[residue.key] = residue
        else:
            heterogen[residue.key] = residue
    key = lambda item: item.key
    return (
        tuple(sorted(water.values(), key=key)),
        tuple(sorted(heterogen.values(), key=key)),
        tuple(sorted(metal.values(), key=key)),
    )


def _pdb_fallback_fields(line: str) -> tuple[str, str, tuple[float, float, float]]:
    fields = line.split()
    if len(fields) < 8:
        raise ValueError("PDB 原子行字段不足。")
    residue_name = fields[3]
    if re.fullmatch(r"[-+]?[0-9]+", fields[4]):
        chain = _BLANK_CHAIN
        start = 5
    else:
        chain = _normalize_chain(fields[4])
        start = 6
    coordinates = tuple(float(fields[start + offset]) for offset in range(3))
    return residue_name, chain, coordinates  # type: ignore[return-value]


def _parse_pdb_qc(text: str) -> dict[str, object]:
    atom_count = 0
    polymer_count = 0
    hetero_count = 0
    water_count = 0
    altloc_count = 0
    malformed_count = 0
    chains: set[str] = set()
    explicit_models: set[str] = set()
    atom_models: set[str] = set()
    current_model = "1"

    for line in text.splitlines():
        record = line[:6].strip().upper()
        if record == "MODEL":
            model = line[10:14].strip() or (
                line.split(maxsplit=1)[1] if len(line.split(maxsplit=1)) == 2 else ""
            )
            current_model = model or str(len(explicit_models) + 1)
            explicit_models.add(current_model)
            continue
        if record not in {"ATOM", "HETATM"}:
            continue

        atom_count += 1
        atom_models.add(current_model)
        if record == "ATOM":
            polymer_count += 1
        else:
            hetero_count += 1

        try:
            residue_name = line[17:20].strip()
            chain = _normalize_chain(line[21:22])
            coordinates = (
                float(line[30:38]),
                float(line[38:46]),
                float(line[46:54]),
            )
            if not residue_name or not all(
                math.isfinite(value) for value in coordinates
            ):
                raise ValueError
        except (ValueError, IndexError):
            try:
                residue_name, chain, coordinates = _pdb_fallback_fields(line)
                if not all(math.isfinite(value) for value in coordinates):
                    raise ValueError
            except (ValueError, IndexError):
                malformed_count += 1
                continue

        if record == "ATOM":
            chains.add(chain)
        if residue_name.upper() in _WATER_RESIDUES:
            water_count += 1
        if len(line) > 16 and line[16].strip():
            altloc_count += 1

    model_count = len(explicit_models or atom_models) if atom_count else 0
    return {
        "atom_count": atom_count,
        "polymer_atom_count": polymer_count,
        "hetero_atom_count": hetero_count,
        "water_atom_count": water_count,
        "model_count": model_count,
        "chains": tuple(sorted(chains)),
        "alternate_location_atom_count": altloc_count,
        "malformed_coordinate_count": malformed_count,
    }


def _tokenize_cif_row(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError:
        return line.split()


def _atom_site_rows(text: str) -> tuple[list[str], list[list[str]]]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        if raw_line.strip().casefold() != "loop_":
            continue
        headers: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].lstrip().startswith("_"):
            headers.append(lines[cursor].strip().split()[0])
            cursor += 1
        if not headers or not all(item.startswith("_atom_site.") for item in headers):
            continue

        flat_tokens: list[str] = []
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if (
                not stripped
                or stripped == "#"
                or stripped.casefold() == "loop_"
                or stripped.startswith("_")
                or stripped.casefold().startswith("data_")
            ):
                if stripped == "#":
                    break
                if not stripped:
                    cursor += 1
                    continue
                break
            flat_tokens.extend(_tokenize_cif_row(lines[cursor]))
            cursor += 1
        width = len(headers)
        rows = [
            flat_tokens[offset : offset + width]
            for offset in range(0, len(flat_tokens), width)
            if len(flat_tokens[offset : offset + width]) == width
        ]
        return headers, rows
    return [], []


def _parse_mmcif_qc(text: str) -> dict[str, object]:
    headers, rows = _atom_site_rows(text)
    header_index = {name.casefold(): index for index, name in enumerate(headers)}

    def column(*names: str) -> int | None:
        for name in names:
            if name.casefold() in header_index:
                return header_index[name.casefold()]
        return None

    group_index = column("_atom_site.group_PDB")
    x_index = column("_atom_site.Cartn_x")
    y_index = column("_atom_site.Cartn_y")
    z_index = column("_atom_site.Cartn_z")
    chain_index = column("_atom_site.auth_asym_id", "_atom_site.label_asym_id")
    residue_index = column("_atom_site.auth_comp_id", "_atom_site.label_comp_id")
    altloc_index = column("_atom_site.label_alt_id", "_atom_site.auth_alt_id")
    model_index = column("_atom_site.pdbx_PDB_model_num")

    required = (group_index, x_index, y_index, z_index)
    if not headers or any(value is None for value in required):
        return {
            "atom_count": 0,
            "polymer_atom_count": 0,
            "hetero_atom_count": 0,
            "water_atom_count": 0,
            "model_count": 0,
            "chains": (),
            "alternate_location_atom_count": 0,
            "malformed_coordinate_count": 0,
        }

    atom_count = 0
    polymer_count = 0
    hetero_count = 0
    water_count = 0
    altloc_count = 0
    malformed_count = 0
    chains: set[str] = set()
    models: set[str] = set()

    for row in rows:
        group = row[group_index].upper()  # type: ignore[index]
        if group not in {"ATOM", "HETATM"}:
            continue
        atom_count += 1
        polymer_count += group == "ATOM"
        hetero_count += group == "HETATM"
        try:
            coordinates = (
                float(row[x_index]),  # type: ignore[index]
                float(row[y_index]),  # type: ignore[index]
                float(row[z_index]),  # type: ignore[index]
            )
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError
        except (ValueError, IndexError):
            malformed_count += 1
            continue

        chain = (
            _normalize_chain(row[chain_index])
            if chain_index is not None
            else _BLANK_CHAIN
        )
        if group == "ATOM":
            chains.add(chain)
        residue = row[residue_index] if residue_index is not None else ""
        if residue.upper() in _WATER_RESIDUES:
            water_count += 1
        altloc = row[altloc_index] if altloc_index is not None else "."
        if altloc not in {"", ".", "?"}:
            altloc_count += 1
        model = row[model_index] if model_index is not None else "1"
        models.add(model)

    return {
        "atom_count": atom_count,
        "polymer_atom_count": polymer_count,
        "hetero_atom_count": hetero_count,
        "water_atom_count": water_count,
        "model_count": len(models) if atom_count else 0,
        "chains": tuple(sorted(chains)),
        "alternate_location_atom_count": altloc_count,
        "malformed_coordinate_count": malformed_count,
    }


def _detect_receptor_format(filename: str, text: str) -> Literal["pdb", "mmcif"]:
    suffix = Path(filename).suffix.casefold()
    if suffix in {".cif", ".mmcif"}:
        return "mmcif"
    if suffix in {".ent", ".pdb"}:
        return "pdb"
    if "_atom_site." in text and re.search(r"(?im)^\s*data_", text):
        return "mmcif"
    if re.search(r"(?m)^(?:ATOM  |HETATM|MODEL )", text):
        return "pdb"
    raise ValueError("无法识别受体格式；仅接受 PDB 或 mmCIF。")


def inspect_receptor_structure(
    payload: bytes | str,
    *,
    filename: str,
    structure_format: Literal["pdb", "mmcif"] | None = None,
) -> ReceptorQCResult:
    """Inspect PDB/mmCIF text without silently repairing scientific content."""

    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    if not raw.strip():
        raise ValueError("受体结构为空。")
    if len(raw) > _MAX_RECEPTOR_BYTES:
        raise ValueError("受体结构超过 50 MB，拒绝检查。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("受体结构必须是 UTF-8/ASCII 文本。") from exc

    active_format = structure_format or _detect_receptor_format(filename, text)
    metrics = (
        _parse_pdb_qc(text)
        if active_format == "pdb"
        else _parse_mmcif_qc(text)
    )
    atoms = _structure_atoms(raw, active_format)
    water_residues, heterogen_residues, metal_residues = _residue_inventory(
        atoms
    )
    coordinates = [atom.coordinates for atom in atoms]
    bounds = (
        ReceptorBounds(
            minimum_x=min(item[0] for item in coordinates),
            maximum_x=max(item[0] for item in coordinates),
            minimum_y=min(item[1] for item in coordinates),
            maximum_y=max(item[1] for item in coordinates),
            minimum_z=min(item[2] for item in coordinates),
            maximum_z=max(item[2] for item in coordinates),
        )
        if coordinates
        else None
    )
    model_ids = tuple(sorted({atom.model_id for atom in atoms}))
    alternate_locations = tuple(
        sorted(
            {
                atom.alternate_location
                for atom in atoms
                if atom.alternate_location
            }
        )
    )
    blockers: list[str] = []
    warnings: list[str] = []
    atom_count = int(metrics["atom_count"])
    polymer_count = int(metrics["polymer_atom_count"])
    malformed_count = int(metrics["malformed_coordinate_count"])
    model_count = int(metrics["model_count"])
    altloc_count = int(metrics["alternate_location_atom_count"])

    if atom_count == 0:
        blockers.append("未发现可解析的 ATOM/HETATM 坐标记录。")
    if polymer_count == 0:
        blockers.append("未发现蛋白/聚合物 ATOM 记录。")
    if malformed_count:
        blockers.append(f"有 {malformed_count} 条原子记录的坐标无效。")
    if model_count > 1:
        warnings.append(
            f"结构包含 {model_count} 个模型；必须人工选择一个模型后再准备受体。"
        )
    if altloc_count:
        warnings.append(
            f"检测到 {altloc_count} 个带 alternate location 的原子；"
            "必须记录采用的构象。"
        )
    if int(metrics["water_atom_count"]):
        warnings.append("结构含水分子；保留或删除必须由用户确认并记录。")
    if heterogen_residues:
        warnings.append(
            f"结构含 {len(heterogen_residues)} 个非水异源残基；"
            "保留或删除必须由用户确认并记录。"
        )
    if metal_residues:
        warnings.append(
            f"结构含 {len(metal_residues)} 个金属残基；"
            "保留或删除必须由用户确认并记录。"
        )
    if not metrics["chains"]:
        blockers.append("未识别出可供人工选择的链。")
    if metrics["atom_count"] and len(atoms) != metrics["atom_count"]:
        blockers.append("结构原子记录未被完整解析，拒绝继续准备受体。")

    return ReceptorQCResult(
        filename=filename,
        structure_format=active_format,
        sha256=_sha256(raw),
        size_bytes=len(raw),
        model_ids=model_ids,
        alternate_locations=alternate_locations,
        water_residues=water_residues,
        heterogen_residues=heterogen_residues,
        metal_residues=metal_residues,
        bounds=bounds,
        blocking_issues=tuple(blockers),
        warnings=tuple(warnings),
        **metrics,
    )


def _pdb_like_chains(payload: bytes) -> tuple[str, ...]:
    text = payload.decode("utf-8", errors="replace")
    chains: set[str] = set()
    for line in text.splitlines():
        if line[:6].strip().upper() not in {"ATOM", "HETATM"}:
            continue
        chain = _normalize_chain(line[21:22] if len(line) > 21 else "")
        if chain == _BLANK_CHAIN:
            try:
                _, chain, _ = _pdb_fallback_fields(line)
            except (ValueError, IndexError):
                pass
        chains.add(chain)
    return tuple(sorted(chains))


def _residue_keys(
    residues: Sequence[ResidueIdentity],
) -> set[tuple[str, str, str, str, str]]:
    return {item.key for item in residues}


def _validate_residue_policy(
    *,
    label: str,
    inventory: Sequence[ResidueIdentity],
    policy: str,
    retained: Sequence[ResidueIdentity],
) -> None:
    inventory_keys = _residue_keys(inventory)
    retained_keys = _residue_keys(retained)
    if not inventory:
        if policy != "not_present" or retained:
            raise ValueError(f"{label}不存在时必须选择 not_present 且不能保留残基。")
        return
    if policy == "not_present":
        raise ValueError(f"{label}已存在，不能选择 not_present。")
    unknown = retained_keys - inventory_keys
    if unknown:
        raise ValueError(f"{label}保留清单包含 QC 中不存在的残基。")
    if policy == "remove_all" and retained:
        raise ValueError(f"{label}选择 remove_all 时不能保留残基。")
    if policy == "retain_explicit" and not retained:
        raise ValueError(f"{label}选择 retain_explicit 时必须列出保留残基。")


def _selected_original_atoms(
    atoms: Sequence[_StructureAtom],
    *,
    selected_model: str,
    selected_chains: Sequence[str],
    alternate_location_policy: str,
    selected_alternate_locations: Sequence[str],
    retained_waters: Sequence[ResidueIdentity],
    retained_heterogens: Sequence[ResidueIdentity],
    retained_metals: Sequence[ResidueIdentity],
) -> tuple[_StructureAtom, ...]:
    selected_chain_set = set(selected_chains)
    retained_keys = (
        _residue_keys(retained_waters)
        | _residue_keys(retained_heterogens)
        | _residue_keys(retained_metals)
    )
    candidates = [
        atom
        for atom in atoms
        if atom.model_id == selected_model
        and (
            (atom.record == "ATOM" and atom.chain_id in selected_chain_set)
            or (atom.record == "HETATM" and atom.residue.key in retained_keys)
        )
    ]
    if alternate_location_policy == "explicit":
        allowed_altlocs = set(selected_alternate_locations)
        candidates = [
            atom
            for atom in candidates
            if not atom.alternate_location
            or atom.alternate_location in allowed_altlocs
        ]
    elif alternate_location_policy == "highest_occupancy":
        grouped: dict[
            tuple[str, str, str, str, str, str],
            _StructureAtom,
        ] = {}
        for atom in candidates:
            group_key = (
                atom.record,
                atom.chain_id,
                atom.residue_name,
                atom.residue_number,
                atom.insertion_code,
                atom.atom_name.upper(),
            )
            previous = grouped.get(group_key)
            if previous is None or (
                atom.occupancy,
                atom.alternate_location == "",
                atom.alternate_location,
            ) > (
                previous.occupancy,
                previous.alternate_location == "",
                previous.alternate_location,
            ):
                grouped[group_key] = atom
        candidates = list(grouped.values())
    return tuple(candidates)


def _atoms_to_pdb(atoms: Sequence[_StructureAtom]) -> bytes:
    lines: list[str] = []
    for serial, atom in enumerate(atoms, start=1):
        if serial > 99999:
            raise ValueError("选择后的受体原子数超过 PDB 格式上限。")
        try:
            residue_number = int(atom.residue_number)
        except ValueError as exc:
            raise ValueError(
                "受体残基编号不能无损写入 PDB；请先提供标准化结构。"
            ) from exc
        if not -999 <= residue_number <= 9999:
            raise ValueError("受体残基编号超出 PDB 格式范围。")
        chain = " " if atom.chain_id == _BLANK_CHAIN else atom.chain_id
        if len(chain) != 1 or not chain.isascii():
            raise ValueError("受体链 ID 不能无损写入单字符 PDB 链字段。")
        if len(atom.atom_name) > 4:
            raise ValueError("受体原子名超过 PDB 四字符上限。")
        element = atom.element[:2].title()
        lines.append(
            f"{atom.record:<6}{serial:5d} {atom.atom_name:>4s} "
            f"{atom.residue_name:>3s} {chain}{residue_number:4d}"
            f"{atom.insertion_code[:1]:1s}   "
            f"{atom.x:8.3f}{atom.y:8.3f}{atom.z:8.3f}"
            f"{atom.occupancy:6.2f}{0.00:6.2f}          {element:>2s}"
        )
    if not lines:
        raise ValueError("人工选择后没有可用于对接的受体原子。")
    return ("\n".join([*lines, "END"]) + "\n").encode("ascii")


def _coordinate_delta(
    left: _StructureAtom,
    right: _StructureAtom,
) -> float:
    return math.sqrt(
        (left.x - right.x) ** 2
        + (left.y - right.y) ** 2
        + (left.z - right.z) ** 2
    )


def _validate_prepared_receptor_mapping(
    selected_atoms: Sequence[_StructureAtom],
    receptor_pdbqt: bytes,
    selected_chains: Sequence[str],
) -> tuple[float, float]:
    prepared_atoms = _pdb_structure_atoms(
        receptor_pdbqt.decode("utf-8", errors="strict")
    )
    if not prepared_atoms:
        raise ValueError("受体 PDBQT 没有可解析的原子记录。")
    prepared_polymer_chains = {
        atom.chain_id for atom in prepared_atoms if atom.record == "ATOM"
    }
    if prepared_polymer_chains != set(selected_chains):
        raise ValueError(
            "受体 PDBQT 的 ATOM 链与人工选择不一致；"
            f"选择={sorted(selected_chains)}，"
            f"PDBQT={sorted(prepared_polymer_chains)}。"
        )

    original_by_key: dict[
        tuple[str, str, str, str, str],
        list[_StructureAtom],
    ] = {}
    for atom in selected_atoms:
        if not atom.is_hydrogen:
            original_by_key.setdefault(atom.correspondence_key, []).append(atom)
    if not original_by_key:
        raise ValueError("选择后的受体没有可核验的重原子。")
    ambiguous_original = [
        key for key, atoms in original_by_key.items() if len(atoms) != 1
    ]
    if ambiguous_original:
        raise ValueError(
            "人工选择结构含无法一一映射到 PDBQT 的重复重原子；"
            "每个 alternate location 原子必须只保留一个构象。"
        )

    matched: set[tuple[str, str, str, str, str]] = set()
    prepared_keys: set[tuple[str, str, str, str, str]] = set()
    maximum_delta = 0.0
    for atom in prepared_atoms:
        if atom.is_hydrogen:
            continue
        if atom.correspondence_key in prepared_keys:
            raise ValueError("受体 PDBQT 含重复重原子标识，无法建立一一映射。")
        prepared_keys.add(atom.correspondence_key)
        candidates = original_by_key.get(atom.correspondence_key, [])
        if not candidates:
            raise ValueError(
                "受体 PDBQT 含有未在人工选择结构中找到的重原子："
                f"{atom.correspondence_key}。"
            )
        delta = min(_coordinate_delta(atom, candidate) for candidate in candidates)
        if delta > 0.5:
            raise ValueError(
                "受体 PDBQT 重原子坐标与人工选择结构偏差超过 0.5 Å："
                f"{atom.correspondence_key}，delta={delta:.3f} Å。"
            )
        matched.add(atom.correspondence_key)
        maximum_delta = max(maximum_delta, delta)
    match_fraction = len(matched) / len(original_by_key)
    if match_fraction < 0.9:
        raise ValueError(
            "受体 PDBQT 与人工选择结构的重原子对应率低于 90%："
            f"{match_fraction:.1%}。"
        )
    return match_fraction, maximum_delta


def approve_receptor_for_docking(
    qc: ReceptorQCResult,
    receptor_original_payload: bytes,
    receptor_pdbqt: bytes,
    *,
    identity: ReceptorIdentity,
    selected_model: str,
    selected_chains: Sequence[str],
    alternate_location_policy: Literal[
        "not_present",
        "highest_occupancy",
        "explicit",
    ],
    water_policy: Literal[
        "not_present",
        "remove_all",
        "retain_explicit",
    ],
    heterogen_policy: Literal[
        "not_present",
        "remove_all",
        "retain_explicit",
    ],
    metal_policy: Literal[
        "not_present",
        "remove_all",
        "retain_explicit",
    ],
    preparation_audit: ReceptorPreparationAudit,
    pocket: DockingPocket,
    reviewer: str,
    user_confirmed: bool,
    selected_alternate_locations: Sequence[str] = (),
    retained_waters: Sequence[ResidueIdentity] = (),
    retained_heterogens: Sequence[ResidueIdentity] = (),
    retained_metals: Sequence[ResidueIdentity] = (),
    confirmed_at: datetime | None = None,
) -> ReceptorApproval:
    """Create the mandatory human gate; never infer chains or pocket."""

    if not user_confirmed:
        raise ValueError("必须由用户明确确认受体链和对接口袋。")
    if qc.blocking_issues:
        raise ValueError("受体 QC 存在阻断项：" + "；".join(qc.blocking_issues))
    original_sha256 = _sha256(receptor_original_payload)
    if original_sha256 != qc.sha256:
        raise ValueError("受体 QC 与当前原始结构内容不一致。")
    if identity.raw_structure_sha256 != original_sha256:
        raise ValueError("ReceptorIdentity 原始结构 SHA-256 不一致。")
    if selected_model not in qc.model_ids:
        raise ValueError("人工选择的模型不在受体 QC 中。")
    normalized_chains = tuple(
        dict.fromkeys(_normalize_chain(value) for value in selected_chains)
    )
    if not normalized_chains:
        raise ValueError("至少选择一个受体链。")
    unknown = sorted(set(normalized_chains) - set(qc.chains))
    if unknown:
        raise ValueError("选择了 QC 中不存在的链：" + "、".join(unknown))
    if qc.alternate_locations:
        if alternate_location_policy == "not_present":
            raise ValueError("受体含 alternate location，必须记录处理策略。")
        unknown_altlocs = set(selected_alternate_locations) - set(
            qc.alternate_locations
        )
        if unknown_altlocs:
            raise ValueError("选择了 QC 中不存在的 alternate location。")
        if (
            alternate_location_policy == "explicit"
            and not selected_alternate_locations
        ):
            raise ValueError("explicit alternate location 策略必须列出构象。")
    elif (
        alternate_location_policy != "not_present"
        or selected_alternate_locations
    ):
        raise ValueError(
            "受体不含 alternate location，必须选择 not_present。"
        )
    _validate_residue_policy(
        label="水分子",
        inventory=qc.water_residues,
        policy=water_policy,
        retained=retained_waters,
    )
    _validate_residue_policy(
        label="异源物",
        inventory=qc.heterogen_residues,
        policy=heterogen_policy,
        retained=retained_heterogens,
    )
    _validate_residue_policy(
        label="金属",
        inventory=qc.metal_residues,
        policy=metal_policy,
        retained=retained_metals,
    )
    if pocket.source_structure_sha256 != original_sha256:
        raise ValueError("口袋依据没有绑定当前原始受体结构。")
    if qc.bounds is None or not qc.bounds.contains(
        (pocket.center_x, pocket.center_y, pocket.center_z),
        margin=5.0,
    ):
        raise ValueError("口袋中心超出受体坐标边界 5 Å 允许范围。")

    all_atoms = _structure_atoms(receptor_original_payload, qc.structure_format)
    original_residue_keys = {
        atom.residue.key
        for atom in all_atoms
        if (
            atom.model_id == selected_model
            and atom.chain_id in normalized_chains
        )
    }
    if any(
        residue.key not in original_residue_keys
        for residue in pocket.basis_residues
    ):
        raise ValueError("口袋依据残基不属于人工选择的受体模型与链。")
    selected_atoms = _selected_original_atoms(
        all_atoms,
        selected_model=selected_model,
        selected_chains=normalized_chains,
        alternate_location_policy=alternate_location_policy,
        selected_alternate_locations=selected_alternate_locations,
        retained_waters=retained_waters,
        retained_heterogens=retained_heterogens,
        retained_metals=retained_metals,
    )
    selected_pdb = _atoms_to_pdb(selected_atoms)

    receptor_pdbqt_sha256 = validate_pdbqt_bytes(
        receptor_pdbqt,
        role="receptor",
    )
    match_fraction, maximum_delta = _validate_prepared_receptor_mapping(
        selected_atoms,
        receptor_pdbqt,
        normalized_chains,
    )
    return ReceptorApproval(
        identity=identity,
        receptor_structure_sha256=qc.sha256,
        receptor_pdbqt_sha256=receptor_pdbqt_sha256,
        selected_receptor_pdb=selected_pdb,
        selected_receptor_pdb_sha256=_sha256(selected_pdb),
        selected_model=selected_model,
        selected_chains=normalized_chains,
        alternate_location_policy=alternate_location_policy,
        selected_alternate_locations=tuple(selected_alternate_locations),
        water_policy=water_policy,
        retained_waters=tuple(retained_waters),
        heterogen_policy=heterogen_policy,
        retained_heterogens=tuple(retained_heterogens),
        metal_policy=metal_policy,
        retained_metals=tuple(retained_metals),
        preparation_audit=preparation_audit,
        heavy_atom_match_fraction=match_fraction,
        maximum_heavy_atom_coordinate_delta=maximum_delta,
        pocket=pocket,
        reviewer=reviewer,
        confirmed=True,
        confirmed_at=confirmed_at or datetime.now(timezone.utc),
    )


def require_receptor_approval(
    qc: ReceptorQCResult,
    receptor_original_payload: bytes,
    receptor_pdbqt: bytes,
    identity: ReceptorIdentity,
    approval: ReceptorApproval,
) -> None:
    """Reject changed files or chain selections before any Vina call."""

    if qc.blocking_issues:
        raise ValueError("受体 QC 尚未通过。")
    current_qc = inspect_receptor_structure(
        receptor_original_payload,
        filename=qc.filename,
        structure_format=qc.structure_format,
    )
    if current_qc != qc:
        raise ValueError("受体 QC 记录与当前原始结构重算结果不一致。")
    original_sha256 = _sha256(receptor_original_payload)
    if (
        approval.receptor_structure_sha256 != qc.sha256
        or original_sha256 != qc.sha256
    ):
        raise ValueError("受体人工确认不属于当前原始结构。")
    if approval.identity != identity or identity.raw_structure_sha256 != qc.sha256:
        raise ValueError("当前受体身份与人工确认不一致。")
    if approval.selected_model not in qc.model_ids:
        raise ValueError("人工审批的模型不属于当前受体 QC。")
    if not set(approval.selected_chains).issubset(qc.chains):
        raise ValueError("人工审批的链不属于当前受体 QC。")
    if qc.alternate_locations:
        if approval.alternate_location_policy == "not_present":
            raise ValueError("当前受体含 alternate location，但审批未记录处理策略。")
        if not set(approval.selected_alternate_locations).issubset(
            qc.alternate_locations
        ):
            raise ValueError("审批选择了当前受体中不存在的 alternate location。")
    elif (
        approval.alternate_location_policy != "not_present"
        or approval.selected_alternate_locations
    ):
        raise ValueError("当前受体无 alternate location，审批策略不一致。")
    _validate_residue_policy(
        label="水分子",
        inventory=qc.water_residues,
        policy=approval.water_policy,
        retained=approval.retained_waters,
    )
    _validate_residue_policy(
        label="异源物",
        inventory=qc.heterogen_residues,
        policy=approval.heterogen_policy,
        retained=approval.retained_heterogens,
    )
    _validate_residue_policy(
        label="金属",
        inventory=qc.metal_residues,
        policy=approval.metal_policy,
        retained=approval.retained_metals,
    )
    if approval.pocket.source_structure_sha256 != original_sha256:
        raise ValueError("人工审批的口袋未绑定当前原始受体。")
    if qc.bounds is None or not qc.bounds.contains(
        (
            approval.pocket.center_x,
            approval.pocket.center_y,
            approval.pocket.center_z,
        ),
        margin=5.0,
    ):
        raise ValueError("人工审批的口袋中心超出当前受体允许范围。")
    actual_pdbqt_sha256 = validate_pdbqt_bytes(
        receptor_pdbqt,
        role="receptor",
    )
    if actual_pdbqt_sha256 != approval.receptor_pdbqt_sha256:
        raise ValueError("受体 PDBQT 在人工确认后发生变化。")
    all_atoms = _structure_atoms(receptor_original_payload, qc.structure_format)
    original_residue_keys = {
        atom.residue.key
        for atom in all_atoms
        if (
            atom.model_id == approval.selected_model
            and atom.chain_id in approval.selected_chains
        )
    }
    if any(
        residue.key not in original_residue_keys
        for residue in approval.pocket.basis_residues
    ):
        raise ValueError(
            "人工审批的口袋依据残基不属于当前受体模型与所选链。"
        )
    selected_atoms = _selected_original_atoms(
        all_atoms,
        selected_model=approval.selected_model,
        selected_chains=approval.selected_chains,
        alternate_location_policy=approval.alternate_location_policy,
        selected_alternate_locations=approval.selected_alternate_locations,
        retained_waters=approval.retained_waters,
        retained_heterogens=approval.retained_heterogens,
        retained_metals=approval.retained_metals,
    )
    selected_pdb = _atoms_to_pdb(selected_atoms)
    if (
        selected_pdb != approval.selected_receptor_pdb
        or _sha256(selected_pdb) != approval.selected_receptor_pdb_sha256
    ):
        raise ValueError("人工确认后的受体选择产物不一致。")
    match_fraction, maximum_delta = _validate_prepared_receptor_mapping(
        selected_atoms,
        receptor_pdbqt,
        approval.selected_chains,
    )
    if (
        not math.isclose(
            match_fraction,
            approval.heavy_atom_match_fraction,
            abs_tol=1e-12,
        )
        or not math.isclose(
            maximum_delta,
            approval.maximum_heavy_atom_coordinate_delta,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("受体 PDBQT 原子映射审计与人工确认不一致。")


def _preparation_record(
    ligand: LigandBatchItem,
    preparer: LigandPreparer | None,
) -> LigandPreparationRecord:
    original_sha256 = _sha256(ligand.original_payload)
    try:
        metadata: dict[str, object] | None
        if ligand.input_format == "pdbqt":
            prepared = ligand.original_payload
            metadata = {"adapter": "user_provided_pdbqt"}
        else:
            if preparer is None:
                raise ValueError(
                    "非 PDBQT 配体需要显式提供 Open Babel 或 Meeko 准备适配器。"
                )
            produced = preparer(ligand)
            if isinstance(produced, OpenBabelPreparationArtifacts):
                prepared = produced.output_pdbqt
                metadata = produced.metadata.model_dump(mode="json")
            elif isinstance(produced, bytes):
                prepared = produced
                metadata = {"adapter": "injected"}
            else:
                candidate = getattr(produced, "output_pdbqt", None)
                if not isinstance(candidate, bytes):
                    raise TypeError("配体准备适配器没有返回 PDBQT bytes。")
                prepared = candidate
                raw_metadata = getattr(produced, "metadata", None)
                metadata = (
                    raw_metadata.model_dump(mode="json")
                    if isinstance(raw_metadata, BaseModel)
                    else {"adapter": type(produced).__name__}
                )
        prepared_sha256 = validate_pdbqt_bytes(prepared, role="ligand")
        return LigandPreparationRecord(
            ligand_id=ligand.ligand_id,
            compound_name=ligand.compound_name,
            identity=ligand.identity,
            filename=ligand.filename,
            input_format=ligand.input_format,
            original_payload=ligand.original_payload,
            original_sha256=original_sha256,
            status="succeeded",
            prepared_pdbqt=prepared,
            prepared_pdbqt_sha256=prepared_sha256,
            preparation_metadata=metadata,
        )
    except Exception as exc:
        return LigandPreparationRecord(
            ligand_id=ligand.ligand_id,
            compound_name=ligand.compound_name,
            identity=ligand.identity,
            filename=ligand.filename,
            input_format=ligand.input_format,
            original_payload=ligand.original_payload,
            original_sha256=original_sha256,
            status="failed",
            error=_bounded_error(exc),
        )


def _parameters_for_attempt(
    pocket: DockingPocket,
    settings: DockingRunSettings,
    seed: int,
) -> VinaParameters:
    return VinaParameters(
        center_x=pocket.center_x,
        center_y=pocket.center_y,
        center_z=pocket.center_z,
        size_x=pocket.size_x,
        size_y=pocket.size_y,
        size_z=pocket.size_z,
        exhaustiveness=settings.exhaustiveness,
        num_modes=settings.num_modes,
        energy_range=settings.energy_range,
        seed=seed,
    )


def _stability_summary(
    ligand_id: str,
    seeds: tuple[int, ...],
    attempts: Sequence[DockingAttempt],
) -> DockingStabilitySummary:
    scores = tuple(
        attempt.docking_run.best_affinity_kcal_mol
        for attempt in attempts
        if (
            attempt.ligand_id == ligand_id
            and attempt.status == "succeeded"
            and attempt.docking_run is not None
        )
    )
    success_count = len(scores)
    failed_count = len(seeds) - success_count
    if not scores:
        return DockingStabilitySummary(
            ligand_id=ligand_id,
            requested_seeds=seeds,
            successful_seed_count=0,
            failed_or_skipped_seed_count=failed_count,
            best_scores_kcal_mol=(),
            assessment="unavailable",
        )
    minimum = min(scores)
    maximum = max(scores)
    return DockingStabilitySummary(
        ligand_id=ligand_id,
        requested_seeds=seeds,
        successful_seed_count=success_count,
        failed_or_skipped_seed_count=failed_count,
        best_scores_kcal_mol=scores,
        minimum_score_kcal_mol=minimum,
        maximum_score_kcal_mol=maximum,
        mean_score_kcal_mol=statistics.fmean(scores),
        median_score_kcal_mol=statistics.median(scores),
        population_sd_kcal_mol=statistics.pstdev(scores),
        score_range_kcal_mol=maximum - minimum,
        assessment=(
            "insufficient_replicates"
            if success_count == 1
            else "descriptive_only"
        ),
    )


def _bounded_task_id(batch_id: str, ligand_id: str, seed: int) -> str:
    raw = f"{batch_id}-{ligand_id}-seed-{seed}"
    if len(raw) <= 64:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    candidate = f"{batch_id[:19]}-{ligand_id[:19]}-{digest}"
    if len(candidate) > 64 or not re.fullmatch(_SAFE_ID_PATTERN, candidate):
        raise ValueError("无法生成安全且不超过 64 字符的 Vina 任务 ID。")
    return candidate


_VINA_RESULT_REMARK = re.compile(
    r"^\s*REMARK\s+VINA\s+RESULT:\s*"
    r"(?P<score>[+-]?\d+(?:\.\d+)?)\b",
    flags=re.IGNORECASE,
)


def _pdbqt_mode_scores(payload: bytes) -> dict[int, float]:
    text = payload.decode("utf-8", errors="strict")
    has_models = any(line[:5].strip().upper() == "MODEL" for line in text.splitlines())
    current_mode = 1
    scores: dict[int, float] = {}
    for line in text.splitlines():
        if line[:5].strip().upper() == "MODEL":
            parts = line.split()
            try:
                current_mode = int(parts[1])
            except (IndexError, ValueError) as exc:
                raise ValueError("Vina PDBQT MODEL 编号无效。") from exc
        match = _VINA_RESULT_REMARK.match(line)
        if match is None:
            continue
        if current_mode in scores:
            raise ValueError(f"Vina PDBQT mode {current_mode} 含重复评分。")
        scores[current_mode] = float(match.group("score"))
    if not scores:
        raise ValueError("Vina PDBQT 缺少 REMARK VINA RESULT 评分。")
    if not has_models and set(scores) != {1}:
        raise ValueError("单构象 Vina PDBQT 只能包含 mode 1。")
    return scores


def _expected_vina_audit_arguments(
    manifest: VinaTaskManifest,
    executable_path: str,
) -> list[str]:
    parameters = manifest.parameters
    format_number = lambda value: format(value, ".15g")
    return [
        Path(executable_path).name,
        "--receptor",
        "receptor.pdbqt",
        "--ligand",
        "ligand.pdbqt",
        "--center_x",
        format_number(parameters.center_x),
        "--center_y",
        format_number(parameters.center_y),
        "--center_z",
        format_number(parameters.center_z),
        "--size_x",
        format_number(parameters.size_x),
        "--size_y",
        format_number(parameters.size_y),
        "--size_z",
        format_number(parameters.size_z),
        "--exhaustiveness",
        str(parameters.exhaustiveness),
        "--num_modes",
        str(parameters.num_modes),
        "--energy_range",
        format_number(parameters.energy_range),
        "--out",
        "output.pdbqt",
        "--seed",
        str(parameters.seed),
    ]


def _validate_vina_execution_artifacts(
    manifest: VinaTaskManifest,
    artifacts: VinaExecutionArtifacts,
) -> None:
    run = artifacts.docking_run
    if run.manifest != manifest:
        raise ValueError("Vina 适配器返回了其他任务的 manifest。")
    output_sha256 = validate_pdbqt_bytes(
        artifacts.output_pdbqt,
        role="ligand",
        require_single_ligand=False,
    )
    if artifacts.metadata.output_pdbqt_sha256 != output_sha256:
        raise ValueError("Vina metadata 的构象 SHA-256 不一致。")
    audit = run.execution_audit
    if audit is None:
        raise ValueError("本机 Vina 成功结果必须包含 execution audit。")
    if (
        audit.output_pdbqt_sha256 != output_sha256
        or audit.executable_sha256 != artifacts.metadata.executable_sha256
        or audit.executable_version != artifacts.metadata.executable_version
        or audit.arguments != artifacts.metadata.arguments
        or audit.exit_code != artifacts.metadata.exit_code
        or not math.isclose(
            audit.duration_seconds,
            artifacts.metadata.duration_seconds,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("Vina execution metadata 与 execution audit 不一致。")
    if artifacts.metadata.exit_code != 0:
        raise ValueError("成功的 Vina 产物不能记录非零退出码。")
    if (
        artifacts.metadata.executable_version.removeprefix("v")
        != manifest.engine_version.removeprefix("v")
    ):
        raise ValueError("Vina execution audit 的版本与 manifest 不一致。")
    expected_arguments = _expected_vina_audit_arguments(
        manifest,
        artifacts.metadata.executable_path,
    )
    if artifacts.metadata.arguments != expected_arguments:
        raise ValueError(
            "Vina execution audit 的完整参数（含 seed）与 manifest 不一致。"
        )
    if _sha256(artifacts.bound_log) != run.output_source.sha256:
        raise ValueError("Vina bound log 与解析来源 SHA-256 不一致。")
    reparsed = parse_vina_output(
        artifacts.bound_log,
        manifest=manifest,
        output_source=run.output_source,
    )
    if (
        reparsed.poses != run.poses
        or reparsed.best_affinity_kcal_mol != run.best_affinity_kcal_mol
    ):
        raise ValueError("Vina bound log 重解析结果与 docking run 不一致。")

    pose_scores = _pdbqt_mode_scores(artifacts.output_pdbqt)
    parsed_scores = {pose.mode: pose.affinity_kcal_mol for pose in run.poses}
    if set(pose_scores) != set(parsed_scores) or any(
        not math.isclose(pose_scores[mode], parsed_scores[mode], abs_tol=1e-6)
        for mode in parsed_scores
    ):
        raise ValueError("Vina PDBQT pose 评分与 bound log 模式表不一致。")
    seed = manifest.parameters.seed
    if seed is None:
        raise ValueError("批量 Vina 任务必须记录显式 seed。")


def validate_successful_docking_attempt(
    attempt: DockingAttempt,
    preparation: LigandPreparationRecord,
    approval: ReceptorApproval,
) -> None:
    """Revalidate all immutable bindings before visualization or export."""

    if (
        attempt.status != "succeeded"
        or attempt.manifest is None
        or attempt.docking_run is None
        or attempt.execution_metadata is None
        or attempt.bound_log is None
        or attempt.output_pdbqt is None
        or preparation.status != "succeeded"
        or preparation.prepared_pdbqt_sha256 is None
    ):
        raise ValueError("只有完整且成功的 Vina 尝试可以进入后续分析。")
    manifest = attempt.manifest
    if manifest.parameters.seed != attempt.seed:
        raise ValueError("DockingAttempt seed 与 Vina manifest 不一致。")
    if manifest.ligand_source.sha256 != preparation.prepared_pdbqt_sha256:
        raise ValueError("Vina manifest 未绑定当前准备后配体。")
    if manifest.ligand_accession != preparation.identity.canonical_accession:
        raise ValueError("Vina manifest 配体身份与准备记录不一致。")
    if manifest.receptor_source.sha256 != approval.receptor_pdbqt_sha256:
        raise ValueError("Vina manifest 未绑定人工确认的受体 PDBQT。")
    if manifest.receptor_accession != f"PDB:{approval.identity.pdb_id}":
        raise ValueError("Vina manifest 受体 PDB ID 与人工确认不一致。")
    artifacts = VinaExecutionArtifacts(
        docking_run=attempt.docking_run,
        metadata=attempt.execution_metadata,
        bound_log=attempt.bound_log,
        output_pdbqt=attempt.output_pdbqt,
    )
    _validate_vina_execution_artifacts(manifest, artifacts)


def run_docking_batch(
    *,
    batch_id: str,
    ligands: Sequence[LigandBatchItem],
    seeds: Sequence[int],
    receptor_original_filename: str,
    receptor_original_payload: bytes,
    receptor_pdbqt: bytes,
    receptor_qc: ReceptorQCResult,
    receptor_approval: ReceptorApproval,
    receptor_identity: ReceptorIdentity,
    engine_version: str,
    settings: DockingRunSettings,
    ligand_preparer: LigandPreparer | None = None,
    vina_executor: VinaExecutor = execute_vina,
    fail_fast: bool = False,
) -> DockingBatchResult:
    """Run ligands × seeds while preserving every input, output, and failure."""

    if not re.fullmatch(_SAFE_ID_PATTERN, batch_id):
        raise ValueError("批次 ID 只能包含安全的字母、数字、点、下划线和连字符。")
    if not ligands:
        raise ValueError("批量对接至少需要一个配体。")
    ligand_ids = [item.ligand_id for item in ligands]
    if len(ligand_ids) != len(set(ligand_ids)):
        raise ValueError("批量对接的 ligand_id 不能重复。")
    normalized_seeds = tuple(seeds)
    if not normalized_seeds:
        raise ValueError("至少提供一个显式 Vina seed。")
    if any(
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or seed < -(2**31)
        or seed > (2**31 - 1)
        for seed in normalized_seeds
    ):
        raise ValueError("Vina seed 必须是 32 位有符号整数。")
    if len(normalized_seeds) != len(set(normalized_seeds)):
        raise ValueError("Vina seed 不能重复。")
    if _sha256(receptor_original_payload) != receptor_qc.sha256:
        raise ValueError("受体 QC 与当前原始结构内容不一致。")
    require_receptor_approval(
        receptor_qc,
        receptor_original_payload,
        receptor_pdbqt,
        receptor_identity,
        receptor_approval,
    )

    preparations = tuple(
        _preparation_record(ligand, ligand_preparer) for ligand in ligands
    )
    preparation_by_id = {item.ligand_id: item for item in preparations}
    ligand_by_id = {item.ligand_id: item for item in ligands}
    receptor_pdbqt_sha256 = _sha256(receptor_pdbqt)
    receptor_source = SourceProvenance(
        source_name="receptor.pdbqt",
        accession=f"PDB:{receptor_identity.pdb_id}",
        version=(
            f"RCSB-revision:{receptor_identity.revision};"
            "user-confirmed:"
            f"{receptor_approval.receptor_structure_sha256[:12]}"
        ),
        sha256=receptor_pdbqt_sha256,
    )

    attempts: list[DockingAttempt] = []
    for ligand_id in ligand_ids:
        preparation = preparation_by_id[ligand_id]
        ligand = ligand_by_id[ligand_id]
        for seed in normalized_seeds:
            task_id = _bounded_task_id(batch_id, ligand_id, seed)
            if preparation.status != "succeeded":
                attempts.append(
                    DockingAttempt(
                        ligand_id=ligand_id,
                        seed=seed,
                        task_id=task_id,
                        status="skipped",
                        error=preparation.error or "配体准备失败。",
                    )
                )
                continue

            assert preparation.prepared_pdbqt is not None
            assert preparation.prepared_pdbqt_sha256 is not None
            manifest = build_vina_manifest(
                task_id=task_id,
                compound_name=ligand.compound_name,
                ligand_accession=ligand.identity.canonical_accession,
                receptor_name=receptor_identity.target_name,
                receptor_accession=f"PDB:{receptor_identity.pdb_id}",
                receptor_organism=(
                    f"{receptor_identity.organism} "
                    f"(NCBI TaxID {receptor_identity.ncbi_taxid})"
                ),
                ligand_source=SourceProvenance(
                    source_name=f"{ligand_id}.pdbqt",
                    accession=ligand.identity.canonical_accession,
                    version=ligand.identity.source_revision,
                    sha256=preparation.prepared_pdbqt_sha256,
                ),
                receptor_source=receptor_source,
                parameters=_parameters_for_attempt(
                    receptor_approval.pocket,
                    settings,
                    seed,
                ),
                engine_version=engine_version,
            )
            try:
                artifacts = vina_executor(
                    manifest,
                    preparation.prepared_pdbqt,
                    receptor_pdbqt,
                )
                _validate_vina_execution_artifacts(manifest, artifacts)
                attempts.append(
                    DockingAttempt(
                        ligand_id=ligand_id,
                        seed=seed,
                        task_id=task_id,
                        status="succeeded",
                        manifest=manifest,
                        docking_run=artifacts.docking_run,
                        execution_metadata=artifacts.metadata,
                        bound_log=artifacts.bound_log,
                        output_pdbqt=artifacts.output_pdbqt,
                    )
                )
            except Exception as exc:
                if fail_fast:
                    raise
                attempts.append(
                    DockingAttempt(
                        ligand_id=ligand_id,
                        seed=seed,
                        task_id=task_id,
                        status="failed",
                        manifest=manifest,
                        error=_bounded_error(exc),
                    )
                )

    stability = tuple(
        _stability_summary(ligand_id, normalized_seeds, attempts)
        for ligand_id in ligand_ids
    )
    return DockingBatchResult(
        batch_id=batch_id,
        receptor_original_filename=receptor_original_filename,
        receptor_original_payload=receptor_original_payload,
        receptor_pdbqt=receptor_pdbqt,
        receptor_qc=receptor_qc,
        receptor_approval=receptor_approval,
        preparations=preparations,
        attempts=tuple(attempts),
        stability=stability,
    )


def probe_meeko(
    executable_path: str | os.PathLike[str] | None,
    *,
    runner: Runner | None = None,
    timeout_seconds: float = 10.0,
) -> OptionalExternalToolStatus:
    """Verify an explicitly configured Meeko CLI without making it mandatory."""

    if executable_path is None:
        return OptionalExternalToolStatus(
            tool="Meeko",
            available=False,
            reason="未配置 Meeko 可执行文件；Open Babel 或用户提供的 PDBQT 仍可使用。",
        )
    if timeout_seconds <= 0:
        raise ValueError("Meeko 版本检查超时必须大于 0。")
    try:
        path = Path(os.fspath(executable_path).strip().strip('"')).resolve(
            strict=True
        )
        if not path.is_file():
            raise OSError("路径不是文件")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        active_runner = runner or subprocess.run
        completed = active_runner(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
            **(
                {
                    "creationflags": getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )
                }
                if os.name == "nt"
                else {}
            ),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"--version 退出码 {completed.returncode}")
        output = (
            _payload_bytes(completed.stdout)
            + b"\n"
            + _payload_bytes(completed.stderr)
        ).decode("utf-8", errors="replace").strip()
        if not output:
            raise RuntimeError("没有版本输出")
        return OptionalExternalToolStatus(
            tool="Meeko",
            available=True,
            executable_path=str(path),
            version_output=output[-1000:],
            executable_sha256=digest,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return OptionalExternalToolStatus(
            tool="Meeko",
            available=False,
            reason=_bounded_error(exc),
        )


__all__ = [
    "DockingAttempt",
    "DockingBatchResult",
    "DockingPocket",
    "DockingRunSettings",
    "DockingStabilitySummary",
    "LigandBatchItem",
    "LigandIdentity",
    "LigandPreparationRecord",
    "OptionalExternalToolStatus",
    "ReceptorApproval",
    "ReceptorBounds",
    "ReceptorIdentity",
    "ReceptorPreparationAudit",
    "ReceptorQCResult",
    "ResidueIdentity",
    "approve_receptor_for_docking",
    "inspect_receptor_structure",
    "probe_meeko",
    "require_receptor_approval",
    "run_docking_batch",
    "validate_successful_docking_attempt",
]
