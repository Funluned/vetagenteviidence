from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import vetevidence.md_ui as md_ui
from vetevidence.md_worker import MDJobState, MDJobStore
from vetevidence.md_workflow import (
    MDChemistryConfirmation,
    MDInputSource,
    MDPreset,
    build_md_manifest,
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


def _manifest():
    return build_md_manifest(
        task_id="ui-stale-job",
        receptor_payload=RECEPTOR_PDB,
        receptor_source=MDInputSource(
            source_name="receptor.pdb",
            accession="PDB:TEST",
            version="1",
            format="pdb",
        ),
        ligand_payload=LIGAND_SDF,
        ligand_source=MDInputSource(
            source_name="ligand.sdf",
            accession="PubChem:TEST",
            version="1",
            format="sdf",
        ),
        chemistry_confirmation=MDChemistryConfirmation(
            reviewed_by="researcher",
            confirmed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            receptor_chain_selection=["A"],
            receptor_protonation_assumption="reviewed assumption",
            ligand_formal_charge=0,
            ligand_protonation_state="reviewed neutral",
            ligand_tautomer_state="reviewed tautomer",
            ligand_stereochemistry="reviewed achiral",
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
        ),
        preset=MDPreset.TECHNICAL_SMOKE,
    )


def test_public_renderer_has_integration_friendly_signature() -> None:
    signature = inspect.signature(md_ui.render_md_workbench)

    assert tuple(signature.parameters) == (
        "run_id",
        "audit_callback",
        "store_root",
    )
    assert signature.parameters["run_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_ui_can_only_launch_the_dedicated_worker() -> None:
    source = Path(md_ui.__file__).read_text(encoding="utf-8")

    assert "launch_md_worker(" in source
    assert "execute_prepared_openmm_smoke" not in source
    assert "process_queued_job" not in source
    assert "@st.fragment(run_every=\"2s\")" in source


def test_unknown_queued_mode_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        md_ui,
        "st",
        SimpleNamespace(session_state={}),
    )

    assert md_ui._queued_dry_run_default("run", "job") is True


def test_launch_failure_preserves_dry_run_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_state: dict[str, object] = {}
    monkeypatch.setattr(
        md_ui,
        "st",
        SimpleNamespace(session_state=session_state),
    )

    def fail_launch(*args: object, **kwargs: object) -> object:
        raise OSError("simulated Popen failure")

    monkeypatch.setattr(md_ui, "launch_md_worker", fail_launch)
    record = SimpleNamespace(job_id="queued-job")

    with pytest.raises(OSError, match="simulated Popen failure"):
        md_ui._launch_background_worker(
            store=SimpleNamespace(),
            record=record,
            run_id="run",
            dry_run=True,
            audit_callback=None,
        )

    assert session_state[md_ui._mode_key("run", "queued-job")] is True
    assert md_ui._process_key("run", "queued-job") not in session_state


def test_empty_md_workbench_renders_without_execution(
    tmp_path: Path,
) -> None:
    script = (
        "from vetevidence.md_ui import render_md_workbench\n"
        "render_md_workbench("
        "run_id='ui-test', "
        f"store_root=r'{tmp_path / 'md'}'"
        ")\n"
    )

    app = AppTest.from_string(script).run(timeout=20)

    assert not app.exception
    rendered_text = " ".join(
        item.value
        for collection in (
            app.header,
            app.subheader,
            app.info,
            app.warning,
            app.caption,
        )
        for item in collection
    )
    assert "分子动力学" in rendered_text
    assert "NVT/NPT" in rendered_text
    assert "尚无可核验的 MD 任务" in rendered_text


def test_workbench_startup_reconciles_dead_worker(
    tmp_path: Path,
) -> None:
    store = MDJobStore(tmp_path / "md")
    job = store.enqueue(
        _manifest(),
        receptor_payload=RECEPTOR_PDB,
        ligand_payload=LIGAND_SDF,
    )
    store.claim(job.job_id, worker_pid=2_147_483_647)
    script = (
        "from vetevidence.md_ui import render_md_workbench\n"
        "render_md_workbench("
        "run_id='ui-reconcile', "
        f"store_root=r'{store.root}'"
        ")\n"
    )

    app = AppTest.from_string(script).run(timeout=20)

    assert not app.exception
    reconciled = store.load(job.job_id)
    assert reconciled.state is MDJobState.FAILED
    assert "PID" in (reconciled.error or "")
    captions = " ".join(item.value for item in app.caption)
    assert "启动时已自动协调 1 个遗留 MD 任务" in captions
