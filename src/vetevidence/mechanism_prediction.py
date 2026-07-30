from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NETWORK_ALGORITHM_VERSION = "network-pharmacology-v1"
VINA_MANIFEST_VERSION = "vina-manifest-v1"
VINA_PARSER_VERSION = "vina-text-parser-v1"
BUNDLE_VERSION = "mechanism-prediction-v1"


class PredictionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class PredictionEvidenceGrade(StrEnum):
    """Evidence labels that prevent predictions being presented as experiments."""

    USER_PROVIDED_RECORD = "user_provided_record"
    COMPUTATIONAL_PREDICTION = "computational_prediction"


class SourceProvenance(PredictionModel):
    source_name: str = Field(min_length=1)
    accession: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class RowReference(PredictionModel):
    source_accession: str = Field(min_length=1)
    row_number: int = Field(ge=2)


class CompoundTargetRecord(PredictionModel):
    compound: str = Field(min_length=1)
    compound_accession: str = Field(min_length=1)
    organism: str = Field(min_length=1)
    target: str = Field(min_length=1)
    target_accession: str = Field(min_length=1)
    source: SourceProvenance
    row_number: int = Field(ge=2)
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.USER_PROVIDED_RECORD
    )


class TargetPathwayRecord(PredictionModel):
    organism: str = Field(min_length=1)
    target: str = Field(min_length=1)
    target_accession: str = Field(min_length=1)
    pathway: str = Field(min_length=1)
    pathway_accession: str = Field(min_length=1)
    source: SourceProvenance
    row_number: int = Field(ge=2)
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.USER_PROVIDED_RECORD
    )


class NetworkPharmacologyParameters(PredictionModel):
    algorithm_version: str = NETWORK_ALGORITHM_VERSION
    target_join_key: str = "normalized_organism+target_accession"
    ranking_method: str = "compound_degree_x_pathway_degree"


class NetworkCompoundLink(PredictionModel):
    compound: str = Field(min_length=1)
    compound_accession: str = Field(min_length=1)


class RankedNetworkTarget(PredictionModel):
    rank: int = Field(ge=1)
    target: str = Field(min_length=1)
    target_accession: str = Field(min_length=1)
    organism: str = Field(min_length=1)
    compounds: list[NetworkCompoundLink] = Field(default_factory=list)
    compound_accessions: list[str]
    pathway_accessions: list[str]
    compound_degree: int = Field(ge=1)
    pathway_degree: int = Field(ge=1)
    network_score: int = Field(ge=1)
    source_rows: list[RowReference]


class NetworkSummary(PredictionModel):
    input_compound_count: int = Field(ge=0)
    input_organism_count: int = Field(ge=0)
    compound_target_target_count: int = Field(ge=0)
    target_pathway_target_count: int = Field(ge=0)
    intersection_target_count: int = Field(ge=0)
    intersection_compound_count: int = Field(ge=0)
    intersection_pathway_count: int = Field(ge=0)
    compound_target_edge_count: int = Field(ge=0)
    target_pathway_edge_count: int = Field(ge=0)


class NetworkPharmacologyResult(PredictionModel):
    sources: list[SourceProvenance] = Field(min_length=2)
    parameters: NetworkPharmacologyParameters
    compounds: list[str] = Field(min_length=1)
    organisms: list[str] = Field(min_length=1)
    ranked_targets: list[RankedNetworkTarget]
    summary: NetworkSummary
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    )


class VinaParameters(PredictionModel):
    center_x: float
    center_y: float
    center_z: float
    size_x: float = Field(gt=0)
    size_y: float = Field(gt=0)
    size_z: float = Field(gt=0)
    exhaustiveness: int = Field(default=8, ge=1)
    num_modes: int = Field(default=9, ge=1)
    energy_range: float = Field(default=3.0, gt=0)
    seed: int | None = None


