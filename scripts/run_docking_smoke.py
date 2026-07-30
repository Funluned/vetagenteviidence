"""Run the official AutoDock Vina 1IEP example through VetEvidence v0.5.

Download the public inputs referenced by the AutoDock Vina basic-docking
tutorial before running this script.  The example is a technical integrity
check, not veterinary evidence and not a biological validation of the score.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from vetevidence.docking_visualization import (
    build_visualization_package,
    verify_plip_executable,
    verify_plip_runtime_environment,
    verify_pymol_executable,
)
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
    run_docking_batch,
)
from vetevidence.vina_execution import discover_vina, execute_vina


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        required=True,
        help=(
            "Directory containing 1IEP.rcsb.pdb, 1iep_receptor.pdbqt and "
            "1iep_ligand.pdbqt."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--exhaustiveness", type=int, default=1)
    parser.add_argument("--pymol", type=Path)
    parser.add_argument("--plip", type=Path)
    parser.add_argument("--plip-babel-libdir", type=Path)
    parser.add_argument("--plip-babel-datadir", type=Path)
    parser.add_argument(
        "--allow-external-tools",
        action="store_true",
        help="Explicitly permit this invocation to probe and run PyMOL/PLIP.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    fixture_dir = args.fixture_dir.resolve(strict=True)
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if not output_root.is_dir():
        raise ValueError("output-dir 不是目录。")
    if args.allow_external_tools and args.plip is not None:
        if (
            args.plip_babel_libdir is None
            or args.plip_babel_datadir is None
        ):
            raise ValueError(
                "运行 PLIP 时必须同时提供 --plip-babel-libdir 和 "
                "--plip-babel-datadir。"
            )

    original_path = fixture_dir / "1IEP.rcsb.pdb"
    receptor_pdbqt_path = fixture_dir / "1iep_receptor.pdbqt"
    ligand_pdbqt_path = fixture_dir / "1iep_ligand.pdbqt"
    receptor_original = original_path.read_bytes()
    receptor_pdbqt = receptor_pdbqt_path.read_bytes()
    ligand_pdbqt = ligand_pdbqt_path.read_bytes()

    qc = inspect_receptor_structure(
        receptor_original,
        filename=original_path.name,
    )
    identity = ReceptorIdentity(
        pdb_id="1IEP",
        ncbi_taxid=10090,
        target_name="ABL1 kinase domain",
        organism="Mus musculus",
        source_url="https://files.rcsb.org/download/1IEP.pdb",
        revision="RCSB public download; access date recorded by caller",
        raw_structure_sha256=qc.sha256,
        uniprot_ids=("P00520",),
    )
    pocket = DockingPocket(
        center_x=15.190,
        center_y=53.903,
        center_z=16.917,
        size_x=20.0,
        size_y=20.0,
        size_z=20.0,
        basis_type="co_crystal",
        basis_residues=(
            ResidueIdentity(
                model_id="1",
                chain_id="A",
                residue_name="STI",
                residue_number="201",
            ),
        ),
        selection_basis=(
            "Co-crystallized STI in RCSB 1IEP and the official AutoDock "
            "Vina basic-docking tutorial box."
        ),
        source_structure_sha256=qc.sha256,
    )
    approval = approve_receptor_for_docking(
        qc,
        receptor_original,
        receptor_pdbqt,
        identity=identity,
        selected_model="1",
        selected_chains=("A",),
        alternate_location_policy="not_present",
        water_policy="remove_all",
        heterogen_policy="remove_all",
        metal_policy="not_present",
        preparation_audit=ReceptorPreparationAudit(
            method="user_provided",
            tool="Meeko output distributed with AutoDock Vina tutorial",
            version="AutoDock-Vina develop example snapshot",
            arguments=(),
            executable_sha256=None,
        ),
        pocket=pocket,
        reviewer="VetEvidence v0.5 technical smoke",
        user_confirmed=True,
    )
    ligand = LigandBatchItem(
        ligand_id="imatinib",
        compound_name="imatinib",
        identity=LigandIdentity(
            namespace="pubchem",
            structure_sha256=_sha256(ligand_pdbqt),
            pubchem_cid=5291,
            inchikey="KTUFNOKKBVMGRW-UHFFFAOYSA-N",
            source_url="https://pubchem.ncbi.nlm.nih.gov/compound/5291",
            source_revision="AutoDock Vina tutorial PDBQT snapshot",
        ),
        filename=ligand_pdbqt_path.name,
        input_format="pdbqt",
        original_payload=ligand_pdbqt,
    )
    vina = discover_vina()

    def vina_executor(manifest, ligand_payload, receptor_payload):
        return execute_vina(
            manifest,
            ligand_payload,
            receptor_payload,
            executable=vina,
        )

    batch = run_docking_batch(
        batch_id=f"smoke-{uuid4().hex[:12]}",
        ligands=(ligand,),
        seeds=(args.seed,),
        receptor_original_filename=original_path.name,
        receptor_original_payload=receptor_original,
        receptor_pdbqt=receptor_pdbqt,
        receptor_qc=qc,
        receptor_approval=approval,
        receptor_identity=identity,
        engine_version=vina.version,
        settings=DockingRunSettings(
            exhaustiveness=args.exhaustiveness,
            num_modes=3,
            energy_range=3.0,
        ),
        vina_executor=vina_executor,
        fail_fast=True,
    )
    attempt = next(item for item in batch.attempts if item.status == "succeeded")
    assert attempt.docking_run is not None

    if args.allow_external_tools:
        pymol = verify_pymol_executable(
            args.pymol,
            user_confirmed=True,
        )
        plip_runtime = (
            verify_plip_runtime_environment(
                babel_libdir=args.plip_babel_libdir,
                babel_datadir=args.plip_babel_datadir,
            )
            if args.plip is not None
            else None
        )
        plip = verify_plip_executable(
            args.plip,
            user_confirmed=True,
            runtime_environment=plip_runtime,
        )
    else:
        pymol = None
        plip = None
    package = build_visualization_package(
        batch=batch,
        ligand_id="imatinib",
        seed=args.seed,
        pose_mode=1,
        user_confirmed_external_tools=args.allow_external_tools,
        pymol_tool=pymol,
        plip_tool=plip,
    )

    run_directory = output_root / batch.batch_id
    run_directory.mkdir()
    (run_directory / "package.zip").write_bytes(package.zip_payload)
    for item in package.files:
        (run_directory / item.filename).write_bytes(item.payload)
    summary = {
        "scope": "technical_integrity_smoke_only",
        "interpretation": "computational_prediction",
        "batch_id": batch.batch_id,
        "vina_version": vina.version,
        "vina_executable_sha256": vina.sha256,
        "seed": args.seed,
        "best_vina_prediction_kcal_mol": (
            attempt.docking_run.best_affinity_kcal_mol
        ),
        "manifest_sha256": attempt.manifest.manifest_sha256
        if attempt.manifest
        else None,
        "zip_sha256": package.zip_sha256,
        "complex_pdb_sha256": package.complex_pdb_sha256,
        "pml_sha256": package.pml_sha256,
        "pymol_png_status": package.pymol_render.png.status,
        "pymol_pse_status": package.pymol_render.pse.status,
        "plip_xml_status": package.plip_analysis.xml.status,
        "plip_xml_reason": package.plip_analysis.xml.reason,
        "plip_command": (
            list(package.plip_analysis.command)
            if package.plip_analysis.command is not None
            else None
        ),
        "plip_stderr": package.plip_analysis.stderr,
        "plip_runtime_manifest_sha256": (
            plip.runtime_environment.manifest_sha256
            if plip is not None and plip.runtime_environment is not None
            else None
        ),
        "receptor_raw_sha256": qc.sha256,
        "receptor_prepared_sha256": approval.receptor_pdbqt_sha256,
        "receptor_heavy_atom_match_fraction": (
            approval.heavy_atom_match_fraction
        ),
    }
    (run_directory / "smoke_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**summary, "output_directory": str(run_directory)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
