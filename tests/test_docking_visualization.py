from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from vetevidence.docking_visualization import (
    VerifiedExternalTool,
    analyze_with_plip,
    build_complex_pdb,
    build_visualization_package,
    extract_vina_pose,
    generate_pymol_script,
    launch_pymol_session,
    render_with_pymol,
    verify_plip_runtime_environment,
    verify_plip_executable,
    verify_pymol_executable,
)
from vetevidence.docking_workflow import (
    DockingPocket,
    DockingRunSettings,
    LigandBatchItem,
    LigandIdentity,
    ReceptorIdentity,
    ReceptorPreparationAudit,
    approve_receptor_for_docking,
    inspect_receptor_structure,
    run_docking_batch,
)
from vetevidence.mechanism_prediction import (
    SourceProvenance,
    VinaExecutionAudit,
    parse_vina_output,
)
from vetevidence.vina_execution import (
    VinaExecutionArtifacts,
    VinaLocalExecutionMetadata,
)


RECEPTOR_PDB = b"""HEADER    TEST
ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00           N
ATOM      2  CA  ALA A   1      12.000  13.000   9.000  1.00 20.00           C
ATOM      3  N   GLY B   1      21.104  23.207  19.120  1.00 20.00           N
END
"""
RECEPTOR_PDBQT_A = b"""ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00    -0.300 N
ATOM      2  CA  ALA A   1      12.000  13.000   9.000  1.00 20.00    +0.100 C
"""
LIGAND_PDBQT = b"""ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
"""
MULTI_VINA_OUTPUT = b"""MODEL 1
REMARK VINA RESULT: -8.100 0.000 0.000
ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
ENDMDL
MODEL 2
REMARK VINA RESULT: -7.400 1.200 2.100
ROOT
HETATM    1  C1  LIG L   1       4.000   5.000   6.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
ENDMDL
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color="white").save(output, format="PNG")
    return output.getvalue()


def _approval():
    qc = inspect_receptor_structure(RECEPTOR_PDB, filename="receptor.pdb")
    identity = ReceptorIdentity(
        pdb_id="1ABC",
        ncbi_taxid=9606,
        target_name="Test target",
        organism="Homo sapiens",
        source_url="https://files.rcsb.org/download/1ABC.pdb",
        revision="2026-07-30",
        raw_structure_sha256=_sha256(RECEPTOR_PDB),
        uniprot_ids=("P69905",),
    )
    approval = approve_receptor_for_docking(
        qc,
        RECEPTOR_PDB,
        RECEPTOR_PDBQT_A,
        identity=identity,
        selected_model="1",
        selected_chains=["A"],
        alternate_location_policy="not_present",
        water_policy="not_present",
        heterogen_policy="not_present",
        metal_policy="not_present",
        preparation_audit=ReceptorPreparationAudit(
            method="user_provided",
            tool="user-supplied PDBQT",
            version="unreported",
        ),
        pocket=DockingPocket(
            center_x=11.5,
            center_y=13.1,
            center_z=9.1,
            size_x=20.0,
            size_y=20.0,
            size_z=20.0,
            basis_type="manual",
            selection_basis="用户在受体结构上人工确认",
            source_structure_sha256=_sha256(RECEPTOR_PDB),
        ),
        reviewer="tester",
        user_confirmed=True,
        confirmed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    return qc, identity, approval


def _fake_executor(manifest, ligand_pdbqt, receptor_pdbqt):
    del ligand_pdbqt, receptor_pdbqt
    score = -8.1
    output_pdbqt = f"""MODEL 1
REMARK VINA RESULT: {score:.3f} 0.000 0.000
ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
ENDMDL
""".encode()
    stdout = f"""AutoDock Vina v1.2.5