class VinaTaskManifest(PredictionModel):
    manifest_version: str = VINA_MANIFEST_VERSION
    task_id: str = Field(min_length=1)
    compound_name: str = Field(min_length=1)
    ligand_accession: str = Field(min_length=1)
    receptor_name: str = Field(min_length=1)
    receptor_accession: str = Field(min_length=1)
    receptor_organism: str = Field(min_length=1)
    ligand_source: SourceProvenance
    receptor_source: SourceProvenance
    parameters: VinaParameters
    engine: str = "AutoDock Vina"
    engine_version: str = Field(min_length=1)
    manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    )

    @model_validator(mode="after")
    def structures_require_content_hashes(self) -> VinaTaskManifest:
        if self.ligand_source.sha256 is None:
            raise ValueError("配体结构必须记录 SHA-256。")
        if self.receptor_source.sha256 is None:
            raise ValueError("受体结构必须记录 SHA-256。")
        expected_hash = canonical_manifest_sha256(self)
        if self.manifest_sha256 is None:
            object.__setattr__(self, "manifest_sha256", expected_hash)
        elif expected_hash != self.manifest_sha256:
            raise ValueError("Vina 任务清单的 canonical SHA-256 不一致。")
        return self


class VinaPose(PredictionModel):
    mode: int = Field(ge=1)
    affinity_kcal_mol: float
    rmsd_lower_bound: float = Field(ge=0)
    rmsd_upper_bound: float = Field(ge=0)


class VinaDockingRun(PredictionModel):
    parser_version: str = VINA_PARSER_VERSION
    manifest: VinaTaskManifest
    output_source: SourceProvenance
    poses: list[VinaPose] = Field(min_length=1)
    best_affinity_kcal_mol: float
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    )

    @model_validator(mode="after")
    def best_affinity_must_come_from_poses(self) -> VinaDockingRun:
        if self.best_affinity_kcal_mol != min(
            pose.affinity_kcal_mol for pose in self.poses
        ):
            raise ValueError("best_affinity_kcal_mol 必须来自已解析的 Vina 模式行。")
        return self


class MechanismPredictionBundle(PredictionModel):
    bundle_version: str = BUNDLE_VERSION
    network: NetworkPharmacologyResult | None = None
    prepared_manifests: list[VinaTaskManifest] = Field(default_factory=list)
    docking_runs: list[VinaDockingRun] = Field(default_factory=list)
    evidence_grade: PredictionEvidenceGrade = (
        PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    )


def _csv_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _provenance_with_digest(
    source: SourceProvenance,
    payload: bytes,
) -> SourceProvenance:
    digest = hashlib.sha256(payload).hexdigest()
    if source.sha256 is not None and source.sha256.casefold() != digest:
        raise ValueError(f"{source.source_name} 的 SHA-256 与输入内容不一致。")
    return source.model_copy(update={"sha256": digest})


def _read_csv_rows(
    payload: str | bytes,
    *,
    required_columns: set[str],
    source: SourceProvenance,
) -> tuple[list[tuple[int, dict[str, str]]], SourceProvenance]:
    raw = _csv_bytes(payload)
    traced_source = _provenance_with_digest(source, raw)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source.source_name} 必须是 UTF-8 CSV。") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = {field.strip() for field in (reader.fieldnames or []) if field}
    missing = sorted(required_columns - fieldnames)
    if missing:
        raise ValueError(
            f"{source.source_name} 缺少必需列：" + "、".join(missing)
        )

    rows: list[tuple[int, dict[str, str]]] = []
    for row_number, raw_row in enumerate(reader, start=2):
        row = {
            (key or "").strip(): (value or "").strip()
            for key, value in raw_row.items()
        }
        if not any(row.values()):
            continue
        empty = sorted(column for column in required_columns if not row.get(column))
        if empty:
            raise ValueError(
                f"{source.source_name} 第 {row_number} 行缺少值："
                + "、".join(empty)
            )
        rows.append((row_number, row))
    if not rows:
        raise ValueError(f"{source.source_name} 没有可用数据行。")
    return rows, traced_source


def parse_compound_target_csv(
    payload: str | bytes,
    *,
    source: SourceProvenance,
) -> list[CompoundTargetRecord]:
    required = {
        "compound",
        "compound_accession",
        "organism",
        "target",
        "target_accession",
    }
    rows, traced_source = _read_csv_rows(
        payload,
        required_columns=required,
        source=source,
    )
    return [
        CompoundTargetRecord(
            compound=row["compound"],
            compound_accession=row["compound_accession"],
            organism=row["organism"],
            target=row["target"],
            target_accession=row["target_accession"],
            source=traced_source,
            row_number=row_number,
        )
        for row_number, row in rows
    ]


