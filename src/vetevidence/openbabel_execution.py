"""Controlled local ligand preparation with Open Babel."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vetevidence.mechanism_prediction import validate_pdbqt_bytes


_OPENBABEL_VERSION_PATTERN = re.compile(
    r"\bOpen\s+Babel\s+(?P<version>[0-9]+(?:\.[0-9]+){1,2})\b",
    flags=re.IGNORECASE,
)
_CONVERTED_COUNT_PATTERN = re.compile(
    r"\b(?P<count>[0-9]+)\s+molecules?\s+converted\b",
    flags=re.IGNORECASE,
)
_OPENBABEL_ERROR_PATTERN = re.compile(
    r"(?:\*{3}\s*)?Open\s+Babel\s+Error\b",
    flags=re.IGNORECASE,
)
_SDF_SEPARATOR_PATTERN = re.compile(r"(?m)^\s*\$\$\$\$\s*$")
_MOL2_RECORD_PATTERN = re.compile(
    r"(?im)^\s*@<TRIPOS>MOLECULE\s*$"
)
_PDB_MODEL_PATTERN = re.compile(r"(?im)^\s*MODEL(?:\s|$)")
_MOL_END_PATTERN = re.compile(r"(?im)^\s*M\s+END\s*$")
_ALLOWED_INPUT_FORMATS = frozenset(
    {"smi", "smiles", "sdf", "mol", "mol2", "pdb"}
)
_CLI_INPUT_FORMATS = {
    "smi": "smi",
    "smiles": "smi",
    "sdf": "sdf",
    "mol": "mol",
    "mol2": "mol2",
    "pdb": "pdb",
}
_DATA_MARKERS = ("atomtyp.txt", "phmodel.txt", "UFF.prm")
_FIXED_OUTPUT_NAME = "ligand.pdbqt"
_DEFAULT_VERSION_TIMEOUT_SECONDS = 10.0
_DEFAULT_EXECUTION_TIMEOUT_SECONDS = 120.0
_MAX_INPUT_BYTES = 10 * 1024 * 1024
_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
_MAX_AUDIT_OUTPUT_BYTES = 4000
_COORDINATE_TOLERANCE = 1e-6
_SUPPORTED_OPENBABEL_VERSION = "3.2.1"

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class OpenBabelExecutionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class OpenBabelExecutableInfo(OpenBabelExecutionModel):
    """Verified Open Babel executable and its required chemistry data."""

    path: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    data_directory: str = Field(min_length=1)


class OpenBabelPreparationOptions(OpenBabelExecutionModel):
    """The complete allowlisted set of ligand preparation controls."""

    input_format: str
    generate_3d: bool = True
    protonation_ph: float | None = Field(default=7.4, ge=0.0, le=14.0)
    charge_model: Literal["gasteiger"] = "gasteiger"

    @field_validator("input_format", mode="before")
    @classmethod
    def normalize_input_format(cls, value: object) -> str:
        normalized = str(value).strip().lower().removeprefix(".")
        if normalized not in _ALLOWED_INPUT_FORMATS:
            allowed = "、".join(sorted(_ALLOWED_INPUT_FORMATS))
            raise ValueError(f"不支持的配体格式；仅允许：{allowed}。")
        return normalized


class OpenBabelLocalExecutionMetadata(OpenBabelExecutionModel):
    """JSON-safe audit for one successful, controlled conversion."""

    executable_path: str = Field(min_length=1)
    executable_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    executable_version: str = Field(min_length=1)
    data_directory: str = Field(min_length=1)
    input_format: str = Field(min_length=1)
    input_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    output_pdbqt_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    generate_3d: bool
    protonation_ph: float | None
    charge_model: Literal["gasteiger"]
    arguments: list[str] = Field(min_length=1)
    exit_code: int
    duration_seconds: float = Field(ge=0)
    stdout: str
    stderr: str


class OpenBabelPreparationArtifacts(OpenBabelExecutionModel):
    """Successful PDBQT payload and its execution audit."""

    metadata: OpenBabelLocalExecutionMetadata
    output_pdbqt: bytes

    @property
    def audit(self) -> OpenBabelLocalExecutionMetadata:
        """Alias retained for callers that describe metadata as an audit."""

        return self.metadata


class OpenBabelExecutionError(RuntimeError):
    """Raised when discovery or ligand preparation cannot be trusted."""


def _active_runner(runner: Runner | None) -> Runner:
    return runner or subprocess.run


def _hidden_window_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _controlled_environment(
    *,
    overrides: Mapping[str, str] | None,
    executable: Path,
    data_directory: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    if overrides is not None:
        environment.update(overrides)
    environment["BABEL_DATADIR"] = str(data_directory)
    environment["BABEL_LIBDIR"] = str(executable.parent)
    return environment


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


def _resolve_executable(path_value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(path_value).strip().strip('"')
    if not raw:
        raise OpenBabelExecutionError("Open Babel 可执行文件路径为空。")
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise OpenBabelExecutionError(
            f"Open Babel 可执行文件不存在：{candidate}。"
        ) from exc
    if not resolved.is_file():
        raise OpenBabelExecutionError(
            f"Open Babel 路径不是文件：{resolved}。"
        )
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise OpenBabelExecutionError(
            f"Open Babel 文件不可执行：{resolved}。"
        )
    return resolved


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


def _wheel_native_candidates(launcher: Path) -> list[Path]:
    """Find the native binary behind a wheel-generated console launcher."""

    candidates: list[Path] = []
    parent_name = launcher.parent.name.casefold()
    if parent_name == "scripts":
        environment_root = launcher.parent.parent
        candidates.append(
            environment_root
            / "Lib"
            / "site-packages"
            / "openbabel"
            / "bin"
            / "obabel.exe"
        )
    elif parent_name == "bin":
        environment_root = launcher.parent.parent
        for site_packages in sorted(
            (environment_root / "lib").glob("python*/site-packages")
        ):
            candidates.extend(
                [
                    site_packages / "openbabel" / "bin" / "obabel",
                    site_packages / "openbabel" / "bin" / "obabel.exe",
                ]
            )
    return _deduplicated_candidates(candidates)


def _prefer_wheel_native_binary(candidate: Path) -> Path:
    for native in _wheel_native_candidates(candidate):
        if native.is_file():
            return native.resolve()
    return candidate


def _valid_data_directory(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in _DATA_MARKERS)


def _data_directory_candidates(executable: Path) -> list[Path]:
    candidates = [executable.parent / "data"]
    share_root = executable.parent.parent / "share" / "openbabel"
    if share_root.is_dir():
        candidates.extend(
            sorted(
                (path for path in share_root.iterdir() if path.is_dir()),
                reverse=True,
            )
        )
    return _deduplicated_candidates(candidates)


def _resolve_data_directory(executable: Path) -> Path:
    for candidate in _data_directory_candidates(executable):
        if _valid_data_directory(candidate):
            return candidate.resolve()
    raise OpenBabelExecutionError(
        "未找到与 Open Babel 可执行文件绑定的化学参数目录"
        f"（需要 {', '.join(_DATA_MARKERS)}）：{executable}。"
    )


def _semantic_version(value: str) -> tuple[int, int, int]:
    normalized = value.strip().removeprefix("v")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", normalized):
        raise OpenBabelExecutionError(
            f"无法解析 Open Babel 版本：{value!r}。"
        )
    parts = [int(part) for part in normalized.split(".")]
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _inspect_executable(
    path_value: str | os.PathLike[str],
    *,
    environment: Mapping[str, str] | None,
    runner: Runner | None,
    timeout_seconds: float,
) -> OpenBabelExecutableInfo:
    if timeout_seconds <= 0:
        raise ValueError("Open Babel 版本检查超时必须大于 0。")
    launcher_or_binary = _resolve_executable(path_value)
    path = _prefer_wheel_native_binary(launcher_or_binary)
    data_directory = _resolve_data_directory(path)
    active_runner = _active_runner(runner)
    arguments = [str(path), "-V"]
    try:
        completed = active_runner(
            arguments,
            cwd=str(data_directory),
            env=_controlled_environment(
                overrides=environment,
                executable=path,
                data_directory=data_directory,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
            **_hidden_window_options(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenBabelExecutionError(
            f"Open Babel -V 在 {timeout_seconds:g} 秒后超时：{path}。"
        ) from exc
    except OSError as exc:
        raise OpenBabelExecutionError(
            f"无法启动 Open Babel -V：{path}。"
        ) from exc
    if completed.returncode != 0:
        raise OpenBabelExecutionError(
            "Open Babel -V 返回非零退出码 "
            f"{completed.returncode}：{path}。"
        )
    version_output = (
        _payload_bytes(completed.stdout)
        + b"\n"
        + _payload_bytes(completed.stderr)
    )
    match = _OPENBABEL_VERSION_PATTERN.search(
        version_output.decode("utf-8", errors="replace")
    )
    if match is None:
        raise OpenBabelExecutionError(
            f"文件未报告有效的 Open Babel 版本：{path}。"
        )
    version = match.group("version")
    if _semantic_version(version) != _semantic_version(
        _SUPPORTED_OPENBABEL_VERSION
    ):
        raise OpenBabelExecutionError(
            "当前只允许已验收的 Open Babel "
            f"{_SUPPORTED_OPENBABEL_VERSION}；实际检测到 {version}。"
        )
    try:
        executable_sha256 = _sha256_file(path)
    except OSError as exc:
        raise OpenBabelExecutionError(
            "无法读取 Open Babel 可执行文件以计算 SHA-256："
            f"{path}。"
        ) from exc
    return OpenBabelExecutableInfo(
        path=str(path),
        version=version,
        sha256=executable_sha256,
        data_directory=str(data_directory),
    )


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _project_venv_candidates(project_root: Path) -> list[Path]:
    environment_root = project_root / ".venv"
    candidates = [
        environment_root / "Scripts" / "obabel.exe",
        environment_root
        / "Lib"
        / "site-packages"
        / "openbabel"
        / "bin"
        / "obabel.exe",
        environment_root / "bin" / "obabel",
    ]
    for site_packages in sorted(
        (environment_root / "lib").glob("python*/site-packages")
    ):
        candidates.extend(
            [
                site_packages / "openbabel" / "bin" / "obabel",
                site_packages / "openbabel" / "bin" / "obabel.exe",
            ]
        )
    return _deduplicated_candidates(candidates)


def _path_candidates(path_value: str) -> list[Path]:
    names = (
        ("obabel.exe", "obabel")
        if os.name == "nt"
        else ("obabel", "obabel.exe")
    )
    return _deduplicated_candidates(
        [
            Path(directory) / name
            for directory in path_value.split(os.pathsep)
            if directory
            for name in names
        ]
    )


def _select_first_valid(
    candidates: Sequence[Path],
    *,
    environment: Mapping[str, str] | None,
    runner: Runner | None,
    timeout_seconds: float,
) -> OpenBabelExecutableInfo | None:
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return _inspect_executable(
                candidate,
                environment=environment,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )
        except OpenBabelExecutionError:
            continue
    return None


def discover_openbabel(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = _DEFAULT_VERSION_TIMEOUT_SECONDS,
    project_root: str | os.PathLike[str] | None = None,
) -> OpenBabelExecutableInfo:
    """Discover Open Babel in strict explicit/env/project/PATH order."""

    active_environment = dict(os.environ if environment is None else environment)
    if explicit_path is not None:
        return _inspect_executable(
            explicit_path,
            environment=active_environment,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    configured_path = active_environment.get("OPENBABEL_EXECUTABLE")
    if configured_path:
        return _inspect_executable(
            configured_path,
            environment=active_environment,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    active_project_root = (
        Path(project_root).resolve()
        if project_root is not None
        else _project_root()
    )
    project_match = _select_first_valid(
        _project_venv_candidates(active_project_root),
        environment=active_environment,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if project_match is not None:
        return project_match

    path_match = _select_first_valid(
        _path_candidates(active_environment.get("PATH", "")),
        environment=active_environment,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if path_match is not None:
        return path_match

    raise OpenBabelExecutionError(
        "未找到可验证的 Open Babel。请提供显式路径、设置 "
        "OPENBABEL_EXECUTABLE、安装到项目 .venv，或加入 PATH。"
    )


def _decode_for_structure_scan(payload: bytes) -> str:
    return payload.decode("utf-8-sig", errors="replace")


def _nonempty_smiles_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _reject_obvious_multiple_molecules(
    payload: bytes,
    input_format: str,
) -> None:
    text = _decode_for_structure_scan(payload)
    if input_format in {"smi", "smiles"}:
        records = _nonempty_smiles_lines(text)
        if len(records) > 1:
            raise OpenBabelExecutionError(
                "SMILES 输入包含多条分子记录；一次只允许一个配体。"
            )
        if records:
            smiles = records[0].split(maxsplit=1)[0]
            if "." in smiles:
                raise OpenBabelExecutionError(
                    "SMILES 输入包含断开的多组分结构；一次只允许一个配体。"
                )
        return

    if input_format == "sdf":
        records = [
            record
            for record in _SDF_SEPARATOR_PATTERN.split(text)
            if record.strip()
        ]
        if len(records) > 1:
            raise OpenBabelExecutionError(
                "SDF 输入包含多个分子记录；一次只允许一个配体。"
            )
        return

    if input_format == "mol2":
        if len(_MOL2_RECORD_PATTERN.findall(text)) > 1:
            raise OpenBabelExecutionError(
                "MOL2 输入包含多个分子记录；一次只允许一个配体。"
            )
        return

    if input_format == "pdb":
        if len(_PDB_MODEL_PATTERN.findall(text)) > 1:
            raise OpenBabelExecutionError(
                "PDB 输入包含多个 MODEL；一次只允许一个配体。"
            )
        return

    if input_format == "mol" and len(_MOL_END_PATTERN.findall(text)) > 1:
        raise OpenBabelExecutionError(
            "MOL 输入包含多个结构记录；一次只允许一个配体。"
        )


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _controlled_arguments(
    options: OpenBabelPreparationOptions,
    *,
    input_path: str,
    output_path: str,
) -> list[str]:
    arguments = [
        f"-i{_CLI_INPUT_FORMATS[options.input_format]}",
        input_path,
        "-opdbqt",
        "-O",
        output_path,
    ]
    if options.generate_3d:
        arguments.append("--gen3d")
    if options.protonation_ph is not None:
        arguments.extend(["-p", _format_number(options.protonation_ph)])
    arguments.extend(["--partialcharge", options.charge_model])
    return arguments


def _bounded_output(payload: bytes | str | None) -> str:
    raw = _payload_bytes(payload)
    if len(raw) > _MAX_AUDIT_OUTPUT_BYTES:
        raw = raw[-_MAX_AUDIT_OUTPUT_BYTES:]
    return raw.decode("utf-8", errors="replace").strip()


def _coordinates_from_pdbqt(payload: bytes) -> list[tuple[float, float, float]]:
    text = payload.decode("utf-8", errors="replace")
    coordinates: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        record = line[:6].strip().upper()
        if record not in {"ATOM", "HETATM"}:
            continue
        try:
            coordinate = (
                float(line[30:38]),
                float(line[38:46]),
                float(line[46:54]),
            )
        except (ValueError, IndexError):
            parts = line.split()
            try:
                coordinate = (
                    float(parts[5]),
                    float(parts[6]),
                    float(parts[7]),
                )
            except (ValueError, IndexError) as exc:
                raise OpenBabelExecutionError(
                    "Open Babel 输出的 PDBQT 原子坐标无法解析。"
                ) from exc
        if not all(math.isfinite(value) for value in coordinate):
            raise OpenBabelExecutionError(
                "Open Babel 输出的 PDBQT 含非有限原子坐标。"
            )
        coordinates.append(coordinate)
    return coordinates


def _validate_non_degenerate_coordinates(payload: bytes) -> None:
    coordinates = _coordinates_from_pdbqt(payload)
    if not coordinates:
        raise OpenBabelExecutionError(
            "Open Babel 输出的 PDBQT 没有可解析的原子坐标。"
        )
    if all(
        abs(value) <= _COORDINATE_TOLERANCE
        for coordinate in coordinates
        for value in coordinate
    ):
        raise OpenBabelExecutionError(
            "Open Babel 输出了全零坐标，配体三维结构无效。"
        )
    if len(coordinates) > 1:
        first = coordinates[0]
        maximum_squared_distance = max(
            sum((value - origin) ** 2 for value, origin in zip(item, first))
            for item in coordinates[1:]
        )
        if maximum_squared_distance <= _COORDINATE_TOLERANCE**2:
            raise OpenBabelExecutionError(
                "Open Babel 输出的原子坐标完全重合，配体结构退化。"
            )


def _assert_single_converted_molecule(stdout: bytes, stderr: bytes) -> None:
    summary = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    counts = [
        int(match.group("count"))
        for match in _CONVERTED_COUNT_PATTERN.finditer(summary)
    ]
    if counts and any(count != 1 for count in counts):
        raise OpenBabelExecutionError(
            "Open Babel 报告转换了多个或零个分子；拒绝使用该输出。"
        )


def _reject_reported_openbabel_error(stdout: bytes, stderr: bytes) -> None:
    combined = (stdout + b"\n" + stderr).decode(
        "utf-8",
        errors="replace",
    )
    if _OPENBABEL_ERROR_PATTERN.search(combined):
        details = _bounded_output(stderr or stdout)
        suffix = f"：{details}" if details else "。"
        raise OpenBabelExecutionError(
            f"Open Babel 报告转换错误，拒绝使用输出{suffix}"
        )


def prepare_ligand_pdbqt(
    payload: bytes | str,
    *,
    options: OpenBabelPreparationOptions,
    executable: OpenBabelExecutableInfo | None = None,
    explicit_path: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_seconds: float = _DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    version_timeout_seconds: float = _DEFAULT_VERSION_TIMEOUT_SECONDS,
    project_root: str | os.PathLike[str] | None = None,
) -> OpenBabelPreparationArtifacts:
    """Prepare exactly one ligand as a validated PDBQT payload."""

    if timeout_seconds <= 0:
        raise ValueError("Open Babel 执行超时必须大于 0。")
    raw = _payload_bytes(payload)
    if not raw.strip():
        raise OpenBabelExecutionError("配体输入为空。")
    if len(raw) > _MAX_INPUT_BYTES:
        raise OpenBabelExecutionError("配体输入超过 10 MB，拒绝转换。")
    _reject_obvious_multiple_molecules(raw, options.input_format)

    if executable is not None and explicit_path is not None:
        raise ValueError("executable 与 explicit_path 只能提供一个。")
    if executable is None:
        actual_executable = discover_openbabel(
            explicit_path,
            environment=environment,
            runner=runner,
            timeout_seconds=version_timeout_seconds,
            project_root=project_root,
        )
    else:
        actual_executable = _inspect_executable(
            executable.path,
            environment=environment,
            runner=runner,
            timeout_seconds=version_timeout_seconds,
        )
        if (
            actual_executable.sha256 != executable.sha256
            or _semantic_version(actual_executable.version)
            != _semantic_version(executable.version)
            or Path(actual_executable.data_directory).resolve()
            != Path(executable.data_directory).resolve()
        ):
            raise OpenBabelExecutionError(
                "Open Babel 可执行文件或数据目录在发现后已发生变化，"
                "拒绝转换。"
            )

    executable_path = Path(actual_executable.path)
    data_directory = Path(actual_executable.data_directory)
    active_runner = _active_runner(runner)
    input_sha256 = hashlib.sha256(raw).hexdigest()
    with tempfile.TemporaryDirectory(
        prefix="vetevidence-openbabel-"
    ) as temporary:
        temporary_directory = Path(temporary).resolve()
        input_name = f"ligand.{options.input_format}"
        input_path = temporary_directory / input_name
        output_path = temporary_directory / _FIXED_OUTPUT_NAME
        input_path.write_bytes(raw)
        controlled_arguments = _controlled_arguments(
            options,
            input_path=str(input_path),
            output_path=str(output_path),
        )
        command = [actual_executable.path, *controlled_arguments]
        started = time.perf_counter()
        try:
            completed = active_runner(
                command,
                cwd=str(data_directory),
                env=_controlled_environment(
                    overrides=environment,
                    executable=executable_path,
                    data_directory=data_directory,
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                **_hidden_window_options(),
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenBabelExecutionError(
                "Open Babel 配体转换在 "
                f"{timeout_seconds:g} 秒后超时，未生成可用 PDBQT。"
            ) from exc
        except OSError as exc:
            raise OpenBabelExecutionError(
                "无法启动本机 Open Babel，未生成可用 PDBQT。"
            ) from exc
        duration_seconds = max(0.0, time.perf_counter() - started)
        stdout = _payload_bytes(completed.stdout)
        stderr = _payload_bytes(completed.stderr)
        if completed.returncode != 0:
            details = _bounded_output(stderr or stdout)
            suffix = f"：{details}" if details else "。"
            raise OpenBabelExecutionError(
                "Open Babel 返回非零退出码 "
                f"{completed.returncode}{suffix}"
            )
        if _sha256_file(executable_path) != actual_executable.sha256:
            raise OpenBabelExecutionError(
                "Open Babel 可执行文件在转换期间发生变化，拒绝使用输出。"
            )
        if not _valid_data_directory(data_directory):
            raise OpenBabelExecutionError(
                "Open Babel 化学参数目录在转换期间发生变化，"
                "拒绝使用输出。"
            )
        _reject_reported_openbabel_error(stdout, stderr)
        _assert_single_converted_molecule(stdout, stderr)
        if not output_path.is_file():
            raise OpenBabelExecutionError(
                "Open Babel 未生成 ligand.pdbqt。"
            )
        if output_path.stat().st_size > _MAX_OUTPUT_BYTES:
            raise OpenBabelExecutionError(
                "Open Babel 输出的 PDBQT 超过 25 MB，拒绝读取。"
            )
        output_pdbqt = output_path.read_bytes()
        try:
            output_pdbqt_sha256 = validate_pdbqt_bytes(
                output_pdbqt,
                role="ligand",
            )
        except ValueError as exc:
            raise OpenBabelExecutionError(
                f"Open Babel 输出的 PDBQT 无效：{exc}"
            ) from exc
        _validate_non_degenerate_coordinates(output_pdbqt)

        normalized_arguments = _controlled_arguments(
            options,
            input_path=input_name,
            output_path=_FIXED_OUTPUT_NAME,
        )
        metadata = OpenBabelLocalExecutionMetadata(
            executable_path=actual_executable.path,
            executable_sha256=actual_executable.sha256,
            executable_version=actual_executable.version,
            data_directory=actual_executable.data_directory,
            input_format=options.input_format,
            input_sha256=input_sha256,
            output_pdbqt_sha256=output_pdbqt_sha256,
            generate_3d=options.generate_3d,
            protonation_ph=options.protonation_ph,
            charge_model=options.charge_model,
            arguments=[executable_path.name, *normalized_arguments],
            exit_code=completed.returncode,
            duration_seconds=duration_seconds,
            stdout=_bounded_output(stdout),
            stderr=_bounded_output(stderr),
        )
        return OpenBabelPreparationArtifacts(
            metadata=metadata,
            output_pdbqt=output_pdbqt,
        )


__all__ = [
    "OpenBabelExecutableInfo",
    "OpenBabelExecutionError",
    "OpenBabelLocalExecutionMetadata",
    "OpenBabelPreparationArtifacts",
    "OpenBabelPreparationOptions",
    "discover_openbabel",
    "prepare_ligand_pdbqt",
]
