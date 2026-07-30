from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vetevidence.md_workflow import (
    MDAnalysisResult,
    MDChemistryConfirmation,
    MDExecutionAudit,
    MDInputSource,
    MDPreset,
    MDReplicaAnalysis,
    MDReplicateSummary,
    MDRunResult,
    MDTimeSeries,
    MDValidationStatus,
    build_md_manifest,
)
from vetevidence.md_worker import (
    MDBackendPreflight,
    MDHardwareSnapshot,
    MDJobState,
    MDJobStore,
    MDSystemSummary,
    MDWorkerExecutionError,
    _artifact_reference,
    _context_platform_properties,
    _execution_environment_fingerprint,
    _start_worker_deadline,
    _technical_smoke_qc,
    build_openmm_dry_run,
    execute_prepared_openmm_smoke,
    launch_md_worker,
    preflight_openmm,
    process_queued_job,
)


RECEPTOR_PDB = b"""\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00 20.00           N
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
TOPOLOGY_PDB = b"""\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00 20.00           N
HETATM    2  C1  LIG B   1       1.000   0.000   0.000  1.00 20.00           C
END
"""


def _manifest(*, approved: bool = True):
    confirmation = MDChemistryConfirmation(
        reviewed_by="researcher",
        confirmed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        receptor_chain_selection=["A"],
        receptor_protonation_assumption="pH 7.4 reviewed",
        ligand_formal_charge=0,
        ligand_protonation_state="neutral at pH 7.4",
        ligand_tautomer_state="specified tautomer",
        ligand_stereochemistry="achiral",
        chemical_identity_confirmed=True,
        receptor_structure_reviewed=True,
        formal_charge_confirmed=True,
        protonation_confirmed=True,
        tautomer_confirmed=True,
        stereochemistry_confirmed=True,
        all_stereocenters_defined=True,
        metals_reviewed=True,
        covalent_links_reviewed=True,
        unknown_residues_reviewed=True,
    )
    return build_md_manifest(
        task_id="md-worker-001",
        receptor_payload=RECEPTOR_PDB,
        receptor_source=MDInputSource(
            source_name="receptor.pdb",
            accession="PDB:1ABC",
            version="2026-07-30",
            format="pdb",
        ),
        ligand_payload=LIGAND_SDF,
        ligand_source=MDInputSource(
            source_name="ligand.sdf",
            accession="PubChem:1",
            version="2026-07-30",
            format="sdf",
        ),
        chemistry_confirmation=confirmation,
        preset=MDPreset.TECHNICAL_SMOKE,
        protocol_approved_by_user=approved,
    )


def _hardware(*, platforms: list[str] | None = None) -> MDHardwareSnapshot:
    return MDHardwareSnapshot(
        operating_system="test-os",
        machine="test-machine",
        processor="test-cpu",
        python_version="3.11",
        cpu_count=4,
        openmm_platforms=platforms or ["CPU"],
        gpu_platforms=[
            item
            for item in (platforms or ["CPU"])
            if item in {"CUDA", "HIP", "OpenCL"}
        ],
        fingerprint_sha256="f" * 64,
    )


def _available_preflight() -> MDBackendPreflight:
    return MDBackendPreflight(
        execution_available=True,
        parameterization_available=False,
        missing_parameterization_modules=[
            "pdbfixer",
            "openff.toolkit",
            "openmmforcefields",
        ],
        package_versions={
            "openmm": "8.test",
            "openmm.app": "8.test",
        },
        hardware=_hardware(),
    )


def _enqueue(store: MDJobStore):
    return store.enqueue(
        _manifest(),
        receptor_payload=RECEPTOR_PDB,
        ligand_payload=LIGAND_SDF,
    )


def _prepare(
    store: MDJobStore,
    job_id: str,
    *,
    system_xml: bytes | str = b"<System type='test'></System>",
    topology_pdb: bytes | str = TOPOLOGY_PDB,
    summary: MDSystemSummary | None = None,
):
    return store.save_prepared_system(
        job_id,
        system_xml=system_xml,
        topology_pdb=topology_pdb,
        parameterization_backend="unit-test-builder",
        parameterization_version="1",
        forcefield_files={"unit-test.xml": b"<ForceField/>"},
        preparation_command=["unit-test-builder", "--explicit-mapping"],
        prepared_by="researcher",
        declared_system_summary=summary
        or MDSystemSummary(
            particle_count=2,
            force_count=1,
            constraint_count=0,
            force_types=["HarmonicBondForce"],
            uses_periodic_boundary_conditions=False,
        ),
        receptor_topology_atom_indices=[0],
        ligand_topology_atom_indices=[1],
        mapping_method="explicit source atom order",
        mapping_evidence=b'{"receptor":[0],"ligand":[1]}',
    )


def test_preflight_reports_missing_backend_without_import_failure() -> None:
    def missing_importer(name: str) -> ModuleType:
        raise ModuleNotFoundError(name)

    result = preflight_openmm(importer=missing_importer)

    assert result.execution_available is False
    assert result.parameterization_available is False
    assert "openmm" in result.missing_execution_modules
    assert "不能生成轨迹或能量" in (result.reason or "")


def test_preflight_distinguishes_execution_from_parameterization_stack() -> None:
    class FakePlatformInstance:
        def __init__(self, name: str) -> None:
            self._name = name

        def getName(self) -> str:
            return self._name

    class FakePlatform:
        @staticmethod
        def getNumPlatforms() -> int:
            return 2

        @staticmethod
        def getPlatform(index: int) -> FakePlatformInstance:
            return FakePlatformInstance(["CPU", "CUDA"][index])

    openmm = ModuleType("openmm")
    openmm.__version__ = "8.fake"
    openmm.Platform = FakePlatform  # type: ignore[attr-defined]
    app = ModuleType("openmm.app")
    app.__version__ = "8.fake"

    def importer(name: str) -> ModuleType:
        if name == "openmm":
            return openmm
        if name == "openmm.app":
            return app
        raise ModuleNotFoundError(name)

    result = preflight_openmm(importer=importer)

    assert result.execution_available is True
    assert result.parameterization_available is False
    assert result.hardware.openmm_platforms == ["CPU", "CUDA"]
    assert result.hardware.gpu_platforms == ["CUDA"]


def test_dry_run_is_traceable_and_never_plans_free_energy() -> None:
    plan = build_openmm_dry_run(
        _manifest(),
        preflight=_available_preflight(),
    )

    assert plan.manifest_sha256 == _manifest().manifest_sha256
    assert plan.replica_plans[0].integration_steps == 30
    assert plan.binding_free_energy_planned is False
    assert plan.automatic_parameterization_planned is False
    assert any("参数化栈不可用" in item for item in plan.warnings)


def test_technical_smoke_qc_fails_when_real_series_are_missing() -> None:
    passed, warnings = _technical_smoke_qc(None, None)

    assert passed is False
    assert any("缺少" in warning for warning in warnings)


def test_atomic_job_queue_round_trips_and_runs_dry_plan(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    enqueued = _enqueue(store)

    assert store.load(enqueued.job_id) == enqueued
    completed = process_queued_job(
        store,
        enqueued.job_id,
        dry_run=True,
        preflight=_available_preflight(),
    )

    assert completed.state is MDJobState.SUCCEEDED
    assert completed.dry_run_plan is not None
    assert completed.run_result is None
    assert completed.worker_pid is None
    assert completed.attempts == 1


def test_unapproved_protocol_can_dry_run_but_cannot_execute(
    tmp_path: Path,
) -> None:
    dry_store = MDJobStore(tmp_path / "dry")
    dry_job = dry_store.enqueue(
        _manifest(approved=False),
        receptor_payload=RECEPTOR_PDB,
        ligand_payload=LIGAND_SDF,
    )
    dry_completed = process_queued_job(
        dry_store,
        dry_job.job_id,
        dry_run=True,
        preflight=_available_preflight(),
    )
    assert dry_completed.state is MDJobState.SUCCEEDED
    assert dry_completed.dry_run_plan is not None

    real_store = MDJobStore(tmp_path / "real")
    real_job = real_store.enqueue(
        _manifest(approved=False),
        receptor_payload=RECEPTOR_PDB,
        ligand_payload=LIGAND_SDF,
    )
    blocked = process_queued_job(
        real_store,
        real_job.job_id,
        dry_run=False,
        preflight=_available_preflight(),
    )
    assert blocked.state is MDJobState.FAILED
    assert "用户明确批准" in (blocked.error or "")


def test_real_job_without_parameterized_system_fails_instead_of_faking(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)

    failed = process_queued_job(
        store,
        job.job_id,
        dry_run=False,
        preflight=_available_preflight(),
    )

    assert failed.state is MDJobState.FAILED
    assert "System XML" in (failed.error or "")
    assert "不会猜测或伪造参数" in (failed.error or "")
    assert failed.run_result is None


def test_preflight_exception_is_persisted_as_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)

    def broken_preflight():
        raise OSError("DLL load failed")

    monkeypatch.setattr(
        "vetevidence.md_worker.preflight_openmm",
        broken_preflight,
    )
    failed = process_queued_job(
        store,
        job.job_id,
        dry_run=True,
    )

    assert failed.state is MDJobState.FAILED
    assert "DLL load failed" in (failed.error or "")
    assert failed.worker_pid is None


def test_prepared_inputs_are_hash_bound_and_refuse_overwrite(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    system_xml = b"<System type='test'></System>"
    updated = _prepare(
        store,
        job.job_id,
        system_xml=system_xml,
        topology_pdb=TOPOLOGY_PDB,
    )

    assert updated.prepared_system is not None
    assert updated.prepared_system.manifest_sha256 == (
        job.manifest.manifest_sha256
    )
    assert updated.prepared_system.system_xml_sha256 == hashlib.sha256(
        system_xml
    ).hexdigest()
    mapping_document = json.loads(
        Path(updated.prepared_system.mapping_evidence_path).read_text(
            encoding="utf-8"
        )
    )
    assert mapping_document["schema"] == "vetevidence-md-atom-mapping-v2"
    assert mapping_document["atoms"]["receptor"][0]["source_identity"] == (
        mapping_document["atoms"]["receptor"][0]["topology_identity"]
    )
    assert mapping_document["atoms"]["ligand"][0]["source_identity"][
        "element"
    ] == "C"
    with pytest.raises(ValueError, match="拒绝覆盖"):
        _prepare(store, job.job_id, system_xml=system_xml)


def test_preparation_rejects_identity_mismatch_and_external_xml(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    wrong_topology = b"""\
ATOM      1  H1  ALA A   1       0.000   0.000   0.000  1.00 20.00           H
HETATM    2  H2  LIG B   1       1.000   0.000   0.000  1.00 20.00           H
END
"""
    with pytest.raises(ValueError, match="元素序列"):
        _prepare(store, job.job_id, topology_pdb=wrong_topology)

    same_element_wrong_identity = TOPOLOGY_PDB.replace(
        b"ALA A   1",
        b"GLY B   9",
        1,
    )
    identity_store = MDJobStore(tmp_path / "md-second")
    identity_job = _enqueue(identity_store)
    with pytest.raises(ValueError, match="逐原子|链、残基"):
        _prepare(
            identity_store,
            identity_job.job_id,
            topology_pdb=same_element_wrong_identity,
        )

    malicious = (
        b"<!DOCTYPE x [<!ENTITY ext SYSTEM 'file:///etc/passwd'>]>"
        b"<System>&ext;</System>"
    )
    with pytest.raises(ValueError, match="外部实体"):
        _prepare(store, job.job_id, system_xml=malicious)

    excessive_expression = (
        b"<System expression='" + b"x" * (1024 * 1024) + b"'></System>"
    )
    with pytest.raises(ValueError, match="表达式"):
        _prepare(store, job.job_id, system_xml=excessive_expression)


def test_periodic_inputs_are_rejected_until_box_vectors_are_bound(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="仅开放非周期"):
        MDSystemSummary(
            particle_count=2,
            force_count=1,
            constraint_count=0,
            force_types=["HarmonicBondForce"],
            uses_periodic_boundary_conditions=True,
        )

    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    topology = b"CRYST1   10.000   10.000   10.000  90.00  90.00  90.00 P 1\n" + (
        TOPOLOGY_PDB
    )
    with pytest.raises(ValueError, match="CRYST1"):
        _prepare(store, job.job_id, topology_pdb=topology)


def test_mapping_supporting_evidence_tampering_is_detected(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    prepared = _prepare(store, job.job_id)
    assert prepared.prepared_system is not None
    evidence = Path(prepared.prepared_system.mapping_evidence_path)
    submitted = evidence.parent / "submitted-mapping-evidence.bin"
    submitted.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="支持证据"):
        store.load(job.job_id)


def test_original_input_tampering_is_detected_on_load(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    Path(job.original_inputs.ligand.stored_path).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="原始输入.*SHA-256"):
        store.load(job.job_id)


def test_cross_process_claim_is_compare_and_swap_safe(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)

    def claim() -> str:
        try:
            return store.claim(job.job_id).state.value
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: claim(), range(2)))

    assert outcomes.count("running") == 1
    assert any(
        "queued" in outcome or "revision" in outcome
        for outcome in outcomes
        if outcome != "running"
    )


def test_startup_reconcile_marks_dead_worker_failed(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    store.claim(job.job_id, worker_pid=2_147_483_647)

    reconciled = store.reconcile_stale_jobs()

    assert len(reconciled) == 1
    assert reconciled[0].state is MDJobState.FAILED
    assert "PID" in (reconciled[0].error or "")


def test_execution_fingerprint_binds_actual_platform_device_and_precision() -> None:
    preflight = _available_preflight()
    cpu = _execution_environment_fingerprint(
        preflight=preflight,
        backend_version="8.test",
        platform_name="CPU",
        platform_properties={"Threads": "4"},
    )
    cuda = _execution_environment_fingerprint(
        preflight=preflight,
        backend_version="8.test",
        platform_name="CUDA",
        platform_properties={
            "DeviceIndex": "0",
            "DeviceName": "GPU-A",
            "Precision": "mixed",
        },
    )
    cuda_other_device = _execution_environment_fingerprint(
        preflight=preflight,
        backend_version="8.test",
        platform_name="CUDA",
        platform_properties={
            "DeviceIndex": "1",
            "DeviceName": "GPU-B",
            "Precision": "mixed",
        },
    )

    assert len({cpu, cuda, cuda_other_device}) == 3
    assert all(len(item) == 64 for item in (cpu, cuda, cuda_other_device))


def test_worker_hard_deadline_marks_running_job_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    store.claim(job.job_id)
    exited = threading.Event()

    def fake_exit(code: int) -> None:
        assert code == 124
        exited.set()

    monkeypatch.setattr("vetevidence.md_worker.os._exit", fake_exit)
    _start_worker_deadline(
        store,
        job.job_id,
        timeout_seconds=0.01,
    )

    assert exited.wait(1)
    failed = store.load(job.job_id)
    assert failed.state is MDJobState.FAILED
    assert "硬截止" in (failed.error or "")


def test_success_result_artifact_tampering_is_detected_without_openmm(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    prepared = _prepare(store, job.job_id)
    claimed = store.claim(job.job_id)
    attempt = store.attempt_directory(job.job_id, claimed.attempts)
    attempt.mkdir(parents=True)
    role_names = {
        "manifest": "manifest.json",
        "topology": "topology.pdb",
        "system": "system.xml",
        "portable_state": "portable-state.xml",
        "checkpoint": "checkpoint.chk",
        "trajectory": "trajectory.dcd",
        "state_log": "state.csv",
        "analysis": "analysis.json",
        "pymol_script": "view_md.pml",
        "representative_structure": "representative.pdb",
    }
    for role, name in role_names.items():
        (attempt / name).write_bytes(f"{role}-payload".encode())
    series_temperature = MDTimeSeries(
        times_ps=[0.01],
        values=[300.0],
        unit="K",
    )
    series_energy = MDTimeSeries(
        times_ps=[0.01],
        values=[-1.0],
        unit="kJ/mol",
    )
    analysis = MDAnalysisResult(
        replicas=[
            MDReplicaAnalysis(
                replica_index=1,
                seed=job.manifest.protocol.seeds[0],
                qc_passed=True,
                temperature_kelvin=series_temperature,
                potential_energy_kj_mol=series_energy,
            )
        ],
        replicate_summary=MDReplicateSummary(
            total_replicas=1,
            successful_replicas=1,
        ),
        produced_metrics=[
            "temperature_kelvin",
            "potential_energy_kj_mol",
        ],
    )
    now = datetime.now(timezone.utc)
    result = MDRunResult(
        manifest=job.manifest,
        validation_status=MDValidationStatus.TECHNICAL_SMOKE_PASSED,
        analysis=analysis,
        execution_audit=MDExecutionAudit(
            execution_mode="openmm_local",
            backend_version="8.test",
            package_versions={"openmm": "8.test"},
            hardware_fingerprint="f" * 64,
            platform_name="CPU",
            precision="not_applicable",
            forcefield_file_sha256=(
                prepared.prepared_system.forcefield_file_sha256
            ),
            seeds=job.manifest.protocol.seeds,
            random_seed_assignments={
                "LangevinMiddleIntegrator": job.manifest.protocol.seeds[0]
            },
            started_at=now,
            completed_at=now,
            duration_seconds=0,
        ),
        artifacts=[
            _artifact_reference(attempt / name, role)
            for role, name in role_names.items()
        ],
        attempt_id="attempt-0001",
    )
    store.mark_run_succeeded(job.job_id, result)
    (attempt / "analysis.json").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="产物 analysis.*SHA-256"):
        store.load(job.job_id)


def test_cancel_checkpoint_and_resume_verify_content(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    _prepare(store, job.job_id)
    cancelled = store.request_cancel(job.job_id)
    assert cancelled.state is MDJobState.CANCELLED

    with pytest.raises(ValueError, match="checkpoint"):
        store.resume(job.job_id)

    checkpoint = b"binary-checkpoint"
    portable = b"<State></State>"
    with_checkpoint = store.save_checkpoint(
        job.job_id,
        checkpoint_payload=checkpoint,
        portable_state_payload=portable,
        step=5,
        backend_version="8.test",
        hardware_fingerprint="f" * 64,
    )
    assert with_checkpoint.checkpoint is not None
    resumed = store.resume(job.job_id)
    assert resumed.state is MDJobState.QUEUED
    assert resumed.resume_count == 1

    checkpoint_path = Path(
        resumed.checkpoint.checkpoint_path  # type: ignore[union-attr]
    )
    checkpoint_path.write_bytes(b"corrupt")
    store.request_cancel(job.job_id)
    with pytest.raises(ValueError, match="SHA-256"):
        store.resume(job.job_id)


def test_completed_checkpoint_cannot_be_resumed(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    _prepare(store, job.job_id)
    store.request_cancel(job.job_id)
    store.save_checkpoint(
        job.job_id,
        checkpoint_payload=b"checkpoint",
        portable_state_payload=b"<State/>",
        step=job.manifest.protocol.integration_steps,
        backend_version="8.test",
        hardware_fingerprint="f" * 64,
    )

    with pytest.raises(ValueError, match="总步数"):
        store.resume(job.job_id)


def test_resume_rejects_checkpoint_from_other_seed(tmp_path: Path) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    _prepare(store, job.job_id)
    store.request_cancel(job.job_id)
    store.save_checkpoint(
        job.job_id,
        checkpoint_payload=b"checkpoint",
        portable_state_payload=b"<State/>",
        step=5,
        backend_version="8.test",
        hardware_fingerprint="f" * 64,
    )
    record_path = store.job_path(job.job_id)
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["checkpoint"]["seed"] += 1
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest/seed"):
        store.resume(job.job_id)


def test_worker_launcher_uses_argument_list_and_shell_false(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    captured: dict[str, Any] = {}

    class DummyProcess:
        pass

    def fake_popen(command: list[str], **options: object) -> DummyProcess:
        captured["command"] = command
        captured["options"] = options
        return DummyProcess()

    process = launch_md_worker(
        store,
        job.job_id,
        python_executable="python-test",
        popen_factory=fake_popen,
    )

    assert isinstance(process, DummyProcess)
    assert captured["command"] == [
        "python-test",
        "-m",
        "vetevidence.md_worker",
        "run-job",
        "--root",
        str(store.root),
        "--job-id",
        job.job_id,
        "--dry-run",
    ]
    options = captured["options"]
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL


def test_optional_openmm_executes_tiny_preparameterized_cpu_smoke(
    tmp_path: Path,
) -> None:
    openmm = pytest.importorskip("openmm")
    pytest.importorskip("openmm.app")

    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    system = openmm.System()
    system.addParticle(14.0)
    system.addParticle(12.0)
    bond = openmm.HarmonicBondForce()
    bond.addBond(
        0,
        1,
        0.1 * openmm.unit.nanometer,
        1000.0
        * openmm.unit.kilojoule_per_mole
        / openmm.unit.nanometer**2,
    )
    system.addForce(bond)
    system_xml = openmm.XmlSerializer.serialize(system)
    with_prepared = _prepare(
        store,
        job.job_id,
        system_xml=system_xml,
        summary=MDSystemSummary(
            particle_count=2,
            force_count=1,
            constraint_count=0,
            force_types=["HarmonicBondForce"],
            uses_periodic_boundary_conditions=False,
        ),
    )
    assert with_prepared.prepared_system is not None

    completed = process_queued_job(
        store,
        job.job_id,
        dry_run=False,
    )
    assert completed.run_result is not None
    result = completed.run_result

    assert result.validation_status.value == "technical_smoke_passed"
    roles = {artifact.role for artifact in result.artifacts}
    assert {"trajectory", "checkpoint", "state_log", "pymol_script"} <= roles
    assert result.analysis.free_energy_computed is False
    assert result.analysis.produced_metrics == [
        "temperature_kelvin",
        "potential_energy_kj_mol",
    ]

    attempt = store.attempt_directory(job.job_id, 1)
    (attempt / "analysis.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="产物 analysis.*SHA-256"):
        store.load(job.job_id)


def test_real_checkpoint_internal_step_must_match_audit_metadata(
    tmp_path: Path,
) -> None:
    active_preflight = preflight_openmm()
    openmm = pytest.importorskip("openmm")
    app = pytest.importorskip("openmm.app")

    store = MDJobStore(tmp_path / "md")
    job = _enqueue(store)
    system = openmm.System()
    system.addParticle(14.0)
    system.addParticle(12.0)
    bond = openmm.HarmonicBondForce()
    bond.addBond(
        0,
        1,
        0.1 * openmm.unit.nanometer,
        1000.0
        * openmm.unit.kilojoule_per_mole
        / openmm.unit.nanometer**2,
    )
    system.addForce(bond)
    prepared = _prepare(
        store,
        job.job_id,
        system_xml=openmm.XmlSerializer.serialize(system),
        summary=MDSystemSummary(
            particle_count=2,
            force_count=1,
            constraint_count=0,
            force_types=["HarmonicBondForce"],
            uses_periodic_boundary_conditions=False,
        ),
    )
    assert prepared.prepared_system is not None
    topology = app.PDBFile(prepared.prepared_system.topology_pdb_path)
    integrator = openmm.LangevinMiddleIntegrator(
        300 * openmm.unit.kelvin,
        1 / openmm.unit.picosecond,
        2 * openmm.unit.femtoseconds,
    )
    integrator.setRandomNumberSeed(job.manifest.protocol.seeds[0])
    simulation = app.Simulation(topology.topology, system, integrator)
    simulation.context.setPositions(topology.positions)
    simulation.context.setVelocitiesToTemperature(
        300 * openmm.unit.kelvin,
        job.manifest.protocol.seeds[0],
    )
    simulation.step(5)
    properties = _context_platform_properties(simulation.context)
    fingerprint = _execution_environment_fingerprint(
        preflight=active_preflight,
        backend_version=active_preflight.package_versions["openmm"],
        platform_name=simulation.context.getPlatform().getName(),
        platform_properties=properties,
    )
    checkpoint_payload = simulation.context.createCheckpoint()
    del simulation

    store.request_cancel(job.job_id)
    with_checkpoint = store.save_checkpoint(
        job.job_id,
        checkpoint_payload=checkpoint_payload,
        portable_state_payload=None,
        step=4,
        backend_version=active_preflight.package_versions["openmm"],
        hardware_fingerprint=fingerprint,
    )
    assert with_checkpoint.checkpoint is not None

    with pytest.raises(
        MDWorkerExecutionError,
        match="currentStep.*元数据",
    ):
        execute_prepared_openmm_smoke(
            job.manifest,
            prepared.prepared_system,
            original_inputs=prepared.original_inputs,
            output_directory=tmp_path / "mismatch-output",
            attempt_id="attempt-0001",
            preflight=active_preflight,
            checkpoint=with_checkpoint.checkpoint,
        )