def parse_target_pathway_csv(
    payload: str | bytes,
    *,
    source: SourceProvenance,
) -> list[TargetPathwayRecord]:
    required = {
        "organism",
        "target",
        "target_accession",
        "pathway",
        "pathway_accession",
    }
    rows, traced_source = _read_csv_rows(
        payload,
        required_columns=required,
        source=source,
    )
    return [
        TargetPathwayRecord(
            organism=row["organism"],
            target=row["target"],
            target_accession=row["target_accession"],
            pathway=row["pathway"],
            pathway_accession=row["pathway_accession"],
            source=traced_source,
            row_number=row_number,
        )
        for row_number, row in rows
    ]


def analyze_network_pharmacology_csv(
    compound_target_csv: str | bytes,
    target_pathway_csv: str | bytes,
    *,
    compound_target_source: SourceProvenance,
    target_pathway_source: SourceProvenance,
    parameters: NetworkPharmacologyParameters | None = None,
) -> NetworkPharmacologyResult:
    """Build a traceable intersection network from user-provided CSV records."""

    compound_records = parse_compound_target_csv(
        compound_target_csv,
        source=compound_target_source,
    )
    pathway_records = parse_target_pathway_csv(
        target_pathway_csv,
        source=target_pathway_source,
    )
    active_parameters = parameters or NetworkPharmacologyParameters()

    TargetKey = tuple[str, str]
    compound_by_target: dict[TargetKey, set[str]] = defaultdict(set)
    compound_names_by_target: dict[TargetKey, dict[str, str]] = defaultdict(dict)
    pathway_by_target: dict[TargetKey, set[str]] = defaultdict(set)
    target_names: dict[TargetKey, str] = {}
    organism_names: dict[TargetKey, str] = {}
    references: dict[TargetKey, set[tuple[str, int]]] = defaultdict(set)

    for record in compound_records:
        key = (record.organism.casefold(), record.target_accession)
        compound_by_target[key].add(record.compound_accession)
        existing_name = compound_names_by_target[key].get(record.compound_accession)
        if (
            existing_name is not None
            and _scope_key(existing_name) != _scope_key(record.compound)
        ):
            raise ValueError(
                f"化合物 accession {record.compound_accession} 在同一靶点"
                f"对应多个名称：{existing_name}、{record.compound}。"
            )
        compound_names_by_target[key][record.compound_accession] = record.compound
        target_names.setdefault(key, record.target)
        organism_names.setdefault(key, record.organism)
        references[key].add((record.source.accession, record.row_number))
    for record in pathway_records:
        key = (record.organism.casefold(), record.target_accession)
        pathway_by_target[key].add(record.pathway_accession)
        target_names.setdefault(key, record.target)
        organism_names.setdefault(key, record.organism)
        references[key].add((record.source.accession, record.row_number))

    intersection = set(compound_by_target) & set(pathway_by_target)
    ranked_data = sorted(
        intersection,
        key=lambda key: (
            -(len(compound_by_target[key]) * len(pathway_by_target[key])),
            key,
        ),
    )
    ranked_targets = [
        RankedNetworkTarget(
            rank=index,
            target=target_names[key],
            target_accession=key[1],
            organism=organism_names[key],
            compounds=[
                NetworkCompoundLink(
                    compound=compound_names_by_target[key][accession],
                    compound_accession=accession,
                )
                for accession in sorted(compound_by_target[key])
            ],
            compound_accessions=sorted(compound_by_target[key]),
            pathway_accessions=sorted(pathway_by_target[key]),
            compound_degree=len(compound_by_target[key]),
            pathway_degree=len(pathway_by_target[key]),
            network_score=(
                len(compound_by_target[key]) * len(pathway_by_target[key])
            ),
            source_rows=[
                RowReference(source_accession=accession, row_number=row_number)
                for accession, row_number in sorted(references[key])
            ],
        )
        for index, key in enumerate(ranked_data, start=1)
    ]

    intersection_compounds = {
        accession
        for key in intersection
        for accession in compound_by_target[key]
    }
    intersection_pathways = {
        accession
        for key in intersection
        for accession in pathway_by_target[key]
    }
    summary = NetworkSummary(
        input_compound_count=len(
            {record.compound_accession for record in compound_records}
        ),
        input_organism_count=len(
            {record.organism.casefold() for record in compound_records}
            | {record.organism.casefold() for record in pathway_records}
        ),
        compound_target_target_count=len(compound_by_target),
        target_pathway_target_count=len(pathway_by_target),
        intersection_target_count=len(intersection),
        intersection_compound_count=len(intersection_compounds),
        intersection_pathway_count=len(intersection_pathways),
        compound_target_edge_count=sum(
            len(compound_by_target[key]) for key in intersection
        ),
        target_pathway_edge_count=sum(
            len(pathway_by_target[key]) for key in intersection
        ),
    )
    return NetworkPharmacologyResult(
        sources=[compound_records[0].source, pathway_records[0].source],
        parameters=active_parameters,
        compounds=sorted({record.compound for record in compound_records}),
        organisms=sorted(
            {
                organism_names[key]
                for key in set(compound_by_target) | set(pathway_by_target)
            }
        ),
        ranked_targets=ranked_targets,
        summary=summary,
    )


