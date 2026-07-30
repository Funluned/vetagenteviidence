from __future__ import annotations

import hashlib

import pytest

from vetevidence.mechanism_prediction import (
    MechanismPredictionBundle,
    PredictionEvidenceGrade,
    SourceProvenance,
    VinaParameters,
    analyze_network_pharmacology_csv,
    build_vina_manifest,
    canonical_manifest_sha256,
    parse_compound_target_csv,
    parse_vina_output,
    require_docking_scope,
    require_network_scope,
    validate_pdbqt_bytes,
)


COMPOUND_TARGET_CSV = """compound,compound_accession,organism,target,target_accession
Compound A,CID:1,Target bacterium,Target One,UniProt:P11111
Compound B,CID:2,Target bacterium,Target One,UniProt:P11111
Compound A,CID:1,Target bacterium,Target Two,UniProt:P22222
"""
TARGET_PATHWAY_CSV = """organism,target,target_accession,pathway,pathway_accession
Target bacterium,Target One,UniProt:P11111,Pathway A,KEGG:map00010
Target bacterium,Target One,UniProt:P11111,Pathway B,KEGG:map00020
Target bacterium,Target Two,UniProt:P22222,Pathway C,KEGG:map00030
Target bacterium,Target Three,UniProt:P33333,Pathway D,KEGG:map00040
"""


def source(
    name: str,
    accession: str,
    *,
    sha256: str | None = None,
) -> SourceProvenance:
    return SourceProvenance(
        source_name=name,
        accession=accession,
        version="2026-07-30",
        sha256=sha256,
    )


def vina_manifest(*, num_modes: int = 3):
    return build_vina_manifest(
        task_id="dock-001",
        compound_name="Compound A",
        ligand_accession="PubChem:1",
        receptor_name="Target One",
        receptor_accession="PDB:1ABC",
        receptor_organism="Target bacterium",
        ligand_source=source(
            "PubChem export",
            "PubChem:1",
            sha256="1" * 64,
        ),
        receptor_source=source(
            "RCSB PDB",
            "PDB:1ABC",
            sha256="2" * 64,
        ),
        parameters=VinaParameters(
            center_x=1.0,
            center_y=2.0,
            center_z=3.0,
            size_x=20.0,
            size_y=20.0,
            size_z=20.0,
            exhaustiveness=16,
            num_modes=num_modes,
            seed=42,
        ),
        engine_version="1.2.5",
    )


def tagged_vina_output(manifest, table: str, *, version: str = "1.2.5") -> str:
    return (
        f"VetEvidence-Manifest-SHA256: {manifest.manifest_sha256}\n"
        f"AutoDock Vina v{version}\n"
        f"{table}"
    )


def test_network_csv_builds_traceable_intersection_and_ranking() -> None:
    result = analyze_network_pharmacology_csv(
        COMPOUND_TARGET_CSV,
        TARGET_PATHWAY_CSV,
        compound_target_source=source("compound-target.csv", "dataset:ct-1"),
        target_pathway_source=source("target-pathway.csv", "dataset:tp-1"),
    )

    assert result.evidence_grade is PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    assert result.parameters.ranking_method == "compound_degree_x_pathway_degree"
    assert result.compounds == ["Compound A", "Compound B"]
    assert result.organisms == ["Target bacterium"]
    assert result.summary.intersection_target_count == 2
    assert result.summary.input_organism_count == 1
    assert result.summary.intersection_compound_count == 2
    assert result.summary.intersection_pathway_count == 3
    assert result.summary.compound_target_edge_count == 3
    assert result.summary.target_pathway_edge_count == 3
    assert [
        (item.rank, item.target_accession, item.network_score)
        for item in result.ranked_targets
    ] == [
        (1, "UniProt:P11111", 4),
        (2, "UniProt:P22222", 1),
    ]
    assert result.sources[0].sha256 == hashlib.sha256(
        COMPOUND_TARGET_CSV.encode()
    ).hexdigest()
    assert result.ranked_targets[0].source_rows[0].row_number == 2
    assert [
        (link.compound, link.compound_accession)
        for link in result.ranked_targets[0].compounds
    ] == [("Compound A", "CID:1"), ("Compound B", "CID:2")]
    assert [
        (link.pathway, link.pathway_accession)
        for link in result.ranked_targets[0].pathways
    ] == [
        ("Pathway A", "KEGG:map00010"),
        ("Pathway B", "KEGG:map00020"),
    ]


