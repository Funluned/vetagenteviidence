from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vetevidence.md_ui_support import (
    build_job_progress,
    build_mapping_evidence,
    default_md_task_id,
    infer_source_format,
    list_md_jobs,
    normalize_forcefield_files,
    parse_atom_indices,
    parse_preparation_command,
    summarize_series,
    validate_task_id,
    verified_artifact_downloads,
)
from vetevidence.md_worker import MDJobRecord, MDJobState


def test_task_id_and_source_format_are_strict() -> None:
    assert validate_task_id("md-case_01") == "md-case_01"
    assert default_md_task_id("run / 01").startswith("md-run-01")
    assert infer_source_format("receptor.PDB", role="receptor") == "pdb"
    assert infer_source_format("ligand.SDF", role="ligand") == "sdf"

    with pytest.raises(ValueError, match="任务 ID"):
        validate_task_id("../escape")
    with pytest.raises(ValueError, match="不受支持"):
        infer_source_format("receptor.pdbqt", role="receptor")
    with pytest.raises(ValueError, match="不受支持"):
        infer_source_format("receptor.mmcif", role="receptor")
    with pytest.raises(ValueError, match="不受支持"):
        infer_source_format("ligand.smiles", role="ligand")
    with pytest.raises(ValueError, match="不受支持"):
        infer_source_format("ligand.mol2", role="ligand")


def test_atom_indices_expand_ranges_and_reject_ambiguity() -> None:
    assert parse_atom_indices(
        "0-2, 5；8",
        label="映射",
    ) == (0, 1, 2, 5, 8)
    assert parse_atom_indices("", label="设备", allow_empty=True) == ()

    with pytest.raises(ValueError, match="重复索引"):
        parse_atom_indices("0-2,2", label="映射")
    with pytest.raises(ValueError, match="终点小于起点"):
        parse_atom_indices("5-2", label="映射")
    with pytest.raises(ValueError, match="无效条目"):
        parse_atom_indices("1:3", label="映射")


def test_preparation_command_is_exact_argv_not_shell_parsing() -> None:
    command = parse_preparation_command(
        'python\nprepare.py\n--label\n"value with spaces"'
    )

    assert command == (
        "python",
        "prepare.py",
        "--label",
        '"value with spaces"',
    )
    with pytest.raises(ValueError, match="不能为空"):
        parse_preparation_command("")


def test_forcefield_uploads_are_safe_and_bounded() -> None:
    files = normalize_forcefield_files(
        {
            "protein.ffxml": b"<ForceField/>",
            "ligand.offxml": b"<SMIRNOFF/>",
        }
    )

    assert set(files) == {"protein.ffxml", "ligand.offxml"}
    with pytest.raises(ValueError, match="不安全"):
        normalize_forcefield_files({"../secret.xml": b"x"})
    with pytest.raises(ValueError, match="为空"):
        normalize_forcefield_files({"empty.xml": b""})


def test_mapping_evidence_is_canonical_and_hash_bound() -> None:
    recorded_at = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    arguments = {
        "manifest_sha256": "a" * 64,
        "receptor_source_sha256": "b" * 64,
        "ligand_source_sha256": "c" * 64,
        "topology_pdb": b"ATOM\n",
        "receptor_indices": (0, 1),
        "ligand_indices": (2,),
        "mapping_method": "explicit element-order review",
        "prepared_by": "researcher",
        "preparation_command": ("python", "prepare.py"),
        "recorded_at": recorded_at,
    }

    first = build_mapping_evidence(**arguments)
    second = build_mapping_evidence(**arguments)
    payload = json.loads(first)

    assert first == second
    assert payload["topology_pdb_sha256"] == hashlib.sha256(
        b"ATOM\n"
    ).hexdigest()
    assert payload["zero_based_indices"]["ligand"] == [2]
    assert payload["recorded_at"].endswith("+00:00")


