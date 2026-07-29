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
