from __future__ import annotations

import json
from pathlib import Path

import pytest

from vetevidence.vina_artifacts import VinaArtifactStore


def test_vina_artifact_store_round_trips_and_verifies_hashes(
    tmp_path: Path,
) -> None:
    store = VinaArtifactStore(tmp_path / "vina")
    saved = store.save(
        run_id="run-123",
        task_id="dock-abc",
        manifest_sha256="a" * 64,
        bound_log=b"AutoDock Vina v1.2.7\n",
        output_pdbqt=b"MODEL 1\nENDMDL\n",
        execution={"exit_code": 0, "version": "1.2.7"},
    )

    restored = store.load(
        "run-123",
        "dock-abc",
        expected_manifest_sha256="a" * 64,
    )

    assert restored == saved
    assert restored.metadata.log_sha256
    assert restored.metadata.output_pdbqt_sha256


def test_vina_artifact_store_rejects_unsafe_ids(tmp_path: Path) -> None:
    store = VinaArtifactStore(tmp_path / "vina")

    with pytest.raises(ValueError, match="运行 ID"):
        store.directory_for("../outside", "dock-abc")


def test_vina_artifact_store_rejects_manifest_mismatch(tmp_path: Path) -> None:
    store = VinaArtifactStore(tmp_path / "vina")
    store.save(
        run_id="run-123",
        task_id="dock-abc",
        manifest_sha256="a" * 64,
        bound_log=b"log",
        output_pdbqt=b"poses",
        execution={},
    )

    with pytest.raises(ValueError, match="任务清单"):
        store.load(
            "run-123",
            "dock-abc",
            expected_manifest_sha256="b" * 64,
        )


def test_vina_artifact_store_detects_corruption(tmp_path: Path) -> None:
    store = VinaArtifactStore(tmp_path / "vina")
    store.save(
        run_id="run-123",
        task_id="dock-abc",
        manifest_sha256="a" * 64,
        bound_log=b"log",
        output_pdbqt=b"poses",
        execution={},
    )
    artifact_dir = store.directory_for("run-123", "dock-abc")
    (artifact_dir / "run.log").write_bytes(b"changed")

    with pytest.raises(ValueError, match="日志 SHA-256"):
        store.load("run-123", "dock-abc")


@pytest.mark.parametrize("unsafe_id", ["CON", "nul.txt", "run.", "A" * 65])
def test_vina_artifact_store_rejects_windows_unsafe_ids(
    tmp_path: Path,
    unsafe_id: str,
) -> None:
    store = VinaArtifactStore(tmp_path / "vina")

    with pytest.raises(ValueError, match="运行 ID"):
        store.directory_for(unsafe_id, "dock-abc")


def test_vina_artifact_store_refuses_to_overwrite_task(tmp_path: Path) -> None:
    store = VinaArtifactStore(tmp_path / "vina")
    arguments = {
        "run_id": "run-123",
        "task_id": "dock-abc",
        "manifest_sha256": "a" * 64,
        "bound_log": b"log",
        "output_pdbqt": b"poses",
        "execution": {},
    }
    store.save(**arguments)

    with pytest.raises(ValueError, match="拒绝覆盖"):
        store.save(**arguments)


def test_vina_artifact_metadata_is_json_serializable(tmp_path: Path) -> None:
    store = VinaArtifactStore(tmp_path / "vina")
    saved = store.save(
        run_id="run-123",
        task_id="dock-abc",
        manifest_sha256="a" * 64,
        bound_log=b"log",
        output_pdbqt=b"poses",
        execution={"arguments": ["--receptor", "receptor.pdbqt"]},
    )

    payload = json.loads(
        (store.directory_for("run-123", "dock-abc") / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["log_sha256"] == saved.metadata.log_sha256
    assert payload["execution"]["arguments"][0] == "--receptor"
