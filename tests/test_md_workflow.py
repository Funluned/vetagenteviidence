from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from vetevidence.md_workflow import (
    MDAnalysisResult,
    MDChemistryConfirmation,
    MDEvidenceGrade,
    MDInputSource,
    MDPreset,
    MDReplicaAnalysis,
    MDReplicateSummary,
    MDTaskManifest,
    MDTimeSeries,
    build_md_manifest,
    canonical_md_manifest_sha256,
    protocol_for_preset,
)


RECEPTOR_PDB = b"""\
ATOM      1  N   ALA A   1      11.104  13.207   2.100  1.00 20.00           N
ATOM      2  CA  ALA A   1      12.200  12.300   2.400  1.00 20.00           C
END
"""

LIGAND_SDF = b"""\
Ligand
  VetEvidence

  1  0  0  0  0  0  0  0  0  0  1 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
M  END
$$$$
"""


def _source(
    name: str,
    accession: str,
    source_format: str,
) -> MDInputSource:
    return MDInputSource(
        source_name=name,
        accession=accession,
        version="2026-07-30",
        format=source_format,
    )


def _confirmation(**updates: object) -> MDChemistryConfirmation:
    values: dict[str, object] = {
        "reviewed_by": "researcher",
        "confirmed_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "receptor_chain_selection": ["A"],
        "receptor_protonation_assumption": "pH 7.4 reviewed",
        "ligand_formal_charge": 0,
        "ligand_protonation_state": "neutral at pH 7.4",
        "ligand_tautomer_state": "specified tautomer",
        "ligand_stereochemistry": "achiral",
        "chemical_identity_confirmed": True,
        "receptor_structure_reviewed": True,
        "formal_charge_confirmed": True,
        "protonation_confirmed": True,
        "tautomer_confirmed": True,
        "stereochemistry_confirmed": True,
        "all_stereocenters_defined": True,
        "metals_reviewed": True,
        "covalent_links_reviewed": True,
        "unknown_residues_reviewed": True,
    }
    values.update(updates)
    return MDChemistryConfirmation(**values)


def _manifest(
    *,
    preset: MDPreset = MDPreset.TECHNICAL_SMOKE,
    confirmation: MDChemistryConfirmation | None = None,
    receptor_payload: bytes = RECEPTOR_PDB,
    receptor_format: str = "pdb",
    ligand_payload: bytes | str = LIGAND_SDF,
    ligand_format: str = "sdf",
    protocol_approved_by_user: bool = False,
) -> MDTaskManifest:
    return build_md_manifest(
        task_id="md-001",
        receptor_payload=receptor_payload,
        receptor_source=_source(
            "receptor.pdb",
            "PDB:1ABC",
            receptor_format,
        ),
        ligand_payload=ligand_payload,
        ligand_source=_source(
            "ligand.sdf",
            "PubChem:1",
            ligand_format,
        ),
        chemistry_confirmation=confirmation or _confirmation(),
        preset=preset,
        protocol_approved_by_user=protocol_approved_by_user,
    )


def test_manifest_binds_original_inputs_protocol_and_prediction_grade() -> None:
    manifest = _manifest()

    assert manifest.receptor_source.sha256 == hashlib.sha256(
        RECEPTOR_PDB
    ).hexdigest()
    assert manifest.ligand_source.sha256 == hashlib.sha256(
        LIGAND_SDF
    ).hexdigest()
    assert manifest.receptor_source.size_bytes == len(RECEPTOR_PDB)
    assert manifest.protocol.preset is MDPreset.TECHNICAL_SMOKE
    assert manifest.protocol.replica_count == 1
    assert manifest.protocol.integration_steps == 30
    assert manifest.protocol.chunk_steps == 5
    assert manifest.protocol.scientific_interpretation_allowed is False
    assert manifest.manifest_sha256 == canonical_md_manifest_sha256(
        manifest
    )
    assert manifest.evidence_grade is MDEvidenceGrade.COMPUTATIONAL_PREDICTION
    serialized = manifest.model_dump(mode="json")
    assert "binding_free_energy" not in str(serialized)


