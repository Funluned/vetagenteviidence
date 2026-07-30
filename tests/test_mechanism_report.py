from __future__ import annotations

from vetevidence.experiment_analysis import analyze_fici_csv
from vetevidence.mechanism_prediction import (
    MechanismPredictionBundle,
    SourceProvenance,
    VinaParameters,
    analyze_network_pharmacology_csv,
    build_vina_manifest,
    parse_vina_output,
)
from vetevidence.workbench import ResearchQuestion, TaskStatus, build_task_event
from vetevidence.workbench_pipeline import (
    build_decision_report,
    decision_report_to_markdown,
)


def _source(
    name: str,
    accession: str,
    *,
    digest: str | None = None,
) -> SourceProvenance:
    return SourceProvenance(
        source_name=name,
        accession=accession,
        version="2026-07-30",
        sha256=digest,
    )


def test_report_keeps_predictions_separate_and_traceable() -> None:
    question = ResearchQuestion(
        id="rq-mechanism",
        text="Compound A 与 Compound B 对 Target bacterium 是否协同？",
        population="Target bacterium",
        intervention="Compound A",
        comparator="Compound B",
        outcomes=["FICI"],
    )
    analysis = analyze_fici_csv(
        """\
drug_a,drug_b,population_or_strain,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
Compound A,Compound B,Target bacterium,8,2,4,1
"""
    )
    network = analyze_network_pharmacology_csv(
        (
            "compound,compound_accession,organism,target,target_accession\n"
            "Compound A,CID:1,Target bacterium,T1,UniProt:P11111\n"
            "Compound B,CID:2,Target bacterium,T1,UniProt:P11111\n"
        ),
        (
            "organism,target,target_accession,pathway,pathway_accession\n"
            "Target bacterium,T1,UniProt:P11111,P1,KEGG:map00010\n"
        ),
        compound_target_source=_source("ct.csv", "dataset:ct"),
        target_pathway_source=_source("tp.csv", "dataset:tp"),
    )
    manifest = build_vina_manifest(
        task_id="dock-1",
        compound_name="Compound A",
        ligand_accession="PubChem:1",
        receptor_name="T1",
        receptor_accession="PDB:1ABC",
        receptor_organism="Target bacterium",
        ligand_source=_source(
            "ligand.pdbqt",
            "PubChem:1",
            digest="1" * 64,
        ),
        receptor_source=_source(
            "receptor.pdbqt",
            "PDB:1ABC",
            digest="2" * 64,
        ),
        parameters=VinaParameters(
            center_x=1,
            center_y=2,
            center_z=3,
            size_x=20,
            size_y=20,
            size_z=20,
            seed=42,
        ),
        engine_version="1.2.5",
    )
    docking = parse_vina_output(
        f"""\
VetEvidence-Manifest-SHA256: {manifest.manifest_sha256}
AutoDock Vina v1.2.5
mode | affinity | dist from best mode
-----+----------+--------------------
1 -8.1 0.0 0.0
""",
        manifest=manifest,
        output_source=_source("vina.log", "run:dock-1"),
    )
    bundle = MechanismPredictionBundle(
        network=network,
        docking_runs=[docking],
    )

    report = build_decision_report(
        question,
        conditions=[],
        analysis=analysis,
        mechanism_prediction=bundle,
        task_events=[
            build_task_event(
                "run-mechanism",
                TaskStatus.AWAITING_REVIEW,
                "等待复核",
            )
        ],
    )
    markdown = decision_report_to_markdown(report)

    assert report.mechanism_prediction == bundle
    assert "计算预测（不等同于实验或直接文献证据）" in markdown
    assert "UniProt:P11111" in markdown
    assert "最佳解析得分：-8.1 kcal/mol" in markdown
    assert manifest.manifest_sha256 in markdown
    assert "不能证明体内外活性或药物协同" in markdown
    assert "不能认证该文件确由 Vina 实际运行产生" in markdown
    assert report.recommendation.evidence
    assert all(
        reference.source_type == "experiment_csv"
        for reference in report.recommendation.evidence
    )


def test_report_drops_predictions_outside_current_question_scope() -> None:
    question = ResearchQuestion(
        id="rq-scope-gate",
        text="Compound A 与 Compound B 对 Target bacterium 是否协同？",
        population="Target bacterium",
        intervention="Compound A",
        comparator="Compound B",
        outcomes=["FICI"],
    )
    analysis = analyze_fici_csv(
        """\
drug_a,drug_b,population_or_strain,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
Compound A,Compound B,Target bacterium,8,2,4,1
"""
    )
    wrong_network = analyze_network_pharmacology_csv(
        (
            "compound,compound_accession,organism,target,target_accession\n"
            "Compound A,CID:1,Other bacterium,T1,UniProt:P11111\n"
            "Compound B,CID:2,Other bacterium,T1,UniProt:P11111\n"
        ),
        (
            "organism,target,target_accession,pathway,pathway_accession\n"
            "Other bacterium,T1,UniProt:P11111,P1,KEGG:map00010\n"
        ),
        compound_target_source=_source("ct.csv", "dataset:ct"),
        target_pathway_source=_source("tp.csv", "dataset:tp"),
    )
    wrong_manifest = build_vina_manifest(
        task_id="dock-wrong-scope",
        compound_name="Compound A",
        ligand_accession="PubChem:1",
        receptor_name="T1",
        receptor_accession="PDB:1ABC",
        receptor_organism="Other bacterium",
        ligand_source=_source(
            "ligand.pdbqt",
            "PubChem:1",
            digest="1" * 64,
        ),
        receptor_source=_source(
            "receptor.pdbqt",
            "PDB:1ABC",
            digest="2" * 64,
        ),
        parameters=VinaParameters(
            center_x=1,
            center_y=2,
            center_z=3,
            size_x=20,
            size_y=20,
            size_z=20,
        ),
        engine_version="1.2.5",
    )

    report = build_decision_report(
        question,
        conditions=[],
        analysis=analysis,
        mechanism_prediction=MechanismPredictionBundle(
            network=wrong_network,
            prepared_manifests=[wrong_manifest],
        ),
        task_events=[
            build_task_event(
                "run-scope-gate",
                TaskStatus.AWAITING_REVIEW,
                "等待复核",
            )
        ],
    )

    assert report.mechanism_prediction.network is None
    assert report.mechanism_prediction.prepared_manifests == []
