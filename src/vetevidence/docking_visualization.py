"""Reproducible and optional-tool-safe docking visualization artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.docking_workflow import (
    DockingBatchResult,
    ReceptorApproval,
    validate_successful_docking_attempt,
)


_MAX_STRUCTURE_BYTES = 50 * 1024 * 1024
_MAX_TOOL_OUTPUT_BYTES = 100 * 1024 * 1024
_MAX_RUNTIME_MARKER_BYTES = 250 * 1024 * 1024
_MAX_DIAGNOSTIC_CHARACTERS = 2000
_MAX_PNG_PIXELS = 100_000_000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_LIGAND_CHAIN_CANDIDATES = tuple(reversed("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

Runner = Callable[..., subprocess.CompletedProcess[bytes]]
PopenFactory = Callable[..., object]


class DockingVisualizationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class RuntimeMarkerAudit(DockingVisualizationModel):
    filename: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1, le=_MAX_RUNTIME_MARKER_BYTES)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def marker_name_is_direct_child(self) -> RuntimeMarkerAudit:
        if (
            Path(self.filename).name != self.filename
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ValueError("PLIP runtime 标志文件必须是目录直属文件。")
        return self


class PLIPRuntimeDirectoryAudit(DockingVisualizationModel):
    variable: Literal["BABEL_LIBDIR", "BABEL_DATADIR"]
    path: str = Field(min_length=1)
    markers: tuple[RuntimeMarkerAudit, ...] = Field(min_length=1)
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def marker_manifest_matches(self) -> PLIPRuntimeDirectoryAudit:
        filenames = [marker.filename for marker in self.markers]
        if len(filenames) != len(set(filenames)):
            raise ValueError("PLIP runtime 标志文件不能重复。")
        canonical = json.dumps(
            {
                "variable": self.variable,
                "path": self.path,
                "markers": [
                    marker.model_dump(mode="json")
                    for marker in self.markers
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != self.manifest_sha256:
            raise ValueError("PLIP runtime 目录审计 manifest SHA-256 不一致。")
        return self


class PLIPRuntimeEnvironment(DockingVisualizationModel):
    babel_libdir: PLIPRuntimeDirectoryAudit
    babel_datadir: PLIPRuntimeDirectoryAudit
    controlled_path: str = Field(min_length=1)
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def environment_manifest_matches(self) -> PLIPRuntimeEnvironment:
        if self.babel_libdir.variable != "BABEL_LIBDIR":
            raise ValueError("PLIP libdir 审计变量必须是 BABEL_LIBDIR。")
        if self.babel_datadir.variable != "BABEL_DATADIR":
            raise ValueError("PLIP datadir 审计变量必须是 BABEL_DATADIR。")
        if self.controlled_path != self.babel_libdir.path:
            raise ValueError("PLIP 受控 PATH 必须精确等于已验证的 BABEL_LIBDIR。")
        canonical = json.dumps(
            {
                "BABEL_LIBDIR": self.babel_libdir.model_dump(mode="json"),
                "BABEL_DATADIR": self.babel_datadir.model_dump(mode="json"),
                "PATH": self.controlled_path,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != self.manifest_sha256:
            raise ValueError("PLIP runtime environment manifest SHA-256 不一致。")
        return self

    def environment_overrides(self) -> dict[str, str]:
        return {
            "BABEL_LIBDIR": self.babel_libdir.path,
            "BABEL_DATADIR": self.babel_datadir.path,
            "PATH": self.controlled_path,
        }


class VerifiedExternalTool(DockingVisualizationModel):
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
    runtime_environment: PLIPRuntimeEnvironment | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def details_match_availability(self) -> VerifiedExternalTool:
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
        if self.tool == "PLIP" and self.available and self.runtime_environment is None:
            raise ValueError("可用 PLIP 必须绑定已审计的 Open Babel runtime。")
        if self.tool != "PLIP" and self.runtime_environment is not None:
            raise ValueError("只有 PLIP 可以记录 Open Babel runtime environment。")
        return self


class OptionalArtifact(DockingVisualizationModel):
    artifact: str = Field(min_length=1)
    status: Literal["available", "generated_unverified", "unavailable"]
    filename: str | None = None
    media_type: str | None = None
    payload: bytes | None = None
    sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason: str | None = None

    @model_validator(mode="after")
    def payload_matches_status(self) -> OptionalArtifact:
        if self.status in {"available", "generated_unverified"}:
            if (
                self.filename is None
                or self.media_type is None
                or self.payload is None
                or self.sha256 is None
            ):
                raise ValueError("已生成产物必须包含文件名、类型、内容和 SHA-256。")
            if self.status == "available" and self.reason is not None:
                raise ValueError("可用产物不能包含不可用原因。")
            if self.status == "generated_unverified" and self.reason is None:
                raise ValueError("未验证产物必须明确记录验证缺口。")
            if _sha256(self.payload) != self.sha256:
                raise ValueError("外部工具产物内容 SHA-256 不一致。")
        else:
            if self.reason is None:
                raise ValueError("不可用产物必须记录原因。")
            if any(
                value is not None
                for value in (
                    self.filename,
                    self.media_type,
                    self.payload,
                    self.sha256,
                )
            ):
                raise ValueError("不可用产物不能包含伪造内容。")
        return self


class ComplexPDBArtifact(DockingVisualizationModel):
    payload: bytes = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    receptor_atom_count: int = Field(ge=1)
    ligand_atom_count: int = Field(ge=1)
    ligand_chain: str = Field(min_length=1, max_length=1)
    ligand_residue_number: int = Field(ge=1, le=9999)

    @model_validator(mode="after")
    def payload_hash_matches(self) -> ComplexPDBArtifact:
        if _sha256(self.payload) != self.sha256:
            raise ValueError("复合物 PDB 内容 SHA-256 不一致。")
        return self


class GeneratedPyMOLScript(DockingVisualizationModel):
    payload: bytes = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    bound_complex_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    generator_version: Literal["vetevidence-pml-v2"] = "vetevidence-pml-v2"

    @model_validator(mode="after")
    def payload_hash_matches(self) -> GeneratedPyMOLScript:
        if _sha256(self.payload) != self.sha256:
            raise ValueError("PyMOL PML 内容 SHA-256 不一致。")
        try:
            lines = self.payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("PyMOL PML 必须是 UTF-8 固定命令文本。") from exc
        expected_exact = {
            0: "# VetEvidence generated script; edit styles and camera as needed.",
            1: f"# Bound-Complex-SHA256: {self.bound_complex_sha256}",
            3: "reinitialize",
            4: "load complex.pdb, docking_complex",
            5: "hide everything, all",
            6: "show cartoon, polymer.protein",
            7: "color slate, polymer.protein",
            9: "show sticks, docked_ligand",
            10: "color orange, docked_ligand",
            12: "show sticks, pocket_residues",
            13: "color cyan, pocket_residues",
            14: "hide everything, solvent",
            15: "bg_color white",
            16: "set antialias, 2",
            17: "set ray_opaque_background, off",
            18: "set stick_radius, 0.18",
            19: "orient docked_ligand",
            20: "zoom docked_ligand, 10",
            21: "save interaction.pse",
            23: "quit",
        }
        if len(lines) != 24 or any(
            lines[index] != value for index, value in expected_exact.items()
        ):
            raise ValueError("PyMOL PML 不符合 VetEvidence 固定命令模板。")
        score_match = re.fullmatch(
            r"# Vina 预测评分; mode=([1-9][0-9]*); "
            r"score_kcal_mol=([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
            r"(?:[eE][+-]?[0-9]+)?)",
            lines[2],
        )
        ligand_match = re.fullmatch(
            r"select docked_ligand, chain ([A-Za-z0-9]) "
            r"and resn LIG and resi ([1-9][0-9]{0,3})",
            lines[8],
        )
        pocket_match = re.fullmatch(
            r"select pocket_residues, byres "
            r"\(polymer\.protein within "
            r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+) of docked_ligand\)",
            lines[11],
        )
        png_match = re.fullmatch(
            r"png interaction\.png, width=([0-9]+), height=([0-9]+), "
            r"dpi=([0-9]+), ray=1",
            lines[22],
        )
        if not all((score_match, ligand_match, pocket_match, png_match)):
            raise ValueError("PyMOL PML 的可变参数不符合固定模板。")
        assert score_match is not None
        assert ligand_match is not None
        assert pocket_match is not None
        assert png_match is not None
        if not math.isfinite(float(score_match.group(2))):
            raise ValueError("PyMOL PML 的 Vina 预测评分无效。")
        if not 1 <= int(ligand_match.group(2)) <= 9999:
            raise ValueError("PyMOL PML 的配体残基编号无效。")
        if not 1.0 <= float(pocket_match.group(1)) <= 15.0:
            raise ValueError("PyMOL PML 的口袋距离无效。")
        width, height, dpi = (int(value) for value in png_match.groups())
        if not (
            100 <= width <= 10000
            and 100 <= height <= 10000
            and 72 <= dpi <= 1200
        ):
            raise ValueError("PyMOL PML 的 PNG 输出参数超出安全范围。")
        return self


class PyMOLRenderResult(DockingVisualizationModel):
    png: OptionalArtifact
    pse: OptionalArtifact
    command: tuple[str, ...] | None = None
    stdout: str = ""
    stderr: str = ""


class PLIPAnalysisResult(DockingVisualizationModel):
    xml: OptionalArtifact
    text: OptionalArtifact
    png: OptionalArtifact
    pse: OptionalArtifact
    command: tuple[str, ...] | None = None
    stdout: str = ""
    stderr: str = ""


class PackageFile(DockingVisualizationModel):
    filename: str = Field(min_length=1)
    payload: bytes
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def payload_hash_matches(self) -> PackageFile:
        if _safe_archive_name(self.filename) != self.filename:
            raise ValueError("任务包文件名不是规范的归档相对路径。")
        if _sha256(self.payload) != self.sha256:
            raise ValueError("任务包文件内容 SHA-256 不一致。")
        return self


class DockingVisualizationPackage(DockingVisualizationModel):
    batch_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    ligand_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    seed: int
    pose_mode: int = Field(ge=1)
    receptor_model: str = Field(min_length=1)
    receptor_chains: tuple[str, ...] = Field(min_length=1)
    ligand_chain: str = Field(
        min_length=1,
        max_length=1,
        pattern=r"^[A-Za-z0-9]$",
    )
    ligand_residue_number: int = Field(ge=1, le=9999)
    files: tuple[PackageFile, ...] = Field(min_length=1)
    zip_payload: bytes = Field(min_length=1)
    zip_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    task_manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    complex_pdb_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    pml_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    pymol_render: PyMOLRenderResult
    plip_analysis: PLIPAnalysisResult

    @model_validator(mode="after")
    def archive_and_bindings_match(self) -> DockingVisualizationPackage:
        if hashlib.sha256(self.zip_payload).hexdigest() != self.zip_sha256:
            raise ValueError("可视化 ZIP SHA-256 不一致。")
        by_name = {item.filename: item for item in self.files}
        if len(by_name) != len(self.files):
            raise ValueError("可视化任务包包含重复文件名。")
        for item in self.files:
            if hashlib.sha256(item.payload).hexdigest() != item.sha256:
                raise ValueError(f"任务包文件 SHA-256 不一致：{item.filename}。")
        if (
            by_name.get("complex.pdb") is None
            or by_name["complex.pdb"].sha256 != self.complex_pdb_sha256
            or by_name.get("view.pml") is None
            or by_name["view.pml"].sha256 != self.pml_sha256
        ):
            raise ValueError("任务包 complex/PML 绑定不一致。")
        try:
            vina_manifest = json.loads(by_name["vina_manifest.json"].payload)
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("任务包缺少有效 Vina manifest。") from exc
        if vina_manifest.get("manifest_sha256") != self.task_manifest_sha256:
            raise ValueError("任务包 Vina manifest SHA-256 绑定不一致。")
        try:
            visualization_manifest = json.loads(
                by_name["visualization_manifest.json"].payload
            )
            manifest_inputs = visualization_manifest["inputs"]
            manifest_file_hashes = visualization_manifest["file_sha256"]
            manifest_score = visualization_manifest["score"]
            manifest_selection = visualization_manifest["selection"]
        except (
            KeyError,
            TypeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise ValueError("任务包缺少有效的可视化 manifest。") from exc
        if (
            visualization_manifest.get("batch_id") != self.batch_id
            or visualization_manifest.get("ligand_id") != self.ligand_id
            or manifest_score.get("seed") != self.seed
            or manifest_score.get("mode") != self.pose_mode
        ):
            raise ValueError("可视化 manifest 的批次、配体、seed 或 pose 归属不一致。")
        if manifest_selection != {
            "receptor_model": self.receptor_model,
            "receptor_chains": list(self.receptor_chains),
            "ligand_chain": self.ligand_chain,
            "ligand_residue_number": self.ligand_residue_number,
        }:
            raise ValueError("可视化 manifest 的受体/配体选择归属不一致。")
        if (
            manifest_inputs.get("vina_manifest_sha256")
            != self.task_manifest_sha256
            or manifest_inputs.get("complex_pdb_sha256")
            != self.complex_pdb_sha256
            or manifest_inputs.get("pml_sha256") != self.pml_sha256
        ):
            raise ValueError("可视化 manifest 与任务包核心 SHA-256 不一致。")
        hashed_payload_names = set(by_name) - {
            "visualization_manifest.json",
            "SHA256SUMS.txt",
        }
        expected_file_hashes = {
            name: by_name[name].sha256 for name in sorted(hashed_payload_names)
        }
        if manifest_file_hashes != expected_file_hashes:
            raise ValueError("可视化 manifest 的文件 SHA-256 清单不一致。")
        expected_checksums = "".join(
            f"{by_name[name].sha256}  {name}\n"
            for name in sorted(set(by_name) - {"SHA256SUMS.txt"})
        ).encode("utf-8")
        if by_name.get("SHA256SUMS.txt") is None or (
            by_name["SHA256SUMS.txt"].payload != expected_checksums
        ):
            raise ValueError("SHA256SUMS.txt 与任务包文件不一致。")
        GeneratedPyMOLScript(
            payload=by_name["view.pml"].payload,
            sha256=self.pml_sha256,
            bound_complex_sha256=self.complex_pdb_sha256,
        )
        for artifact in (
            self.pymol_render.png,
            self.pymol_render.pse,
            self.plip_analysis.xml,
            self.plip_analysis.text,
            self.plip_analysis.png,
            self.plip_analysis.pse,
        ):
            if artifact.status not in {"available", "generated_unverified"}:
                continue
            assert artifact.filename is not None
            assert artifact.sha256 is not None
            package_artifact = by_name.get(artifact.filename)
            if package_artifact is None or package_artifact.sha256 != artifact.sha256:
                raise ValueError("外部工具产物未绑定可视化任务包。")
        try:
            with zipfile.ZipFile(io.BytesIO(self.zip_payload)) as archive:
                archive_names = set(archive.namelist())
                if archive_names != set(by_name):
                    raise ValueError("ZIP 文件清单与 PackageFile 不一致。")
                for name, item in by_name.items():
                    if archive.read(name) != item.payload:
                        raise ValueError(f"ZIP 文件内容不一致：{name}。")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise ValueError("可视化 ZIP 无法通过完整性检查。") from exc
        return self

    def file_payload(self, filename: str) -> bytes:
        for item in self.files:
            if item.filename == filename:
                return item.payload
        raise KeyError(filename)


def _payload_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_directory_audit(
    *,
    variable: Literal["BABEL_LIBDIR", "BABEL_DATADIR"],
    path_value: str | os.PathLike[str],
) -> PLIPRuntimeDirectoryAudit:
    root = Path(os.fspath(path_value).strip().strip('"')).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"{variable} 必须指向目录。")
    if variable == "BABEL_LIBDIR":
        candidates = [
            path
            for path in root.iterdir()
            if path.is_file()
            and (
                path.name.casefold() in {
                    "openbabel-3.dll",
                    "libopenbabel.dylib",
                }
                or path.name.casefold().startswith("libopenbabel.so")
            )
        ]
        marker_description = "Open Babel 动态库"
    else:
        allowed_names = {
            "aromatic.txt",
            "atomtyp.txt",
            "element.txt",
            "isotope-small.txt",
            "isotope.txt",
            "phmodel.txt",
            "plugindefines.txt",
            "resdata.txt",
            "space-groups.txt",
            "splash.png",
            "types.txt",
        }
        candidates = [
            path
            for path in root.iterdir()
            if path.is_file() and path.name.casefold() in allowed_names
        ]
        marker_description = "Open Babel 数据标志文件"
    if not candidates:
        raise ValueError(f"{variable} 缺少{marker_description}。")

    markers: list[RuntimeMarkerAudit] = []
    for candidate in sorted(candidates, key=lambda item: item.name.casefold()):
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{variable} 标志文件越出声明目录。") from exc
        size_bytes = resolved.stat().st_size
        if not 1 <= size_bytes <= _MAX_RUNTIME_MARKER_BYTES:
            raise ValueError(f"{variable} 标志文件为空或过大。")
        markers.append(
            RuntimeMarkerAudit(
                filename=candidate.name,
                size_bytes=size_bytes,
                sha256=_sha256_file(resolved),
            )
        )
    canonical = json.dumps(
        {
            "variable": variable,
            "path": str(root),
            "markers": [
                marker.model_dump(mode="json") for marker in markers
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PLIPRuntimeDirectoryAudit(
        variable=variable,
        path=str(root),
        markers=tuple(markers),
        manifest_sha256=_sha256(canonical),
    )


def verify_plip_runtime_environment(
    *,
    babel_libdir: str | os.PathLike[str],
    babel_datadir: str | os.PathLike[str],
) -> PLIPRuntimeEnvironment:
    """Build an immutable two-variable Open Babel runtime audit for PLIP."""

    libdir = _runtime_directory_audit(
        variable="BABEL_LIBDIR",
        path_value=babel_libdir,
    )
    datadir = _runtime_directory_audit(
        variable="BABEL_DATADIR",
        path_value=babel_datadir,
    )
    canonical = json.dumps(
        {
            "BABEL_LIBDIR": libdir.model_dump(mode="json"),
            "BABEL_DATADIR": datadir.model_dump(mode="json"),
            "PATH": libdir.path,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PLIPRuntimeEnvironment(
        babel_libdir=libdir,
        babel_datadir=datadir,
        controlled_path=libdir.path,
        manifest_sha256=_sha256(canonical),
    )


def _assert_plip_runtime_unchanged(
    runtime_environment: PLIPRuntimeEnvironment,
) -> dict[str, str]:
    refreshed = verify_plip_runtime_environment(
        babel_libdir=runtime_environment.babel_libdir.path,
        babel_datadir=runtime_environment.babel_datadir.path,
    )
    if refreshed != runtime_environment:
        raise ValueError("PLIP Open Babel runtime 在验证后发生变化。")
    return runtime_environment.environment_overrides()


def _bounded_text(payload: bytes | str | None) -> str:
    value = _payload_bytes(payload).decode("utf-8", errors="replace").strip()
    return value[-_MAX_DIAGNOSTIC_CHARACTERS:]


def _hidden_window_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _safe_archive_name(filename: str) -> str:
    if not filename or "\\" in filename or "\x00" in filename:
        raise ValueError("产物文件名无效。")
    path = PurePosixPath(filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"产物文件名越出归档目录：{filename!r}。")
    return path.as_posix()


def _unavailable(artifact: str, reason: str) -> OptionalArtifact:
    return OptionalArtifact(
        artifact=artifact,
        status="unavailable",
        reason=reason,
    )


def _available(
    artifact: str,
    filename: str,
    media_type: str,
    payload: bytes,
) -> OptionalArtifact:
    if not payload:
        raise ValueError(f"{artifact} 产物为空。")
    return OptionalArtifact(
        artifact=artifact,
        status="available",
        filename=_safe_archive_name(filename),
        media_type=media_type,
        payload=payload,
        sha256=_sha256(payload),
    )


def _generated_unverified(
    artifact: str,
    filename: str,
    media_type: str,
    payload: bytes,
    *,
    reason: str,
) -> OptionalArtifact:
    if not payload:
        raise ValueError(f"{artifact} 产物为空。")
    return OptionalArtifact(
        artifact=artifact,
        status="generated_unverified",
        filename=_safe_archive_name(filename),
        media_type=media_type,
        payload=payload,
        sha256=_sha256(payload),
        reason=reason,
    )


def _validate_png_payload(payload: bytes) -> None:
    if not payload.startswith(_PNG_MAGIC):
        raise ValueError("PNG 文件签名无效。")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "PNG":
                raise ValueError("图片格式不是 PNG。")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_PNG_PIXELS:
                raise ValueError("PNG 图片尺寸无效或超过一亿像素。")
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as exc:
        raise ValueError("PNG 无法被完整解码。") from exc


def verify_external_executable(
    *,
    tool: str,
    executable_path: str | os.PathLike[str] | None,
    user_confirmed: bool,
    version_arguments: Sequence[str] = ("--version",),
    runtime_environment: PLIPRuntimeEnvironment | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = 10.0,
) -> VerifiedExternalTool:
    """Verify only an explicitly configured executable; absence is non-fatal."""

    if tool != "PLIP" and runtime_environment is not None:
        raise ValueError("只有 PLIP 可以接收 Open Babel runtime environment。")
    if executable_path is None:
        return VerifiedExternalTool(
            tool=tool,
            available=False,
            runtime_environment=runtime_environment,
            reason=f"未配置 {tool} 可执行文件。",
        )
    if not user_confirmed:
        raise PermissionError(f"必须由用户显式确认后才能探测 {tool}。")
    if tool == "PLIP" and runtime_environment is None:
        raise ValueError("探测 PLIP 前必须显式提供已审计的 Open Babel runtime。")
    if timeout_seconds <= 0:
        raise ValueError(f"{tool} 版本检查超时必须大于 0。")
    try:
        controlled_environment = (
            _assert_plip_runtime_unchanged(runtime_environment)
            if runtime_environment is not None
            else None
        )
        raw_path = os.fspath(executable_path).strip().strip('"')
        if not raw_path:
            raise OSError("路径为空")
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file():
            raise OSError("路径不是文件")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise OSError("文件不可执行")
        digest = _sha256_file(path)
        active_runner = runner or subprocess.run
        run_options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "capture_output": True,
            "check": False,
            "shell": False,
            "timeout": timeout_seconds,
            **_hidden_window_options(),
        }
        if controlled_environment is not None:
            run_options["env"] = controlled_environment
        completed = active_runner(
            [str(path), *version_arguments],
            **run_options,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"版本检查退出码 {completed.returncode}")
        version_output = _bounded_text(
            _payload_bytes(completed.stdout)
            + b"\n"
            + _payload_bytes(completed.stderr)
        )
        if not version_output:
            raise RuntimeError("没有版本输出")
        if runtime_environment is not None:
            _assert_plip_runtime_unchanged(runtime_environment)
        return VerifiedExternalTool(
            tool=tool,
            available=True,
            executable_path=str(path),
            version_output=version_output,
            executable_sha256=digest,
            runtime_environment=runtime_environment,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        return VerifiedExternalTool(
            tool=tool,
            available=False,
            runtime_environment=runtime_environment,
            reason=f"{type(exc).__name__}: {exc}",
        )


def verify_pymol_executable(
    executable_path: str | os.PathLike[str] | None,
    *,
    user_confirmed: bool,
    runner: Runner | None = None,
    timeout_seconds: float = 10.0,
) -> VerifiedExternalTool:
    return verify_external_executable(
        tool="PyMOL",
        executable_path=executable_path,
        user_confirmed=user_confirmed,
        version_arguments=("--version",),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


def verify_plip_executable(
    executable_path: str | os.PathLike[str] | None,
    *,
    user_confirmed: bool,
    runtime_environment: PLIPRuntimeEnvironment | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = 10.0,
) -> VerifiedExternalTool:
    return verify_external_executable(
        tool="PLIP",
        executable_path=executable_path,
        user_confirmed=user_confirmed,
        version_arguments=("-h",),
        runtime_environment=runtime_environment,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


def _assert_tool_unchanged(tool: VerifiedExternalTool) -> Path:
    if (
        not tool.available
        or tool.executable_path is None
        or tool.executable_sha256 is None
    ):
        raise ValueError(f"{tool.tool} 当前不可用。")
    path = Path(tool.executable_path).resolve(strict=True)
    if _sha256_file(path) != tool.executable_sha256:
        raise ValueError(f"{tool.tool} 可执行文件在验证后发生变化。")
    return path


def extract_vina_pose(output_pdbqt: bytes | str, *, mode: int) -> bytes:
    """Return one exact Vina MODEL block without modifying the source file."""

    if mode < 1:
        raise ValueError("Vina mode 必须大于等于 1。")
    raw = output_pdbqt if isinstance(output_pdbqt, bytes) else output_pdbqt.encode()
    if not raw.strip():
        raise ValueError("Vina 构象文件为空。")
    if len(raw) > _MAX_STRUCTURE_BYTES:
        raise ValueError("Vina 构象文件超过 50 MB。")
    text = raw.decode("utf-8", errors="strict")
    lines = text.splitlines(keepends=True)
    has_models = any(line[:5].upper() == "MODEL" for line in lines)
    if not has_models:
        if mode != 1:
            raise ValueError("单构象 PDBQT 只有 mode 1。")
        return raw

    selected: list[str] = []
    active = False
    for line in lines:
        if line[:5].upper() == "MODEL":
            fields = line.split()
            try:
                current_mode = int(fields[1])
            except (IndexError, ValueError):
                current_mode = -1
            active = current_mode == mode
        if active:
            selected.append(line)
        if active and line[:6].upper() == "ENDMDL":
            break
    if not selected or selected[-1][:6].upper() != "ENDMDL":
        raise ValueError(f"未找到完整的 Vina mode {mode}。")
    return "".join(selected).encode("utf-8")


def _pdbqt_atom_fields(
    line: str,
) -> tuple[str, float, float, float, str]:
    atom_name = line[12:16].strip() if len(line) >= 16 else ""
    try:
        coordinates = (
            float(line[30:38]),
            float(line[38:46]),
            float(line[46:54]),
        )
    except (ValueError, IndexError):
        fields = line.split()
        if len(fields) < 8:
            raise ValueError("PDBQT 原子行字段不足。")
        atom_name = atom_name or fields[2]
        start = 6 if not re_full_int(fields[4]) else 5
        coordinates = (
            float(fields[start]),
            float(fields[start + 1]),
            float(fields[start + 2]),
        )
    if not atom_name or not all(math.isfinite(value) for value in coordinates):
        raise ValueError("PDBQT 原子名称或坐标无效。")
    fields = line.split()
    atom_type = fields[-1] if fields else atom_name[:2]
    return atom_name, *coordinates, _element_from_autodock_type(atom_type)


def re_full_int(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


def _element_from_autodock_type(atom_type: str) -> str:
    normalized = atom_type.strip()
    mapping = {
        "A": "C",
        "Br": "Br",
        "C": "C",
        "Cl": "Cl",
        "F": "F",
        "HD": "H",
        "I": "I",
        "N": "N",
        "NA": "N",
        "OA": "O",
        "O": "O",
        "P": "P",
        "S": "S",
        "SA": "S",
    }
    if normalized in mapping:
        return mapping[normalized]
    letters = "".join(character for character in normalized if character.isalpha())
    if not letters:
        return "C"
    return letters[:2].title()


def _pdb_chain_ids(lines: Sequence[str]) -> set[str]:
    return {
        line[21:22].strip()
        for line in lines
        if line[:6].strip().upper() in {"ATOM", "HETATM"} and len(line) > 21
    }


def build_complex_pdb(
    receptor_approval: ReceptorApproval,
    pose_pdbqt: bytes | str,
    *,
    ligand_residue_number: int = 9999,
) -> ComplexPDBArtifact:
    """Create a data-only PDB complex; no input text is executed as commands."""

    if not 1 <= ligand_residue_number <= 9999:
        raise ValueError("配体残基编号必须在 1 到 9999 之间。")
    receptor_raw = receptor_approval.selected_receptor_pdb
    pose_raw = pose_pdbqt if isinstance(pose_pdbqt, bytes) else pose_pdbqt.encode()
    if not receptor_raw.strip() or not pose_raw.strip():
        raise ValueError("受体和配体构象都不能为空。")
    if len(receptor_raw) > _MAX_STRUCTURE_BYTES or len(pose_raw) > _MAX_STRUCTURE_BYTES:
        raise ValueError("受体或配体构象超过 50 MB。")
    if _sha256(receptor_raw) != receptor_approval.selected_receptor_pdb_sha256:
        raise ValueError("可视化受体与 ReceptorApproval SHA-256 不一致。")
    receptor_text = receptor_raw.decode("utf-8", errors="strict")
    pose_text = pose_raw.decode("utf-8", errors="strict")

    model_count = sum(
        line[:5].upper() == "MODEL" for line in receptor_text.splitlines()
    )
    if model_count > 1:
        raise ValueError("受体 PDB 含多个模型；必须先人工选择一个模型。")
    receptor_lines = [
        line.rstrip("\r\n")
        for line in receptor_text.splitlines()
        if line[:6].strip().upper() in {"ATOM", "HETATM"}
    ]
    if not receptor_lines:
        raise ValueError("受体 PDB 没有 ATOM/HETATM 记录。")
    used_chains = _pdb_chain_ids(receptor_lines)
    expected_chains = {
        "" if chain == "__blank__" else chain
        for chain in receptor_approval.selected_chains
    }
    polymer_chains = {
        line[21:22].strip()
        for line in receptor_lines
        if line[:6].strip().upper() == "ATOM"
    }
    if polymer_chains != expected_chains:
        raise ValueError("可视化受体链与 ReceptorApproval 不一致。")
    ligand_chain = next(
        (
            candidate
            for candidate in _LIGAND_CHAIN_CANDIDATES
            if candidate not in used_chains
        ),
        None,
    )
    if ligand_chain is None:
        raise ValueError("受体占用了所有单字符链 ID，无法安全加入配体。")

    receptor_serials: list[int] = []
    for line in receptor_lines:
        try:
            receptor_serials.append(int(line[6:11]))
        except (ValueError, IndexError):
            continue
    serial = max(receptor_serials, default=0) + 1
    ligand_lines: list[str] = []
    for line in pose_text.splitlines():
        if line[:6].strip().upper() not in {"ATOM", "HETATM"}:
            continue
        if serial > 99999:
            raise ValueError("复合物原子序号超过 PDB 格式上限。")
        atom_name, x, y, z, element = _pdbqt_atom_fields(line)
        ligand_lines.append(
            f"HETATM{serial:5d} {atom_name:>4s} LIG {ligand_chain}"
            f"{ligand_residue_number:4d}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}"
            f"{1.00:6.2f}{0.00:6.2f}          {element:>2s}"
        )
        serial += 1
    if not ligand_lines:
        raise ValueError("Vina 构象没有可解析的 ATOM/HETATM 记录。")

    payload = (
        "\n".join([*receptor_lines, *ligand_lines, "END"]) + "\n"
    ).encode("ascii", errors="strict")
    return ComplexPDBArtifact(
        payload=payload,
        sha256=_sha256(payload),
        receptor_atom_count=len(receptor_lines),
        ligand_atom_count=len(ligand_lines),
        ligand_chain=ligand_chain,
        ligand_residue_number=ligand_residue_number,
    )


def generate_pymol_script(
    *,
    complex_sha256: str,
    ligand_chain: str,
    ligand_residue_number: int,
    vina_score_kcal_mol: float,
    pose_mode: int,
    pocket_distance_angstrom: float = 5.0,
    width: int = 1600,
    height: int = 1200,
    dpi: int = 300,
) -> GeneratedPyMOLScript:
    """Generate a fixed-command, editable PML script with no raw user text."""

    if (
        len(ligand_chain) != 1
        or not ligand_chain.isascii()
        or not ligand_chain.isalnum()
    ):
        raise ValueError("PML 配体链必须是单个 ASCII 字母或数字。")
    if not 1 <= ligand_residue_number <= 9999:
        raise ValueError("PML 配体残基编号超出范围。")
    if not math.isfinite(vina_score_kcal_mol):
        raise ValueError("Vina 预测评分必须是有限数值。")
    if pose_mode < 1 or not 1.0 <= pocket_distance_angstrom <= 15.0:
        raise ValueError("PML pose 或口袋距离无效。")
    if not 100 <= width <= 10000 or not 100 <= height <= 10000:
        raise ValueError("PML 图片尺寸必须在 100 到 10000 像素之间。")
    if not 72 <= dpi <= 1200:
        raise ValueError("PML DPI 必须在 72 到 1200 之间。")
    if (
        len(complex_sha256) != 64
        or any(character not in "0123456789abcdef" for character in complex_sha256)
    ):
        raise ValueError("PML 绑定的 complex SHA-256 无效。")

    score = format(vina_score_kcal_mol, ".6g")
    distance = format(pocket_distance_angstrom, ".6g")
    script = "\n".join(
        [
            "# VetEvidence generated script; edit styles and camera as needed.",
            f"# Bound-Complex-SHA256: {complex_sha256}",
            f"# Vina 预测评分; mode={pose_mode}; score_kcal_mol={score}",
            "reinitialize",
            "load complex.pdb, docking_complex",
            "hide everything, all",
            "show cartoon, polymer.protein",
            "color slate, polymer.protein",
            (
                "select docked_ligand, "
                f"chain {ligand_chain} and resn LIG "
                f"and resi {ligand_residue_number}"
            ),
            "show sticks, docked_ligand",
            "color orange, docked_ligand",
            (
                "select pocket_residues, byres "
                f"(polymer.protein within {distance} of docked_ligand)"
            ),
            "show sticks, pocket_residues",
            "color cyan, pocket_residues",
            "hide everything, solvent",
            "bg_color white",
            "set antialias, 2",
            "set ray_opaque_background, off",
            "set stick_radius, 0.18",
            "orient docked_ligand",
            "zoom docked_ligand, 10",
            "save interaction.pse",
            (
                "png interaction.png, "
                f"width={width}, height={height}, dpi={dpi}, ray=1"
            ),
            "quit",
            "",
        ]
    )
    payload = script.encode("utf-8")
    return GeneratedPyMOLScript(
        payload=payload,
        sha256=_sha256(payload),
        bound_complex_sha256=complex_sha256,
    )


def render_with_pymol(
    *,
    tool: VerifiedExternalTool,
    complex_pdb: ComplexPDBArtifact,
    pml_script: GeneratedPyMOLScript,
    user_confirmed: bool,
    runner: Runner | None = None,
    timeout_seconds: float = 180.0,
) -> PyMOLRenderResult:
    """Run system-generated PML headlessly; missing PyMOL yields no fake files."""

    if not tool.available:
        reason = tool.reason or "PyMOL unavailable"
        return PyMOLRenderResult(
            png=_unavailable("PyMOL PNG", reason),
            pse=_unavailable("PyMOL PSE", reason),
        )
    if not user_confirmed:
        raise PermissionError("必须由用户显式确认后才能运行 PyMOL 无头渲染。")
    if (
        _sha256(complex_pdb.payload) != complex_pdb.sha256
        or pml_script.bound_complex_sha256 != complex_pdb.sha256
        or _sha256(pml_script.payload) != pml_script.sha256
    ):
        raise ValueError("PyMOL 输入没有通过 complex/PML SHA-256 绑定校验。")
    if timeout_seconds <= 0:
        raise ValueError("PyMOL 渲染超时必须大于 0。")
    try:
        path = _assert_tool_unchanged(tool)
        active_runner = runner or subprocess.run
        with tempfile.TemporaryDirectory(
            prefix="vetevidence-pymol-"
        ) as temporary:
            root = Path(temporary)
            (root / "complex.pdb").write_bytes(complex_pdb.payload)
            (root / "view.pml").write_bytes(pml_script.payload)
            command = [str(path), "-cq", "view.pml"]
            completed = active_runner(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                **_hidden_window_options(),
            )
            stdout = _bounded_text(completed.stdout)
            stderr = _bounded_text(completed.stderr)
            if completed.returncode != 0:
                reason = (
                    f"PyMOL 返回非零退出码 {completed.returncode}："
                    f"{stderr or stdout}"
                )
                return PyMOLRenderResult(
                    png=_unavailable("PyMOL PNG", reason),
                    pse=_unavailable("PyMOL PSE", reason),
                    command=tuple([path.name, "-cq", "view.pml"]),
                    stdout=stdout,
                    stderr=stderr,
                )
            _assert_tool_unchanged(tool)
            png_path = root / "interaction.png"
            pse_path = root / "interaction.pse"
            png_payload = (
                png_path.read_bytes()
                if png_path.is_file()
                and 0 < png_path.stat().st_size <= _MAX_TOOL_OUTPUT_BYTES
                else b""
            )
            try:
                _validate_png_payload(png_payload)
                png = _available(
                    "PyMOL PNG",
                    "interaction.png",
                    "image/png",
                    png_payload,
                )
            except ValueError as exc:
                png = _unavailable("PyMOL PNG", f"PyMOL PNG 验证失败：{exc}")
            pse = (
                _generated_unverified(
                    "PyMOL PSE",
                    "interaction.pse",
                    "application/octet-stream",
                    pse_path.read_bytes(),
                    reason="PSE 已由 PyMOL 生成，但未执行二次重开验证。",
                )
                if pse_path.is_file()
                and 0 < pse_path.stat().st_size <= _MAX_TOOL_OUTPUT_BYTES
                else _unavailable("PyMOL PSE", "PyMOL 未生成有效 PSE。")
            )
            return PyMOLRenderResult(
                png=png,
                pse=pse,
                command=tuple([path.name, "-cq", "view.pml"]),
                stdout=stdout,
                stderr=stderr,
            )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        return PyMOLRenderResult(
            png=_unavailable("PyMOL PNG", reason),
            pse=_unavailable("PyMOL PSE", reason),
        )


def _first_tool_output(
    root: Path,
    suffix: str,
    *,
    source_filenames: Sequence[str],
    artifact: str,
    filename: str,
    media_type: str,
) -> OptionalArtifact:
    expected_names = {name.casefold() for name in source_filenames}
    if (
        not expected_names
        or any(Path(name).name != name for name in source_filenames)
        or any(not name.casefold().endswith(suffix.casefold()) for name in source_filenames)
    ):
        raise ValueError("PLIP 预期输出文件名无效。")
    candidates = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.casefold() in expected_names
    )
    valid_candidates = [
        path
        for path in candidates
        if 0 < path.stat().st_size <= _MAX_TOOL_OUTPUT_BYTES
    ]
    if len(valid_candidates) > 1:
        return _unavailable(
            artifact,
            f"PLIP 生成了多个匹配的 {suffix} 候选，无法唯一绑定产物。",
        )
    for path in valid_candidates:
        payload = path.read_bytes()
        if media_type == "image/png":
            try:
                _validate_png_payload(payload)
            except ValueError:
                continue
        if suffix.casefold() == ".pse":
            return _generated_unverified(
                artifact,
                filename,
                media_type,
                payload,
                reason="PSE 已由 PLIP 生成，但未执行 PyMOL 二次重开验证。",
            )
        return _available(
            artifact,
            filename,
            media_type,
            payload,
        )
    expected_label = "、".join(source_filenames)
    return _unavailable(
        artifact,
        f"PLIP 未生成预期的 {suffix} 文件（{expected_label}）。",
    )


def _plip_xml_matches_selected_ligand(
    payload: bytes,
    *,
    ligand_chain: str,
    ligand_residue_number: int,
) -> bool:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return False

    def local_name(tag: str) -> str:
        return tag.rsplit("}", maxsplit=1)[-1].casefold()

    expected_position = str(ligand_residue_number)
    for element in root.iter():
        if local_name(element.tag) not in {
            "identifiers",
            "ligand",
            "binding_site",
            "bindingsite",
        }:
            continue
        values: dict[str, set[str]] = {}
        for child in element.iter():
            text = (child.text or "").strip()
            if text:
                values.setdefault(local_name(child.tag), set()).add(text)
        ligand_ids = values.get("hetid", set()) | values.get(
            "ligand_id", set()
        )
        chains = values.get("chain", set()) | values.get("reschain", set())
        positions = values.get("position", set()) | values.get("resnr", set())
        if (
            "LIG" in {value.upper() for value in ligand_ids}
            and ligand_chain in chains
            and expected_position in positions
        ):
            return True
    return False


def analyze_with_plip(
    *,
    tool: VerifiedExternalTool,
    complex_pdb: ComplexPDBArtifact,
    ligand_chain: str,
    ligand_residue_number: int,
    user_confirmed: bool,
    runner: Runner | None = None,
    timeout_seconds: float = 180.0,
) -> PLIPAnalysisResult:
    """Run PLIP as an optional GPL external CLI with fixed arguments."""

    if not tool.available:
        reason = tool.reason or "PLIP unavailable"
        return PLIPAnalysisResult(
            xml=_unavailable("PLIP XML", reason),
            text=_unavailable("PLIP TXT", reason),
            png=_unavailable("PLIP PNG", reason),
            pse=_unavailable("PLIP PSE", reason),
        )
    if not user_confirmed:
        raise PermissionError("必须由用户显式确认后才能运行 PLIP。")
    if (
        _sha256(complex_pdb.payload) != complex_pdb.sha256
        or ligand_chain != complex_pdb.ligand_chain
        or ligand_residue_number != complex_pdb.ligand_residue_number
    ):
        raise ValueError("PLIP 配体选择没有绑定当前 complex PDB。")
    if timeout_seconds <= 0:
        raise ValueError("PLIP 分析超时必须大于 0。")
    try:
        if tool.runtime_environment is None:
            raise ValueError("PLIP 未绑定已审计的 Open Babel runtime。")
        controlled_environment = _assert_plip_runtime_unchanged(
            tool.runtime_environment
        )
        path = _assert_tool_unchanged(tool)
        active_runner = runner or subprocess.run
        with tempfile.TemporaryDirectory(
            prefix="vetevidence-plip-"
        ) as temporary:
            root = Path(temporary)
            output_root = root / "plip-output"
            output_root.mkdir()
            (root / "complex.pdb").write_bytes(complex_pdb.payload)
            command = [
                str(path),
                "-f",
                "complex.pdb",
                "-x",
                "-t",
                "-o",
                "plip-output",
            ]
            completed = active_runner(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                env=controlled_environment,
                **_hidden_window_options(),
            )
            stdout = _bounded_text(completed.stdout)
            stderr = _bounded_text(completed.stderr)
            normalized_command = tuple(
                [
                    path.name,
                    "-f",
                    "complex.pdb",
                    "-x",
                    "-t",
                    "-o",
                    "plip-output",
                ]
            )
            if completed.returncode != 0:
                reason = (
                    f"PLIP 返回非零退出码 {completed.returncode}："
                    f"{stderr or stdout}"
                )
                return PLIPAnalysisResult(
                    xml=_unavailable("PLIP XML", reason),
                    text=_unavailable("PLIP TXT", reason),
                    png=_unavailable("PLIP PNG", reason),
                    pse=_unavailable("PLIP PSE", reason),
                    command=normalized_command,
                    stdout=stdout,
                    stderr=stderr,
                )
            _assert_tool_unchanged(tool)
            _assert_plip_runtime_unchanged(tool.runtime_environment)
            xml = _first_tool_output(
                output_root,
                ".xml",
                source_filenames=("complex_report.xml", "report.xml"),
                artifact="PLIP XML",
                filename="plip_report.xml",
                media_type="application/xml",
            )
            if xml.status != "available" or xml.payload is None:
                reason = xml.reason or "PLIP 未生成可验证的 XML。"
                return PLIPAnalysisResult(
                    xml=xml,
                    text=_unavailable("PLIP TXT", reason),
                    png=_unavailable("PLIP PNG", reason),
                    pse=_unavailable("PLIP PSE", reason),
                    command=normalized_command,
                    stdout=stdout,
                    stderr=stderr,
                )
            if not _plip_xml_matches_selected_ligand(
                xml.payload,
                ligand_chain=ligand_chain,
                ligand_residue_number=ligand_residue_number,
            ):
                reason = (
                    "PLIP XML 未能唯一绑定当前 docked LIG "
                    f"{ligand_chain}:{ligand_residue_number}。"
                )
                return PLIPAnalysisResult(
                    xml=_unavailable("PLIP XML", reason),
                    text=_unavailable("PLIP TXT", reason),
                    png=_unavailable("PLIP PNG", reason),
                    pse=_unavailable("PLIP PSE", reason),
                    command=normalized_command,
                    stdout=stdout,
                    stderr=stderr,
                )
            return PLIPAnalysisResult(
                xml=xml,
                text=_first_tool_output(
                    output_root,
                    ".txt",
                    source_filenames=("complex_report.txt", "report.txt"),
                    artifact="PLIP TXT",
                    filename="plip_report.txt",
                    media_type="text/plain",
                ),
                png=_unavailable(
                    "PLIP PNG",
                    "首版不调用 PLIP 可视化；请使用已验证的 PyMOL/3Dmol 图。",
                ),
                pse=_unavailable(
                    "PLIP PSE",
                    "首版不调用 PLIP 可视化；请使用已验证的 PyMOL/3Dmol 图。",
                ),
                command=normalized_command,
                stdout=stdout,
                stderr=stderr,
            )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        return PLIPAnalysisResult(
            xml=_unavailable("PLIP XML", reason),
            text=_unavailable("PLIP TXT", reason),
            png=_unavailable("PLIP PNG", reason),
            pse=_unavailable("PLIP PSE", reason),
        )


def _artifact_manifest(artifact: OptionalArtifact) -> dict[str, object]:
    return {
        "status": artifact.status,
        "filename": artifact.filename,
        "sha256": artifact.sha256,
        "reason": artifact.reason,
    }


def _tool_manifest(tool: VerifiedExternalTool) -> dict[str, object]:
    return {
        "available": tool.available,
        "version_output": tool.version_output,
        "executable_sha256": tool.executable_sha256,
        "runtime_environment": (
            tool.runtime_environment.model_dump(mode="json")
            if tool.runtime_environment is not None
            else None
        ),
        "reason": tool.reason,
    }


def _selected_pose_csv(
    *,
    pose_mode: int,
    seed: int,
    vina_score_kcal_mol: float,
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["mode", "seed", "metric", "score_kcal_mol"])
    writer.writerow(
        [pose_mode, seed, "Vina 预测评分", format(vina_score_kcal_mol, ".15g")]
    )
    return buffer.getvalue().encode("utf-8-sig")


def _checksum_file(files: Mapping[str, bytes]) -> bytes:
    return "".join(
        f"{_sha256(files[name])}  {name}\n" for name in sorted(files)
    ).encode("utf-8")


def _deterministic_zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for filename in sorted(files):
            safe_name = _safe_archive_name(filename)
            info = zipfile.ZipInfo(safe_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, files[filename])
    return output.getvalue()


def build_visualization_package(
    *,
    batch: DockingBatchResult,
    ligand_id: str,
    seed: int,
    pose_mode: int,
    user_confirmed_external_tools: bool,
    pymol_tool: VerifiedExternalTool | None = None,
    plip_tool: VerifiedExternalTool | None = None,
    pymol_runner: Runner | None = None,
    plip_runner: Runner | None = None,
    additional_files: Mapping[str, bytes] | None = None,
    created_at: datetime | None = None,
) -> DockingVisualizationPackage:
    """Build a ZIP strictly derived from one revalidated batch attempt."""

    preparations = [
        item for item in batch.preparations if item.ligand_id == ligand_id
    ]
    attempts = [
        item
        for item in batch.attempts
        if item.ligand_id == ligand_id and item.seed == seed
    ]
    if len(preparations) != 1 or len(attempts) != 1:
        raise ValueError("可视化必须唯一定位一个配体准备记录和 Vina 尝试。")
    preparation = preparations[0]
    attempt = attempts[0]
    validate_successful_docking_attempt(
        attempt,
        preparation,
        batch.receptor_approval,
    )
    assert attempt.manifest is not None
    assert attempt.docking_run is not None
    assert attempt.execution_metadata is not None
    assert attempt.bound_log is not None
    assert attempt.output_pdbqt is not None
    assert preparation.prepared_pdbqt is not None
    selected_pose = next(
        (
            candidate
            for candidate in attempt.docking_run.poses
            if candidate.mode == pose_mode
        ),
        None,
    )
    if selected_pose is None:
        raise ValueError("所选 Vina pose mode 不在已验证模式表中。")
    vina_score_kcal_mol = selected_pose.affinity_kcal_mol
    pose = extract_vina_pose(attempt.output_pdbqt, mode=pose_mode)
    complex_artifact = build_complex_pdb(batch.receptor_approval, pose)
    pml_script = generate_pymol_script(
        complex_sha256=complex_artifact.sha256,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        vina_score_kcal_mol=vina_score_kcal_mol,
        pose_mode=pose_mode,
    )
    active_pymol = pymol_tool or VerifiedExternalTool(
        tool="PyMOL",
        available=False,
        reason="未配置 PyMOL；PML 仍可下载并由用户自行运行。",
    )
    active_plip = plip_tool or VerifiedExternalTool(
        tool="PLIP",
        available=False,
        reason="未配置 PLIP；不生成相互作用 XML/TXT/PNG/PSE。",
    )
    pymol_render = render_with_pymol(
        tool=active_pymol,
        complex_pdb=complex_artifact,
        pml_script=pml_script,
        user_confirmed=user_confirmed_external_tools,
        runner=pymol_runner,
    )
    plip_analysis = analyze_with_plip(
        tool=active_plip,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=user_confirmed_external_tools,
        runner=plip_runner,
    )

    vina_manifest_payload = json.dumps(
        attempt.manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    execution_payload = json.dumps(
        attempt.execution_metadata.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receptor_approval_payload = json.dumps(
        batch.receptor_approval.model_dump(
            mode="json",
            exclude={"selected_receptor_pdb"},
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ligand_identity_payload = json.dumps(
        preparation.identity.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    files: dict[str, bytes] = {
        "receptor_original": batch.receptor_original_payload,
        "receptor_selected.pdb": batch.receptor_approval.selected_receptor_pdb,
        "receptor_prepared.pdbqt": batch.receptor_pdbqt,
        "receptor_approval.json": receptor_approval_payload,
        "ligand_original": preparation.original_payload,
        "ligand_prepared.pdbqt": preparation.prepared_pdbqt,
        "ligand_identity.json": ligand_identity_payload,
        "vina_manifest.json": vina_manifest_payload,
        "vina_execution_audit.json": execution_payload,
        "vina_out.pdbqt": attempt.output_pdbqt,
        "vina_bound.log": attempt.bound_log,
        "selected_pose.pdbqt": pose,
        "selected_pose.csv": _selected_pose_csv(
            pose_mode=pose_mode,
            seed=seed,
            vina_score_kcal_mol=vina_score_kcal_mol,
        ),
        "complex.pdb": complex_artifact.payload,
        "view.pml": pml_script.payload,
    }
    for filename, payload in (additional_files or {}).items():
        safe_name = _safe_archive_name(filename)
        if safe_name in files:
            raise ValueError(f"附加产物与保留文件重名：{safe_name}。")
        files[safe_name] = payload

    optional_artifacts = (
        pymol_render.png,
        pymol_render.pse,
        plip_analysis.xml,
        plip_analysis.text,
        plip_analysis.png,
        plip_analysis.pse,
    )
    for artifact in optional_artifacts:
        if (
            artifact.status in {"available", "generated_unverified"}
            and artifact.filename is not None
            and artifact.payload is not None
        ):
            if artifact.filename in files:
                raise ValueError(f"外部工具产物重名：{artifact.filename}。")
            files[artifact.filename] = artifact.payload

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("可视化清单时间必须包含时区。")
    manifest = {
        "schema_version": "vetevidence-docking-visualization-v2",
        "created_at": timestamp.isoformat(),
        "batch_id": batch.batch_id,
        "ligand_id": ligand_id,
        "score": {
            "metric": "Vina 预测评分",
            "mode": pose_mode,
            "seed": seed,
            "value_kcal_mol": vina_score_kcal_mol,
            "interpretation": "computational_prediction",
        },
        "selection": {
            "receptor_model": batch.receptor_approval.selected_model,
            "receptor_chains": list(batch.receptor_approval.selected_chains),
            "ligand_chain": complex_artifact.ligand_chain,
            "ligand_residue_number": complex_artifact.ligand_residue_number,
        },
        "inputs": {
            "receptor_original_sha256": (
                batch.receptor_approval.receptor_structure_sha256
            ),
            "receptor_selected_sha256": (
                batch.receptor_approval.selected_receptor_pdb_sha256
            ),
            "receptor_pdbqt_sha256": (
                batch.receptor_approval.receptor_pdbqt_sha256
            ),
            "ligand_original_sha256": preparation.original_sha256,
            "ligand_prepared_sha256": preparation.prepared_pdbqt_sha256,
            "vina_manifest_sha256": attempt.manifest.manifest_sha256,
            "vina_output_sha256": (
                attempt.execution_metadata.output_pdbqt_sha256
            ),
            "vina_log_sha256": attempt.docking_run.output_source.sha256,
            "selected_pose_sha256": _sha256(pose),
            "complex_pdb_sha256": complex_artifact.sha256,
            "pml_sha256": pml_script.sha256,
        },
        "scientific_limits": {
            "score_interpretation": "computational_prediction",
            "complex_conversion": (
                "Ligand PDBQT coordinates were projected into PDB for "
                "visualization; PDB does not preserve PDBQT charges or "
                "torsion tree."
            ),
            "pse_validation": (
                "generated_unverified unless separately reopened"
            ),
        },
        "tools": {
            "PyMOL": _tool_manifest(active_pymol),
            "PLIP": _tool_manifest(active_plip),
        },
        "availability": {
            "pymol_png": _artifact_manifest(pymol_render.png),
            "pymol_pse": _artifact_manifest(pymol_render.pse),
            "plip_xml": _artifact_manifest(plip_analysis.xml),
            "plip_text": _artifact_manifest(plip_analysis.text),
            "plip_png": _artifact_manifest(plip_analysis.png),
            "plip_pse": _artifact_manifest(plip_analysis.pse),
        },
        "file_sha256": {
            filename: _sha256(payload) for filename, payload in sorted(files.items())
        },
    }
    files["visualization_manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    files["SHA256SUMS.txt"] = _checksum_file(files)
    archive = _deterministic_zip(files)
    package_files = tuple(
        PackageFile(
            filename=filename,
            payload=files[filename],
            sha256=_sha256(files[filename]),
        )
        for filename in sorted(files)
    )
    return DockingVisualizationPackage(
        batch_id=batch.batch_id,
        ligand_id=ligand_id,
        seed=seed,
        pose_mode=pose_mode,
        receptor_model=batch.receptor_approval.selected_model,
        receptor_chains=batch.receptor_approval.selected_chains,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        files=package_files,
        zip_payload=archive,
        zip_sha256=_sha256(archive),
        task_manifest_sha256=attempt.manifest.manifest_sha256,
        complex_pdb_sha256=complex_artifact.sha256,
        pml_sha256=pml_script.sha256,
        pymol_render=pymol_render,
        plip_analysis=plip_analysis,
    )


def launch_pymol_session(
    *,
    tool: VerifiedExternalTool,
    package: DockingVisualizationPackage,
    session_relative_path: str,
    allowed_root: str | os.PathLike[str],
    user_confirmed: bool,
    popen_factory: PopenFactory | None = None,
) -> object:
    """Open only a verified, in-scope PML/PSE after an explicit UI click."""

    if not user_confirmed:
        raise PermissionError("必须由用户显式点击后才能打开 PyMOL。")
    executable = _assert_tool_unchanged(tool)
    root = Path(allowed_root).resolve(strict=True)
    safe_relative_path = _safe_archive_name(session_relative_path)
    if safe_relative_path not in {
        "view.pml",
        "interaction.pse",
        "plip_interaction.pse",
    }:
        raise ValueError("只能打开任务生成的固定 PyMOL PML/PSE 产物。")
    package_file = next(
        (item for item in package.files if item.filename == safe_relative_path),
        None,
    )
    if package_file is None:
        raise ValueError("PyMOL 文件不属于当前可视化任务包。")
    if _sha256(package_file.payload) != package_file.sha256:
        raise ValueError("PyMOL 任务包文件 SHA-256 校验失败。")
    session = (root / safe_relative_path).resolve(strict=True)
    try:
        session.relative_to(root)
    except ValueError as exc:
        raise ValueError("PyMOL 文件不在当前任务目录内。") from exc
    if not session.is_file() or session.suffix.casefold() not in {".pml", ".pse"}:
        raise ValueError("PyMOL 只能打开当前任务生成的 PML/PSE 文件。")
    if _sha256_file(session) != package_file.sha256:
        raise ValueError("PyMOL 文件与任务清单记录的 SHA-256 不一致。")
    if session.suffix.casefold() == ".pml" and (
        package_file.sha256 != package.pml_sha256
        or package.file_payload("complex.pdb") == b""
        or _sha256(package.file_payload("complex.pdb"))
        != package.complex_pdb_sha256
    ):
        raise ValueError("PML 没有绑定当前任务的 complex PDB。")
    active_popen = popen_factory or subprocess.Popen
    return active_popen(
        [str(executable), str(session)],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


__all__ = [
    "ComplexPDBArtifact",
    "DockingVisualizationPackage",
    "GeneratedPyMOLScript",
    "OptionalArtifact",
    "PLIPRuntimeDirectoryAudit",
    "PLIPRuntimeEnvironment",
    "PLIPAnalysisResult",
    "PackageFile",
    "PyMOLRenderResult",
    "RuntimeMarkerAudit",
    "VerifiedExternalTool",
    "analyze_with_plip",
    "build_complex_pdb",
    "build_visualization_package",
    "extract_vina_pose",
    "generate_pymol_script",
    "launch_pymol_session",
    "render_with_pymol",
    "verify_external_executable",
    "verify_plip_executable",
    "verify_plip_runtime_environment",
    "verify_pymol_executable",
]
