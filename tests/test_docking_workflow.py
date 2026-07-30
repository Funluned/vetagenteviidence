from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vetevidence.docking_workflow import (
    DockingPocket,
    DockingRunSettings,
    LigandBatchItem,
    LigandIdentity,
    ReceptorIdentity,
    ReceptorPreparationAudit,
    ResidueIdentity,
    approve_receptor_for_docking,
    inspect_receptor_structure,
    probe_meeko,
    require_receptor_approval,
    run_docking_batch,
    validate_successful_docking_attempt,
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


RECEPTOR_PDB = b"""HEADER    TEST RECEPTOR
ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00           N
ATOM      2  CA  ALA A   1      12.000  13.000   9.000  1.00 20.00           C
ATOM      3  N   GLY B   1      21.104  23.207  19.120  1.00 20.00           N
END
"""
RECEPTOR_WITH_DECISIONS_PDB = b"""HEADER    TEST RECEPTOR WITH DECISIONS
ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00           N
ATOM      2  CA AALA A   1      12.000  13.000   9.000  0.60 20.00           C
ATOM      3  CA BALA A   1      12.100  13.100   9.100  0.40 20.00           C
HETATM    4  O   HOH A 101      13.000  14.000  10.000  1.00 20.00           O
HETATM    5 ZN    ZN A 201      14.000  15.000  11.000  1.00 20.00          ZN
HETATM    6  C1  HEM A 301      15.000  16.000  12.000  1.00 20.00           C
END
"""
RECEPTOR_PDBQT_A = b"""ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00    -0.300 N
ATOM      2  CA  ALA A   1      12.000  13.000   9.000  1.00 20.00    +0.100 C
"""
RECEPTOR_PDBQT_WRONG_CHAIN = b"""ATOM      1  N   ALA A   1      11.104  13.207   9.120  1.00 20.00    -0.300 N
HETATM    2  CA  ALA B   1      12.000  13.000   9.000  1.00 20.00    +0.100 C
"""
RECEPTOR_PDBQT_WRONG_COORDINATES = b"""ATOM      1  N   ALA A   1      30.104  33.207  29.120  1.00 20.00    -0.300 N
ATOM      2  CA  ALA A   1      32.000  33.000  29.000  1.00 20.00    +0.100 C
"""
LIGAND_PDBQT = b"""ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _receptor_identity(payload: bytes = RECEPTOR_PDB) -> ReceptorIdentity:
    return ReceptorIdentity(
        pdb_id="1ABC",
        ncbi_taxid=9606,
        target_name="Test target",
        organism="Homo sapiens",
        source_url="https://files.rcsb.org/download/1ABC.pdb",
        revision="2026-07-30",
        raw_structure_sha256=_sha256(payload),
        uniprot_ids=("P69905",),
    )


def _pocket(payload: bytes = RECEPTOR_PDB, **updates: object) -> DockingPocket:
    values: dict[str, object] = {
        "center_x": 11.5,
        "center_y": 13.1,
        "center_z": 9.1,
        "size_x": 20.0,
        "size_y": 20.0,
        "size_z": 20.0,
        "basis_type": "manual",
        "selection_basis": "用户在受体结构上人工确认",
        "source_structure_sha256": _sha256(payload),
    }
    values.update(updates)
    return DockingPocket(**values)


def _preparation_audit() -> ReceptorPreparationAudit:
    return ReceptorPreparationAudit(
        method="user_provided",
        tool="user-supplied PDBQT",
        version="unreported",
        arguments=(),
    )


def _approved_receptor():
    qc = inspect_receptor_structure(RECEPTOR_PDB, filename="receptor.pdb")
    identity = _receptor_identity()
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
        preparation_audit=_preparation_audit(),
        pocket=_pocket(),
        reviewer="tester",
        user_confirmed=True,
        confirmed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    return qc, identity, approval


def _ligand(
    *,
    ligand_id: str = "lig-a",
    payload: bytes = LIGAND_PDBQT,
    filename: str = "lig-a.pdbqt",
    input_format: str = "pdbqt",
) -> LigandBatchItem:
    return LigandBatchItem(
        ligand_id=ligand_id,
        compound_name=f"Compound {ligand_id}",
        identity=LigandIdentity(
            namespace="user",
            structure_sha256=_sha256(payload),
            user_namespace="local",
            user_accession=ligand_id,
            source_revision="test-fixture-v1",
        ),
        filename=filename,
        input_format=input_format,
        original_payload=payload,
    )


def _fake_vina_artifacts(
    manifest,
    *,
    log_score: float,
    pose_score: float | None = None,
    audit_seed: int | None = None,
) -> VinaExecutionArtifacts:
    actual_pose_score = log_score if pose_score is None else pose_score
    output_pdbqt = f"""MODEL 1
REMARK VINA RESULT: {actual_pose_score:.3f} 0.000 0.000
ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
ENDMDL
""".encode()
    stdout = f"""AutoDock Vina v1.2.5
mode | affinity | dist from best mode
-----+----------+--------------------
1 {log_score:.3f} 0.0 0.0
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
        str(parameters.seed if audit_seed is None else audit_seed),
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


def _fake_executor(manifest, ligand_pdbqt, receptor_pdbqt):
    del ligand_pdbqt, receptor_pdbqt
    score = -8.0 if manifest.parameters.seed == 11 else -7.6
    return _fake_vina_artifacts(manifest, log_score=score)


def _run_batch(
    *,
    ligands: list[LigandBatchItem] | None = None,
    seeds: list[int] | None = None,
    ligand_preparer=None,
    vina_executor=_fake_executor,
    fail_fast: bool = False,
):
    qc, identity, approval = _approved_receptor()
    return run_docking_batch(
        batch_id="batch-001",
        ligands=ligands or [_ligand()],
        seeds=seeds or [11],
        receptor_original_filename="receptor.pdb",
        receptor_original_payload=RECEPTOR_PDB,
        receptor_pdbqt=RECEPTOR_PDBQT_A,
        receptor_qc=qc,
        receptor_approval=approval,
        receptor_identity=identity,
        engine_version="1.2.5",
        settings=DockingRunSettings(exhaustiveness=16, num_modes=3),
        ligand_preparer=ligand_preparer,
        vina_executor=vina_executor,
        fail_fast=fail_fast,
    )


def test_receptor_qc_inventories_require_explicit_scientific_decisions() -> None:
    qc = inspect_receptor_structure(
        RECEPTOR_WITH_DECISIONS_PDB,
        filename="receptor.pdb",
    )

    assert qc.usable
    assert qc.model_ids == ("1",)
    assert qc.chains == ("A",)
    assert qc.alternate_locations == ("A", "B")
    assert len(qc.water_residues) == 1
    assert len(qc.metal_residues) == 1
    assert len(qc.heterogen_residues) == 1
    assert any("alternate location" in warning for warning in qc.warnings)
    assert any("水分子" in warning for warning in qc.warnings)
    assert any("金属" in warning for warning in qc.warnings)

    with pytest.raises(ValueError, match="alternate location"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_WITH_DECISIONS_PDB,
            RECEPTOR_PDBQT_A,
            identity=_receptor_identity(RECEPTOR_WITH_DECISIONS_PDB),
            selected_model="1",
            selected_chains=["A"],
            alternate_location_policy="not_present",
            water_policy="remove_all",
            heterogen_policy="remove_all",
            metal_policy="remove_all",
            preparation_audit=_preparation_audit(),
            pocket=_pocket(RECEPTOR_WITH_DECISIONS_PDB),
            reviewer="tester",
            user_confirmed=True,
        )
    with pytest.raises(ValueError, match="一一映射"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_WITH_DECISIONS_PDB,
            RECEPTOR_PDBQT_A,
            identity=_receptor_identity(RECEPTOR_WITH_DECISIONS_PDB),
            selected_model="1",
            selected_chains=["A"],
            alternate_location_policy="explicit",
            selected_alternate_locations=["A", "B"],
            water_policy="remove_all",
            heterogen_policy="remove_all",
            metal_policy="remove_all",
            preparation_audit=_preparation_audit(),
            pocket=_pocket(RECEPTOR_WITH_DECISIONS_PDB),
            reviewer="tester",
            user_confirmed=True,
        )


def test_receptor_qc_supports_mmcif_atom_site_loop() -> None:
    mmcif = b"""data_demo
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.label_alt_id
_atom_site.pdbx_PDB_model_num
ATOM 1 N N ALA A 1 1.0 2.0 3.0 . 1
HETATM 2 O O HOH A 101 2.0 3.0 4.0 . 1
#
"""
    qc = inspect_receptor_structure(mmcif, filename="receptor.cif")

    assert qc.structure_format == "mmcif"
    assert qc.atom_count == 2
    assert qc.chains == ("A",)
    assert qc.water_atom_count == 1
    assert qc.usable


def test_receptor_approval_binds_identity_selection_pocket_and_atom_mapping() -> None:
    qc = inspect_receptor_structure(RECEPTOR_PDB, filename="receptor.pdb")
    common = {
        "identity": _receptor_identity(),
        "selected_model": "1",
        "selected_chains": ["A"],
        "alternate_location_policy": "not_present",
        "water_policy": "not_present",
        "heterogen_policy": "not_present",
        "metal_policy": "not_present",
        "preparation_audit": _preparation_audit(),
        "pocket": _pocket(),
        "reviewer": "tester",
    }

    with pytest.raises(ValueError, match="明确确认"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            **common,
            user_confirmed=False,
        )
    with pytest.raises(ValueError, match="原始结构 SHA-256"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            **{**common, "identity": _receptor_identity(b"different")},
            user_confirmed=True,
        )
    with pytest.raises(ValueError, match="口袋依据"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            **{**common, "pocket": _pocket(b"different")},
            user_confirmed=True,
        )
    cross_chain_pocket = _pocket(
        basis_type="residue_selection",
        basis_residues=(
            ResidueIdentity(
                model_id="1",
                chain_id="B",
                residue_name="GLY",
                residue_number="1",
            ),
        ),
    )
    with pytest.raises(ValueError, match="模型与链"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            **{**common, "pocket": cross_chain_pocket},
            user_confirmed=True,
        )
    with pytest.raises(ValueError, match="未在人工选择结构"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_WRONG_CHAIN,
            **common,
            user_confirmed=True,
        )
    with pytest.raises(ValueError, match="偏差超过"):
        approve_receptor_for_docking(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_WRONG_COORDINATES,
            **common,
            user_confirmed=True,
        )

    _, _, approval = _approved_receptor()
    assert approval.selected_model == "1"
    assert approval.selected_chains == ("A",)
    assert b" GLY B" not in approval.selected_receptor_pdb
    assert approval.heavy_atom_match_fraction == 1.0

    forged_pocket = approval.pocket.model_copy(
        update={"source_structure_sha256": "0" * 64}
    )
    forged_approval = approval.model_copy(update={"pocket": forged_pocket})
    with pytest.raises(ValueError, match="口袋未绑定"):
        require_receptor_approval(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            _receptor_identity(),
            forged_approval,
        )

    forged_cross_chain_approval = approval.model_copy(
        update={"pocket": cross_chain_pocket}
    )
    with pytest.raises(ValueError, match="模型与所选链"):
        require_receptor_approval(
            qc,
            RECEPTOR_PDB,
            RECEPTOR_PDBQT_A,
            _receptor_identity(),
            forged_cross_chain_approval,
        )


def test_ligand_identity_is_bound_to_exact_input_bytes() -> None:
    with pytest.raises(ValueError, match="LigandIdentity"):
        LigandBatchItem(
            ligand_id="lig-a",
            compound_name="Compound A",
            identity=LigandIdentity(
                namespace="user",
                structure_sha256="0" * 64,
                user_namespace="local",
                user_accession="lig-a",
                source_revision="v1",
            ),
            filename="lig-a.pdbqt",
            input_format="pdbqt",
            original_payload=LIGAND_PDBQT,
        )


def test_batch_orchestrates_ligands_by_seeds_and_preserves_audit() -> None:
    original_sdf = b"original sdf"
    ligands = [
        _ligand(ligand_id="lig-a"),
        _ligand(
            ligand_id="lig-b",
            payload=original_sdf,
            filename="lig-b.sdf",
            input_format="sdf",
        ),
    ]

    def preparer(item: LigandBatchItem) -> bytes:
        assert item.original_payload == original_sdf
        return LIGAND_PDBQT

    result = _run_batch(
        ligands=ligands,
        seeds=[11, 22],
        ligand_preparer=preparer,
    )

    assert len(result.attempts) == 4
    assert all(attempt.status == "succeeded" for attempt in result.attempts)
    assert [attempt.seed for attempt in result.attempts] == [11, 22, 11, 22]
    assert all(attempt.score_label == "Vina 预测评分" for attempt in result.attempts)
    assert result.preparations[1].identity.user_accession == "lig-b"
    assert result.preparations[1].original_payload == original_sdf
    assert result.stability[0].best_scores_kcal_mol == (-8.0, -7.6)
    assert result.stability[0].mean_score_kcal_mol == pytest.approx(-7.8)
    assert result.stability[0].score_range_kcal_mol == pytest.approx(0.4)
    assert result.stability[0].assessment == "descriptive_only"
    assert result.stability[0].cross_seed_pose_rmsd_available is False
    validate_successful_docking_attempt(
        result.attempts[0],
        result.preparations[0],
        result.receptor_approval,
    )


def test_task_ids_are_bounded_and_deterministic_for_long_safe_ids() -> None:
    qc, identity, approval = _approved_receptor()
    ligand = _ligand(ligand_id="l" * 64)
    kwargs = {
        "batch_id": "b" * 64,
        "ligands": [ligand],
        "seeds": [11, 22],
        "receptor_original_filename": "receptor.pdb",
        "receptor_original_payload": RECEPTOR_PDB,
        "receptor_pdbqt": RECEPTOR_PDBQT_A,
        "receptor_qc": qc,
        "receptor_approval": approval,
        "receptor_identity": identity,
        "engine_version": "1.2.5",
        "settings": DockingRunSettings(),
        "vina_executor": _fake_executor,
    }
    first = run_docking_batch(**kwargs)
    second = run_docking_batch(**kwargs)
    first_ids = [attempt.task_id for attempt in first.attempts]

    assert first_ids == [attempt.task_id for attempt in second.attempts]
    assert len(set(first_ids)) == 2
    assert all(len(task_id) <= 64 for task_id in first_ids)


@pytest.mark.parametrize(
    ("executor", "message"),
    [
        (
            lambda manifest, ligand, receptor: _fake_vina_artifacts(
                manifest,
                log_score=-8.0,
                pose_score=-6.0,
            ),
            "pose 评分",
        ),
        (
            lambda manifest, ligand, receptor: _fake_vina_artifacts(
                manifest,
                log_score=-8.0,
                audit_seed=999,
            ),
            "seed",
        ),
    ],
)
def test_vina_score_and_seed_must_match_execution_artifacts(
    executor,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _run_batch(vina_executor=executor, fail_fast=True)


def test_preparation_failure_is_preserved_and_never_creates_a_score() -> None:
    ligand = _ligand(
        payload=b"not prepared",
        filename="lig-a.sdf",
        input_format="sdf",
    )
    result = _run_batch(ligands=[ligand], seeds=[1, 2], ligand_preparer=None)

    assert result.preparations[0].status == "failed"
    assert result.preparations[0].original_payload == b"not prepared"
    assert [attempt.status for attempt in result.attempts] == [
        "skipped",
        "skipped",
    ]
    assert all(attempt.docking_run is None for attempt in result.attempts)
    assert result.stability[0].assessment == "unavailable"


def test_meeko_probe_is_optional_and_uses_shell_false(tmp_path: Path) -> None:
    assert probe_meeko(None).available is False
    executable = tmp_path / "mk_prepare_ligand.py"
    executable.write_bytes(b"fake")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(arguments: list[str], **kwargs: object):
        calls.append((arguments, dict(kwargs)))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=b"Meeko 0.7.1\n",
            stderr=b"",
        )

    status = probe_meeko(executable, runner=runner)

    assert status.available is True
    assert calls[0][1]["shell"] is False
    assert calls[0][0][1:] == ["--version"]
