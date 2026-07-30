"""Auditable molecular-dynamics manifests and result schemas.

The v0.6 pilot deliberately separates scientific system preparation from
execution.  A manifest can only be created from original chemical sources and
an explicit user review.  PDBQT files may be linked as upstream docking
artifacts, but they are never accepted as the sole receptor or ligand source
for molecular dynamics.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


MD_MANIFEST_VERSION = "md-manifest-v0.6"
MD_RESULT_VERSION = "md-result-v0.6"
MD_ANALYSIS_VERSION = "md-analysis-v0.6"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RECEPTOR_BYTES = 50 * 1024 * 1024
_MAX_LIGAND_BYTES = 25 * 1024 * 1024
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TECHNICAL_SMOKE_PROTOCOL = {
    "replica_count": 1,
    "energy_minimization_max_iterations": 100,
    "integration_steps": 30,
    "timestep_fs": 2.0,
    "report_interval_steps": 5,
    "checkpoint_interval_steps": 10,
    "chunk_steps": 5,
    "walltime_limit_seconds": 300,
}
_RESERVED_METRICS = (
    "protein_backbone_rmsd_nm",
    "ligand_pocket_aligned_rmsd_nm",
    "rmsf_nm",
    "radius_of_gyration_nm",
    "contacts",
    "hydrogen_bond_count",
    "pressure_bar",
    "density_g_ml",
)

_STANDARD_PROTEIN_RESIDUES = {
    "ALA",
    "ARG",
    "ASN",
    "ASP",
    "CYS",
    "GLN",
    "GLU",
    "GLY",
    "HIS",
    "ILE",
    "LEU",
    "LYS",
    "MET",
    "PHE",
    "PRO",
    "SER",
    "THR",
    "TRP",
    "TYR",
    "VAL",
}
_WATER_RESIDUES = {"HOH", "WAT"}
_METAL_ELEMENTS = {
    "AG",
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


class MDModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class MDEvidenceGrade(StrEnum):
    """MD never leaves the computational-prediction evidence layer."""

    COMPUTATIONAL_PREDICTION = "computational_prediction"


class MDPreset(StrEnum):
    TECHNICAL_SMOKE = "technical_smoke"
    EXPLORATORY_REPLICATES = "exploratory_replicates"
    RESEARCH_REVIEW = "research_review"


class MDValidationStatus(StrEnum):
    QC_FAILED = "qc_failed"
    TECHNICAL_SMOKE_PASSED = "technical_smoke_passed"


class MDInputSource(MDModel):
    """Original, content-addressed structure or chemical-identity source."""

    source_name: str = Field(min_length=1)
    accession: str = Field(min_length=1)
    version: str = Field(min_length=1)
    format: str = Field(min_length=1)
    sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    size_bytes: int | None = Field(default=None, ge=1)

    @field_validator("format")
    @classmethod
    def normalize_format(cls, value: str) -> str:
        return value.strip().casefold().lstrip(".")


class MDDockingReference(MDModel):
    """Optional link to the selected upstream docking pose."""

    task_id: str = Field(min_length=1, max_length=64)
    pose_mode: int = Field(ge=1)
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    output_pdbqt_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )


class MDChemistryConfirmation(MDModel):
    """Human gate for chemical choices that cannot be safely inferred."""

    reviewed_by: str = Field(min_length=1)
    confirmed_at: datetime
    receptor_chain_selection: list[str] = Field(min_length=1)
    receptor_protonation_assumption: str = Field(min_length=2)
    ligand_formal_charge: int = Field(ge=-20, le=20)
    ligand_protonation_state: str = Field(min_length=2)
    ligand_tautomer_state: str = Field(min_length=2)
    ligand_stereochemistry: str = Field(min_length=2)

    chemical_identity_confirmed: bool
    receptor_structure_reviewed: bool
    formal_charge_confirmed: bool
    protonation_confirmed: bool
    tautomer_confirmed: bool
    stereochemistry_confirmed: bool
    all_stereocenters_defined: bool
    metals_reviewed: bool
    covalent_links_reviewed: bool
    unknown_residues_reviewed: bool

    metals_present: list[str] = Field(default_factory=list)
    covalent_links_present: list[str] = Field(default_factory=list)
    unknown_residues_present: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)

    @field_validator("confirmed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("化学确认时间必须包含时区。")
        return value

    @field_validator("receptor_chain_selection")
    @classmethod
    def unique_chains(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("受体链选择不能包含空值。")
        if any(len(item) != 1 or item.isspace() for item in normalized):
            raise ValueError("v0.6 PDB 受体链必须是单字符 chain ID。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("受体链选择不能重复。")
        return normalized


class MDDetectedRisks(MDModel):
    """Machine-detected obvious risks retained in the immutable manifest."""

    metal_atoms: list[str] = Field(default_factory=list)
    covalent_records: list[str] = Field(default_factory=list)
    unknown_residues: list[str] = Field(default_factory=list)


class MDForceFieldParameters(MDModel):
    """Only execution settings used by the v0.6 technical smoke.

    Scientific force-field, solvent and ion choices belong to the separately
    persisted preparation manifest and are not inferred here.
    """

    temperature_kelvin: float = Field(default=300.0, gt=0)
    integrator: Literal["LangevinMiddleIntegrator"] = "LangevinMiddleIntegrator"
    friction_per_ps: float = Field(default=1.0, gt=0)


class MDHardwareRequest(MDModel):
    platform: Literal["auto", "CPU", "CUDA", "HIP", "OpenCL"] = "auto"
    device_indices: list[int] = Field(default_factory=list)
    precision: Literal["single", "mixed", "double"] = "mixed"
    gpu_required: bool = False

    @field_validator("device_indices")
    @classmethod
    def unique_device_indices(cls, value: list[int]) -> list[int]:
        if any(item < 0 for item in value):
            raise ValueError("GPU 设备编号不能为负数。")
        if len(value) != len(set(value)):
            raise ValueError("GPU 设备编号不能重复。")
        return value


class MDProtocol(MDModel):
    preset: MDPreset
    replica_count: Literal[1] = 1
    seeds: list[int] = Field(min_length=1, max_length=1)
    energy_minimization_max_iterations: int = Field(ge=1)
    integration_steps: int = Field(ge=1, le=100_000)
    timestep_fs: float = Field(gt=0, le=2)
    report_interval_steps: int = Field(ge=1)
    checkpoint_interval_steps: int = Field(ge=1)
    chunk_steps: int = Field(ge=1, le=10_000)
    walltime_limit_seconds: int = Field(ge=1, le=3600)
    scientific_interpretation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_replicas(self) -> "MDProtocol":
        if self.preset is not MDPreset.TECHNICAL_SMOKE:
            raise ValueError(
                "v0.6 只实现 technical_smoke；探索性重复和科研级 MD "
                "仅保留为后续路线。"
            )
        if len(self.seeds) != self.replica_count:
            raise ValueError("随机种子数量必须与重复数一致。")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("每个 MD 重复必须使用不同的随机种子。")
        if any(seed <= 0 or seed > 2_147_483_647 for seed in self.seeds):
            raise ValueError("MD 随机种子必须位于 1..2147483647。")
        if self.report_interval_steps > self.integration_steps:
            raise ValueError("状态报告间隔不能超过 technical smoke 总步数。")
        if self.checkpoint_interval_steps > self.integration_steps:
            raise ValueError("checkpoint 间隔不能超过 technical smoke 总步数。")
        if self.chunk_steps > self.integration_steps:
            raise ValueError("分块步数不能超过 technical smoke 总步数。")
        return self


class MDTaskManifest(MDModel):
    manifest_version: Literal["md-manifest-v0.6"] = MD_MANIFEST_VERSION
    task_id: str = Field(min_length=1, max_length=64)
    receptor_source: MDInputSource
    ligand_source: MDInputSource
    docking_reference: MDDockingReference | None = None
    chemistry_confirmation: MDChemistryConfirmation
    detected_risks: MDDetectedRisks
    forcefield: MDForceFieldParameters
    protocol: MDProtocol
    hardware_request: MDHardwareRequest
    protocol_approved_by_user: bool = False
    backend: Literal["OpenMM"] = "OpenMM"
    required_backend_modules: list[str] = Field(
        default_factory=lambda: [
            "openmm",
            "pdbfixer",
            "openff.toolkit",
            "openmmforcefields",
        ]
    )
    manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    evidence_grade: MDEvidenceGrade = (
        MDEvidenceGrade.COMPUTATIONAL_PREDICTION
    )

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _SAFE_TASK_ID.fullmatch(value):
            raise ValueError(
                "MD 任务 ID 只能包含不超过 64 位的字母、数字、点、"
                "下划线和连字符。"
            )
        return value

    @model_validator(mode="after")
    def bind_manifest(self) -> "MDTaskManifest":
        if self.detected_risks.metal_atoms:
            raise ValueError("MVP 不支持含金属的受体。")
        if self.detected_risks.covalent_records:
            raise ValueError("MVP 不支持共价配体或未解析共价连接。")
        if self.detected_risks.unknown_residues:
            raise ValueError("MVP 不支持未知或非标准残基。")
        actual_protocol = {
            field: getattr(self.protocol, field)
            for field in _TECHNICAL_SMOKE_PROTOCOL
        }
        if actual_protocol != _TECHNICAL_SMOKE_PROTOCOL:
            raise ValueError(
                "v0.6 technical_smoke 协议必须固定为 1 个重复、100 次以内"
                "最小化、30 步积分、2 fs 步长、5 步报告、10 步 checkpoint、"
                "5 步分块和 300 秒上限。"
            )
        expected = canonical_md_manifest_sha256(self)
        if self.manifest_sha256 is None:
            object.__setattr__(self, "manifest_sha256", expected)
        elif self.manifest_sha256 != expected:
            raise ValueError("MD 任务清单 canonical SHA-256 不一致。")
        return self


class MDTimeSeries(MDModel):
    times_ps: list[float] = Field(min_length=1)
    values: list[float] = Field(min_length=1)
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def matching_lengths(self) -> "MDTimeSeries":
        if len(self.times_ps) != len(self.values):
            raise ValueError("MD 时间序列的时间和值数量必须一致。")
        if any(value < 0 for value in self.times_ps):
            raise ValueError("MD 时间不能为负数。")
        if any(
            later <= earlier
            for earlier, later in zip(self.times_ps, self.times_ps[1:])
        ):
            raise ValueError("MD 时间序列必须严格递增。")
        return self


class MDResidueMetric(MDModel):
    residue_id: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)


class MDContactOccupancy(MDModel):
    contact_id: str = Field(min_length=1)
    contact_type: Literal[
        "hydrogen_bond",
        "salt_bridge",
        "hydrophobic",
        "water_bridge",
        "other",
    ]
    occupancy_fraction: float = Field(ge=0, le=1)
    definition: str = Field(min_length=2)


class MDMetricSummary(MDModel):
    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    mean: float
    standard_deviation: float | None = Field(default=None, ge=0)
    minimum: float
    maximum: float
    sample_count: int = Field(ge=1)


class MDReplicaAnalysis(MDModel):
    replica_index: int = Field(ge=1)
    seed: int = Field(ge=1, le=2_147_483_647)
    qc_passed: bool
    protein_backbone_rmsd_nm: MDTimeSeries | None = None
    ligand_pocket_aligned_rmsd_nm: MDTimeSeries | None = None
    rmsf_nm: list[MDResidueMetric] = Field(default_factory=list)
    radius_of_gyration_nm: MDTimeSeries | None = None
    contacts: list[MDContactOccupancy] = Field(default_factory=list)
    hydrogen_bond_count: MDTimeSeries | None = None
    temperature_kelvin: MDTimeSeries | None = None
    pressure_bar: MDTimeSeries | None = None
    density_g_ml: MDTimeSeries | None = None
    potential_energy_kj_mol: MDTimeSeries | None = None
    warnings: list[str] = Field(default_factory=list)


class MDReplicateSummary(MDModel):
    total_replicas: int = Field(ge=1)
    successful_replicas: int = Field(ge=0)
    convergence_checked: bool = False
    between_replica_consistent: bool = False
    uncertainty_method: str | None = None
    metrics: list[MDMetricSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def successful_not_above_total(self) -> "MDReplicateSummary":
        if self.successful_replicas > self.total_replicas:
            raise ValueError("成功重复数不能超过总重复数。")
        return self


class MDAnalysisResult(MDModel):
    """Reserved analysis schema; it intentionally has no binding-free-energy."""

    analysis_version: Literal["md-analysis-v0.6"] = MD_ANALYSIS_VERSION
    replicas: list[MDReplicaAnalysis] = Field(default_factory=list)
    replicate_summary: MDReplicateSummary
    produced_metrics: list[
        Literal["temperature_kelvin", "potential_energy_kj_mol"]
    ] = Field(default_factory=list)
    reserved_metrics_not_produced: list[str] = Field(
        default_factory=lambda: list(_RESERVED_METRICS)
    )
    free_energy_computed: Literal[False] = False
    limitations: list[str] = Field(
        default_factory=lambda: [
            "MD 势能或蛋白-配体相互作用能不是结合自由能。",
            "轨迹稳定性不能证明真实结合、抗菌作用或药物协同。",
        ]
    )

    @model_validator(mode="after")
    def produced_metrics_are_real(self) -> "MDAnalysisResult":
        actual: set[str] = set()
        for replica in self.replicas:
            reserved_values = {
                "protein_backbone_rmsd_nm": (
                    replica.protein_backbone_rmsd_nm
                ),
                "ligand_pocket_aligned_rmsd_nm": (
                    replica.ligand_pocket_aligned_rmsd_nm
                ),
                "rmsf_nm": replica.rmsf_nm,
                "radius_of_gyration_nm": replica.radius_of_gyration_nm,
                "contacts": replica.contacts,
                "hydrogen_bond_count": replica.hydrogen_bond_count,
                "pressure_bar": replica.pressure_bar,
                "density_g_ml": replica.density_g_ml,
            }
            populated = [
                name for name, value in reserved_values.items() if value
            ]
            if populated:
                raise ValueError(
                    "v0.6 不允许写入尚未实现的科研分析指标："
                    + "、".join(populated)
                )
            if replica.temperature_kelvin is not None:
                actual.add("temperature_kelvin")
            if replica.potential_energy_kj_mol is not None:
                actual.add("potential_energy_kj_mol")
        if set(self.produced_metrics) != actual:
            raise ValueError("produced_metrics 必须与实际非空分析字段完全一致。")
        if self.reserved_metrics_not_produced != list(_RESERVED_METRICS):
            raise ValueError(
                "reserved_metrics_not_produced 必须精确列出 v0.6 未生成指标。"
            )
        summary = self.replicate_summary
        if (
            summary.convergence_checked
            or summary.between_replica_consistent
            or summary.uncertainty_method is not None
            or summary.metrics
        ):
            raise ValueError(
                "v0.6 technical_smoke 不允许声明收敛、重复一致性、不确定性"
                "或科研指标汇总。"
            )
        return self


class MDArtifactReference(MDModel):
    role: Literal[
        "manifest",
        "topology",
        "system",
        "portable_state",
        "checkpoint",
        "trajectory",
        "state_log",
        "analysis",
        "plot",
        "pymol_script",
        "representative_structure",
    ]
    filename: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    size_bytes: int = Field(ge=1)


class MDExecutionAudit(MDModel):
    execution_mode: Literal["openmm_local"]
    backend_version: str = Field(min_length=1)
    package_versions: dict[str, str]
    hardware_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    platform_name: str = Field(min_length=1)
    precision: str = Field(min_length=1)
    platform_properties: dict[str, str] = Field(default_factory=dict)
    selected_device: str | None = None
    driver_version: str | None = None
    forcefield_file_sha256: dict[str, str]
    seeds: list[int] = Field(min_length=1)
    random_seed_assignments: dict[str, int] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def execution_times_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MD 执行时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def completion_after_start(self) -> "MDExecutionAudit":
        if self.completed_at < self.started_at:
            raise ValueError("MD 完成时间不能早于开始时间。")
        return self


class MDRunResult(MDModel):
    result_version: Literal["md-result-v0.6"] = MD_RESULT_VERSION
    manifest: MDTaskManifest
    validation_status: MDValidationStatus
    analysis: MDAnalysisResult
    execution_audit: MDExecutionAudit
    artifacts: list[MDArtifactReference] = Field(default_factory=list)
    attempt_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^attempt-[0-9]{4}$",
    )
    result_manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=_SHA256_PATTERN,
    )
    evidence_grade: MDEvidenceGrade = (
        MDEvidenceGrade.COMPUTATIONAL_PREDICTION
    )

    @model_validator(mode="after")
    def validate_status_claim(self) -> "MDRunResult":
        summary = self.analysis.replicate_summary
        if summary.total_replicas != self.manifest.protocol.replica_count:
            raise ValueError("分析重复数必须与 MD 清单一致。")
        roles = {artifact.role for artifact in self.artifacts}
        if len(roles) != len(self.artifacts):
            raise ValueError("每种 MD 产物角色只能出现一次。")
        required_roles = {
            "manifest",
            "topology",
            "system",
            "portable_state",
            "checkpoint",
            "trajectory",
            "state_log",
            "analysis",
            "pymol_script",
            "representative_structure",
        }
        if roles != required_roles:
            raise ValueError("MD 成功结果必须包含且只能包含规定的产物角色。")
        passed = (
            len(self.analysis.replicas) == 1
            and self.analysis.replicas[0].qc_passed
            and summary.successful_replicas == 1
            and set(self.analysis.produced_metrics)
            == {"temperature_kelvin", "potential_energy_kj_mol"}
        )
        if (
            self.validation_status
            is MDValidationStatus.TECHNICAL_SMOKE_PASSED
        ) != passed:
            raise ValueError("technical smoke 状态与真实 QC 结果不一致。")
        expected = canonical_md_result_sha256(self)
        if self.result_manifest_sha256 is None:
            object.__setattr__(self, "result_manifest_sha256", expected)
        elif self.result_manifest_sha256 != expected:
            raise ValueError("MD 结果清单 canonical SHA-256 不一致。")
        return self


def _payload_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _steps_for(duration_ps: float, timestep_fs: float) -> int:
    return max(1, int(round(duration_ps * 1000.0 / timestep_fs)))


def _source_with_digest(
    source: MDInputSource,
    payload: bytes,
    *,
    maximum_bytes: int,
) -> MDInputSource:
    if not payload.strip():
        raise ValueError(f"{source.source_name} 内容为空。")
    if len(payload) > maximum_bytes:
        raise ValueError(f"{source.source_name} 超过 MVP 文件大小限制。")
    digest = hashlib.sha256(payload).hexdigest()
    if source.sha256 is not None and source.sha256 != digest:
        raise ValueError(f"{source.source_name} 的 SHA-256 与内容不一致。")
    if source.size_bytes is not None and source.size_bytes != len(payload):
        raise ValueError(f"{source.source_name} 的字节数与内容不一致。")
    return source.model_copy(
        update={"sha256": digest, "size_bytes": len(payload)}
    )


def _validate_receptor_payload(payload: bytes, source_format: str) -> None:
    if source_format != "pdb":
        if source_format == "pdbqt":
            raise ValueError(
                "不能从 PDBQT 单独启动 MD；v0.6 必须提供原始受体 PDB。"
            )
        raise ValueError(
            "v0.6 真实 technical_smoke 只接受已选链、单模型原始 PDB；"
            "mmCIF 待正式解析与风险门禁完成后再开放。"
        )
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    atom_lines = [
        line for line in lines if line.startswith(("ATOM  ", "HETATM"))
    ]
    if not atom_lines:
        raise ValueError("受体 PDB 不包含 ATOM/HETATM 坐标记录。")
    if sum(line.startswith("MODEL ") for line in lines) > 1:
        raise ValueError("v0.6 只接受单模型 PDB，不能上传多 MODEL 受体。")
    if any(len(line) > 16 and line[16].strip() for line in atom_lines):
        raise ValueError(
            "v0.6 不自动选择 alternate location；请先输出已人工选择 altloc "
            "的单一受体 PDB。"
        )
    if any(len(line) <= 21 or not line[21].strip() for line in atom_lines):
        raise ValueError("v0.6 要求受体 PDB 每个原子都有非空 chain ID。")


def _validate_ligand_payload(payload: bytes, source_format: str) -> None:
    if source_format != "sdf":
        if source_format == "pdbqt":
            raise ValueError(
                "不能从 PDBQT 单独启动 MD；v0.6 必须提供单记录配体 SDF。"
            )
        raise ValueError(
            "v0.6 真实 technical_smoke 只接受单记录 V2000 SDF；"
            "MOL2/SMILES 待正式多记录与化学身份解析门禁完成后再开放。"
        )
    text = payload.decode("utf-8", errors="strict").strip()
    if "\x00" in text:
        raise ValueError("配体输入包含 NUL 字节。")
    if "V2000" not in text or "M  END" not in text:
        raise ValueError("配体 SDF 缺少 M  END 结构终止记录。")
    records = [item for item in text.split("$$$$") if item.strip()]
    if len(records) != 1 or text.count("$$$$") != 1:
        raise ValueError("v0.6 配体必须是恰好一个记录的 SDF。")


def _pdb_chain_ids(payload: bytes) -> set[str]:
    return {
        line[21].strip()
        for line in payload.decode("utf-8", errors="strict").splitlines()
        if line.startswith(("ATOM  ", "HETATM")) and len(line) > 21
    }


def _pdb_element(line: str) -> str:
    element = line[76:78].strip().upper() if len(line) >= 78 else ""
    if element:
        return element
    atom_name = line[12:16].strip().upper() if len(line) >= 16 else ""
    letters = "".join(char for char in atom_name if char.isalpha())
    return letters[:2]


def _detect_pdb_risks(payload: bytes, source_format: str) -> MDDetectedRisks:
    if source_format != "pdb":
        return MDDetectedRisks()
    metal_atoms: set[str] = set()
    covalent_records: set[str] = set()
    unknown_residues: set[str] = set()
    text = payload.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), start=1):
        record = line[0:6].strip().upper()
        if record in {"LINK", "CONECT"}:
            covalent_records.add(f"line {line_number}: {record}")
            continue
        if record not in {"ATOM", "HETATM"}:
            continue
        residue = line[17:20].strip().upper() if len(line) >= 20 else ""
        if record == "ATOM":
            if residue not in _STANDARD_PROTEIN_RESIDUES:
                unknown_residues.add(residue or f"line-{line_number}")
            continue
        element = _pdb_element(line)
        if element in _METAL_ELEMENTS:
            serial = line[6:11].strip() if len(line) >= 11 else str(line_number)
            metal_atoms.add(f"{element}:{serial}")
        if residue not in _WATER_RESIDUES:
            unknown_residues.add(residue or f"line-{line_number}")
    return MDDetectedRisks(
        metal_atoms=sorted(metal_atoms),
        covalent_records=sorted(covalent_records),
        unknown_residues=sorted(unknown_residues),
    )


def _confirmation_blockers(
    confirmation: MDChemistryConfirmation,
    detected: MDDetectedRisks,
) -> list[str]:
    required_confirmations = {
        "化学身份": confirmation.chemical_identity_confirmed,
        "受体结构": confirmation.receptor_structure_reviewed,
        "形式电荷": confirmation.formal_charge_confirmed,
        "质子化状态": confirmation.protonation_confirmed,
        "互变异构状态": confirmation.tautomer_confirmed,
        "立体化学": confirmation.stereochemistry_confirmed,
        "全部立体中心": confirmation.all_stereocenters_defined,
        "金属检查": confirmation.metals_reviewed,
        "共价连接检查": confirmation.covalent_links_reviewed,
        "未知残基检查": confirmation.unknown_residues_reviewed,
    }
    blockers = [
        f"{label}尚未确认"
        for label, confirmed in required_confirmations.items()
        if not confirmed
    ]
    if confirmation.metals_present or detected.metal_atoms:
        blockers.append(
            "MVP 不支持金属："
            + "、".join(
                [*confirmation.metals_present, *detected.metal_atoms]
            )
        )
    if confirmation.covalent_links_present or detected.covalent_records:
        blockers.append(
            "MVP 不支持共价配体或未解析连接："
            + "、".join(
                [
                    *confirmation.covalent_links_present,
                    *detected.covalent_records,
                ]
            )
        )
    if confirmation.unknown_residues_present or detected.unknown_residues:
        blockers.append(
            "MVP 不支持未知或非标准残基："
            + "、".join(
                [
                    *confirmation.unknown_residues_present,
                    *detected.unknown_residues,
                ]
            )
        )
    blockers.extend(
        f"MVP 不支持：{item}" for item in confirmation.unsupported_features
    )
    return blockers


def _derive_seeds(namespace: str, count: int) -> list[int]:
    seeds: list[int] = []
    index = 0
    while len(seeds) < count:
        digest = hashlib.sha256(
            f"{namespace}:{index}".encode("utf-8")
        ).digest()
        seed = int.from_bytes(digest[:4], "big") % 2_147_483_647
        seed = seed or 1
        if seed not in seeds:
            seeds.append(seed)
        index += 1
    return seeds


def protocol_for_preset(
    preset: MDPreset | str,
    *,
    seed_namespace: str = "vetevidence-md",
    seeds: list[int] | None = None,
) -> MDProtocol:
    active = MDPreset(preset)
    if active is not MDPreset.TECHNICAL_SMOKE:
        raise ValueError(
            "v0.6 只开放 technical_smoke；exploratory_replicates 和 "
            "research_review 尚未实现，不能生成执行协议。"
        )
    values = dict(_TECHNICAL_SMOKE_PROTOCOL)
    replica_count = 1
    active_seeds = (
        list(seeds)
        if seeds is not None
        else _derive_seeds(
            f"{seed_namespace}:{active.value}",
            replica_count,
        )
    )
    return MDProtocol(
        preset=active,
        seeds=active_seeds,
        **values,
    )


def canonical_md_manifest_sha256(manifest: MDTaskManifest) -> str:
    payload = manifest.model_dump(
        mode="json",
        exclude={"manifest_sha256"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_md_result_sha256(result: MDRunResult) -> str:
    payload = result.model_dump(
        mode="json",
        exclude={"result_manifest_sha256"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_md_manifest(
    *,
    task_id: str,
    receptor_payload: bytes | str,
    receptor_source: MDInputSource,
    ligand_payload: bytes | str,
    ligand_source: MDInputSource,
    chemistry_confirmation: MDChemistryConfirmation,
    preset: MDPreset | str,
    docking_reference: MDDockingReference | None = None,
    forcefield: MDForceFieldParameters | None = None,
    hardware_request: MDHardwareRequest | None = None,
    seeds: list[int] | None = None,
    protocol_approved_by_user: bool = False,
) -> MDTaskManifest:
    """Validate original inputs and build an immutable MD task manifest."""

    receptor_bytes = _payload_bytes(receptor_payload)
    ligand_bytes = _payload_bytes(ligand_payload)
    _validate_receptor_payload(receptor_bytes, receptor_source.format)
    _validate_ligand_payload(ligand_bytes, ligand_source.format)
    source_chains = _pdb_chain_ids(receptor_bytes)
    selected_chains = set(chemistry_confirmation.receptor_chain_selection)
    if source_chains != selected_chains:
        raise ValueError(
            "v0.6 要求上传已经裁剪为所选链子集的受体 PDB；"
            f"文件链={sorted(source_chains)}，人工选择={sorted(selected_chains)}。"
        )
    traced_receptor = _source_with_digest(
        receptor_source,
        receptor_bytes,
        maximum_bytes=_MAX_RECEPTOR_BYTES,
    )
    traced_ligand = _source_with_digest(
        ligand_source,
        ligand_bytes,
        maximum_bytes=_MAX_LIGAND_BYTES,
    )
    detected = _detect_pdb_risks(
        receptor_bytes,
        traced_receptor.format,
    )
    blockers = _confirmation_blockers(chemistry_confirmation, detected)
    if blockers:
        raise ValueError("MD 化学确认门禁未通过：" + "；".join(blockers))
    protocol = protocol_for_preset(
        preset,
        seed_namespace=task_id,
        seeds=seeds,
    )
    return MDTaskManifest(
        task_id=task_id,
        receptor_source=traced_receptor,
        ligand_source=traced_ligand,
        docking_reference=docking_reference,
        chemistry_confirmation=chemistry_confirmation,
        detected_risks=detected,
        forcefield=forcefield or MDForceFieldParameters(),
        protocol=protocol,
        hardware_request=hardware_request or MDHardwareRequest(),
        protocol_approved_by_user=protocol_approved_by_user,
    )


__all__ = [
    "MD_ANALYSIS_VERSION",
    "MD_MANIFEST_VERSION",
    "MD_RESULT_VERSION",
    "MDAnalysisResult",
    "MDArtifactReference",
    "MDChemistryConfirmation",
    "MDContactOccupancy",
    "MDDetectedRisks",
    "MDDockingReference",
    "MDEvidenceGrade",
    "MDExecutionAudit",
    "MDForceFieldParameters",
    "MDHardwareRequest",
    "MDInputSource",
    "MDMetricSummary",
    "MDPreset",
    "MDProtocol",
    "MDReplicaAnalysis",
    "MDReplicateSummary",
    "MDResidueMetric",
    "MDRunResult",
    "MDTaskManifest",
    "MDTimeSeries",
    "MDValidationStatus",
    "build_md_manifest",
    "canonical_md_manifest_sha256",
    "canonical_md_result_sha256",
    "protocol_for_preset",
]
