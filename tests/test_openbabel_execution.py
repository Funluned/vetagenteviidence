from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import vetevidence.openbabel_execution as openbabel_execution
from vetevidence.openbabel_execution import (
    OpenBabelExecutionError,
    OpenBabelPreparationOptions,
    discover_openbabel,
    prepare_ligand_pdbqt,
)


VALID_PDBQT = b"""REMARK  Name = ethanol
ROOT
ATOM      1  C   UNL     1       1.074  -0.012  -0.071  1.00  0.00    +0.034 C
ATOM      2  C   UNL     1       2.589  -0.011  -0.077  1.00  0.00    +0.152 C
ENDROOT
TORSDOF 0
"""
ZERO_COORDINATE_PDBQT = b"""ROOT
ATOM      1  C   UNL     1       0.000   0.000   0.000  1.00  0.00    +0.000 C
ATOM      2  C   UNL     1       0.000   0.000   0.000  1.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
"""
COLLAPSED_COORDINATE_PDBQT = b"""ROOT
ATOM      1  C   UNL     1       1.000   2.000   3.000  1.00  0.00    +0.000 C
ATOM      2  C   UNL     1       1.000   2.000   3.000  1.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
"""
MULTI_COMPONENT_PDBQT = VALID_PDBQT + b"""ROOT
ATOM      1  O   HOH     2       4.000   5.000   6.000  1.00  0.00    +0.000 OA
ENDROOT
TORSDOF 0
"""