def test_network_rejects_one_pathway_accession_with_multiple_names() -> None:
    with pytest.raises(ValueError, match="通路 accession.*多个名称"):
        analyze_network_pharmacology_csv(
            COMPOUND_TARGET_CSV,
            (
                "organism,target,target_accession,pathway,pathway_accession\n"
                "Target bacterium,Target One,UniProt:P11111,Pathway A,"
                "KEGG:map00010\n"
                "Target bacterium,Target One,UniProt:P11111,Wrong name,"
                "KEGG:map00010\n"
            ),
            compound_target_source=source(
                "compound-target.csv",
                "dataset:ct-1",
            ),
            target_pathway_source=source(
                "target-pathway.csv",
                "dataset:tp-1",
            ),
        )


def test_network_csv_missing_columns_are_reported_by_name() -> None:
    with pytest.raises(ValueError, match="target_accession"):
        parse_compound_target_csv(
            "compound,compound_accession,organism,target\n"
            "A,CID:1,Target bacterium,T1\n",
            source=source("bad.csv", "dataset:bad"),
        )


def test_network_does_not_join_same_accession_across_organisms() -> None:
    result = analyze_network_pharmacology_csv(
        (
            "compound,compound_accession,organism,target,target_accession\n"
            "A,CID:1,Organism A,T1,UniProt:P11111\n"
        ),
        (
            "organism,target,target_accession,pathway,pathway_accession\n"
            "Organism B,T1,UniProt:P11111,P1,KEGG:map00010\n"
        ),
        compound_target_source=source("ct.csv", "dataset:ct"),
        target_pathway_source=source("tp.csv", "dataset:tp"),
    )

    assert result.summary.intersection_target_count == 0
    assert result.ranked_targets == []


def test_network_scope_requires_both_compounds_and_current_organism() -> None:
    result = analyze_network_pharmacology_csv(
        COMPOUND_TARGET_CSV,
        TARGET_PATHWAY_CSV,
        compound_target_source=source("ct.csv", "dataset:ct"),
        target_pathway_source=source("tp.csv", "dataset:tp"),
    )

    require_network_scope(
        result,
        expected_compounds=["Compound A", "Compound B"],
        expected_organism="Target bacterium",
    )
    with pytest.raises(ValueError, match="缺少化合物"):
        require_network_scope(
            result,
            expected_compounds=["Compound A", "Compound C"],
            expected_organism="Target bacterium",
        )
    with pytest.raises(ValueError, match="缺少研究对象"):
        require_network_scope(
            result,
            expected_compounds=["Compound A", "Compound B"],
            expected_organism="Different organism",
        )


def test_network_scope_rejects_mixed_organisms_and_extra_compounds() -> None:
    result = analyze_network_pharmacology_csv(
        (
            "compound,compound_accession,organism,target,target_accession\n"
            "Compound A,CID:1,Target bacterium,T1,UniProt:P11111\n"
            "Compound B,CID:2,Target bacterium,T1,UniProt:P11111\n"
            "Contaminant,CID:9,Human,H1,UniProt:H11111\n"
        ),
        (
            "organism,target,target_accession,pathway,pathway_accession\n"
            "Target bacterium,T1,UniProt:P11111,P1,KEGG:map00010\n"
            "Human,H1,UniProt:H11111,HP1,KEGG:hsa00010\n"
        ),
        compound_target_source=source("ct.csv", "dataset:ct"),
        target_pathway_source=source("tp.csv", "dataset:tp"),
    )

    with pytest.raises(
        ValueError,
        match="混入额外化合物.*混入其他研究对象",
    ):
        require_network_scope(
            result,
            expected_compounds=["Compound A", "Compound B"],
            expected_organism="Target bacterium",
        )