def canonical_manifest_sha256(manifest: VinaTaskManifest) -> str:
    """Hash the canonical JSON representation, excluding the hash itself."""

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


def build_vina_manifest(
    *,
    task_id: str,
    compound_name: str,
    ligand_accession: str,
    receptor_name: str,
    receptor_accession: str,
    receptor_organism: str,
    ligand_source: SourceProvenance,
    receptor_source: SourceProvenance,
    parameters: VinaParameters,
    engine_version: str,
) -> VinaTaskManifest:
    """Prepare a traceable task only; this function never creates a score."""

    values = {
        "manifest_version": VINA_MANIFEST_VERSION,
        "task_id": task_id,
        "compound_name": compound_name,
        "ligand_accession": ligand_accession,
        "receptor_name": receptor_name,
        "receptor_accession": receptor_accession,
        "receptor_organism": receptor_organism,
        "ligand_source": ligand_source,
        "receptor_source": receptor_source,
        "parameters": parameters,
        "engine": "AutoDock Vina",
        "engine_version": engine_version,
        "evidence_grade": PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION,
    }
    draft = VinaTaskManifest.model_construct(
        **values,
        manifest_sha256="0" * 64,
    )
    return VinaTaskManifest(
        **values,
        manifest_sha256=canonical_manifest_sha256(draft),
    )


def _scope_key(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold())


def require_network_scope(
    result: NetworkPharmacologyResult,
    *,
    expected_compounds: list[str],
    expected_organism: str,
) -> None:
    """Reject a network that is not explicitly bound to the current question."""

    expected_compound_keys = {
        _scope_key(value) for value in expected_compounds if _scope_key(value)
    }
    actual_compounds = {_scope_key(value) for value in result.compounds}
    missing_compounds = [
        value
        for value in expected_compounds
        if _scope_key(value) not in actual_compounds
    ]
    extra_compounds = sorted(actual_compounds - expected_compound_keys)
    actual_organisms = {_scope_key(value) for value in result.organisms}
    expected_organism_key = _scope_key(expected_organism)
    participating_compounds = {
        _scope_key(link.compound)
        for target in result.ranked_targets
        if _scope_key(target.organism) == expected_organism_key
        for link in target.compounds
    }
    missing_from_intersection = [
        value
        for value in expected_compounds
        if _scope_key(value) not in participating_compounds
    ]
    mixed_organisms = actual_organisms - {expected_organism_key}
    if (
        missing_compounds
        or extra_compounds
        or expected_organism_key not in actual_organisms
        or mixed_organisms
        or missing_from_intersection
    ):
        details = []
        if missing_compounds:
            details.append("缺少化合物：" + "、".join(missing_compounds))
        if extra_compounds:
            details.append("混入额外化合物：" + "、".join(extra_compounds))
        if expected_organism_key not in actual_organisms:
            details.append(
                f"缺少研究对象：{expected_organism}；"
                f"实际为 {'、'.join(result.organisms)}"
            )
        if mixed_organisms:
            details.append(
                "混入其他研究对象："
                + "、".join(
                    value
                    for value in result.organisms
                    if _scope_key(value) in mixed_organisms
                )
            )
        if missing_from_intersection:
            details.append(
                "交集网络未包含当前干预："
                + "、".join(missing_from_intersection)
            )
        raise ValueError("网络药理学数据与当前科研问题不匹配：" + "；".join(details))


