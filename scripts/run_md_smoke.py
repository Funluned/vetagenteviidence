"""Run the auditable v0.6 OpenMM technical-integrity smoke.

This command deliberately uses a fully disclosed synthetic two-atom system.
It checks the real VetEvidence MD storage and execution chain; it is not a
biomolecular simulation and cannot support biological or free-energy claims.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from vetevidence.md_workflow import (
    MDChemistryConfirmation,
    MDHardwareRequest,
    MDInputSource,
    MDPreset,
    build_md_manifest,
)
from vetevidence.md_worker import (
    MDJobState,
    MDJobStore,
    MDSystemSummary,
    preflight_openmm,
    process_queued_job,
)


FIXTURE_ID = "vetevidence-public-two-atom-n-c-v1"
TASK_ID = "md-public-two-atom-smoke"
FIXED_REVIEW_TIME = datetime(2026, 7, 30, tzinfo=timezone.utc)

RECEPTOR_PDB = b"""\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
END
"""

LIGAND_SDF = b"""\
Synthetic carbon technical-smoke input
  VetEvidence

  1  0  0  0  0  0  0  0  0  0  1 V2000
    0.1000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
M  END
$$$$
"""

TOPOLOGY_PDB = b"""\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
HETATM    2  C1  LIG B   1       1.000   0.000   0.000  1.00  0.00           C
END
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real OpenMM 30-step technical-integrity smoke on the "
            "fully disclosed synthetic N+C fixture."
        )
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="New directory for the immutable MD job and artifacts.",
    )
    parser.add_argument(
        "--platform",
        choices=("CPU", "CUDA", "OpenCL"),
        default="CPU",
        help="OpenMM platform to require (default: CPU).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260730,
        help="OpenMM random seed in 1..2147483647.",
    )
    return parser


def _hardware_request(platform: str) -> MDHardwareRequest:
    if platform == "CUDA":
        return MDHardwareRequest(
            platform="CUDA",
            device_indices=[0],
            precision="mixed",
            gpu_required=True,
        )
    return MDHardwareRequest(
        platform=platform,
        precision="mixed",
        gpu_required=False,
    )


def _chemistry_confirmation() -> MDChemistryConfirmation:
    return MDChemistryConfirmation(
        reviewed_by="VetEvidence fully disclosed technical fixture",
        confirmed_at=FIXED_REVIEW_TIME,
        receptor_chain_selection=["A"],
        receptor_protonation_assumption=(
            "not applicable to the synthetic single nitrogen fixture"
        ),
        ligand_formal_charge=0,
        ligand_protonation_state=(
            "not applicable to the synthetic single carbon fixture"
        ),
        ligand_tautomer_state="not applicable to a single carbon atom",
        ligand_stereochemistry="achiral single atom",
        chemical_identity_confirmed=True,
        receptor_structure_reviewed=True,
        formal_charge_confirmed=True,
        protonation_confirmed=True,
        tautomer_confirmed=True,
        stereochemistry_confirmed=True,
        all_stereocenters_defined=True,
        metals_reviewed=True,
        covalent_links_reviewed=True,
        unknown_residues_reviewed=True,
    )