def test_series_and_job_progress_only_report_persisted_values() -> None:
    summary = summarize_series([300.0, 301.0, 299.0], unit="K")
    assert summary is not None
    assert summary.latest == 299.0
    assert summary.mean == 300.0

    record = SimpleNamespace(
        job_id="md-progress",
        state=MDJobState.RUNNING,
        checkpoint=SimpleNamespace(step=10),
        run_result=None,
        manifest=SimpleNamespace(
            protocol=SimpleNamespace(integration_steps=30),
            hardware_request=SimpleNamespace(platform="CPU"),
        ),
    )
    progress = build_job_progress(record)

    assert progress.completed_steps == 10
    assert progress.total_steps == 30
    assert progress.actual_platform is None
    assert progress.temperature is None


def test_success_progress_exposes_actual_platform_and_real_metrics() -> None:
    temperature = SimpleNamespace(values=[299.0, 301.0], unit="K")
    potential = SimpleNamespace(values=[-10.0, -9.5], unit="kJ/mol")
    record = SimpleNamespace(
        job_id="md-success",
        state=MDJobState.SUCCEEDED,
        checkpoint=SimpleNamespace(step=30),
        manifest=SimpleNamespace(
            protocol=SimpleNamespace(integration_steps=30),
            hardware_request=SimpleNamespace(platform="CUDA"),
        ),
        run_result=SimpleNamespace(
            execution_audit=SimpleNamespace(
                platform_name="CUDA",
                selected_device="0",
                driver_version="610.47",
            ),
            analysis=SimpleNamespace(
                replicas=[
                    SimpleNamespace(
                        temperature_kelvin=temperature,
                        potential_energy_kj_mol=potential,
                    )
                ],
                reserved_metrics_not_produced=["protein_backbone_rmsd_nm"],
            ),
        ),
    )

    progress = build_job_progress(record)

    assert progress.actual_platform == "CUDA"
    assert progress.selected_device == "0"
    assert progress.temperature is not None
    assert progress.temperature.latest == 301.0
    assert progress.potential_energy is not None
    assert progress.potential_energy.minimum == -10.0
    assert progress.reserved_metrics_not_produced == (
        "protein_backbone_rmsd_nm",
    )


def test_job_listing_reports_corrupt_files_and_sorts_records(
    tmp_path: Path,
) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    (jobs_root / "new.json").write_text(
        json.dumps({"job_id": "new"}),
        encoding="utf-8",
    )
    (jobs_root / "old.json").write_text(
        json.dumps({"job_id": "old"}),
        encoding="utf-8",
    )
    (jobs_root / "broken.json").write_text("{", encoding="utf-8")
    records = {
        "new": MDJobRecord.model_construct(
            job_id="new",
            updated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        ),
        "old": MDJobRecord.model_construct(
            job_id="old",
            updated_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        ),
    }

    class FakeStore:
        def __init__(self) -> None:
            self.jobs_root = jobs_root

        def load(self, job_id: str):
            return records[job_id]

    listing = list_md_jobs(FakeStore())  # type: ignore[arg-type]

    assert [item.job_id for item in listing.records] == ["new", "old"]
    assert listing.invalid_files == ("broken.json",)


def test_artifact_download_rechecks_size_and_sha256(tmp_path: Path) -> None:
    job_id = "md-artifact"
    artifact_root = tmp_path / job_id
    attempt = artifact_root / "attempt-0001"
    attempt.mkdir(parents=True)
    payload = b'{"technical_smoke":true}'
    target = attempt / "analysis.json"
    target.write_bytes(payload)
    artifact = SimpleNamespace(
        role="analysis",
        filename="analysis.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    record = SimpleNamespace(
        job_id=job_id,
        run_result=SimpleNamespace(
            attempt_id="attempt-0001",
            artifacts=[artifact],
        ),
    )

    class FakeStore:
        def load(self, active_job_id: str):
            assert active_job_id == job_id
            return record

        def artifact_directory(self, active_job_id: str) -> Path:
            assert active_job_id == job_id
            return artifact_root.resolve()

    downloads = verified_artifact_downloads(  # type: ignore[arg-type]
        FakeStore(),
        record,
    )

    assert downloads[0].payload == payload
    assert downloads[0].mime == "application/json"
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="校验失败"):
        verified_artifact_downloads(  # type: ignore[arg-type]
            FakeStore(),
            record,
        )
