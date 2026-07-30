"""Local persistence for task-bound AutoDock Vina output artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = r"^[0-9a-f]{64}$"
_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class VinaArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    manifest_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256,
    )
    log_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256)
    output_pdbqt_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_SHA256,
    )
    created_at: datetime
    execution: dict[str, JsonValue]

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Vina 产物创建时间必须包含时区。")
        return value


class VinaStoredArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: VinaArtifactMetadata
    bound_log: bytes
    output_pdbqt: bytes


class VinaArtifactStore:
    """Store Vina logs and generated poses below the ignored workbench folder."""

    def __init__(self, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.root = (root or project_root / ".workbench" / "vina").resolve()

    @staticmethod
    def _safe_id(value: str, *, label: str) -> str:
        reserved_stem = value.split(".", maxsplit=1)[0].upper()
        if (
            not _SAFE_ID.fullmatch(value)
            or value.endswith(".")
            or reserved_stem in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(
                f"{label} 格式不安全；请使用不超过 64 位的字母、数字、"
                "点、下划线和连字符。"
            )
        return value

    def directory_for(self, run_id: str, task_id: str) -> Path:
        safe_run_id = self._safe_id(run_id, label="运行 ID")
        safe_task_id = self._safe_id(task_id, label="Vina 任务 ID")
        target = (
            self.root / safe_run_id.casefold() / safe_task_id.casefold()
        ).resolve()
        if self.root not in target.parents:
            raise ValueError("Vina 产物路径超出允许目录。")
        return target

    def save(
        self,
        *,
        run_id: str,
        task_id: str,
        manifest_sha256: str,
        bound_log: bytes,
        output_pdbqt: bytes,
        execution: dict[str, JsonValue],
    ) -> VinaStoredArtifacts:
        if not bound_log.strip():
            raise ValueError("Vina 绑定日志为空，不能保存。")
        if not output_pdbqt.strip():
            raise ValueError("Vina 对接构象文件为空，不能保存。")
        metadata = VinaArtifactMetadata(
            run_id=run_id,
            task_id=task_id,
            manifest_sha256=manifest_sha256,
            log_sha256=hashlib.sha256(bound_log).hexdigest(),
            output_pdbqt_sha256=hashlib.sha256(output_pdbqt).hexdigest(),
            created_at=datetime.now(timezone.utc),
            execution=execution,
        )
        metadata_payload = json.dumps(
            metadata.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        target = self.directory_for(run_id, task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError("该 Vina 任务的本地产物已存在，拒绝覆盖。")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.mkdir()
        try:
            (temporary / "run.log").write_bytes(bound_log)
            (temporary / "poses.pdbqt").write_bytes(output_pdbqt)
            (temporary / "metadata.json").write_bytes(metadata_payload)
            temporary.replace(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        return VinaStoredArtifacts(
            metadata=metadata,
            bound_log=bound_log,
            output_pdbqt=output_pdbqt,
        )

    def load(
        self,
        run_id: str,
        task_id: str,
        *,
        expected_manifest_sha256: str | None = None,
    ) -> VinaStoredArtifacts:
        target = self.directory_for(run_id, task_id)
        try:
            metadata = VinaArtifactMetadata.model_validate_json(
                (target / "metadata.json").read_text(encoding="utf-8")
            )
            bound_log = (target / "run.log").read_bytes()
            output_pdbqt = (target / "poses.pdbqt").read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("未找到该 Vina 任务的本地产物。") from exc
        if metadata.run_id != run_id or metadata.task_id != task_id:
            raise ValueError("Vina 产物元数据与请求的运行或任务不一致。")
        if (
            expected_manifest_sha256 is not None
            and metadata.manifest_sha256 != expected_manifest_sha256
        ):
            raise ValueError("Vina 产物绑定的任务清单 SHA-256 不匹配。")
        if hashlib.sha256(bound_log).hexdigest() != metadata.log_sha256:
            raise ValueError("Vina 绑定日志 SHA-256 校验失败。")
        if (
            hashlib.sha256(output_pdbqt).hexdigest()
            != metadata.output_pdbqt_sha256
        ):
            raise ValueError("Vina 对接构象文件 SHA-256 校验失败。")
        return VinaStoredArtifacts(
            metadata=metadata,
            bound_log=bound_log,
            output_pdbqt=output_pdbqt,
        )


__all__ = [
    "VinaArtifactMetadata",
    "VinaArtifactStore",
    "VinaStoredArtifacts",
]