def test_md_cannot_start_from_receptor_or_ligand_pdbqt_alone() -> None:
    with pytest.raises(ValueError, match="PDBQT.*原始受体"):
        _manifest(
            receptor_payload=b"ATOM receptor\n",
            receptor_format="pdbqt",
        )

    with pytest.raises(ValueError, match="PDBQT.*配体 SDF"):
        _manifest(
            ligand_payload=b"ROOT\nENDROOT\nTORSDOF 0\n",
            ligand_format="pdbqt",
        )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("chemical_identity_confirmed", "化学身份"),
        ("formal_charge_confirmed", "形式电荷"),
        ("protonation_confirmed", "质子化"),
        ("tautomer_confirmed", "互变异构"),
        ("stereochemistry_confirmed", "立体化学"),
        ("all_stereocenters_defined", "全部立体中心"),
    ],
)
def test_chemistry_confirmation_gate_blocks_ambiguity(
    field: str,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        _manifest(confirmation=_confirmation(**{field: False}))


def test_obvious_metal_covalent_and_unknown_residue_risks_are_blocked() -> None:
    metal_pdb = RECEPTOR_PDB.replace(
        b"END\n",
        (
            b"HETATM    3 ZN   ZN  A 900      13.000  11.000   2.000"
            b"  1.00 20.00          ZN\nEND\n"
        ),
    )
    with pytest.raises(ValueError, match="金属"):
        _manifest(receptor_payload=metal_pdb)

    linked_pdb = RECEPTOR_PDB.replace(
        b"END\n",
        b"LINK         N   ALA A   1                 C1  LIG A 900\nEND\n",
    )
    with pytest.raises(ValueError, match="共价"):
        _manifest(receptor_payload=linked_pdb)

    unknown_pdb = RECEPTOR_PDB.replace(b"ALA", b"MSE", 1)
    with pytest.raises(ValueError, match="未知或非标准残基"):
        _manifest(receptor_payload=unknown_pdb)


def test_real_smoke_restricts_ligand_to_one_v2000_sdf_record() -> None:
    for source_format, payload in (
        ("smiles", "C[C@H](O)C(=O)O"),
        ("mol2", "@<TRIPOS>MOLECULE\nligand"),
    ):
        with pytest.raises(ValueError, match="单记录 V2000 SDF"):
            _manifest(
                ligand_payload=payload,
                ligand_format=source_format,
            )

    with pytest.raises(ValueError, match="恰好一个记录"):
        _manifest(
            ligand_payload=LIGAND_SDF + LIGAND_SDF,
            ligand_format="sdf",
        )


def test_only_truthful_technical_smoke_preset_is_executable() -> None:
    smoke = protocol_for_preset(
        MDPreset.TECHNICAL_SMOKE,
        seed_namespace="same",
    )
    assert smoke.replica_count == 1
    assert smoke.integration_steps == 30
    assert smoke.seeds == protocol_for_preset(
        MDPreset.TECHNICAL_SMOKE,
        seed_namespace="same",
    ).seeds
    for unsupported in (
        MDPreset.EXPLORATORY_REPLICATES,
        MDPreset.RESEARCH_REVIEW,
    ):
        with pytest.raises(ValueError, match="尚未实现"):
            protocol_for_preset(unsupported)
        with pytest.raises(ValueError, match="尚未实现"):
            _manifest(preset=unsupported)


def test_analysis_schema_reserves_metrics_but_forbids_binding_free_energy() -> None:
    result = MDAnalysisResult(
        replicate_summary=MDReplicateSummary(
            total_replicas=1,
            successful_replicas=0,
        )
    )
    assert result.free_energy_computed is False
    assert result.replicas == []
    assert result.produced_metrics == []
    assert "protein_backbone_rmsd_nm" in (
        result.reserved_metrics_not_produced
    )

    with pytest.raises(ValidationError, match="binding_free_energy"):
        MDAnalysisResult(
            replicate_summary=MDReplicateSummary(
                total_replicas=1,
                successful_replicas=0,
            ),
            binding_free_energy_kcal_mol=-7.2,
        )

    with pytest.raises(ValidationError, match="尚未实现"):
        MDAnalysisResult(
            replicas=[
                MDReplicaAnalysis(
                    replica_index=1,
                    seed=1,
                    qc_passed=False,
                    protein_backbone_rmsd_nm=MDTimeSeries(
                        times_ps=[0.01],
                        values=[0.1],
                        unit="nm",
                    ),
                )
            ],
            replicate_summary=MDReplicateSummary(
                total_replicas=1,
                successful_replicas=0,
            ),
        )

    with pytest.raises(
        ValidationError,
        match="reserved_metrics_not_produced",
    ):
        MDAnalysisResult(
            replicate_summary=MDReplicateSummary(
                total_replicas=1,
                successful_replicas=0,
            ),
            reserved_metrics_not_produced=[],
        )


def test_receptor_must_be_single_model_selected_chain_pdb() -> None:
    with pytest.raises(ValueError, match="裁剪为所选链子集"):
        _manifest(
            receptor_payload=RECEPTOR_PDB.replace(
                b"END\n",
                (
                    b"ATOM      3  N   ALA B   1      13.000  12.000"
                    b"   2.000  1.00 20.00           N\nEND\n"
                ),
            )
        )

    with pytest.raises(ValueError, match="mmCIF"):
        _manifest(
            receptor_payload=b"data_test\n_atom_site.type_symbol N\n",
            receptor_format="mmcif",
        )


def test_tampered_manifest_hash_is_rejected_on_restore() -> None:
    payload = _manifest().model_dump(mode="json")
    payload["task_id"] = "md-tampered"

    with pytest.raises(ValidationError, match="canonical SHA-256"):
        MDTaskManifest.model_validate(payload)