def _make_executable(
    path: Path,
    payload: bytes = b"fake-openbabel",
    *,
    with_data: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if with_data:
        data_directory = path.parent / "data"
        data_directory.mkdir()
        for marker in ("atomtyp.txt", "phmodel.txt", "UFF.prm"):
            (data_directory / marker).write_text(marker, encoding="utf-8")
    return path.resolve()


class FakeRunner:
    def __init__(
        self,
        versions: dict[str, str],
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"1 molecule converted\n",
        output_pdbqt: bytes | None = VALID_PDBQT,
        timeout_on_execution: bool = False,
    ) -> None:
        self.versions = {
            str(Path(path).resolve()): version
            for path, version in versions.items()
        }
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.output_pdbqt = output_pdbqt
        self.timeout_on_execution = timeout_on_execution
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        arguments: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        normalized_arguments = [str(item) for item in arguments]
        self.calls.append((normalized_arguments, dict(kwargs)))
        executable = str(Path(normalized_arguments[0]).resolve())
        if normalized_arguments[1:] == ["-V"]:
            version = self.versions.get(executable)
            if version is None:
                return subprocess.CompletedProcess(
                    normalized_arguments,
                    1,
                    stdout=b"",
                    stderr=b"not Open Babel",
                )
            return subprocess.CompletedProcess(
                normalized_arguments,
                0,
                stdout=f"Open Babel {version} -- test build\n".encode(),
                stderr=b"",
            )

        if self.timeout_on_execution:
            raise subprocess.TimeoutExpired(
                cmd=normalized_arguments,
                timeout=float(kwargs["timeout"]),
            )
        output_index = normalized_arguments.index("-O") + 1
        output_path = Path(normalized_arguments[output_index])
        if self.output_pdbqt is not None:
            output_path.write_bytes(self.output_pdbqt)
        return subprocess.CompletedProcess(
            normalized_arguments,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _options(
    *,
    input_format: str = "smi",
    generate_3d: bool = True,
    protonation_ph: float | None = 7.4,
) -> OpenBabelPreparationOptions:
    return OpenBabelPreparationOptions(
        input_format=input_format,
        generate_3d=generate_3d,
        protonation_ph=protonation_ph,
    )


def test_discover_explicit_path_has_priority_and_is_fingerprinted(
    tmp_path: Path,
) -> None:
    explicit = _make_executable(
        tmp_path / "explicit" / "obabel.exe",
        b"explicit",
    )
    configured = _make_executable(
        tmp_path / "configured" / "obabel.exe",
        b"configured",
    )
    runner = FakeRunner(
        {
            str(explicit): "3.2.1",
            str(configured): "9.9.9",
        }
    )

    discovered = discover_openbabel(
        explicit,
        environment={
            "OPENBABEL_EXECUTABLE": str(configured),
            "PATH": "",
        },
        runner=runner,
        project_root=tmp_path / "project",
    )

    assert discovered.path == str(explicit)
    assert discovered.version == "3.2.1"
    assert discovered.sha256 == hashlib.sha256(b"explicit").hexdigest()
    assert discovered.data_directory == str(explicit.parent / "data")
    assert [call[0] for call in runner.calls] == [
        [str(explicit), "-V"]
    ]
    assert runner.calls[0][1]["cwd"] == discovered.data_directory
    assert runner.calls[0][1]["shell"] is False
    version_environment = runner.calls[0][1]["env"]
    assert isinstance(version_environment, dict)
    assert version_environment["BABEL_DATADIR"] == discovered.data_directory
    assert version_environment["BABEL_LIBDIR"] == str(explicit.parent)


def test_discover_environment_precedes_project_venv(
    tmp_path: Path,
) -> None:
    configured = _make_executable(
        tmp_path / "configured" / "obabel.exe",
        b"configured",
    )
    project_executable = _make_executable(
        tmp_path / "project" / ".venv" / "Scripts" / "obabel.exe",
        b"project",
    )
    runner = FakeRunner(
        {
            str(configured): "3.2.1",
            str(project_executable): "9.9.9",
        }
    )

    discovered = discover_openbabel(
        environment={
            "OPENBABEL_EXECUTABLE": str(configured),
            "PATH": "",
        },
        runner=runner,
        project_root=tmp_path / "project",
    )

    assert discovered.path == str(configured)
    assert len(runner.calls) == 1


def test_discover_project_wheel_uses_native_binary_and_data_directory(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    launcher = _make_executable(
        project_root / ".venv" / "Scripts" / "obabel.exe",
        b"python-launcher",
        with_data=False,
    )
    native = _make_executable(
        project_root
        / ".venv"
        / "Lib"
        / "site-packages"
        / "openbabel"
        / "bin"
        / "obabel.exe",
        b"native-binary",
    )
    runner = FakeRunner({str(native): "3.2.1"})

    discovered = discover_openbabel(
        environment={"PATH": ""},
        runner=runner,
        project_root=project_root,
    )

    assert launcher.is_file()
    assert discovered.path == str(native)
    assert discovered.sha256 == hashlib.sha256(b"native-binary").hexdigest()
    assert discovered.data_directory == str(native.parent / "data")
    assert runner.calls[0][0] == [str(native), "-V"]


def test_discover_falls_back_to_path(tmp_path: Path) -> None:
    path_executable = _make_executable(
        tmp_path / "path" / "obabel.exe",
        b"path-binary",
    )
    runner = FakeRunner({str(path_executable): "3.2.1"})

    discovered = discover_openbabel(
        environment={"PATH": str(path_executable.parent)},
        runner=runner,
        project_root=tmp_path / "empty-project",
    )

    assert discovered.path == str(path_executable)


def test_discover_requires_bound_chemistry_data(tmp_path: Path) -> None:
    executable = _make_executable(
        tmp_path / "obabel.exe",
        with_data=False,
    )

    with pytest.raises(OpenBabelExecutionError, match="化学参数目录"):
        discover_openbabel(
            executable,
            runner=FakeRunner({str(executable): "3.2.1"}),
        )


def test_discover_rejects_unvalidated_openbabel_version(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")

    with pytest.raises(OpenBabelExecutionError, match="只允许.*3\\.2\\.1"):
        discover_openbabel(
            executable,
            runner=FakeRunner({str(executable): "3.3.0"}),
        )


def test_options_allow_only_supported_ligand_formats() -> None:
    assert _options(input_format=".SMILES").input_format == "smiles"

    with pytest.raises(ValidationError, match="仅允许"):
        _options(input_format="pdbqt")


def test_prepare_uses_fixed_arguments_absolute_paths_and_returns_audit(
    tmp_path: Path,
) -> None:
    executable = _make_executable(
        tmp_path / "openbabel" / "obabel.exe",
        b"real-binary",
    )
    runner = FakeRunner({str(executable): "3.2.1"})
    payload = b"CCO ethanol\n"

    artifacts = prepare_ligand_pdbqt(
        payload,
        options=_options(),
        explicit_path=executable,
        runner=runner,
        timeout_seconds=30,
    )

    execution_arguments, execution_options = runner.calls[1]
    assert execution_arguments[0] == str(executable)
    assert execution_arguments[1] == "-ismi"
    assert Path(execution_arguments[2]).is_absolute()
    assert Path(execution_arguments[2]).name == "ligand.smi"
    assert execution_arguments[3:5] == ["-opdbqt", "-O"]
    assert Path(execution_arguments[5]).is_absolute()
    assert Path(execution_arguments[5]).name == "ligand.pdbqt"
    assert execution_arguments[6:] == [
        "--gen3d",
        "-p",
        "7.4",
        "--partialcharge",
        "gasteiger",
    ]
    # Deliberately omit -xh: PDBQT uses AutoDock's united-atom convention.
    assert "-xh" not in execution_arguments
    assert execution_options["cwd"] == str(executable.parent / "data")
    assert execution_options["shell"] is False
    assert execution_options["capture_output"] is True
    assert execution_options["stdin"] is subprocess.DEVNULL
    execution_environment = execution_options["env"]
    assert isinstance(execution_environment, dict)
    assert execution_environment["BABEL_DATADIR"] == str(
        executable.parent / "data"
    )
    assert execution_environment["BABEL_LIBDIR"] == str(executable.parent)
    assert not Path(execution_arguments[2]).parent.exists()

    assert artifacts.output_pdbqt == VALID_PDBQT
    assert artifacts.audit is artifacts.metadata
    assert artifacts.metadata.input_sha256 == hashlib.sha256(payload).hexdigest()
    assert artifacts.metadata.output_pdbqt_sha256 == hashlib.sha256(
        VALID_PDBQT
    ).hexdigest()
    assert artifacts.metadata.executable_sha256 == hashlib.sha256(
        b"real-binary"
    ).hexdigest()
    assert artifacts.metadata.arguments == [
        "obabel.exe",
        "-ismi",
        "ligand.smi",
        "-opdbqt",
        "-O",
        "ligand.pdbqt",
        "--gen3d",
        "-p",
        "7.4",
        "--partialcharge",
        "gasteiger",
    ]
    assert all(
        "vetevidence-openbabel-" not in item
        for item in artifacts.metadata.arguments
    )
    assert artifacts.metadata.exit_code == 0
    assert artifacts.metadata.stderr == "1 molecule converted"


def test_prepare_can_disable_3d_and_ph_but_not_charge_model(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner({str(executable): "3.2.1"})

    prepare_ligand_pdbqt(
        b"CCO\n",
        options=_options(generate_3d=False, protonation_ph=None),
        explicit_path=executable,
        runner=runner,
    )

    execution_arguments = runner.calls[1][0]
    assert "--gen3d" not in execution_arguments
    assert "-p" not in execution_arguments
    assert execution_arguments[-2:] == ["--partialcharge", "gasteiger"]
    with pytest.raises(ValidationError):
        OpenBabelPreparationOptions(
            input_format="smi",
            charge_model="user-controlled",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("input_format", "payload"),
    [
        ("smi", b"CCO\nCCN\n"),
        ("smiles", b"CCO.CCN\n"),
        ("sdf", b"one\n$$$$\ntwo\n$$$$\n"),
        (
            "mol2",
            b"@<TRIPOS>MOLECULE\none\n@<TRIPOS>MOLECULE\ntwo\n",
        ),
        ("pdb", b"MODEL 1\nENDMDL\nMODEL 2\nENDMDL\n"),
        ("mol", b"one\nM  END\ntwo\nM  END\n"),
    ],
)
def test_prepare_rejects_obvious_multiple_molecules_before_discovery(
    tmp_path: Path,
    input_format: str,
    payload: bytes,
) -> None:
    runner = FakeRunner({})

    with pytest.raises(OpenBabelExecutionError, match="一次只允许一个配体"):
        prepare_ligand_pdbqt(
            payload,
            options=_options(input_format=input_format),
            runner=runner,
            project_root=tmp_path / "empty",
        )

    assert runner.calls == []


def test_prepare_rejects_oversized_input_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openbabel_execution, "_MAX_INPUT_BYTES", 4)
    runner = FakeRunner({})

    with pytest.raises(OpenBabelExecutionError, match="超过 10 MB"):
        prepare_ligand_pdbqt(
            b"CCCCC",
            options=_options(),
            runner=runner,
            project_root=tmp_path / "empty",
        )

    assert runner.calls == []


def test_prepare_nonzero_exit_never_returns_payload(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        returncode=2,
        stderr=b"conversion failed",
    )

    with pytest.raises(OpenBabelExecutionError, match="非零退出码 2"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=runner,
        )


def test_prepare_timeout_never_returns_payload(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        timeout_on_execution=True,
    )

    with pytest.raises(OpenBabelExecutionError, match="超时.*未生成可用 PDBQT"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=runner,
            timeout_seconds=0.1,
        )


def test_prepare_rejects_missing_or_invalid_output(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    missing_runner = FakeRunner(
        {str(executable): "3.2.1"},
        output_pdbqt=None,
    )

    with pytest.raises(OpenBabelExecutionError, match="未生成 ligand.pdbqt"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=missing_runner,
        )

    invalid_runner = FakeRunner(
        {str(executable): "3.2.1"},
        output_pdbqt=b"not a PDBQT",
    )
    with pytest.raises(OpenBabelExecutionError, match="PDBQT 无效"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=invalid_runner,
        )


@pytest.mark.parametrize(
    "output_pdbqt",
    [ZERO_COORDINATE_PDBQT, COLLAPSED_COORDINATE_PDBQT],
)
def test_prepare_rejects_zero_or_collapsed_coordinates(
    tmp_path: Path,
    output_pdbqt: bytes,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        output_pdbqt=output_pdbqt,
    )

    with pytest.raises(
        OpenBabelExecutionError,
        match="全零坐标|完全重合",
    ):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=runner,
        )


def test_prepare_rejects_cli_report_of_multiple_molecules(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        stderr=b"2 molecules converted\n",
    )

    with pytest.raises(OpenBabelExecutionError, match="转换了多个或零个分子"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=runner,
        )


@pytest.mark.parametrize(
    ("input_format", "payload"),
    [
        ("sdf", b"single record with disconnected components\n$$$$\n"),
        ("mol", b"single record\nM  END\n"),
        ("mol2", b"@<TRIPOS>MOLECULE\nsingle record\n"),
        ("pdb", b"HETATM    1  C   LIG A   1\nHETATM    2  O   HOH B   2\n"),
    ],
)
def test_prepare_rejects_single_record_with_multiple_output_components(
    tmp_path: Path,
    input_format: str,
    payload: bytes,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        stderr=b"1 molecule converted\n",
        output_pdbqt=MULTI_COMPONENT_PDBQT,
    )

    with pytest.raises(
        OpenBabelExecutionError,
        match="只能包含一个完整配体块",
    ):
        prepare_ligand_pdbqt(
            payload,
            options=_options(input_format=input_format),
            explicit_path=executable,
            runner=runner,
        )


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (b"*** Open Babel Error in Convert", b"1 molecule converted\n"),
        (b"", b"Open Babel Error: force field setup failed"),
    ],
)
def test_prepare_rejects_reported_error_even_with_zero_exit(
    tmp_path: Path,
    stdout: bytes,
    stderr: bytes,
) -> None:
    executable = _make_executable(tmp_path / "obabel.exe")
    runner = FakeRunner(
        {str(executable): "3.2.1"},
        stdout=stdout,
        stderr=stderr,
    )

    with pytest.raises(OpenBabelExecutionError, match="报告转换错误"):
        prepare_ligand_pdbqt(
            b"CCO\n",
            options=_options(),
            explicit_path=executable,
            runner=runner,
        )