def require_docking_scope(
    manifest: VinaTaskManifest,
    *,
    expected_compounds: list[str],
    expected_organism: str,
) -> None:
    """Reject a docking task with the wrong ligand or receptor organism."""

    expected_compound_keys = {_scope_key(value) for value in expected_compounds}
    problems = []
    if _scope_key(manifest.compound_name) not in expected_compound_keys:
        problems.append(f"配体 {manifest.compound_name} 不属于当前两种干预")
    if _scope_key(manifest.receptor_organism) != _scope_key(expected_organism):
        problems.append(
            f"受体研究对象 {manifest.receptor_organism} 与"
            f" {expected_organism} 不一致"
        )
    if problems:
        raise ValueError("分子对接任务与当前科研问题不匹配：" + "；".join(problems))


def validate_pdbqt_bytes(
    payload: str | bytes,
    *,
    role: Literal["ligand", "receptor"],
) -> str:
    """Validate minimal PDBQT structure records and return the content hash."""

    raw = _csv_bytes(payload)
    if not raw.strip():
        raise ValueError(f"{role} PDBQT 为空。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{role} PDBQT 必须是 UTF-8 文本。") from exc
    records = {
        line.strip().split(maxsplit=1)[0].upper()
        for line in text.splitlines()
        if line.strip()
    }
    if not ({"ATOM", "HETATM"} & records):
        raise ValueError(f"{role} PDBQT 缺少 ATOM/HETATM 记录。")
    if role == "ligand":
        missing = sorted({"ROOT", "TORSDOF"} - records)
        if missing:
            raise ValueError("ligand PDBQT 缺少记录：" + "、".join(missing))
    return hashlib.sha256(raw).hexdigest()


