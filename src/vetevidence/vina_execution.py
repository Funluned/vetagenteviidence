"""Controlled local execution boundary for AutoDock Vina."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.mechanism_prediction import (
    SourceProvenance,
    VinaDockingRun,
    VinaExecutionAudit,
    VinaTaskManifest,
    canonical_manifest_sha256,
    parse_vina_output,
    validate_pdbqt_bytes,
)


_VINA_VERSION_PATTERN = re.compile(
    r"\bAutoDock\s+Vina\s+v?(?P<version>[0-9]+(?:\.[0-9]+){1,2})\b",
    flags=re.IGNORECASE,
)
_FIXED_LIGAND_NAME = "ligand.pdbqt"
_FIXED_RECEPTOR_NAME = "receptor.pdbqt"
_FIXED_OUTPUT_NAME = "output.pdbqt"
_DEFAULT_VERSION_TIMEOUT_SECONDS = 10.0
_DEFAULT_EXECUTION_TIMEOUT_SECONDS = 900.0
_MAX_PDBQT_BYTES = 25 * 1024 * 1024

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class VinaExecutionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class VinaExecutableInfo(VinaExecutionModel):
    """Verified local executable identity returned by discovery."""

    path: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class VinaLocalExecutionMetadata(VinaExecutionModel):
    """JSON-safe local execution audit without temporary file paths."""

    executable_path: str = Field(min_length=1)
    executable_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    executable_version: str = Field(min_length=1)
    arguments: list[str] = Field(min_length=1)
    exit_code: int
    duration_seconds: float = Field(ge=0)
    output_pdbqt_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class VinaExecutionArtifacts(VinaExecutionModel):
    """Successful local artifacts; failures raise without returning a score."""

    docking_run: VinaDockingRun
    metadata: VinaLocalExecutionMetadata
    bound_log: bytes
    output_pdbqt: bytes


class VinaExecutionError(RuntimeError):
    """Raised when local Vina discovery or execution cannot be authenticated."""


def _active_runner(runner: Runner | None) -> Runner:
    return runner or subprocess.run


def _hidden_window_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _payload_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _semantic_version(value: str) -> tuple[int, int, int]:
    normalized = value.strip().removeprefix("v")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", normalized):
        raise VinaExecutionError(f"无法解析 AutoDock Vina 版本：{value!r}。")
    parts = [int(part) for part in normalized.split(".")]
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _resolve_executable(path_value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(path_value).strip().strip('"')
    if not raw:
        raise VinaExecutionError("Vina 可执行文件路径为空。")
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VinaExecutionError(
            f"Vina 可执行文件不存在：{candidate}。"
        ) from exc
    if not resolved.is_file():
        raise VinaExecutionError(f"Vina 路径不是文件：{resolved}。")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise VinaExecutionError(f"Vina 文件不可执行：{resolved}。")
    return resolved


def _inspect_executable(
    path_value: str | os.PathLike[str],
    *,
    runner: Runner | None,
    timeout_seconds: float,
) -> VinaExecutableInfo:
    if timeout_seconds <= 0:
        raise ValueError("Vina 版本检查超时必须大于 0。")
    path = _resolve_executable(path_value)
    active_runner = _active_runner(runner)
    arguments = [str(path), "--version"]
    try:
        completed = active_runner(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
            **_hidden_window_options(),
        )
    except subprocess.TimeoutExpired as exc:
        raise VinaExecutionError(
            f"Vina --version 在 {timeout_seconds:g} 秒后超时：{path}。"
        ) from exc
    except OSError as exc:
        raise VinaExecutionError(f"无法启动 Vina --version：{path}。") from exc
    if completed.returncode != 0:
        raise VinaExecutionError(
            "Vina --version 返回非零退出码 "
            f"{completed.returncode}：{path}。"
        )
    version_output = (
        _payload_bytes(completed.stdout)
        + b"\n"
        + _payload_bytes(completed.stderr)
    )
    match = _VINA_VERSION_PATTERN.search(
        version_output.decode("utf-8", errors="replace")
    )
    if match is None:
        raise VinaExecutionError(
            f"文件未报告有效的 AutoDock Vina 版本：{path}。"
        )
    version = match.group("version")
    _semantic_version(version)
    try:
        executable_sha256 = _sha256_file(path)
    except OSError as exc:
        raise VinaExecutionError(
            f"无法读取 Vina 可执行文件以计算 SHA-256：{path}。"
        ) from exc
    return VinaExecutableInfo(
        path=str(path),
        version=version,
        sha256=executable_sha256,
    )


def _deduplicated_candidates(paths: Sequence[Path]) -> list[Path]:
    selected: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve(strict=False)).casefold()
        except OSError:
            key = str(path.absolute()).casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(path)
    return selected


def _path_candidates(path_value: str) -> list[Path]:
    names = ("vina.exe", "vina") if os.name == "nt" else ("vina", "vina.exe")
    candidates = [
        Path(directory) / name
        for directory in path_value.split(os.pathsep)
        if directory
        for name in names
    ]
    return _deduplicated_candidates(candidates)


def _local_appdata_candidates(local_appdata: str | None) -> list[Path]:
    if not local_appdata:
        return []
    root = Path(local_appdata) / "Programs" / "AutoDockVina"
    candidates = [
        root / "vina.exe",
        *sorted(root.glob("*/vina.exe")),
    ]
    return _deduplicated_candidates(candidates)


def _select_highest_version(
    candidates: Sequence[Path],
    *,
    runner: Runner | None,
    timeout_seconds: float,
) -> VinaExecutableInfo | None:
    inspected: list[VinaExecutableInfo] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            inspected.append(
                _inspect_executable(
                    candidate,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                )
            )
        except VinaExecutionError:
            continue
    if not inspected:
        return None
    return max(
        inspected,
        key=lambda item: (
            _semantic_version(item.version),
            item.path.casefold(),
        ),
    )


def discover_vina(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = _DEFAULT_VERSION_TIMEOUT_SECONDS,
) -> VinaExecutableInfo:
    """Discover and authenticate Vina in strict source-priority order."""

    active_environment = dict(os.environ if environment is None else environment)
    if explicit_path is not None:
        return _inspect_executable(
            explicit_path,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    configured_path = active_environment.get("VINA_EXECUTABLE")
    if configured_path:
        return _inspect_executable(
            configured_path,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    local_match = _select_highest_version(
        _local_appdata_candidates(active_environment.get("LOCALAPPDATA")),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if local_match is not None:
        return local_match

    path_match = _select_highest_version(
        _path_candidates(active_environment.get("PATH", "")),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if path_match is not None:
        return path_match

    raise VinaExecutionError(
        "未找到可验证的 AutoDock Vina。请提供显式路径、"
        "VINA_EXECUTABLE，安装到 "
        "LOCALAPPDATA/Programs/AutoDockVina/<version>/vina.exe，"
        "或加入 PATH。"
    )


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _manifest_arguments(manifest: VinaTaskManifest) -> list[str]:
    parameters = manifest.parameters
    arguments = [
        "--receptor",
        _FIXED_RECEPTOR_NAME,
        "--ligand",
        _FIXED_LIGAND_NAME,
        "--center_x",
        _format_number(parameters.center_x),
        "--center_y",
        _format_number(parameters.center_y),
        "--center_z",
        _format_number(parameters.center_z),
        "--size_x",
        _format_number(parameters.size_x),
        "--size_y",
        _format_number(parameters.size_y),
        "--size_z",
        _format_number(parameters.size_z),
        "--exhaustiveness",
        str(parameters.exhaustiveness),
        "--num_modes",
        str(parameters.num_modes),
        "--energy_range",
        _format_number(parameters.energy_range),
        "--out",
        _FIXED_OUTPUT_NAME,
    ]
    if parameters.seed is not None:
        arguments.extend(["--seed", str(parameters.seed)])
    return arguments


def _bounded_error_output(payload: bytes | str | None) -> str:
    raw = _payload_bytes(payload)
    if len(raw) > 2000:
        raw = raw[-2000:]
    return raw.decode("utf-8", errors="replace").strip()


def _bound_log(
    manifest: VinaTaskManifest,
    stdout: bytes,
    stderr: bytes,
) -> bytes:
    header = (
        "VetEvidence-Manifest-SHA256: "
        f"{manifest.manifest_sha256}\n"
    ).encode("ascii")
    log = header + stdout
    if log and not log.endswith(b"\n"):
        log += b"\n"
    if stderr:
        log += b"[Vina stderr]\n" + stderr
    return log


def execute_vina(
    manifest: VinaTaskManifest,
    ligand_pdbqt: bytes,
    receptor_pdbqt: bytes,
    *,
    executable: VinaExecutableInfo | None = None,
    explicit_path: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = _DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    version_timeout_seconds: float = _DEFAULT_VERSION_TIMEOUT_SECONDS,
) -> VinaExecutionArtifacts:
    """Execute exactly one manifest-bound local Vina task."""

    if timeout_seconds <= 0:
        raise ValueError("Vina 执行超时必须大于 0。")
    expected_manifest_sha = canonical_manifest_sha256(manifest)
    if manifest.manifest_sha256 != expected_manifest_sha:
        raise VinaExecutionError("Vina 任务清单的 canonical SHA-256 不一致。")
    if manifest.engine.casefold() != "autodock vina":
        raise VinaExecutionError(
            f"不支持的对接引擎：{manifest.engine}。"
        )
    if len(ligand_pdbqt) > _MAX_PDBQT_BYTES:
        raise VinaExecutionError("配体 PDBQT 超过 25 MB，拒绝执行。")
    if len(receptor_pdbqt) > _MAX_PDBQT_BYTES:
        raise VinaExecutionError("受体 PDBQT 超过 25 MB，拒绝执行。")

    ligand_sha = validate_pdbqt_bytes(ligand_pdbqt, role="ligand")
    receptor_sha = validate_pdbqt_bytes(receptor_pdbqt, role="receptor")
    if ligand_sha != manifest.ligand_source.sha256:
        raise VinaExecutionError("配体 PDBQT SHA-256 与任务清单不一致。")
    if receptor_sha != manifest.receptor_source.sha256:
        raise VinaExecutionError("受体 PDBQT SHA-256 与任务清单不一致。")

    if executable is not None and explicit_path is not None:
        raise ValueError("executable 与 explicit_path 只能提供一个。")
    if executable is None:
        actual_executable = discover_vina(
            explicit_path,
            environment=environment,
            runner=runner,
            timeout_seconds=version_timeout_seconds,
        )
    else:
        actual_executable = _inspect_executable(
            executable.path,
            runner=runner,
            timeout_seconds=version_timeout_seconds,
        )
        if (
            actual_executable.sha256 != executable.sha256
            or _semantic_version(actual_executable.version)
            != _semantic_version(executable.version)
        ):
            raise VinaExecutionError(
                "Vina 可执行文件自发现后已发生变化，拒绝执行。"
            )

    expected_version = manifest.engine_version.removeprefix("v")
    if _semantic_version(actual_executable.version) != _semantic_version(
        expected_version
    ):
        raise VinaExecutionError(
            "Vina 实际版本与任务清单不一致："
            f"实际 {actual_executable.version}，"
            f"清单 {manifest.engine_version}。"
        )

    controlled_arguments = _manifest_arguments(manifest)
    executable_path = Path(actual_executable.path)
    active_runner = _active_runner(runner)
    with tempfile.TemporaryDirectory(prefix="vetevidence-vina-") as temporary:
        working_directory = Path(temporary)
        (working_directory / _FIXED_LIGAND_NAME).write_bytes(ligand_pdbqt)
        (working_directory / _FIXED_RECEPTOR_NAME).write_bytes(receptor_pdbqt)
        command = [actual_executable.path, *controlled_arguments]
        started = time.perf_counter()
        try:
            completed = active_runner(
                command,
                cwd=str(working_directory),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                **_hidden_window_options(),
            )
        except subprocess.TimeoutExpired as exc:
            raise VinaExecutionError(
                f"Vina 执行在 {timeout_seconds:g} 秒后超时，未生成分数。"
            ) from exc
        except OSError as exc:
            raise VinaExecutionError("无法启动本机 Vina，未生成分数。") from exc
        duration_seconds = max(0.0, time.perf_counter() - started)
        stdout = _payload_bytes(completed.stdout)
        stderr = _payload_bytes(completed.stderr)
        if completed.returncode != 0:
            details = _bounded_error_output(stderr or stdout)
            suffix = f"：{details}" if details else "。"
            raise VinaExecutionError(
                f"Vina 返回非零退出码 {completed.returncode}{suffix}"
            )

        if _sha256_file(executable_path) != actual_executable.sha256:
            raise VinaExecutionError(
                "Vina 可执行文件在运行期间发生变化，拒绝解析分数。"
            )
        output_path = working_directory / _FIXED_OUTPUT_NAME
        if not output_path.is_file():
            raise VinaExecutionError(
                "Vina 未生成 output.pdbqt，不能生成对接分数。"
            )
        output_pdbqt = output_path.read_bytes()
        output_pdbqt_sha256 = validate_pdbqt_bytes(
            output_pdbqt,
            role="ligand",
            require_single_ligand=False,
        )
        bound_log = _bound_log(manifest, stdout, stderr)
        parsed_run = parse_vina_output(
            bound_log,
            manifest=manifest,
            output_source=SourceProvenance(
                source_name="vina.log",
                accession=f"local-vina:{manifest.task_id}",
                version=actual_executable.version,
            ),
        )

        normalized_arguments = [
            executable_path.name,
            *controlled_arguments,
        ]
        audit = VinaExecutionAudit(
            executable_sha256=actual_executable.sha256,
            executable_version=actual_executable.version,
            arguments=normalized_arguments,
            exit_code=0,
            duration_seconds=duration_seconds,
            output_pdbqt_sha256=output_pdbqt_sha256,
        )
        docking_run = parsed_run.model_copy(
            update={"execution_audit": audit}
        )
        metadata = VinaLocalExecutionMetadata(
            executable_path=actual_executable.path,
            executable_sha256=actual_executable.sha256,
            executable_version=actual_executable.version,
            arguments=normalized_arguments,
            exit_code=0,
            duration_seconds=duration_seconds,
            output_pdbqt_sha256=output_pdbqt_sha256,
        )
        return VinaExecutionArtifacts(
            docking_run=docking_run,
            metadata=metadata,
            bound_log=bound_log,
            output_pdbqt=output_pdbqt,
        )


__all__ = [
    "VinaExecutableInfo",
    "VinaExecutionArtifacts",
    "VinaExecutionError",
    "VinaLocalExecutionMetadata",
    "discover_vina",
    "execute_vina",
]
