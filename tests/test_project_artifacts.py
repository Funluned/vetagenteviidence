from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_required_delivery_artifacts_exist() -> None:
    required_paths = [
        "Dockerfile",
        ".dockerignore",
        "docs/PRD.md",
        "docs/ARCHITECTURE.md",
        "docs/EVALUATION.md",
        "docs/DEMO_SCRIPT.md",
        "docs/RESUME_EVIDENCE.md",
        "docs/INTERVIEW_GUIDE.md",
        "docs/RETROSPECTIVE.md",
        "src/vetevidence/experiment_analysis.py",
        "src/vetevidence/imported_extraction.py",
        "src/vetevidence/literature_import.py",
        "src/vetevidence/mechanism_prediction.py",
        "src/vetevidence/openbabel_execution.py",
        "src/vetevidence/run_store.py",
        "src/vetevidence/workbench.py",
        "src/vetevidence/workbench_pipeline.py",
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


def test_dockerfile_has_runtime_and_health_contracts() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.11-slim")
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "0.0.0.0" in dockerfile
    assert "8501" in dockerfile


def test_readme_document_links_exist() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((docs/[^)]+\.md)\)", readme)

    assert links
    assert all((PROJECT_ROOT / link).is_file() for link in links)


def test_workbench_release_metadata_and_readme_contract() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'version = "0.3.0"' in pyproject
    assert "VetResearch Workbench" in readme
    assert "FICI" in readme
    assert "生长曲线" in readme
    assert "RIS" in readme
    assert "网络药理学" in readme
    assert "Open Babel" in readme
    assert "AutoDock Vina" in readme
    assert 'molecular-docking = [' in pyproject
    assert '"openbabel==3.2.1"' in pyproject