def _build_openmm_system() -> tuple[str, bytes, bytes]:
    import openmm
    from openmm import unit

    system = openmm.System()
    system.addParticle(14.0067 * unit.dalton)
    system.addParticle(12.011 * unit.dalton)
    bond = openmm.HarmonicBondForce()
    bond.addBond(
        0,
        1,
        0.1 * unit.nanometer,
        1000.0
        * unit.kilojoule_per_mole
        / unit.nanometer**2,
    )
    system.addForce(bond)

    potential_definition = {
        "fixture_id": FIXTURE_ID,
        "definition_status": "fully_disclosed_synthetic_technical_fixture",
        "scientific_force_field": False,
        "biomolecular_parameterization": False,
        "periodic_boundary_conditions": False,
        "particles": [
            {
                "topology_index": 0,
                "source_role": "receptor",
                "element": "N",
                "mass_dalton": 14.0067,
            },
            {
                "topology_index": 1,
                "source_role": "ligand",
                "element": "C",
                "mass_dalton": 12.011,
            },
        ],
        "forces": [
            {
                "type": "HarmonicBondForce",
                "particle_indices": [0, 1],
                "equilibrium_length_nm": 0.1,
                "spring_constant_kj_mol_nm2": 1000.0,
            }
        ],
        "limitations": [
            "This is not a protein-ligand or other biomolecular system.",
            "The declared potential exists only to test numerical execution.",
            "No biological, stability, efficacy, or free-energy inference is allowed.",
        ],
    }
    mapping_evidence = {
        "fixture_id": FIXTURE_ID,
        "mapping_method": "explicit source atom order",
        "topology_atoms": [
            {
                "topology_index": 0,
                "source_role": "receptor",
                "source_atom_index": 0,
                "element": "N",
            },
            {
                "topology_index": 1,
                "source_role": "ligand",
                "source_atom_index": 0,
                "element": "C",
            },
        ],
        "technical_integrity_smoke_only": True,
    }
    return (
        openmm.XmlSerializer.serialize(system),
        json.dumps(
            potential_definition,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
        json.dumps(
            mapping_evidence,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
    )


def _series_summary(series: Any) -> dict[str, Any]:
    values = list(series.values)
    return {
        "unit": series.unit,
        "sample_count": len(values),
        "times_ps": list(series.times_ps),
        "values": values,
        "minimum": min(values),
        "maximum": max(values),
        "final": values[-1],
    }


def run_smoke(
    *,
    output_directory: Path,
    platform: str,
    seed: int,
) -> dict[str, Any]:
    """Execute the public two-atom smoke through the formal MD job chain."""

    if seed < 1 or seed > 2_147_483_647:
        raise ValueError("seed must be in 1..2147483647")
    output_directory = output_directory.expanduser().resolve()
    if output_directory.exists():
        raise FileExistsError(
            f"output directory already exists; refusing overwrite: "
            f"{output_directory}"
        )

    active_preflight = preflight_openmm()
    if not active_preflight.execution_available:
        raise RuntimeError(
            active_preflight.reason or "OpenMM execution is unavailable"
        )
    if platform not in active_preflight.hardware.openmm_platforms:
        raise RuntimeError(
            f"requested OpenMM platform {platform} is unavailable; "
            f"available={active_preflight.hardware.openmm_platforms}"
        )

    system_xml, potential_definition, mapping_evidence = (
        _build_openmm_system()
    )
    manifest = build_md_manifest(
        task_id=TASK_ID,
        receptor_payload=RECEPTOR_PDB,
        receptor_source=MDInputSource(
            source_name="fully-disclosed-synthetic-receptor-N.pdb",
            accession=f"{FIXTURE_ID}:receptor-N",
            version="1",
            format="pdb",
        ),
        ligand_payload=LIGAND_SDF,
        ligand_source=MDInputSource(
            source_name="fully-disclosed-synthetic-ligand-C.sdf",
            accession=f"{FIXTURE_ID}:ligand-C",
            version="1",
            format="sdf",
        ),
        chemistry_confirmation=_chemistry_confirmation(),
        preset=MDPreset.TECHNICAL_SMOKE,
        hardware_request=_hardware_request(platform),
        seeds=[seed],
        protocol_approved_by_user=True,
    )

    # Claim the destination before creating any job state.  This makes the
    # no-overwrite guarantee race-safe while retaining partial state if a
    # later execution failure must be audited.
    output_directory.mkdir(parents=True, exist_ok=False)
    store = MDJobStore(output_directory)
    enqueued = store.enqueue(
        manifest,
        receptor_payload=RECEPTOR_PDB,
        ligand_payload=LIGAND_SDF,
    )
    prepared = store.save_prepared_system(
        enqueued.job_id,
        system_xml=system_xml,
        topology_pdb=TOPOLOGY_PDB,
        parameterization_backend=FIXTURE_ID,
        parameterization_version="1",
        forcefield_files={
            "public-two-atom-smoke-potential.json": potential_definition
        },
        preparation_command=[
            "run_md_smoke.py",
            "build-system",
            FIXTURE_ID,
        ],
        prepared_by="VetEvidence technical-smoke builder",
        declared_system_summary=MDSystemSummary(
            particle_count=2,
            force_count=1,
            constraint_count=0,
            force_types=["HarmonicBondForce"],
            uses_periodic_boundary_conditions=False,
        ),
        receptor_topology_atom_indices=[0],
        ligand_topology_atom_indices=[1],
        mapping_method="explicit source atom order",
        mapping_evidence=mapping_evidence,
        notes=[
            "Fully disclosed synthetic N+C numerical fixture.",
            "Not a biomolecular force field or scientific MD preparation.",
        ],
    )
    completed = process_queued_job(
        store,
        prepared.job_id,
        dry_run=False,
        preflight=active_preflight,
        raise_on_error=True,
    )
    if completed.state is not MDJobState.SUCCEEDED:
        raise RuntimeError(
            f"MD smoke ended in unexpected state: {completed.state}"
        )
    result = completed.run_result
    prepared_reference = completed.prepared_system
    if result is None or prepared_reference is None:
        raise RuntimeError("successful job is missing result or prepared input")
    replica = result.analysis.replicas[0]
    if (
        replica.temperature_kelvin is None
        or replica.potential_energy_kj_mol is None
    ):
        raise RuntimeError("successful job is missing measured state series")

    artifacts = {
        item.role: {
            "filename": item.filename,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in result.artifacts
    }
    audit = result.execution_audit
    return {
        "status": "succeeded",
        "scope": "technical_integrity_smoke_only",
        "evidence_grade": "computational_prediction",
        "scientific_interpretation_allowed": False,
        "free_energy_computed": result.analysis.free_energy_computed,
        "output_directory": str(output_directory),
        "job_id": completed.job_id,
        "fixture": {
            "fixture_id": FIXTURE_ID,
            "fully_disclosed": True,
            "synthetic_non_research_system": True,
            "elements": ["N", "C"],
            "particle_count": 2,
            "force_types": ["HarmonicBondForce"],
            "periodic_boundary_conditions": False,
        },
        "protocol": {
            "preset": result.manifest.protocol.preset.value,
            "integration_steps": result.manifest.protocol.integration_steps,
            "timestep_fs": result.manifest.protocol.timestep_fs,
            "seed": seed,
            "requested_platform": platform,
            "gpu_required": result.manifest.hardware_request.gpu_required,
            "requested_device_indices": (
                result.manifest.hardware_request.device_indices
            ),
            "requested_precision": result.manifest.hardware_request.precision,
        },
        "execution": {
            "actual_platform": audit.platform_name,
            "selected_device": audit.selected_device,
            "driver_version": audit.driver_version,
            "backend": result.manifest.backend,
            "backend_version": audit.backend_version,
            "precision": audit.precision,
            "platform_properties": audit.platform_properties,
            "hardware_fingerprint_sha256": audit.hardware_fingerprint,
        },
        "observables": {
            "temperature_kelvin": _series_summary(
                replica.temperature_kelvin
            ),
            "potential_energy_kj_mol": _series_summary(
                replica.potential_energy_kj_mol
            ),
        },
        "validation": {
            "status": result.validation_status.value,
            "qc_passed": replica.qc_passed,
            "produced_metrics": result.analysis.produced_metrics,
            "reserved_metrics_not_produced": (
                result.analysis.reserved_metrics_not_produced
            ),
        },
        "hashes": {
            "manifest_sha256": result.manifest.manifest_sha256,
            "result_manifest_sha256": result.result_manifest_sha256,
            "source_sha256": {
                "receptor": result.manifest.receptor_source.sha256,
                "ligand": result.manifest.ligand_source.sha256,
            },
            "prepared_inputs": {
                "system_xml": prepared_reference.system_xml_sha256,
                "topology_pdb": prepared_reference.topology_pdb_sha256,
                "mapping_evidence": (
                    prepared_reference.receptor_mapping.mapping_evidence_sha256
                ),
                "forcefield_files": (
                    prepared_reference.forcefield_file_sha256
                ),
            },
            "artifacts": artifacts,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = run_smoke(
            output_directory=arguments.output_dir,
            platform=arguments.platform,
            seed=arguments.seed,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "scope": "technical_integrity_smoke_only",
                    "evidence_grade": "computational_prediction",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