def test_network_scope_requires_both_compounds_in_intersection_network() -> None:
    result = analyze_network_pharmacology_csv(
        (
            "compound,compound_accession,organism,target,target_accession\n"
            "Compound A,CID:1,Target bacterium,T1,UniProt:P11111\n"
            "Compound B,CID:2,Target bacterium,T2,UniProt:P22222\n"
        ),
        (
            "organism,target,target_accession,pathway,pathway_accession\n"
            "Target bacterium,T1,UniProt:P11111,P1,KEGG:map00010\n"
        ),
        compound_target_source=source("ct.csv", "dataset:ct"),
        target_pathway_source=source("tp.csv", "dataset:tp"),
    )

    with pytest.raises(ValueError, match="交集网络未包含当前干预.*Compound B"):
        require_network_scope(
            result,
            expected_compounds=["Compound A", "Compound B"],
            expected_organism="Target bacterium",
        )


def test_network_csv_rejects_incorrect_provenance_digest() -> None:
    traced = source("compound-target.csv", "dataset:ct-1").model_copy(
        update={"sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="SHA-256"):
        parse_compound_target_csv(COMPOUND_TARGET_CSV, source=traced)


def test_vina_manifest_is_traceable_but_contains_no_score() -> None:
    manifest = vina_manifest()

    assert manifest.engine == "AutoDock Vina"
    assert manifest.engine_version == "1.2.5"
    assert manifest.receptor_accession == "PDB:1ABC"
    assert manifest.parameters.seed == 42
    assert manifest.manifest_sha256 == canonical_manifest_sha256(manifest)
    assert "affinity" not in manifest.model_dump()
    assert (
        manifest.evidence_grade
        is PredictionEvidenceGrade.COMPUTATIONAL_PREDICTION
    )
    require_docking_scope(
        manifest,
        expected_compounds=["Compound A", "Compound B"],
        expected_organism="Target bacterium",
    )


def test_vina_manifest_scope_rejects_wrong_ligand_or_organism() -> None:
    manifest = vina_manifest().model_copy(
        update={
            "compound_name": "Wrong compound",
            "receptor_organism": "Wrong organism",
        }
    )

    with pytest.raises(ValueError, match="配体.*受体研究对象"):
        require_docking_scope(
            manifest,
            expected_compounds=["Compound A", "Compound B"],
            expected_organism="Target bacterium",
        )


def test_legacy_manifest_without_hash_is_safely_upgraded() -> None:
    payload = vina_manifest().model_dump(mode="json")
    payload.pop("manifest_sha256")

    restored = type(vina_manifest()).model_validate(payload)

    assert restored.manifest_sha256 == canonical_manifest_sha256(restored)


def test_pdbqt_validation_requires_role_specific_records() -> None:
    ligand = b"""ROOT
ATOM      1  C1  LIG A   1       0.0 0.0 0.0  0.00  0.00    +0.0 C
ENDROOT
TORSDOF 0
"""
    receptor = b"""ATOM      1  CA  ALA A   1       0.0 0.0 0.0  0.00  0.00    +0.0 C
"""
    multiple_ligands = ligand + ligand

    assert validate_pdbqt_bytes(ligand, role="ligand") == hashlib.sha256(
        ligand
    ).hexdigest()
    assert validate_pdbqt_bytes(receptor, role="receptor") == hashlib.sha256(
        receptor
    ).hexdigest()
    with pytest.raises(ValueError, match="ROOT.*TORSDOF"):
        validate_pdbqt_bytes(receptor, role="ligand")
    with pytest.raises(ValueError, match="只能包含一个完整配体块"):
        validate_pdbqt_bytes(multiple_ligands, role="ligand")
    assert validate_pdbqt_bytes(
        multiple_ligands,
        role="ligand",
        require_single_ligand=False,
    ) == hashlib.sha256(multiple_ligands).hexdigest()
    with pytest.raises(ValueError, match="为空"):
        validate_pdbqt_bytes(b"", role="receptor")


def test_vina_output_parses_only_real_mode_rows_and_is_serializable() -> None:
    manifest = vina_manifest()
    output = tagged_vina_output(
        manifest,
        """mode |   affinity | dist from best mode
     | (kcal/mol) | rmsd l.b.| rmsd u.b.
-----+------------+----------+----------
   1       -8.1          0.000      0.000
   2       -7.4          1.250      2.100
""",
    )
    run = parse_vina_output(
        output,
        manifest=manifest,
        output_source=source("vina.log", "run:dock-001"),
    )
    bundle = MechanismPredictionBundle(docking_runs=[run])
    serialized = bundle.model_dump(mode="json")

    assert run.best_affinity_kcal_mol == -8.1
    assert [pose.mode for pose in run.poses] == [1, 2]
    assert run.output_source.sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert serialized["docking_runs"][0]["poses"][0]["affinity_kcal_mol"] == -8.1
    assert serialized["evidence_grade"] == "computational_prediction"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "AutoDock Vina v1.2.5\nNo docking output was produced.",
        "mode | affinity | dist from best mode\nNo numeric rows",
    ],
)
def test_vina_output_without_real_scores_is_rejected(output: str) -> None:
    manifest = vina_manifest()
    if output:
        output = (
            f"VetEvidence-Manifest-SHA256: {manifest.manifest_sha256}\n"
            + output
        )
    with pytest.raises(ValueError, match="不能"):
        parse_vina_output(
            output,
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )


def test_vina_output_version_must_match_manifest() -> None:
    manifest = vina_manifest()
    output = tagged_vina_output(
        manifest,
        """mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
""",
        version="1.2.3",
    )

    with pytest.raises(ValueError, match="版本与任务清单不一致"):
        parse_vina_output(
            output,
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )


def test_vina_output_manifest_hash_must_match_selected_task() -> None:
    manifest = vina_manifest()
    output = tagged_vina_output(
        manifest,
        """mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
""",
    ).replace(manifest.manifest_sha256 or "", "f" * 64, 1)

    with pytest.raises(ValueError, match="任务清单 SHA-256 不匹配"):
        parse_vina_output(
            output,
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )


@pytest.mark.parametrize(
    ("table", "error"),
    [
        (
            """mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
3 -7.1 1.0 2.0
""",
            "从 1 开始连续",
        ),
        (
            """mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.1 0.0
""",
            "mode 1 的 RMSD",
        ),
    ],
)
def test_vina_output_enforces_mode_table_invariants(
    table: str,
    error: str,
) -> None:
    manifest = vina_manifest()
    with pytest.raises(ValueError, match=error):
        parse_vina_output(
            tagged_vina_output(manifest, table),
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )


def test_vina_output_rejects_more_modes_than_manifest() -> None:
    manifest = vina_manifest(num_modes=1)
    table = """mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
2 -7.1 1.0 2.0
"""

    with pytest.raises(ValueError, match="超过任务清单 num_modes"):
        parse_vina_output(
            tagged_vina_output(manifest, table),
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )


def test_vina_output_does_not_parse_numeric_rows_outside_table() -> None:
    manifest = vina_manifest()
    output = tagged_vina_output(
        manifest,
        """1 -99.0 0.0 0.0
mode | affinity | dist from best mode
-----+----------+--------------------
No modes were produced
""",
    )

    with pytest.raises(ValueError, match="没有可解析的模式行"):
        parse_vina_output(
            output,
            manifest=manifest,
            output_source=source("vina.log", "run:dock-001"),
        )
