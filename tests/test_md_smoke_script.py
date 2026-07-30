from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_real_md_smoke_cli_is_auditable_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    pytest.importorskip("openmm")
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "scripts" / "run_md_smoke.py"
    output_directory = tmp_path / "md-smoke"
    command = [
        sys.executable,
        str(script),
        "--output-dir",
        str(output_directory),
        "--platform",
        "CPU",
        "--seed",
        "42",
    ]

    completed = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["scope"] == "technical_integrity_smoke_only"
    assert summary["evidence_grade"] == "computational_prediction"
    assert summary["scientific_interpretation_allowed"] is False
    assert summary["free_energy_computed"] is False
    assert summary["fixture"] == {
        "elements": ["N", "C"],
        "fixture_id": "vetevidence-public-two-atom-n-c-v1",
        "force_types": ["HarmonicBondForce"],
        "fully_disclosed": True,
        "particle_count": 2,
        "periodic_boundary_conditions": False,
        "synthetic_non_research_system": True,
    }
    assert summary["protocol"]["integration_steps"] == 30
    assert summary["protocol"]["seed"] == 42
    assert summary["execution"]["actual_platform"] == "CPU"
    assert summary["validation"]["status"] == "technical_smoke_passed"
    assert summary["validation"]["qc_passed"] is True
    assert (
        summary["observables"]["temperature_kelvin"]["sample_count"] == 6
    )
    assert (
        summary["observables"]["potential_energy_kj_mol"]["sample_count"] == 6
    )
    assert len(summary["hashes"]["manifest_sha256"]) == 64
    assert len(summary["hashes"]["result_manifest_sha256"]) == 64
    assert set(summary["hashes"]["artifacts"]) == {
        "analysis",
        "checkpoint",
        "manifest",
        "portable_state",
        "pymol_script",
        "representative_structure",
        "state_log",
        "system",
        "topology",
        "trajectory",
    }

    refused = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert refused.returncode == 2
    failure = json.loads(refused.stderr)
    assert failure["error_type"] == "FileExistsError"
    assert "refusing overwrite" in failure["error"]
