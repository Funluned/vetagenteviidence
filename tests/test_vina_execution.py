from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

import vetevidence.vina_execution as vina_execution
from vetevidence.mechanism_prediction import (
    SourceProvenance,
    VinaParameters,
    build_vina_manifest,
)
from vetevidence.vina_execution import (
    VinaExecutionError,
    discover_vina,
    execute_vina,
)


LIGAND_PDBQT = b"""ROOT
ATOM      1  C1  LIG A   1       0.0 0.0 0.0  0.00  0.00    +0.0 C
ENDROOT
TORSDOF 0
"""
RECEPTOR_PDBQT = b"""ATOM      1  CA  ALA A   1       0.0 0.0 0.0  0.00  0.00    +0.0 C
"""
OUTPUT_PDBQT = b"""MODEL 1
ROOT
ATOM      1  C1  LIG A   1       0.0 0.0 0.0  0.00  0.00    +0.0 C
ENDROOT
TORSDOF 0
ENDMDL
"""
VINA_STDOUT = b"""AutoDock Vina v1.2.5
mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
2 -7.4 1.2 2.1
"""


def _source(
    name: str,
    accession: str,
    payload: bytes,
) -> SourceProvenance:
    return SourceProvenance(
        source_name=name,
        accession=accession,
        version="2026-07-30",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _manifest():
    return build_vina_manifest(
        task_id="dock-local-001",
        compound_name="Compound A",
        ligand_accession="PubChem:1",
        receptor_name="Target One",
        receptor_accession="PDB:1ABC",
        receptor_organism="Target bacterium",
        ligand_source=_source("ligand.pdbqt", "PubChem:1", LIGAND_PDBQT),
        receptor_source=_source(
            "receptor.pdbqt",
            "PDB:1ABC",
            RECEPTOR_PDBQT,
        ),
        parameters=VinaParameters(
            center_x=1.0,
            center_y=2.0,
            center_z=3.0,
            size_x=20.0,
            size_y=21.0,
            size_z=22.0,
            exhaustiveness=16,
            num_modes=3,
            energy_range=4.0,
            seed=42,
        ),
        engine_version="1.2.5",
    )


def _make_executable(path: Path, payload: bytes = b"fake-vina") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path.resolve()


class FakeRunner:
    def __init__(
        self,
        versions: dict[str, str],
        *,
        returncode: int = 0,
        stdout: bytes = VINA_STDOUT,
        stderr: bytes = b"",
        output_pdbqt: bytes | None = OUTPUT_PDBQT,
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
        if normalized_arguments[1:] == ["--version"]:
            version = self.versions.get(executable)
            if version is None:
                return subprocess.CompletedProcess(
                    normalized_arguments,
                    1,
                    stdout=b"",
                    stderr=b"not AutoDock Vina",
                )
            return subprocess.CompletedProcess(
                normalized_arguments,
                0,
                stdout=f"AutoDock Vina v{version}\n".encode(),
                stderr=b"",
            )

        if self.timeout_on_execution:
            raise subprocess.TimeoutExpired(
                cmd=normalized_arguments,
                timeout=float(kwargs["timeout"]),
            )
        working_directory = Path(str(kwargs["cwd"]))
        if self.output_pdbqt is not None:
            (working_directory / "output.pdbqt").write_bytes(
                self.output_pdbqt
            )
        return subprocess.CompletedProcess(
            normalized_arguments,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_discover_vina_explicit_path_has_priority_and_is_fingerprinted(
    tmp_path: Path,
) -> None:
    explicit = _make_executable(tmp_path / "explicit" / "vina.exe", b"explicit")
    configured = _make_executable(
        tmp_path / "configured" / "vina.exe",
        b"configured",
    )
    runner = FakeRunner(
        {
            str(explicit): "1.2.5",
            str(configured): "9.9.9",
        }
    )

    discovered = discover_vina(
        explicit,
        environment={
            "VINA_EXECUTABLE": str(configured),
            "PATH": "",
        },
        runner=runner,
    )

    assert discovered.path == str(explicit)
    assert discovered.version == "1.2.5"
    assert discovered.sha256 == hashlib.sha256(b"explicit").hexdigest()
    assert [call[0] for call in runner.calls] == [
        [str(explicit), "--version"]
    ]


def test_discover_vina_reports_unreadable_executable_as_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner({str(executable): "1.2.5"})

    def unreadable(_: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(vina_execution, "_sha256_file", unreadable)

    with pytest.raises(VinaExecutionError, match="计算 SHA-256"):
        discover_vina(executable, runner=runner)


def test_discover_vina_uses_semantic_version_order_within_path(
    tmp_path: Path,
) -> None:
    older = _make_executable(tmp_path / "old" / "vina.exe", b"old")
    newer = _make_executable(tmp_path / "new" / "vina.exe", b"new")
    runner = FakeRunner(
        {
            str(older): "1.9.0",
            str(newer): "1.10.0",
        }
    )

    discovered = discover_vina(
        environment={
            "PATH": os.pathsep.join(
                [str(older.parent), str(newer.parent)]
            ),
            "LOCALAPPDATA": "",
        },
        runner=runner,
    )

    assert discovered.path == str(newer)
    assert discovered.version == "1.10.0"


def test_discover_vina_falls_back_to_local_appdata_and_rejects_fake_path(
    tmp_path: Path,
) -> None:
    local_vina = _make_executable(
        tmp_path
        / "Local"
        / "Programs"
        / "AutoDockVina"
        / "1.2.5"
        / "vina.exe"
    )
    runner = FakeRunner({str(local_vina): "1.2.5"})

    discovered = discover_vina(
        environment={
            "PATH": "",
            "LOCALAPPDATA": str(tmp_path / "Local"),
        },
        runner=runner,
    )

    assert discovered.path == str(local_vina)
    with pytest.raises(VinaExecutionError, match="不存在"):
        discover_vina(
            tmp_path / "missing-vina.exe",
            runner=runner,
        )


def test_discover_vina_prefers_managed_local_install_over_path(
    tmp_path: Path,
) -> None:
    path_vina = _make_executable(tmp_path / "path" / "vina.exe", b"path")
    local_vina = _make_executable(
        tmp_path
        / "Local"
        / "Programs"
        / "AutoDockVina"
        / "1.2.7"
        / "vina.exe",
        b"managed",
    )
    runner = FakeRunner(
        {
            str(path_vina): "9.9.9",
            str(local_vina): "1.2.7",
        }
    )

    discovered = discover_vina(
        environment={
            "PATH": str(path_vina.parent),
            "LOCALAPPDATA": str(tmp_path / "Local"),
        },
        runner=runner,
    )

    assert discovered.path == str(local_vina)
    assert discovered.version == "1.2.7"


def test_execute_vina_uses_only_manifest_arguments_and_returns_audit(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "vina.exe", b"real-binary")
    runner = FakeRunner({str(executable): "1.2.5"})
    manifest = _manifest()

    artifacts = execute_vina(
        manifest,
        LIGAND_PDBQT,
        RECEPTOR_PDBQT,
        explicit_path=executable,
        runner=runner,
        timeout_seconds=30,
    )

    execution_arguments, execution_options = runner.calls[1]
    assert execution_arguments == [
        str(executable),
        "--receptor",
        "receptor.pdbqt",
        "--ligand",
        "ligand.pdbqt",
        "--center_x",
        "1",
        "--center_y",
        "2",
        "--center_z",
        "3",
        "--size_x",
        "20",
        "--size_y",
        "21",
        "--size_z",
        "22",
        "--exhaustiveness",
        "16",
        "--num_modes",
        "3",
        "--energy_range",
        "4",
        "--out",
        "output.pdbqt",
        "--seed",
        "42",
    ]
    assert execution_options["shell"] is False
    assert execution_options["capture_output"] is True
    assert not Path(str(execution_options["cwd"])).exists()
    assert artifacts.bound_log.startswith(
        (
            "VetEvidence-Manifest-SHA256: "
            f"{manifest.manifest_sha256}\n"
        ).encode()
    )
    assert artifacts.output_pdbqt == OUTPUT_PDBQT
    assert artifacts.docking_run.best_affinity_kcal_mol == -8.1
    assert artifacts.docking_run.execution_audit is not None
    assert artifacts.docking_run.execution_audit.arguments == (
        artifacts.metadata.arguments
    )
    assert artifacts.metadata.executable_path == str(executable)
    assert artifacts.metadata.executable_sha256 == hashlib.sha256(
        b"real-binary"
    ).hexdigest()
    assert artifacts.metadata.output_pdbqt_sha256 == hashlib.sha256(
        OUTPUT_PDBQT
    ).hexdigest()
    serialized = artifacts.metadata.model_dump(mode="json")
    assert serialized["exit_code"] == 0
    assert all("vetevidence-vina-" not in item for item in serialized["arguments"])
    assert serialized["arguments"][0] == "vina.exe"


def test_execute_vina_rejects_input_hash_mismatch_before_starting(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner({str(executable): "1.2.5"})

    with pytest.raises(VinaExecutionError, match="配体.*SHA-256"):
        execute_vina(
            _manifest(),
            LIGAND_PDBQT + b"\nREMARK changed",
            RECEPTOR_PDBQT,
            explicit_path=executable,
            runner=runner,
        )

    assert runner.calls == []


def test_execute_vina_rejects_actual_version_mismatch(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner({str(executable): "1.2.4"})

    with pytest.raises(VinaExecutionError, match="实际版本与任务清单不一致"):
        execute_vina(
            _manifest(),
            LIGAND_PDBQT,
            RECEPTOR_PDBQT,
            explicit_path=executable,
            runner=runner,
        )

    assert len(runner.calls) == 1


def test_execute_vina_timeout_never_returns_scores(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner(
        {str(executable): "1.2.5"},
        timeout_on_execution=True,
    )

    with pytest.raises(VinaExecutionError, match="超时.*未生成分数"):
        execute_vina(
            _manifest(),
            LIGAND_PDBQT,
            RECEPTOR_PDBQT,
            explicit_path=executable,
            runner=runner,
            timeout_seconds=0.1,
        )


def test_execute_vina_nonzero_exit_never_parses_stdout(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner(
        {str(executable): "1.2.5"},
        returncode=2,
        stderr=b"configuration error",
    )

    with pytest.raises(VinaExecutionError, match="非零退出码 2"):
        execute_vina(
            _manifest(),
            LIGAND_PDBQT,
            RECEPTOR_PDBQT,
            explicit_path=executable,
            runner=runner,
        )


def test_execute_vina_requires_output_pdbqt(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "vina.exe")
    runner = FakeRunner(
        {str(executable): "1.2.5"},
        output_pdbqt=None,
    )

    with pytest.raises(VinaExecutionError, match="未生成 output.pdbqt"):
        execute_vina(
            _manifest(),
            LIGAND_PDBQT,
            RECEPTOR_PDBQT,
            explicit_path=executable,
            runner=runner,
        )
