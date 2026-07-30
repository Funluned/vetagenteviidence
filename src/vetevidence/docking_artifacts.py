"""Immutable local storage for verified docking visualization packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.docking_visualization import DockingVisualizationPackage


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_PACKAGE_FILENAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_MAX_ZIP_BYTES = 250 * 1024 * 1024


class DockingArtifactModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class DockingArtifactMetadata(DockingArtifactModel):
    schema_version: str = "vetevidence-docking-artifacts-0.5"
    run_id: str = Field(min_length=1, max_length=64)
    batch_id: str = Field(min_length=1, max_length=64)
    ligand_id: str = Field(min_length=1, max_length=64)
    seed: int
    pose_mode: int = Field(ge=1)
    artifact_id: str = Field(min_length=1, max_length=64)
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
    zip_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    zip_size_bytes: int = Field(ge=1, le=_MAX_ZIP_BYTES)
    package_file_sha256: dict[str, str]
    created_at: datetime


class StoredDockingArtifact(DockingArtifactModel):
    directory: Path
    metadata: DockingArtifactMetadata
    zip_payload: bytes = Field(min_length=1, max_length=_MAX_ZIP_BYTES)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_id(value: str, *, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(
            f"{label} 只能包含字母、数字、点、下划线和连字符，且不超过 64 字符。"
        )
    return value


def _artifact_id(
    ligand_id: str,
    seed: int,
    pose_mode: int,
    zip_sha256: str,
) -> str:
    raw = (
        f"{ligand_id}-seed-{seed}-mode-{pose_mode}-"
        f"{zip_sha256[:12]}"
    )
    if _SAFE_ID.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    prefix = ligand_id[:24].rstrip("._-") or "ligand"
    result = f"{prefix}-{digest}"
    return _safe_id(result, label="artifact_id")


class DockingArtifactStore:
    """Save each generated ZIP once and verify it again on every load."""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None):
        self.base_dir = Path(
            base_dir or _PROJECT_ROOT / ".workbench" / "docking"
        ).resolve()

    def save(
        self,
        *,
        run_id: str,
        batch_id: str,
        ligand_id: str,
        seed: int,
        pose_mode: int,
        package: DockingVisualizationPackage,
        created_at: datetime | None = None,
    ) -> StoredDockingArtifact:
        safe_run_id = _safe_id(run_id, label="run_id")
        safe_batch_id = _safe_id(batch_id, label="batch_id")
        safe_ligand_id = _safe_id(ligand_id, label="ligand_id")
        if (
            package.batch_id != safe_batch_id
            or package.ligand_id != safe_ligand_id
            or package.seed != seed
            or package.pose_mode != pose_mode
        ):
            raise ValueError(
                "任务包身份与 batch_id、ligand_id、seed 或 pose_mode 不一致。"
            )
        if len(package.zip_payload) > _MAX_ZIP_BYTES:
            raise ValueError("对接任务 ZIP 超过 250 MB 上限。")
        if _sha256(package.zip_payload) != package.zip_sha256:
            raise ValueError("对接任务 ZIP 内容与 package SHA-256 不一致。")
        artifact_id = _artifact_id(
            safe_ligand_id,
            seed,
            pose_mode,
            package.zip_sha256,
        )
        package_file_sha256: dict[str, str] = {}
        for item in package.files:
            if not _SAFE_PACKAGE_FILENAME.fullmatch(item.filename):
                raise ValueError(f"任务包文件名不安全：{item.filename!r}。")
            if item.filename in package_file_sha256:
                raise ValueError(f"任务包文件名重复：{item.filename}。")
            if _sha256(item.payload) != item.sha256:
                raise ValueError(f"任务包文件 SHA-256 不一致：{item.filename}。")
            package_file_sha256[item.filename] = item.sha256
        metadata = DockingArtifactMetadata(
            run_id=safe_run_id,
            batch_id=safe_batch_id,
            ligand_id=safe_ligand_id,
            seed=seed,
            pose_mode=pose_mode,
            artifact_id=artifact_id,
            task_manifest_sha256=package.task_manifest_sha256,
            complex_pdb_sha256=package.complex_pdb_sha256,
            pml_sha256=package.pml_sha256,
            zip_sha256=package.zip_sha256,
            zip_size_bytes=len(package.zip_payload),
            package_file_sha256=package_file_sha256,
            created_at=created_at or datetime.now(timezone.utc),
        )

        run_directory = (self.base_dir / safe_run_id).resolve()
        if run_directory.parent != self.base_dir:
            raise ValueError("run_id 超出对接产物根目录。")
        run_directory.mkdir(parents=True, exist_ok=True)
        final_directory = (run_directory / artifact_id).resolve()
        if final_directory.parent != run_directory:
            raise ValueError("artifact_id 超出运行目录。")
        if final_directory.exists():
            raise FileExistsError("同一对接可视化产物已经保存，禁止覆盖。")

        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{artifact_id}-", dir=run_directory)
        )
        try:
            (temporary_directory / "package.zip").write_bytes(
                package.zip_payload
            )
            for item in package.files:
                (temporary_directory / item.filename).write_bytes(item.payload)
            (temporary_directory / "metadata.json").write_text(
                json.dumps(
                    metadata.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(temporary_directory, final_directory)
        except Exception:
            for child in temporary_directory.iterdir():
                child.unlink(missing_ok=True)
            temporary_directory.rmdir()
            raise
        return self.load(safe_run_id, artifact_id)

    def load(self, run_id: str, artifact_id: str) -> StoredDockingArtifact:
        safe_run_id = _safe_id(run_id, label="run_id")
        safe_artifact_id = _safe_id(artifact_id, label="artifact_id")
        directory = (self.base_dir / safe_run_id / safe_artifact_id).resolve()
        expected_parent = (self.base_dir / safe_run_id).resolve()
        if directory.parent != expected_parent:
            raise ValueError("对接产物路径超出运行目录。")
        metadata = DockingArtifactMetadata.model_validate_json(
            (directory / "metadata.json").read_text(encoding="utf-8")
        )
        if (
            metadata.run_id != safe_run_id
            or metadata.artifact_id != safe_artifact_id
        ):
            raise ValueError("对接产物元数据与目录身份不一致。")
        manifest = json.loads(
            (directory / "visualization_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        score = manifest.get("score", {})
        if (
            manifest.get("batch_id") != metadata.batch_id
            or manifest.get("ligand_id") != metadata.ligand_id
            or score.get("seed") != metadata.seed
            or score.get("mode") != metadata.pose_mode
        ):
            raise ValueError("对接任务 manifest 与存储元数据身份不一致。")
        payload = (directory / "package.zip").read_bytes()
        if len(payload) != metadata.zip_size_bytes:
            raise ValueError("对接任务 ZIP 大小与元数据不一致。")
        if _sha256(payload) != metadata.zip_sha256:
            raise ValueError("对接任务 ZIP SHA-256 校验失败。")
        for filename, expected_sha256 in metadata.package_file_sha256.items():
            if not _SAFE_PACKAGE_FILENAME.fullmatch(filename):
                raise ValueError("对接任务包元数据含不安全文件名。")
            candidate = (directory / filename).resolve()
            if candidate.parent != directory:
                raise ValueError("对接任务包文件超出产物目录。")
            file_payload = candidate.read_bytes()
            if _sha256(file_payload) != expected_sha256:
                raise ValueError(
                    f"对接任务包文件 SHA-256 校验失败：{filename}。"
                )
        return StoredDockingArtifact(
            directory=directory,
            metadata=metadata,
            zip_payload=payload,
        )


__all__ = [
    "DockingArtifactMetadata",
    "DockingArtifactStore",
    "StoredDockingArtifact",
]