mode | affinity | dist from best mode
-----+----------+--------------------
1 {score:.3f} 0.0 0.0
"""
    bound_log = (
        f"VetEvidence-Manifest-SHA256: {manifest.manifest_sha256}\n" + stdout
    ).encode()
    parameters = manifest.parameters
    arguments = [
        "vina.exe",
        "--receptor",
        "receptor.pdbqt",
        "--ligand",
        "ligand.pdbqt",
        "--center_x",
        format(parameters.center_x, ".15g"),
        "--center_y",
        format(parameters.center_y, ".15g"),
        "--center_z",
        format(parameters.center_z, ".15g"),
        "--size_x",
        format(parameters.size_x, ".15g"),
        "--size_y",
        format(parameters.size_y, ".15g"),
        "--size_z",
        format(parameters.size_z, ".15g"),
        "--exhaustiveness",
        str(parameters.exhaustiveness),
        "--num_modes",
        str(parameters.num_modes),
        "--energy_range",
        format(parameters.energy_range, ".15g"),
        "--out",
        "output.pdbqt",
        "--seed",
        str(parameters.seed),
    ]
    output_hash = _sha256(output_pdbqt)
    parsed = parse_vina_output(
        bound_log,
        manifest=manifest,
        output_source=SourceProvenance(
            source_name="vina.log",
            accession=f"test:{manifest.task_id}",
            version="1.2.5",
        ),
    )
    audit = VinaExecutionAudit(
        executable_sha256="a" * 64,
        executable_version="1.2.5",
        arguments=arguments,
        exit_code=0,
        duration_seconds=0.1,
        output_pdbqt_sha256=output_hash,
    )
    metadata = VinaLocalExecutionMetadata(
        executable_path="C:/test/vina.exe",
        executable_sha256=audit.executable_sha256,
        executable_version=audit.executable_version,
        arguments=arguments,
        exit_code=0,
        duration_seconds=audit.duration_seconds,
        output_pdbqt_sha256=output_hash,
    )
    return VinaExecutionArtifacts(
        docking_run=parsed.model_copy(update={"execution_audit": audit}),
        metadata=metadata,
        bound_log=bound_log,
        output_pdbqt=output_pdbqt,
    )


def _batch():
    qc, identity, approval = _approval()
    ligand = LigandBatchItem(
        ligand_id="lig-a",
        compound_name="Compound A",
        identity=LigandIdentity(
            namespace="user",
            structure_sha256=_sha256(LIGAND_PDBQT),
            user_namespace="local",
            user_accession="lig-a",
            source_revision="test-fixture-v1",
        ),
        filename="lig-a.pdbqt",
        input_format="pdbqt",
        original_payload=LIGAND_PDBQT,
    )
    return run_docking_batch(
        batch_id="batch-visualization",
        ligands=[ligand],
        seeds=[42],
        receptor_original_filename="receptor.pdb",
        receptor_original_payload=RECEPTOR_PDB,
        receptor_pdbqt=RECEPTOR_PDBQT_A,
        receptor_qc=qc,
        receptor_approval=approval,
        receptor_identity=identity,
        engine_version="1.2.5",
        settings=DockingRunSettings(num_modes=2),
        vina_executor=_fake_executor,
    )


def _verified_tool(
    tmp_path: Path,
    name: str,
    verifier,
) -> VerifiedExternalTool:
    executable = tmp_path / f"{name}.exe"
    executable.write_bytes(f"fake-{name}".encode())
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    runtime_environment = None
    if name == "PLIP":
        libdir = tmp_path / "openbabel-runtime" / "Library" / "bin"
        datadir = (
            tmp_path
            / "openbabel-runtime"
            / "Library"
            / "share"
            / "openbabel"
            / "3.1.0"
        )
        libdir.mkdir(parents=True)
        datadir.mkdir(parents=True)
        (libdir / "openbabel-3.dll").write_bytes(b"openbabel-runtime")
        (datadir / "splash.png").write_bytes(b"openbabel-data")
        runtime_environment = verify_plip_runtime_environment(
            babel_libdir=libdir,
            babel_datadir=datadir,
        )

    def version_runner(arguments: list[str], **kwargs: object):
        assert kwargs["shell"] is False
        if name == "PLIP":
            assert kwargs["env"] == runtime_environment.environment_overrides()
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{name} 1.0\n".encode(),
            stderr=b"",
        )

    status = verifier(
        executable,
        user_confirmed=True,
        **(
            {"runtime_environment": runtime_environment}
            if runtime_environment is not None
            else {}
        ),
        runner=version_runner,
    )
    assert status.available
    return status


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not used")
def test_external_tool_verification_rejects_non_executable_on_posix(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "pymol.exe"
    executable.write_bytes(b"fake-pymol")
    executable.chmod(executable.stat().st_mode & ~0o111)

    def version_runner(arguments: list[str], **kwargs: object):
        pytest.fail("version runner must not be called for a non-executable file")

    status = verify_pymol_executable(
        executable,
        user_confirmed=True,
        runner=version_runner,
    )

    assert status.available is False
    assert status.executable_path is None
    assert "文件不可执行" in (status.reason or "")


def test_extract_pose_and_complex_use_only_approved_receptor_selection() -> None:
    pose = extract_vina_pose(MULTI_VINA_OUTPUT, mode=2)
    assert b"MODEL 2" in pose
    assert b"4.000" in pose
    assert b"MODEL 1" not in pose

    _, _, approval = _approval()
    complex_artifact = build_complex_pdb(approval, pose)
    text = complex_artifact.payload.decode()

    assert complex_artifact.receptor_atom_count == 2
    assert complex_artifact.ligand_atom_count == 1
    assert all(
        line[21:22] != "B"
        for line in text.splitlines()
        if line.startswith(("ATOM", "HETATM"))
    )
    assert " LIG " in text
    assert "   4.000   5.000   6.000" in text
    assert complex_artifact.sha256 == _sha256(complex_artifact.payload)


def test_pml_is_fixed_editable_and_bound_to_exact_complex() -> None:
    _, _, approval = _approval()
    complex_artifact = build_complex_pdb(
        approval,
        extract_vina_pose(MULTI_VINA_OUTPUT, mode=1),
    )
    script = generate_pymol_script(
        complex_sha256=complex_artifact.sha256,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        vina_score_kcal_mol=-8.1,
        pose_mode=1,
    )
    text = script.payload.decode()

    assert "Vina 预测评分" in text
    assert "结合能" not in text
    assert f"Bound-Complex-SHA256: {complex_artifact.sha256}" in text
    assert "load complex.pdb, docking_complex" in text
    assert "save interaction.pse" in text
    assert "png interaction.png" in text
    assert script.sha256 == _sha256(script.payload)

    with pytest.raises(ValueError, match="固定命令模板"):
        type(script)(
            payload=b"run attacker.py\n",
            sha256=_sha256(b"run attacker.py\n"),
            bound_complex_sha256=complex_artifact.sha256,
        )


def test_package_derives_score_from_revalidated_attempt_and_is_deterministic() -> None:
    created = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    batch = _batch()
    kwargs = {
        "batch": batch,
        "ligand_id": "lig-a",
        "seed": 42,
        "pose_mode": 1,
        "user_confirmed_external_tools": False,
        "additional_files": {"notes/readme.txt": b"test note"},
        "created_at": created,
    }
    first = build_visualization_package(**kwargs)
    second = build_visualization_package(**kwargs)

    assert "vina_score_kcal_mol" not in inspect.signature(
        build_visualization_package
    ).parameters
    assert first.pymol_render.png.status == "unavailable"
    assert first.plip_analysis.xml.status == "unavailable"
    assert first.batch_id == "batch-visualization"
    assert first.ligand_id == "lig-a"
    assert first.seed == 42
    assert first.pose_mode == 1
    assert first.zip_payload == second.zip_payload
    assert first.zip_sha256 == second.zip_sha256

    with zipfile.ZipFile(BytesIO(first.zip_payload)) as archive:
        names = set(archive.namelist())
        assert {
            "receptor_original",
            "receptor_selected.pdb",
            "receptor_prepared.pdbqt",
            "receptor_approval.json",
            "ligand_original",
            "ligand_prepared.pdbqt",
            "ligand_identity.json",
            "vina_manifest.json",
            "vina_execution_audit.json",
            "vina_out.pdbqt",
            "vina_bound.log",
            "selected_pose.pdbqt",
            "selected_pose.csv",
            "complex.pdb",
            "view.pml",
            "visualization_manifest.json",
            "SHA256SUMS.txt",
            "notes/readme.txt",
        }.issubset(names)
        manifest = json.loads(archive.read("visualization_manifest.json"))
        assert manifest["batch_id"] == "batch-visualization"
        assert manifest["ligand_id"] == "lig-a"
        assert manifest["score"] == {
            "interpretation": "computational_prediction",
            "metric": "Vina 预测评分",
            "mode": 1,
            "seed": 42,
            "value_kcal_mol": -8.1,
        }
        assert (
            manifest["inputs"]["vina_manifest_sha256"]
            == first.task_manifest_sha256
        )
        checksum_lines = archive.read("SHA256SUMS.txt").decode().splitlines()
        assert any(line.endswith("  view.pml") for line in checksum_lines)

    with pytest.raises(ValueError, match="归属不一致"):
        type(first).model_validate(
            {**first.model_dump(), "batch_id": "another-batch"}
        )

    attempt = batch.attempts[0]
    assert attempt.output_pdbqt is not None
    tampered_attempt = attempt.model_copy(
        update={"output_pdbqt": attempt.output_pdbqt.replace(b"-8.100", b"-6.000")}
    )
    tampered_batch = batch.model_copy(update={"attempts": (tampered_attempt,)})
    with pytest.raises(ValueError, match="SHA-256"):
        build_visualization_package(
            batch=tampered_batch,
            ligand_id="lig-a",
            seed=42,
            pose_mode=1,
            user_confirmed_external_tools=False,
        )


def test_pymol_adapter_requires_consent_and_validates_png_fully(
    tmp_path: Path,
) -> None:
    with pytest.raises(PermissionError, match="显式确认"):
        verify_pymol_executable(
            tmp_path / "not-run.exe",
            user_confirmed=False,
        )
    tool = _verified_tool(tmp_path, "PyMOL", verify_pymol_executable)
    _, _, approval = _approval()
    complex_artifact = build_complex_pdb(
        approval,
        extract_vina_pose(MULTI_VINA_OUTPUT, mode=1),
    )
    pml = generate_pymol_script(
        complex_sha256=complex_artifact.sha256,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        vina_score_kcal_mol=-8.1,
        pose_mode=1,
    )

    with pytest.raises(PermissionError, match="显式确认"):
        render_with_pymol(
            tool=tool,
            complex_pdb=complex_artifact,
            pml_script=pml,
            user_confirmed=False,
        )

    calls: list[tuple[list[str], dict[str, object]]] = []

    def render_runner(arguments: list[str], **kwargs: object):
        calls.append((arguments, dict(kwargs)))
        root = Path(str(kwargs["cwd"]))
        (root / "interaction.png").write_bytes(_valid_png())
        (root / "interaction.pse").write_bytes(b"pse-session")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=b"rendered",
            stderr=b"",
        )

    result = render_with_pymol(
        tool=tool,
        complex_pdb=complex_artifact,
        pml_script=pml,
        user_confirmed=True,
        runner=render_runner,
    )

    assert result.png.status == "available"
    assert result.pse.status == "generated_unverified"
    assert "二次重开" in (result.pse.reason or "")
    assert calls[0][0][1:] == ["-cq", "view.pml"]
    assert calls[0][1]["shell"] is False

    def corrupt_runner(arguments: list[str], **kwargs: object):
        root = Path(str(kwargs["cwd"]))
        (root / "interaction.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    corrupt = render_with_pymol(
        tool=tool,
        complex_pdb=complex_artifact,
        pml_script=pml,
        user_confirmed=True,
        runner=corrupt_runner,
    )
    assert corrupt.png.status == "unavailable"
    assert corrupt.png.payload is None


def test_plip_adapter_binds_xml_to_exact_docked_ligand(tmp_path: Path) -> None:
    tool = _verified_tool(tmp_path, "PLIP", verify_plip_executable)
    _, _, approval = _approval()
    complex_artifact = build_complex_pdb(
        approval,
        extract_vina_pose(MULTI_VINA_OUTPUT, mode=1),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def plip_runner(arguments: list[str], **kwargs: object):
        calls.append((arguments, dict(kwargs)))
        assert tool.runtime_environment is not None
        assert kwargs["env"] == tool.runtime_environment.environment_overrides()
        output = Path(str(kwargs["cwd"])) / "plip-output"
        (output / "complex_report.xml").write_text(
            (
                "<report><bindingsite><identifiers>"
                "<hetid>LIG</hetid>"
                f"<chain>{complex_artifact.ligand_chain}</chain>"
                f"<position>{complex_artifact.ligand_residue_number}</position>"
                "</identifiers></bindingsite></report>"
            ),
            encoding="utf-8",
        )
        (output / "complex_report.txt").write_bytes(b"interaction")
        (output / "unrelated.xml").write_bytes(b"<unrelated/>")
        identity_name = (
            "COMPLEX_PROTEIN_"
            f"LIG_{complex_artifact.ligand_chain}_"
            f"{complex_artifact.ligand_residue_number}"
        )
        (output / f"{identity_name}.png").write_bytes(_valid_png())
        (output / f"{identity_name}.pse").write_bytes(b"pse")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=b"PLIP complete",
            stderr=b"",
        )

    result = analyze_with_plip(
        tool=tool,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=True,
        runner=plip_runner,
    )

    assert result.xml.status == "available"
    assert result.text.status == "available"
    assert result.png.status == "unavailable"
    assert result.pse.status == "unavailable"
    assert "不调用 PLIP 可视化" in (result.png.reason or "")
    assert "--nohydro" not in calls[0][0]
    assert "-y" not in calls[0][0]
    assert "-p" not in calls[0][0]
    assert calls[0][1]["shell"] is False

    def wrong_ligand_runner(arguments: list[str], **kwargs: object):
        output = Path(str(kwargs["cwd"])) / "plip-output"
        (output / "complex_report.xml").write_bytes(
            b"<report><bindingsite><identifiers><hetid>LIG</hetid>"
            b"<chain>X</chain><position>1</position>"
            b"</identifiers></bindingsite></report>"
        )
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    rejected = analyze_with_plip(
        tool=tool,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=True,
        runner=wrong_ligand_runner,
    )
    assert rejected.xml.status == "unavailable"
    assert rejected.text.status == "unavailable"

    def missing_xml_runner(arguments: list[str], **kwargs: object):
        output = Path(str(kwargs["cwd"])) / "plip-output"
        (output / "COMPLEX_PROTEIN_LIG_Z_9999.png").write_bytes(_valid_png())
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=b"",
            stderr=b"tool stopped before XML generation",
        )

    missing_xml = analyze_with_plip(
        tool=tool,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=True,
        runner=missing_xml_runner,
    )
    assert missing_xml.xml.status == "unavailable"
    assert "未生成预期" in (missing_xml.xml.reason or "")
    assert "唯一绑定" not in (missing_xml.xml.reason or "")

    def ambiguous_xml_runner(arguments: list[str], **kwargs: object):
        output = Path(str(kwargs["cwd"])) / "plip-output"
        payload = (
            b"<report><bindingsite><identifiers><hetid>LIG</hetid>"
            b"<chain>Z</chain><position>9999</position>"
            b"</identifiers></bindingsite></report>"
        )
        (output / "complex_report.xml").write_bytes(payload)
        (output / "report.xml").write_bytes(payload)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    ambiguous_xml = analyze_with_plip(
        tool=tool,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=True,
        runner=ambiguous_xml_runner,
    )
    assert ambiguous_xml.xml.status == "unavailable"
    assert "多个匹配" in (ambiguous_xml.xml.reason or "")
    assert "唯一绑定当前" not in (ambiguous_xml.xml.reason or "")

    assert tool.runtime_environment is not None
    marker = (
        Path(tool.runtime_environment.babel_libdir.path)
        / tool.runtime_environment.babel_libdir.markers[0].filename
    )
    marker.write_bytes(b"tampered-runtime")
    runner_called = False

    def should_not_run(arguments: list[str], **kwargs: object):
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    changed_runtime = analyze_with_plip(
        tool=tool,
        complex_pdb=complex_artifact,
        ligand_chain=complex_artifact.ligand_chain,
        ligand_residue_number=complex_artifact.ligand_residue_number,
        user_confirmed=True,
        runner=should_not_run,
    )
    assert changed_runtime.xml.status == "unavailable"
    assert "runtime" in (changed_runtime.xml.reason or "")
    assert runner_called is False


def test_pymol_gui_launch_accepts_only_verified_package_file(
    tmp_path: Path,
) -> None:
    package = build_visualization_package(
        batch=_batch(),
        ligand_id="lig-a",
        seed=42,
        pose_mode=1,
        user_confirmed_external_tools=False,
    )
    tool = _verified_tool(tmp_path, "PyMOL", verify_pymol_executable)
    task_root = tmp_path / "task"
    task_root.mkdir()
    for item in package.files:
        path = task_root / item.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.payload)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(arguments: list[str], **kwargs: object):
        calls.append((arguments, dict(kwargs)))
        return object()

    with pytest.raises(PermissionError, match="显式点击"):
        launch_pymol_session(
            tool=tool,
            package=package,
            session_relative_path="view.pml",
            allowed_root=task_root,
            user_confirmed=False,
            popen_factory=fake_popen,
        )

    launched = launch_pymol_session(
        tool=tool,
        package=package,
        session_relative_path="view.pml",
        allowed_root=task_root,
        user_confirmed=True,
        popen_factory=fake_popen,
    )
    session = (task_root / "view.pml").resolve()
    assert launched is not None
    assert calls[0][0] == [tool.executable_path, str(session)]
    assert calls[0][1]["shell"] is False

    session.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        launch_pymol_session(
            tool=tool,
            package=package,
            session_relative_path="view.pml",
            allowed_root=task_root,
            user_confirmed=True,
            popen_factory=fake_popen,
        )

    with pytest.raises(ValueError, match="固定 PyMOL"):
        launch_pymol_session(
            tool=tool,
            package=package,
            session_relative_path="other.pml",
            allowed_root=task_root,
            user_confirmed=True,
            popen_factory=fake_popen,
        )
