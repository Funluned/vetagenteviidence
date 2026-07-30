from __future__ import annotations

import hashlib
import json

import pytest

from vetevidence.docking_artifacts import DockingArtifactStore
from vetevidence.docking_visualization import (
    DockingVisualizationPackage,
    PackageFile,
)


def _package(
    payload: bytes = b"PK\x03\x04verified-docking-package",
) -> DockingVisualizationPackage:
    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.dumps(
        {
            "batch_id": "batch-001",
            "ligand_id": "ligand-001",
            "score": {"seed": 42, "mode": 1},
        }
    ).encode()
    return DockingVisualizationPackage.model_construct(
        files=(
                PackageFile(
                    filename="visualization_manifest.json",
                    payload=manifest,
                sha256=hashlib.sha256(manifest).hexdigest(),
            ),
        ),
        zip_payload=payload,
        zip_sha256=digest,
        task_manifest_sha256="1" * 64,
        complex_pdb_sha256="2" * 64,
        pml_sha256="3" * 64,
        batch_id="batch-001",
        ligand_id="ligand-001",
        seed=42,
        pose_mode=1,
        receptor_model="1",
        receptor_chains=("A",),
        ligand_chain="Z",
        ligand_residue_number=9999,
        pymol_render=None,
        plip_analysis=None,
    )


def test_store_is_immutable_and_revalidates_zip(tmp_path) -> None:
    store = DockingArtifactStore(tmp_path)

    saved = store.save(
        run_id="run-001",
        batch_id="batch-001",
        ligand_id="ligand-001",
        seed=42,
        pose_mode=1,
        package=_package(),
    )

    assert saved.metadata.zip_sha256 == hashlib.sha256(
        saved.zip_payload
    ).hexdigest()
    assert saved.directory.is_dir()
    with pytest.raises(FileExistsError, match="禁止覆盖"):
        store.save(
            run_id="run-001",
            batch_id="batch-001",
            ligand_id="ligand-001",
            seed=42,
            pose_mode=1,
            package=_package(),
        )

    (saved.directory / "package.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="大小|SHA-256"):
        store.load("run-001", saved.metadata.artifact_id)


def test_store_rejects_traversal_and_package_hash_mismatch(tmp_path) -> None:
    store = DockingArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        store.save(
            run_id="../outside",
            batch_id="batch-001",
            ligand_id="ligand-001",
            seed=42,
            pose_mode=1,
            package=_package(),
        )

    package = _package()
    mismatched = package.model_copy(update={"zip_sha256": "0" * 64})
    with pytest.raises(ValueError, match="package SHA-256"):
        store.save(
            run_id="run-001",
            batch_id="batch-001",
            ligand_id="ligand-001",
            seed=42,
            pose_mode=1,
            package=mismatched,
        )

    with pytest.raises(ValueError, match="任务包身份"):
        store.save(
            run_id="run-001",
            batch_id="different-batch",
            ligand_id="ligand-001",
            seed=42,
            pose_mode=1,
            package=package,
        )