_VINA_MODE_ROW = re.compile(
    r"^\s*(?P<mode>\d+)\s+"
    r"(?P<affinity>[+-]?\d+(?:\.\d+)?)\s+"
    r"(?P<lower>\d+(?:\.\d+)?)\s+"
    r"(?P<upper>\d+(?:\.\d+)?)\s*$"
)
_VINA_VERSION = re.compile(
    r"\bAutoDock\s+Vina\s+v?(?P<version>[0-9]+(?:\.[0-9]+){1,2})\b",
    flags=re.IGNORECASE,
)
_VINA_TABLE_SEPARATOR = re.compile(
    r"^\s*-{3,}(?:\+-{3,}){2,}\s*$"
)
_MANIFEST_SHA_HEADER = re.compile(
    r"^\s*VetEvidence-Manifest-SHA256:\s*(?P<digest>[0-9a-f]{64})\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def parse_vina_output(
    output_text: str | bytes,
    *,
    manifest: VinaTaskManifest,
    output_source: SourceProvenance,
) -> VinaDockingRun:
    """Parse a task-bound Vina-style table without estimating missing scores."""

    raw = _csv_bytes(output_text)
    if not raw.strip():
        raise ValueError("Vina 输出为空，不能生成对接分数。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Vina 输出必须是 UTF-8 文本。") from exc
    normalized = text.casefold()
    if "mode" not in normalized or "affinity" not in normalized:
        raise ValueError("未检测到 Vina mode/affinity 表头，不能生成对接分数。")
    expected_manifest_sha = canonical_manifest_sha256(manifest)
    if expected_manifest_sha != manifest.manifest_sha256:
        raise ValueError("Vina 任务清单的 canonical SHA-256 不一致。")
    manifest_headers = _MANIFEST_SHA_HEADER.findall(text)
    if len(manifest_headers) != 1:
        raise ValueError(
            "Vina 输出必须包含唯一的 VetEvidence-Manifest-SHA256 标记。"
        )
    if manifest_headers[0].casefold() != manifest.manifest_sha256:
        raise ValueError("Vina 输出绑定的任务清单 SHA-256 不匹配。")
    version_match = _VINA_VERSION.search(text)
    if version_match is None:
        raise ValueError("未检测到 AutoDock Vina 版本，不能核验对接来源。")
    reported_version = version_match.group("version")
    expected_version = manifest.engine_version.removeprefix("v")
    if reported_version != expected_version:
        raise ValueError(
            "Vina 输出版本与任务清单不一致："
            f"输出 {reported_version}，清单 {expected_version}。"
        )

    lines = text.splitlines()
    separator_index: int | None = None
    for header_index, line in enumerate(lines):
        normalized_line = line.casefold()
        if "mode" not in normalized_line or "affinity" not in normalized_line:
            continue
        for candidate_index in range(
            header_index + 1,
            min(header_index + 5, len(lines)),
        ):
            if _VINA_TABLE_SEPARATOR.fullmatch(lines[candidate_index]):
                separator_index = candidate_index
                break
        if separator_index is not None:
            break
    if separator_index is None:
        raise ValueError("未检测到 Vina 模式表分隔线，不能生成对接分数。")

    poses: list[VinaPose] = []
    seen_modes: set[int] = set()
    for line in lines[separator_index + 1 :]:
        match = _VINA_MODE_ROW.match(line)
        if match is None:
            if poses and line.strip():
                break
            continue
        mode = int(match.group("mode"))
        if mode in seen_modes:
            raise ValueError(f"Vina 输出包含重复模式 {mode}。")
        seen_modes.add(mode)
        poses.append(
            VinaPose(
                mode=mode,
                affinity_kcal_mol=float(match.group("affinity")),
                rmsd_lower_bound=float(match.group("lower")),
                rmsd_upper_bound=float(match.group("upper")),
            )
        )
    if not poses:
        raise ValueError("Vina 输出没有可解析的模式行，不能生成对接分数。")
    poses.sort(key=lambda pose: pose.mode)
    expected_modes = list(range(1, len(poses) + 1))
    if [pose.mode for pose in poses] != expected_modes:
        raise ValueError("Vina mode 必须从 1 开始连续编号。")
    if len(poses) > manifest.parameters.num_modes:
        raise ValueError(
            "Vina 输出模式数超过任务清单 num_modes："
            f"{len(poses)} > {manifest.parameters.num_modes}。"
        )
    if (
        poses[0].rmsd_lower_bound != 0
        or poses[0].rmsd_upper_bound != 0
    ):
        raise ValueError("Vina mode 1 的 RMSD 上下界必须均为 0。")
    traced_output = _provenance_with_digest(output_source, raw)
    return VinaDockingRun(
        manifest=manifest,
        output_source=traced_output,
        poses=poses,
        best_affinity_kcal_mol=min(
            pose.affinity_kcal_mol for pose in poses
        ),
    )


__all__ = [
    "BUNDLE_VERSION",
    "NETWORK_ALGORITHM_VERSION",
    "VINA_MANIFEST_VERSION",
    "VINA_PARSER_VERSION",
    "CompoundTargetRecord",
    "MechanismPredictionBundle",
    "NetworkCompoundLink",
    "NetworkPharmacologyParameters",
    "NetworkPharmacologyResult",
    "NetworkSummary",
    "PredictionEvidenceGrade",
    "RankedNetworkTarget",
    "RowReference",
    "SourceProvenance",
    "TargetPathwayRecord",
    "VinaDockingRun",
    "VinaParameters",
    "VinaPose",
    "VinaTaskManifest",
    "analyze_network_pharmacology_csv",
    "build_vina_manifest",
    "canonical_manifest_sha256",
    "parse_compound_target_csv",
    "parse_target_pathway_csv",
    "parse_vina_output",
    "require_docking_scope",
    "require_network_scope",
    "validate_pdbqt_bytes",
]
