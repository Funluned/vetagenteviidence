"""Streamlit UI for the receptor-gated, replicated docking workflow."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from vetevidence.docking_artifacts import DockingArtifactStore
from vetevidence.docking_ui_support import (
    build_ligand_batch_items,
    fetch_rcsb_pdb,
    ligand_metadata_template,
    parse_vina_seeds,
    validate_docking_workload,
)
from vetevidence.docking_visualization import (
    DockingVisualizationPackage,
    VerifiedExternalTool,
    build_visualization_package,
    launch_pymol_session,
    verify_plip_executable,
    verify_plip_runtime_environment,
    verify_pymol_executable,
)
from vetevidence.docking_workflow import (
    DockingBatchResult,
    DockingPocket,
    DockingRunSettings,
    ReceptorApproval,
    ReceptorIdentity,
    ReceptorPreparationAudit,
    ReceptorQCResult,
    ResidueIdentity,
    approve_receptor_for_docking,
    inspect_receptor_structure,
    run_docking_batch,
)
from vetevidence.openbabel_execution import (
    OpenBabelExecutionError,
    OpenBabelPreparationOptions,
    discover_openbabel,
    prepare_ligand_pdbqt,
)
from vetevidence.structure_viewer import (
    create_structure_viewer_component,
    decode_viewer_png_data_uri,
    load_local_3dmol_library,
    mount_structure_viewer,
)
from vetevidence.vina_execution import (
    VinaExecutionError,
    discover_vina,
    execute_vina,
)


_THREEDMOL_SHA256 = (
    "95513f6494717cc82fb2ba4d264f29b7ef189a31d4ece36a38d1f9666bf6d427"
)
_ASSET_PATH = (
    Path(__file__).parent
    / "assets"
    / "vendor"
    / "3dmol"
    / "3Dmol.es6-min.js"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_STORE = DockingArtifactStore()

AuditCallback = Callable[..., None]


def _state_key(run_id: str, suffix: str) -> str:
    return f"vetevidence-docking::{run_id}::{suffix}"


def _record(
    callback: AuditCallback | None,
    *,
    tool_name: str,
    input_summary: str,
    status: str,
    output_summary: str | None = None,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    if callback is None:
        return
    callback(
        tool_name=tool_name,
        input_summary=input_summary,
        status=status,
        output_summary=output_summary,
        error=error,
        metadata=metadata,
    )


def _load_receptor_state(run_id: str) -> dict[str, object] | None:
    payload = st.session_state.get(_state_key(run_id, "receptor"))
    if not isinstance(payload, dict):
        return None
    try:
        state = dict(payload)
        state["qc"] = ReceptorQCResult.model_validate(state["qc"])
        state["identity"] = ReceptorIdentity.model_validate(state["identity"])
        if state.get("approval") is not None:
            state["approval"] = ReceptorApproval.model_validate(
                state["approval"]
            )
        if not isinstance(state.get("original_payload"), bytes):
            raise TypeError("original_payload")
        if not isinstance(state.get("receptor_pdbqt"), bytes):
            raise TypeError("receptor_pdbqt")
        return state
    except (KeyError, TypeError, ValidationError, ValueError):
        st.session_state.pop(_state_key(run_id, "receptor"), None)
        return None


def _save_receptor_state(run_id: str, state: dict[str, object]) -> None:
    serializable = dict(state)
    for field in ("qc", "identity", "approval"):
        value = serializable.get(field)
        if hasattr(value, "model_dump"):
            serializable[field] = value.model_dump(mode="python")
    st.session_state[_state_key(run_id, "receptor")] = serializable


def _clear_downstream_state(run_id: str, *suffixes: str) -> None:
    for suffix in suffixes:
        st.session_state.pop(_state_key(run_id, suffix), None)


def _load_batch(run_id: str) -> DockingBatchResult | None:
    payload = st.session_state.get(_state_key(run_id, "batch"))
    if payload is None:
        return None
    try:
        return DockingBatchResult.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        st.session_state.pop(_state_key(run_id, "batch"), None)
        return None


def _save_batch(run_id: str, batch: DockingBatchResult) -> None:
    _clear_downstream_state(run_id, "visualization")
    st.session_state[_state_key(run_id, "batch")] = batch.model_dump(
        mode="python"
    )


def _residue_label(residue: ResidueIdentity) -> str:
    insertion = residue.insertion_code or "-"
    return (
        f"model={residue.model_id} chain={residue.chain_id} "
        f"{residue.residue_name}:{residue.residue_number} ins={insertion}"
    )


def _parse_residue_basis(value: str) -> tuple[ResidueIdentity, ...]:
    residues: list[ResidueIdentity] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) not in {4, 5}:
            raise ValueError(
                "口袋依据残基每行必须是 model,chain,resname,resnum[,insertion]；"
                f"第 {line_number} 行格式错误。"
            )
        residues.append(
            ResidueIdentity(
                model_id=parts[0],
                chain_id=parts[1],
                residue_name=parts[2],
                residue_number=parts[3],
                insertion_code=parts[4] if len(parts) == 5 else "",
            )
        )
    return tuple(residues)


def _selected_residues(
    selected_labels: Sequence[str],
    available: Sequence[ResidueIdentity],
) -> tuple[ResidueIdentity, ...]:
    by_label = {_residue_label(item): item for item in available}
    return tuple(by_label[label] for label in selected_labels)


def _default_external_tool_path(environment_key: str, executable: str) -> str:
    configured = os.environ.get(environment_key, "").strip()
    if configured:
        return configured
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        return ""
    candidate = (
        Path(local_appdata)
        / "VetEvidence"
        / "tools"
        / "pymol-open-source-3.0"
        / "Scripts"
        / executable
    )
    return str(candidate) if candidate.is_file() else ""


def _default_plip_runtime_directory(
    *,
    environment_key: str,
    plip_path: str,
    directory_type: str,
) -> str:
    configured = os.environ.get(environment_key, "").strip()
    if configured:
        return configured
    if not plip_path.strip():
        return ""
    try:
        environment_root = Path(plip_path).expanduser().resolve().parent.parent
    except (OSError, RuntimeError):
        return ""
    if directory_type == "lib":
        candidate = environment_root / "Library" / "bin"
        return str(candidate) if candidate.is_dir() else ""
    openbabel_root = environment_root / "Library" / "share" / "openbabel"
    if not openbabel_root.is_dir():
        return ""
    candidates = sorted(
        (
            item
            for item in openbabel_root.iterdir()
            if item.is_dir()
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    return str(candidates[0]) if candidates else ""


def _render_receptor_qc(qc: ReceptorQCResult) -> None:
    metrics = st.columns(5)
    metrics[0].metric("模型", qc.model_count)
    metrics[1].metric("链", len(qc.chains))
    metrics[2].metric("聚合物原子", qc.polymer_atom_count)
    metrics[3].metric("异源原子", qc.hetero_atom_count)
    metrics[4].metric("altloc 原子", qc.alternate_location_atom_count)
    st.caption(
        f"原始结构 SHA-256：{qc.sha256} · 格式：{qc.structure_format} · "
        f"大小：{qc.size_bytes:,} bytes"
    )
    for issue in qc.blocking_issues:
        st.error(issue)
    for warning in qc.warnings:
        st.warning(warning)
    if qc.water_residues or qc.heterogen_residues or qc.metal_residues:
        st.dataframe(
            [
                {
                    "类别": category,
                    "model": residue.model_id,
                    "chain": residue.chain_id,
                    "residue": residue.residue_name,
                    "number": residue.residue_number,
                    "insertion": residue.insertion_code,
                }
                for category, residues in (
                    ("water", qc.water_residues),
                    ("heterogen", qc.heterogen_residues),
                    ("metal", qc.metal_residues),
                )
                for residue in residues
            ],
            hide_index=True,
            width="stretch",
        )


def _render_receptor_source(
    run_id: str,
    audit_callback: AuditCallback | None,
) -> dict[str, object] | None:
    st.subheader("1 受体来源与质量检查")
    st.caption(
        "原始 PDB/mmCIF 与准备后 PDBQT 必须同时保留；系统不会自动猜链、"
        "口袋、altloc、水、金属或辅因子处理方式。"
    )
    with st.form(f"docking-receptor-source-{run_id}"):
        source_mode = st.radio(
            "原始受体来源",
            ["从 RCSB 下载 PDB", "上传 RCSB PDB/mmCIF"],
            horizontal=True,
        )
        identity_columns = st.columns(3)
        pdb_id = identity_columns[0].text_input("PDB ID *", value="1IEP")
        ncbi_taxid = identity_columns[1].number_input(
            "NCBI TaxID *",
            min_value=1,
            value=10090,
            step=1,
        )
        target_name = identity_columns[2].text_input(
            "受体/靶标名称 *",
            value="ABL1 kinase domain",
        )
        organism = st.text_input("物种 *", value="Mus musculus")
        uniprot_ids = st.text_input(
            "UniProt ID（逗号分隔，可空）",
            value="P00520",
        )
        uploaded_original = None
        source_revision = ""
        if source_mode == "上传 RCSB PDB/mmCIF":
            uploaded_original = st.file_uploader(
                "原始受体 PDB/mmCIF *",
                type=["pdb", "cif", "mmcif"],
                max_upload_size=50,
            )
            source_revision = st.text_input(
                "RCSB 修订/下载版本 *",
                placeholder="例如 2026-07-30 或 RCSB revision 3",
            )
        receptor_pdbqt_file = st.file_uploader(
            "准备后受体 PDBQT *",
            type=["pdbqt"],
            max_upload_size=50,
            help="必须由研究者检查，且重原子需能映射回所选原始受体。",
        )
        inspect_submitted = st.form_submit_button(
            "获取并检查受体",
            type="primary",
            width="stretch",
        )

    state = _load_receptor_state(run_id)
    if inspect_submitted:
        _clear_downstream_state(
            run_id,
            "receptor",
            "batch",
            "visualization",
        )
        state = None
        input_summary = f"PDB:{pdb_id.strip().upper()}"
        try:
            if receptor_pdbqt_file is None:
                raise ValueError("必须上传准备后的受体 PDBQT。")
            if source_mode == "从 RCSB 下载 PDB":
                downloaded = fetch_rcsb_pdb(pdb_id)
                original_payload = downloaded.payload
                original_filename = downloaded.filename
                structure_source_url = downloaded.source_url
                revision = downloaded.source_revision
            else:
                if uploaded_original is None:
                    raise ValueError("必须上传原始受体 PDB/mmCIF。")
                original_payload = uploaded_original.getvalue()
                original_filename = uploaded_original.name
                normalized_pdb_id = pdb_id.strip().upper()
                structure_source_url = (
                    f"https://www.rcsb.org/structure/{normalized_pdb_id}"
                )
                revision = source_revision
            qc = inspect_receptor_structure(
                original_payload,
                filename=original_filename,
            )
            identity = ReceptorIdentity(
                pdb_id=pdb_id,
                ncbi_taxid=int(ncbi_taxid),
                target_name=target_name,
                organism=organism,
                source_url=structure_source_url,
                revision=revision,
                raw_structure_sha256=qc.sha256,
                uniprot_ids=tuple(
                    item.strip()
                    for item in uniprot_ids.replace("；", ",").split(",")
                    if item.strip()
                ),
            )
            state = {
                "original_payload": original_payload,
                "original_filename": original_filename,
                "receptor_pdbqt": receptor_pdbqt_file.getvalue(),
                "qc": qc,
                "identity": identity,
                "approval": None,
            }
            _clear_downstream_state(run_id, "batch", "visualization")
            _save_receptor_state(run_id, state)
        except Exception as exc:
            _record(
                audit_callback,
                tool_name="docking.receptor_qc",
                input_summary=input_summary,
                status="failed",
                error=str(exc),
            )
            st.error(f"受体质量检查失败：{exc}")
        else:
            _record(
                audit_callback,
                tool_name="docking.receptor_qc",
                input_summary=input_summary,
                status="succeeded",
                output_summary=(
                    f"{qc.model_count} 模型、{len(qc.chains)} 链；"
                    f"{len(qc.blocking_issues)} 个阻断项"
                ),
                metadata={
                    "pdb_id": identity.pdb_id,
                    "ncbi_taxid": identity.ncbi_taxid,
                    "raw_structure_sha256": qc.sha256,
                },
            )
            st.success("受体原始结构与身份已保存；请逐项完成人工门禁。")

    if state is not None:
        _render_receptor_qc(state["qc"])  # type: ignore[arg-type]
    return state


def _policy_options(
    inventory: Sequence[ResidueIdentity],
) -> list[str]:
    return ["not_present"] if not inventory else ["remove_all", "retain_explicit"]


def _render_receptor_approval(
    run_id: str,
    state: dict[str, object] | None,
    audit_callback: AuditCallback | None,
) -> dict[str, object] | None:
    st.subheader("2 人工确认受体与口袋")
    if state is None:
        st.info("请先完成受体来源与 QC。")
        return None
    qc = state["qc"]
    identity = state["identity"]
    assert isinstance(qc, ReceptorQCResult)
    assert isinstance(identity, ReceptorIdentity)
    with st.form(f"docking-receptor-approval-{run_id}"):
        selection_columns = st.columns(2)
        selected_model = selection_columns[0].selectbox(
            "模型 *",
            list(qc.model_ids),
        )
        selected_chains = selection_columns[1].multiselect(
            "受体链 *",
            list(qc.chains),
            default=list(qc.chains[:1]),
        )
        altloc_policy = st.selectbox(
            "alternate location 策略",
            (
                ["not_present"]
                if not qc.alternate_locations
                else ["highest_occupancy", "explicit"]
            ),
        )
        selected_altlocs = st.multiselect(
            "保留的 altloc 标识",
            list(qc.alternate_locations),
            disabled=altloc_policy != "explicit",
        )

        policy_columns = st.columns(3)
        water_policy = policy_columns[0].selectbox(
            "水分子策略",
            _policy_options(qc.water_residues),
        )
        heterogen_policy = policy_columns[1].selectbox(
            "其他异源物策略",
            _policy_options(qc.heterogen_residues),
        )
        metal_policy = policy_columns[2].selectbox(
            "金属策略",
            _policy_options(qc.metal_residues),
        )
        retained_waters = st.multiselect(
            "明确保留的水",
            [_residue_label(item) for item in qc.water_residues],
            disabled=water_policy != "retain_explicit",
        )
        retained_heterogens = st.multiselect(
            "明确保留的异源物/辅因子",
            [_residue_label(item) for item in qc.heterogen_residues],
            disabled=heterogen_policy != "retain_explicit",
        )
        retained_metals = st.multiselect(
            "明确保留的金属",
            [_residue_label(item) for item in qc.metal_residues],
            disabled=metal_policy != "retain_explicit",
        )

        st.markdown("**搜索框与依据**")
        pocket_columns = st.columns(3)
        center_x = pocket_columns[0].number_input(
            "center_x", value=15.190, format="%.3f"
        )
        center_y = pocket_columns[1].number_input(
            "center_y", value=53.903, format="%.3f"
        )
        center_z = pocket_columns[2].number_input(
            "center_z", value=16.917, format="%.3f"
        )
        size_columns = st.columns(3)
        size_x = size_columns[0].number_input(
            "size_x (Å)", min_value=1.0, max_value=60.0, value=20.0
        )
        size_y = size_columns[1].number_input(
            "size_y (Å)", min_value=1.0, max_value=60.0, value=20.0
        )
        size_z = size_columns[2].number_input(
            "size_z (Å)", min_value=1.0, max_value=60.0, value=20.0
        )
        basis_type = st.selectbox(
            "口袋依据类型",
            ["manual", "co_crystal", "residue_selection"],
        )
        selection_basis = st.text_input(
            "口袋依据说明 *",
            value="研究者依据共晶配体或已核查残基手工确认",
        )
        basis_residue_text = st.text_area(
            "依据残基（共晶/残基选择时必填）",
            placeholder="每行：model,chain,resname,resnum[,insertion]",
            disabled=basis_type == "manual",
        )

        st.markdown("**受体准备审计**")
        preparation_method = st.selectbox(
            "准备记录方式",
            ["user_provided", "external_tool"],
        )
        preparation_columns = st.columns(2)
        preparation_tool = preparation_columns[0].text_input(
            "准备工具/流程 *",
            value="user-provided receptor PDBQT",
        )
        preparation_version = preparation_columns[1].text_input(
            "工具版本/流程版本 *",
            value="not-reported",
        )
        preparation_arguments = st.text_input(
            "参数（逐项以 | 分隔）",
            value="",
        )
        preparation_executable_sha256 = st.text_input(
            "准备工具可执行文件 SHA-256",
            disabled=preparation_method != "external_tool",
        )
        reviewer = st.text_input("复核人/角色 *", value="researcher")
        confirmed = st.checkbox(
            "我已核对模型、链、altloc、水、异源物、金属、准备方法和口袋；"
            "这些选择将与文件哈希一起冻结。"
        )
        approval_submitted = st.form_submit_button(
            "冻结受体人工审批",
            type="primary",
            width="stretch",
        )

    if approval_submitted:
        state["approval"] = None
        _clear_downstream_state(run_id, "batch", "visualization")
        _save_receptor_state(run_id, state)
        input_summary = f"PDB:{identity.pdb_id}"
        try:
            pocket = DockingPocket(
                center_x=center_x,
                center_y=center_y,
                center_z=center_z,
                size_x=size_x,
                size_y=size_y,
                size_z=size_z,
                basis_type=basis_type,
                basis_residues=_parse_residue_basis(basis_residue_text),
                selection_basis=selection_basis,
                source_structure_sha256=qc.sha256,
            )
            preparation_audit = ReceptorPreparationAudit(
                method=preparation_method,
                tool=preparation_tool,
                version=preparation_version,
                arguments=tuple(
                    item.strip()
                    for item in preparation_arguments.split("|")
                    if item.strip()
                ),
                executable_sha256=(
                    preparation_executable_sha256.strip() or None
                ),
            )
            approval = approve_receptor_for_docking(
                qc,
                state["original_payload"],  # type: ignore[arg-type]
                state["receptor_pdbqt"],  # type: ignore[arg-type]
                identity=identity,
                selected_model=selected_model,
                selected_chains=selected_chains,
                alternate_location_policy=altloc_policy,
                selected_alternate_locations=selected_altlocs,
                water_policy=water_policy,
                retained_waters=_selected_residues(
                    retained_waters,
                    qc.water_residues,
                ),
                heterogen_policy=heterogen_policy,
                retained_heterogens=_selected_residues(
                    retained_heterogens,
                    qc.heterogen_residues,
                ),
                metal_policy=metal_policy,
                retained_metals=_selected_residues(
                    retained_metals,
                    qc.metal_residues,
                ),
                preparation_audit=preparation_audit,
                pocket=pocket,
                reviewer=reviewer,
                user_confirmed=confirmed,
            )
            state["approval"] = approval
            _clear_downstream_state(run_id, "batch", "visualization")
            _save_receptor_state(run_id, state)
        except Exception as exc:
            _record(
                audit_callback,
                tool_name="docking.receptor_approval",
                input_summary=input_summary,
                status="failed",
                error=str(exc),
            )
            st.error(f"受体人工审批未通过：{exc}")
        else:
            _record(
                audit_callback,
                tool_name="docking.receptor_approval",
                input_summary=input_summary,
                status="succeeded",
                output_summary=(
                    f"模型 {approval.selected_model}；"
                    f"链 {','.join(approval.selected_chains)}；"
                    f"重原子映射 {approval.heavy_atom_match_fraction:.1%}"
                ),
                metadata={
                    "receptor_pdbqt_sha256": approval.receptor_pdbqt_sha256,
                    "selected_receptor_pdb_sha256": (
                        approval.selected_receptor_pdb_sha256
                    ),
                },
            )
            st.success("受体人工审批已冻结；文件或选择变化后必须重新审批。")

    approval_value = state.get("approval")
    if isinstance(approval_value, ReceptorApproval):
        st.success(
            f"已审批：模型 {approval_value.selected_model} · "
            f"链 {', '.join(approval_value.selected_chains)} · "
            f"重原子对应率 {approval_value.heavy_atom_match_fraction:.1%} · "
            f"最大坐标差 {approval_value.maximum_heavy_atom_coordinate_delta:.3f} Å"
        )
    return state


def _discover_local_tools() -> tuple[Any | None, str | None, Any | None, str | None]:
    try:
        vina = discover_vina()
        vina_error = None
    except VinaExecutionError as exc:
        vina = None
        vina_error = str(exc)
    try:
        openbabel = discover_openbabel(project_root=_PROJECT_ROOT)
        openbabel_error = None
    except OpenBabelExecutionError as exc:
        openbabel = None
        openbabel_error = str(exc)
    return vina, vina_error, openbabel, openbabel_error


def _render_batch_results(batch: DockingBatchResult) -> None:
    st.dataframe(
        [
            {
                "配体": item.compound_name,
                "ligand_id": item.ligand_id,
                "身份": item.identity.canonical_accession,
                "准备状态": item.status,
                "原始 SHA-256": item.original_sha256,
                "PDBQT SHA-256": item.prepared_pdbqt_sha256 or "",
                "错误": item.error or "",
            }
            for item in batch.preparations
        ],
        hide_index=True,
        width="stretch",
    )
    st.dataframe(
        [
            {
                "ligand_id": attempt.ligand_id,
                "seed": attempt.seed,
                "状态": attempt.status,
                "Vina 预测评分": (
                    attempt.docking_run.best_affinity_kcal_mol
                    if attempt.docking_run is not None
                    else None
                ),
                "任务 ID": attempt.task_id,
                "错误": attempt.error or "",
            }
            for attempt in batch.attempts
        ],
        hide_index=True,
        width="stretch",
    )
    st.dataframe(
        [
            {
                "ligand_id": item.ligand_id,
                "成功 seed": item.successful_seed_count,
                "失败/跳过 seed": item.failed_or_skipped_seed_count,
                "最优": item.minimum_score_kcal_mol,
                "均值": item.mean_score_kcal_mol,
                "中位数": item.median_score_kcal_mol,
                "总体标准差": item.population_sd_kcal_mol,
                "极差": item.score_range_kcal_mol,
                "解释": item.assessment,
            }
            for item in batch.stability
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "多 seed 统计只描述 Vina 预测评分离散程度；系统没有进行跨 seed "
        "原子映射或构象对齐，因此不报告跨 seed pose RMSD。"
    )


def _render_docking_batch(
    run_id: str,
    state: dict[str, object] | None,
    audit_callback: AuditCallback | None,
) -> DockingBatchResult | None:
    st.subheader("3 批量配体与多随机种子")
    approval = state.get("approval") if state else None
    vina, vina_error, openbabel, openbabel_error = _discover_local_tools()
    if vina is None:
        st.error(f"AutoDock Vina 不可用：{vina_error}")
    else:
        st.success(
            f"AutoDock Vina {vina.version} · 可执行文件 SHA-256 "
            f"{vina.sha256}"
        )
    if openbabel is None:
        st.caption(f"Open Babel 未连接：{openbabel_error}")
    else:
        st.caption(
            f"Open Babel {openbabel.version} 可用于非 PDBQT 配体基础准备；"
            "仍须人工复核键级、立体化学、互变异构体和质子化。"
        )
    st.download_button(
        "下载配体身份 CSV 模板",
        data=ligand_metadata_template(),
        file_name="docking_ligand_identity_template.csv",
        mime="text/csv",
        width="stretch",
    )
    with st.form(f"docking-batch-{run_id}"):
        ligand_files = st.file_uploader(
            "配体文件（可多选） *",
            type=["pdbqt", "sdf", "mol", "mol2", "smi", "smiles", "pdb"],
            accept_multiple_files=True,
            max_upload_size=25,
        )
        metadata_file = st.file_uploader(
            "配体身份 CSV *",
            type=["csv"],
            max_upload_size=1,
        )
        seeds_text = st.text_input(
            "Vina seeds *",
            value="42, 137, 2026",
            help="逗号、分号或空格分隔；单批最多 12 个。",
        )
        settings_columns = st.columns(3)
        exhaustiveness = settings_columns[0].number_input(
            "exhaustiveness",
            min_value=1,
            max_value=32,
            value=8,
            step=1,
        )
        num_modes = settings_columns[1].number_input(
            "num_modes",
            min_value=1,
            max_value=50,
            value=9,
            step=1,
        )
        energy_range = settings_columns[2].number_input(
            "energy_range",
            min_value=0.1,
            max_value=20.0,
            value=3.0,
            step=0.5,
        )
        raw_columns = st.columns(2)
        generate_3d = raw_columns[0].checkbox(
            "非 PDBQT 配体由 Open Babel 生成/重建 3D",
            value=True,
        )
        protonation_ph = raw_columns[1].number_input(
            "非 PDBQT 配体质子化 pH",
            min_value=0.0,
            max_value=14.0,
            value=7.4,
            step=0.1,
        )
        synchronous_confirmed = st.checkbox(
            "我确认本批次会在当前本机页面线程中顺序执行；"
            "系统将限制为最多 24 次尝试和 384 工作单位。"
        )
        run_submitted = st.form_submit_button(
            "运行批量 Vina",
            type="primary",
            width="stretch",
            disabled=(
                not isinstance(approval, ReceptorApproval)
                or vina is None
                or not synchronous_confirmed
            ),
        )

    batch = _load_batch(run_id)
    if run_submitted:
        _clear_downstream_state(run_id, "batch", "visualization")
        batch = None
        input_summary = (
            f"{len(ligand_files or [])} ligand files · seeds={seeds_text}"
        )
        try:
            if state is None or not isinstance(approval, ReceptorApproval):
                raise ValueError("受体尚未通过人工审批。")
            if vina is None:
                raise ValueError("本机 AutoDock Vina 不可用。")
            if metadata_file is None:
                raise ValueError("必须上传配体身份 CSV。")
            uploads = ligand_files or []
            names = [item.name for item in uploads]
            if len(names) != len(set(names)):
                raise ValueError("上传的配体文件名不能重复。")
            ligands = build_ligand_batch_items(
                {item.name: item.getvalue() for item in uploads},
                metadata_file.getvalue(),
            )
            seeds = parse_vina_seeds(seeds_text)
            workload = validate_docking_workload(
                ligands,
                seeds,
                exhaustiveness=int(exhaustiveness),
            )
            if any(item.input_format != "pdbqt" for item in ligands):
                if openbabel is None:
                    raise ValueError(
                        "批次含非 PDBQT 配体，但本机 Open Babel 不可用。"
                    )

                def ligand_preparer(item: Any) -> Any:
                    return prepare_ligand_pdbqt(
                        item.original_payload,
                        options=OpenBabelPreparationOptions(
                            input_format=item.input_format,
                            generate_3d=generate_3d,
                            protonation_ph=float(protonation_ph),
                        ),
                        executable=openbabel,
                    )

            else:
                ligand_preparer = None

            def vina_executor(
                manifest: Any,
                ligand_pdbqt: bytes,
                receptor_pdbqt: bytes,
            ) -> Any:
                return execute_vina(
                    manifest,
                    ligand_pdbqt,
                    receptor_pdbqt,
                    executable=vina,
                )

            status_box = st.status(
                "正在运行受控 Vina 批次…",
                expanded=True,
            )
            status_box.write("正在复核受体批准、配体身份与文件哈希。")
            batch = run_docking_batch(
                batch_id=f"dock-{uuid4().hex[:16]}",
                ligands=ligands,
                seeds=seeds,
                receptor_original_filename=str(state["original_filename"]),
                receptor_original_payload=state["original_payload"],  # type: ignore[arg-type]
                receptor_pdbqt=state["receptor_pdbqt"],  # type: ignore[arg-type]
                receptor_qc=state["qc"],  # type: ignore[arg-type]
                receptor_approval=approval,
                receptor_identity=state["identity"],  # type: ignore[arg-type]
                engine_version=vina.version,
                settings=DockingRunSettings(
                    exhaustiveness=int(exhaustiveness),
                    num_modes=int(num_modes),
                    energy_range=float(energy_range),
                ),
                ligand_preparer=ligand_preparer,
                vina_executor=vina_executor,
            )
            _save_batch(run_id, batch)
            succeeded = sum(
                item.status == "succeeded" for item in batch.attempts
            )
            failed = len(batch.attempts) - succeeded
            status_box.update(
                label=f"批量 Vina 完成：{succeeded} 成功，{failed} 失败/跳过",
                state="complete" if succeeded else "error",
                expanded=False,
            )
        except Exception as exc:
            _record(
                audit_callback,
                tool_name="docking.batch_execute",
                input_summary=input_summary,
                status="failed",
                error=str(exc),
            )
            st.error(f"批量对接失败：{exc}")
        else:
            outcome = (
                "failed"
                if succeeded == 0
                else "partial"
                if failed
                else "complete"
            )
            _record(
                audit_callback,
                tool_name="docking.batch_execute",
                input_summary=input_summary,
                status="failed" if succeeded == 0 else "succeeded",
                output_summary=(
                    f"{succeeded} 成功，{failed} 失败/跳过；"
                    "结果为 computational_prediction"
                ),
                error=(
                    "批次没有任何成功的 Vina 尝试。"
                    if succeeded == 0
                    else None
                ),
                metadata={
                    "batch_id": batch.batch_id,
                    "outcome": outcome,
                    "succeeded": succeeded,
                    "failed_or_skipped": failed,
                    "attempt_count": workload.attempt_count,
                    "work_units": workload.work_units,
                    "ligand_upload_bytes": workload.ligand_upload_bytes,
                    "receptor_approval_sha256": (
                        hashlib.sha256(
                            approval.model_dump_json(
                                exclude={"selected_receptor_pdb"}
                            ).encode("utf-8")
                        ).hexdigest()
                    ),
                },
            )
            boundary = "Vina 预测评分不是实验结合能或自由能。"
            if succeeded == 0:
                st.error(f"批次没有成功结果；失败记录已保留。{boundary}")
            elif failed:
                st.warning(
                    f"批次部分完成：{succeeded} 成功，{failed} 失败/跳过；"
                    f"全部尝试均已保留。{boundary}"
                )
            else:
                st.success(f"批次全部完成并保留执行审计。{boundary}")
    if batch is not None:
        _render_batch_results(batch)
    return batch


def _download_optional_artifact(artifact: Any, *, key: str) -> None:
    if (
        artifact.status in {"available", "generated_unverified"}
        and artifact.payload is not None
        and artifact.filename is not None
        and artifact.media_type is not None
    ):
        label = (
            f"下载 {artifact.artifact}"
            if artifact.status == "available"
            else f"下载 {artifact.artifact}（工具生成，未验证）"
        )
        st.download_button(
            label,
            data=artifact.payload,
            file_name=artifact.filename,
            mime=artifact.media_type,
            key=key,
            width="stretch",
        )
    else:
        st.caption(f"{artifact.artifact}：{artifact.reason}")


def _render_visualization(
    run_id: str,
    batch: DockingBatchResult | None,
    audit_callback: AuditCallback | None,
) -> None:
    st.subheader("4 3D 查看、对接图与 PyMOL")
    if batch is None:
        st.info("请先完成至少一个 Vina 尝试。")
        return
    successful = [
        item
        for item in batch.attempts
        if item.status == "succeeded" and item.docking_run is not None
    ]
    if not successful:
        st.warning("当前批次没有可视化所需的完整成功 Vina 产物。")
        return
    labels = [
        (
            f"{item.ligand_id} · seed {item.seed} · "
            f"best {item.docking_run.best_affinity_kcal_mol:g} kcal/mol"
        )
        for item in successful
    ]
    selected_label = st.selectbox("成功任务", labels)
    selected_attempt = successful[labels.index(selected_label)]
    assert selected_attempt.docking_run is not None
    modes = [pose.mode for pose in selected_attempt.docking_run.poses]
    pose_mode = st.selectbox("pose mode", modes)
    selected_pose = next(
        pose
        for pose in selected_attempt.docking_run.poses
        if pose.mode == pose_mode
    )

    with st.form(f"docking-visualization-{run_id}"):
        external_confirmed = st.checkbox(
            "本次明确允许系统探测并运行已配置的 PyMOL/PLIP",
            help=(
                "不勾选仍会生成浏览器 3D、complex.pdb、可编辑 PML 和任务 ZIP；"
                "不会启动任何外部可执行文件。"
            ),
        )
        tool_columns = st.columns(2)
        pymol_path = tool_columns[0].text_input(
            "PyMOL 可执行文件",
            value=_default_external_tool_path(
                "PYMOL_EXECUTABLE",
                "pymol.exe",
            ),
            disabled=not external_confirmed,
        )
        plip_path = tool_columns[1].text_input(
            "PLIP 可执行文件",
            value=_default_external_tool_path(
                "PLIP_EXECUTABLE",
                "plip.exe",
            ),
            disabled=not external_confirmed,
        )
        runtime_columns = st.columns(2)
        plip_babel_libdir = runtime_columns[0].text_input(
            "PLIP 的 BABEL_LIBDIR",
            value=_default_plip_runtime_directory(
                environment_key="PLIP_BABEL_LIBDIR",
                plip_path=plip_path,
                directory_type="lib",
            ),
            disabled=not external_confirmed or not plip_path.strip(),
            help="必须是 Open Babel 动态库和格式插件所在目录；目录标志文件会写入审计。",
        )
        plip_babel_datadir = runtime_columns[1].text_input(
            "PLIP 的 BABEL_DATADIR",
            value=_default_plip_runtime_directory(
                environment_key="PLIP_BABEL_DATADIR",
                plip_path=plip_path,
                directory_type="data",
            ),
            disabled=not external_confirmed or not plip_path.strip(),
            help="必须是 Open Babel 数据文件目录；PLIP 子进程不会继承任意 PATH。",
        )
        visualization_submitted = st.form_submit_button(
            "生成可追溯可视化任务包",
            type="primary",
            width="stretch",
        )

    visual_key = _state_key(run_id, "visualization")
    if visualization_submitted:
        _clear_downstream_state(run_id, "visualization")
        input_summary = (
            f"{selected_attempt.ligand_id} · seed={selected_attempt.seed} · "
            f"mode={pose_mode}"
        )
        try:
            if external_confirmed:
                pymol_tool = verify_pymol_executable(
                    pymol_path or None,
                    user_confirmed=True,
                )
                plip_runtime = (
                    verify_plip_runtime_environment(
                        babel_libdir=plip_babel_libdir,
                        babel_datadir=plip_babel_datadir,
                    )
                    if plip_path.strip()
                    else None
                )
                plip_tool = verify_plip_executable(
                    plip_path or None,
                    user_confirmed=True,
                    runtime_environment=plip_runtime,
                )
            else:
                pymol_tool = None
                plip_tool = None
            package = build_visualization_package(
                batch=batch,
                ligand_id=selected_attempt.ligand_id,
                seed=selected_attempt.seed,
                pose_mode=int(pose_mode),
                user_confirmed_external_tools=external_confirmed,
                pymol_tool=pymol_tool,
                plip_tool=plip_tool,
            )
            stored = _ARTIFACT_STORE.save(
                run_id=run_id,
                batch_id=batch.batch_id,
                ligand_id=selected_attempt.ligand_id,
                seed=selected_attempt.seed,
                pose_mode=int(pose_mode),
                package=package,
            )
            st.session_state[visual_key] = {
                "package": package.model_dump(mode="python"),
                "artifact_directory": str(stored.directory),
                "artifact_id": stored.metadata.artifact_id,
                "batch_id": batch.batch_id,
                "pymol_tool": (
                    pymol_tool.model_dump(mode="python")
                    if pymol_tool is not None
                    else None
                ),
                "ligand_id": selected_attempt.ligand_id,
                "seed": selected_attempt.seed,
                "pose_mode": int(pose_mode),
                "score": selected_pose.affinity_kcal_mol,
            }
        except Exception as exc:
            _record(
                audit_callback,
                tool_name="docking.visualization_package",
                input_summary=input_summary,
                status="failed",
                error=str(exc),
            )
            st.error(f"可视化任务包生成失败：{exc}")
        else:
            _record(
                audit_callback,
                tool_name="docking.visualization_package",
                input_summary=input_summary,
                status="succeeded",
                output_summary=(
                    f"ZIP {package.zip_sha256[:12]}…；"
                    f"PyMOL PNG {package.pymol_render.png.status}；"
                    f"PLIP XML {package.plip_analysis.xml.status}"
                ),
                metadata={
                    "artifact_id": stored.metadata.artifact_id,
                    "zip_sha256": package.zip_sha256,
                    "complex_pdb_sha256": package.complex_pdb_sha256,
                    "pml_sha256": package.pml_sha256,
                    "metadata_sha256": hashlib.sha256(
                        (stored.directory / "metadata.json").read_bytes()
                    ).hexdigest(),
                },
            )
            st.success("可视化任务包已原子保存并复核 SHA-256。")

    visual_state = st.session_state.get(visual_key)
    if not isinstance(visual_state, dict):
        return
    try:
        if str(visual_state["batch_id"]) != batch.batch_id:
            raise ValueError("可视化状态不属于当前批次。")
        package = DockingVisualizationPackage.model_validate(
            visual_state["package"]
        )
        artifact_directory = Path(
            str(visual_state["artifact_directory"])
        ).resolve(strict=True)
    except (KeyError, OSError, ValidationError, ValueError):
        st.session_state.pop(visual_key, None)
        st.warning("上次可视化会话状态已失效，请重新生成任务包。")
        return

    st.download_button(
        "下载完整对接可视化 ZIP",
        data=package.zip_payload,
        file_name=f"{visual_state['artifact_id']}.zip",
        mime="application/zip",
        width="stretch",
    )
    downloads = st.columns(2)
    downloads[0].download_button(
        "下载可编辑 PyMOL PML",
        data=package.file_payload("view.pml"),
        file_name="view.pml",
        mime="text/plain",
        width="stretch",
    )
    downloads[1].download_button(
        "下载复合物 PDB",
        data=package.file_payload("complex.pdb"),
        file_name="complex.pdb",
        mime="chemical/x-pdb",
        width="stretch",
    )
    _download_optional_artifact(
        package.pymol_render.png,
        key=f"pymol-png-{visual_state['artifact_id']}",
    )
    _download_optional_artifact(
        package.pymol_render.pse,
        key=f"pymol-pse-{visual_state['artifact_id']}",
    )
    _download_optional_artifact(
        package.plip_analysis.xml,
        key=f"plip-xml-{visual_state['artifact_id']}",
    )
    _download_optional_artifact(
        package.plip_analysis.png,
        key=f"plip-png-{visual_state['artifact_id']}",
    )
    st.caption(
        "PDBQT→PDB 会丢失或猜测部分键级与电荷；PLIP 输出只能视为启发式"
        "计算相互作用，不能替代原始化学结构复核或实验。"
    )

    st.markdown("**本地 3Dmol.js 查看器**")
    try:
        library = load_local_3dmol_library(
            _ASSET_PATH,
            expected_sha256=_THREEDMOL_SHA256,
        )
        visualization_manifest = json.loads(
            package.file_payload("visualization_manifest.json").decode("utf-8")
        )
        selection = visualization_manifest["selection"]
        ligand_chain = str(selection["ligand_chain"])
        ligand_residue_number = int(selection["ligand_residue_number"])
        if not ligand_chain:
            raise ValueError("可视化 manifest 缺少配体链。")
        renderer = create_structure_viewer_component(library)
        viewer_result = mount_structure_viewer(
            renderer,
            structure_data=package.file_payload("complex.pdb").decode("ascii"),
            structure_format="pdb",
            vina_score_kcal_mol=float(visual_state["score"]),
            pose_mode=int(visual_state["pose_mode"]),
            seed=int(visual_state["seed"]),
            ligand_chain=ligand_chain,
            ligand_residue_number=ligand_residue_number,
            key=f"3dmol-{visual_state['artifact_id']}",
        )
    except Exception as exc:
        st.error(f"本地 3Dmol.js 查看器加载失败：{exc}")
    else:
        if viewer_result.error:
            st.warning(viewer_result.error)
        if viewer_result.png_data_uri:
            try:
                browser_png = decode_viewer_png_data_uri(
                    viewer_result.png_data_uri
                )
            except ValueError as exc:
                st.warning(f"浏览器 PNG 未通过完整性校验：{exc}")
            else:
                st.download_button(
                    "下载浏览器当前视图 PNG",
                    data=browser_png,
                    file_name="3dmol-current-view.png",
                    mime="image/png",
                    width="stretch",
                )

    raw_pymol_tool = visual_state.get("pymol_tool")
    if raw_pymol_tool is not None:
        try:
            pymol_tool = VerifiedExternalTool.model_validate(raw_pymol_tool)
        except ValidationError:
            pymol_tool = None
        if pymol_tool is not None and pymol_tool.available:
            open_clicked = st.button(
                "用本机 PyMOL 打开可编辑视图",
                type="primary",
                width="stretch",
            )
            if open_clicked:
                try:
                    launch_pymol_session(
                        tool=pymol_tool,
                        package=package,
                        session_relative_path="view.pml",
                        allowed_root=artifact_directory,
                        user_confirmed=True,
                    )
                except Exception as exc:
                    _record(
                        audit_callback,
                        tool_name="docking.pymol_open",
                        input_summary=str(visual_state["artifact_id"]),
                        status="failed",
                        error=str(exc),
                    )
                    st.error(f"PyMOL 启动失败：{exc}")
                else:
                    _record(
                        audit_callback,
                        tool_name="docking.pymol_open",
                        input_summary=str(visual_state["artifact_id"]),
                        status="succeeded",
                        output_summary="已由用户点击启动本机 PyMOL",
                    )
                    st.success("已启动本机 PyMOL；可继续编辑并另存会话。")


def render_docking_workbench(
    *,
    run_id: str,
    audit_callback: AuditCallback | None = None,
) -> None:
    """Render the complete v0.5 docking surface for one workbench run."""

    st.header("科研级分子对接与可编辑可视化")
    st.warning(
        "Vina 输出统一标为“Vina 预测评分”。它不是实验结合能、严格结合"
        "自由能，也不能单独证明药效、抗菌活性或药物协同。"
    )
    receptor_state = _render_receptor_source(run_id, audit_callback)
    st.divider()
    receptor_state = _render_receptor_approval(
        run_id,
        receptor_state,
        audit_callback,
    )
    st.divider()
    batch = _render_docking_batch(
        run_id,
        receptor_state,
        audit_callback,
    )
    st.divider()
    _render_visualization(run_id, batch, audit_callback)


__all__ = ["render_docking_workbench"]
