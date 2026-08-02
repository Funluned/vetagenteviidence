from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_required_delivery_artifacts_exist() -> None:
    required_paths = [
        ".gitattributes",
        "Dockerfile",
        ".dockerignore",
        "LICENSE",
        "CHANGELOG.md",
        "docs/AI_COLLABORATION.md",
        "docs/REAL_AGENT_FAILURE_CASE.md",
        "docs/PRD.md",
        "docs/ARCHITECTURE.md",
        "docs/DATABASE_CONNECTORS.md",
        "docs/DOCKING_WORKFLOW.md",
        "docs/MOLECULAR_DYNAMICS.md",
        "docs/EVALUATION.md",
        "docs/V0.7_EVALUATION.md",
        "docs/V0.7_AGENT_EVALUATION.md",
        "docs/DEMO_SCRIPT.md",
        "docs/RESUME_EVIDENCE.md",
        "docs/INTERVIEW_GUIDE.md",
        "docs/RETROSPECTIVE.md",
        "src/vetevidence/experiment_analysis.py",
        "src/vetevidence/connector_artifacts.py",
        "src/vetevidence/database_connectors.py",
        "src/vetevidence/docking_artifacts.py",
        "src/vetevidence/docking_ui.py",
        "src/vetevidence/docking_ui_support.py",
        "src/vetevidence/docking_visualization.py",
        "src/vetevidence/docking_workflow.py",
        "src/vetevidence/evidence_network.py",
        "src/vetevidence/imported_extraction.py",
        "src/vetevidence/literature_import.py",
        "src/vetevidence/md_ui.py",
        "src/vetevidence/md_ui_support.py",
        "src/vetevidence/md_worker.py",
        "src/vetevidence/md_workflow.py",
        "src/vetevidence/mechanism_prediction.py",
        "src/vetevidence/openbabel_execution.py",
        "src/vetevidence/run_store.py",
        "src/vetevidence/structure_viewer.py",
        "src/vetevidence/workbench.py",
        "src/vetevidence/workbench_pipeline.py",
        "src/vetevidence/v07_evaluation.py",
        "src/vetevidence/agent_runtime.py",
        "src/vetevidence/agent_tools.py",
        "src/vetevidence/deepseek_provider.py",
        "src/vetevidence/evidence_reviewer.py",
        "src/vetevidence/v07_agent_comparison.py",
        "src/vetevidence/v07_agent_evaluation.py",
        "src/vetevidence/v07_agent_fake.py",
        "src/vetevidence/assets/vendor/3dmol/3Dmol.es6-min.js",
        "src/vetevidence/assets/vendor/3dmol/LICENSE",
        "src/vetevidence/assets/vendor/3dmol/UPSTREAM.json",
        "tests/test_docking_visualization.py",
        "tests/test_docking_workflow.py",
        "tests/test_docking_artifacts.py",
        "tests/test_docking_ui_support.py",
        "tests/test_structure_viewer.py",
        "tests/test_md_smoke_script.py",
        "tests/test_md_ui.py",
        "tests/test_md_ui_support.py",
        "tests/test_md_worker.py",
        "tests/test_md_workflow.py",
        "scripts/run_docking_smoke.py",
        "scripts/run_md_smoke.py",
        "scripts/run_v07_rule_baseline.py",
        "scripts/run_v07_agent_evaluation.py",
        "data/eval/v0.7/cases.json",
        "data/eval/v0.7/expected.json",
        "data/eval/v0.7/baselines/rules_v1.json",
        "data/eval/v0.7/baselines/agent_fake_v1.json",
        "data/templates/fici_template.csv",
        "data/templates/growth_curve_template.csv",
        "data/templates/compound_target_template.csv",
        "data/templates/target_pathway_template.csv",
        "data/demo/fici_demo.csv",
        "data/demo/growth_curve_demo.csv",
        "data/demo/mechanism_compound_target_demo.csv",
        "data/demo/mechanism_target_pathway_demo.csv",
        "data/demo/cnki_export_demo.ris",
    ]

    missing = [
        path
        for path in required_paths
        if not (PROJECT_ROOT / path).is_file()
    ]
    assert missing == []


def test_sha256_pinned_3dmol_asset_disables_git_eol_conversion() -> None:
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = {
        line.partition("#")[0].strip()
        for line in attributes.splitlines()
        if line.partition("#")[0].strip()
    }

    assert "src/vetevidence/assets/vendor/3dmol/3Dmol.es6-min.js -text" in rules


def test_dockerfile_has_runtime_and_health_contracts() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.11-slim")
    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile
    assert "COPY --chown=appuser:appuser data ./data" in dockerfile
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "0.0.0.0" in dockerfile
    assert "8501" in dockerfile
    assert "data/cache/" in dockerignore
    assert "data/eval/" in dockerignore
    assert ".streamlit/secrets.toml" in dockerignore


def test_readme_document_links_exist() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((docs/[^)]+\.md)\)", readme)

    assert links
    assert all((PROJECT_ROOT / link).is_file() for link in links)


def test_workbench_release_metadata_and_readme_contract() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    license_text = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert 'version = "0.7.0"' in pyproject
    assert 'license = { file = "LICENSE" }' in pyproject
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Sunqi" in license_text
    assert "## [0.7.0] - 2026-08-02" in changelog
    assert "VetResearch Workbench" in readme
    assert "FICI" in readme
    assert "生长曲线" in readme
    assert "RIS" in readme
    assert "网络药理学" in readme
    assert "PubChem" in readme
    assert "STRING" in readme
    assert "DAVID" in readme
    assert "Open Babel" in readme
    assert "AutoDock Vina" in readme
    assert "3Dmol.js" in readme
    assert "PML" in readme
    assert "OpenMM" in readme
    assert "technical_smoke" in readme
    assert "30 步" in readme
    assert "computational_prediction" in readme
    assert 'molecular-docking = [' in pyproject
    assert '"openbabel==3.2.1"' in pyproject
    assert 'molecular-dynamics = [' in pyproject
    assert '"openmm==8.5.2"' in pyproject
