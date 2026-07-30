"""Streamlit surface for auditable OpenMM technical-smoke jobs.

No OpenMM integration step runs in the Streamlit process.  The page only
validates uploads, freezes manifests, enqueues jobs, launches the dedicated
``vetevidence.md_worker`` subprocess, and polls persisted state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from vetevidence.md_ui_support import (
    build_job_progress,
    build_mapping_evidence,
    default_md_task_id,
    infer_source_format,
    list_md_jobs,
    normalize_forcefield_files,
    parse_atom_indices,
    parse_nonempty_lines,
    parse_preparation_command,
    verified_artifact_downloads,
)
from vetevidence.md_worker import (
    MDBackendPreflight,
    MDJobRecord,
    MDJobState,
    MDJobStore,
    MDSystemSummary,
    cancel_md_worker,
    launch_md_worker,
    preflight_openmm,
)
from vetevidence.md_workflow import (
    MDChemistryConfirmation,
    MDForceFieldParameters,
    MDHardwareRequest,
    MDInputSource,
    MDPreset,
    build_md_manifest,
)


AuditCallback = Callable[..., None]
_ACTIVE_STATES = {
    MDJobState.QUEUED,
    MDJobState.RUNNING,
    MDJobState.CANCEL_REQUESTED,
}


def _state_key(run_id: str, suffix: str) -> str:
    return f"vetevidence-md::{run_id}::{suffix}"


def _process_key(run_id: str, job_id: str) -> str:
    return _state_key(run_id, f"process::{job_id.casefold()}")


def _mode_key(run_id: str, job_id: str) -> str:
    return _state_key(run_id, f"dry-run::{job_id.casefold()}")


def _terminal_audit_key(run_id: str, record: MDJobRecord) -> str:
    return _state_key(
        run_id,
        (
            f"terminal-audit::{record.job_id.casefold()}::"
            f"{record.revision}::{record.state.value}"
        ),
    )


def _record(
    callback: AuditCallback | None,
    *,
    tool_name: str,
    input_summary: str,
    status: str,
    output_summary: str | None = None,
    error: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> None:
    if callback is None:
        return
    callback(
        tool_name=tool_name,
        input_summary=input_summary,
        status=status,
        output_summary=output_summary,
        error=error,
        metadata=dict(metadata or {}),
    )


@st.cache_data(ttl="30s", max_entries=2, show_spinner=False)
def _cached_preflight_payload() -> dict[str, object]:
    return preflight_openmm().model_dump(mode="json")


def _current_preflight() -> MDBackendPreflight:
    return MDBackendPreflight.model_validate(_cached_preflight_payload())


def _process_is_alive(process: object | None) -> bool:
    if process is None or not hasattr(process, "poll"):
        return False
    try:
        return process.poll() is None  # type: ignore[attr-defined]
    except Exception:
        return False


def _queued_dry_run_default(run_id: str, job_id: str) -> bool:
    """Fail closed when a queued job has no recoverable execution intent."""

    return bool(st.session_state.get(_mode_key(run_id, job_id), True))


def _launch_background_worker(
    *,
    store: MDJobStore,
    record: MDJobRecord,
    run_id: str,
    dry_run: bool,
    audit_callback: AuditCallback | None,
) -> object:
    """Launch only the external worker; never call its execution function."""

    # Persist intent before Popen so a launch failure or page reload cannot
    # silently turn an explicitly planned dry-run into a real execution.
    st.session_state[_mode_key(run_id, record.job_id)] = dry_run
    process = launch_md_worker(
        store,
        record.job_id,
        dry_run=dry_run,
    )
    st.session_state[_process_key(run_id, record.job_id)] = process
    _record(
        audit_callback,
        tool_name="md.worker_launch",
        input_summary=record.job_id,
        status="succeeded",
        output_summary=(
            "后台 dry-run worker 已启动"
            if dry_run
            else "后台 OpenMM technical_smoke worker 已启动"
        ),
        metadata={
            "job_id": record.job_id,
            "manifest_sha256": record.manifest.manifest_sha256 or "",
            "dry_run": dry_run,
            "worker_pid": int(getattr(process, "pid", 0) or 0),
        },
    )
    return process


def _render_scope_and_preflight(
    audit_callback: AuditCallback | None,
) -> MDBackendPreflight | None:
    st.header("分子动力学：OpenMM 技术烟测")
    st.info(
        "分子动力学（MD）用力场和积分器计算原子随时间的运动，可用于研究构象波动、"
        "口袋稳定性和溶剂环境；但极短技术烟测只验证输入、OpenMM 上下文、积分、"
        "checkpoint 和工件链是否能真实运行。"
    )
    st.warning(
        "v0.6 不声称完成 NVT/NPT 平衡、生产期轨迹、重复模拟、收敛分析或自由能计算；"
        "也不生成 RMSD、RMSF、回转半径、氢键占有率等科研指标。"
        "温度和势能仅作为数值执行健康检查，不能证明结合稳定、药效或协同作用。"
    )

    with st.container(border=True):
        st.subheader("OpenMM 后端检查")
        try:
            preflight = _current_preflight()
        except Exception as exc:
            st.error(f"OpenMM 环境检查失败：{exc}")
            preflight = None
        else:
            status_columns = st.columns(3)
            status_columns[0].metric(
                "执行后端",
                "可用" if preflight.execution_available else "不可用",
            )
            status_columns[1].metric(
                "自动参数化",
                "可用" if preflight.parameterization_available else "未开放/缺依赖",
            )
            status_columns[2].metric(
                "OpenMM 平台",
                "、".join(preflight.hardware.openmm_platforms) or "未发现",
            )
            if preflight.execution_available:
                st.success(
                    "OpenMM 执行模块可用；本界面仍要求上传已经参数化并经人工复核的 "
                    "System XML 与匹配 topology PDB。"
                )
            else:
                st.error(preflight.reason or "OpenMM 执行模块不可用。")
            if not preflight.parameterization_available:
                st.caption(
                    "v0.6 不在界面内自动猜测蛋白或小分子参数；上传完整参数化系统不依赖"
                    "自动参数化模块。"
                )
            with st.expander("查看后端版本与硬件指纹"):
                st.json(preflight.model_dump(mode="json"))
        if st.button(
            "刷新 OpenMM 检查",
            icon=":material/refresh:",
            key="md-refresh-preflight",
        ):
            _cached_preflight_payload.clear()
            _record(
                audit_callback,
                tool_name="md.preflight",
                input_summary="OpenMM local backend",
                status="succeeded",
                output_summary="已清除缓存，将重新检查本机后端",
            )
            st.rerun()
    return preflight


def _uploaded_file_map(uploaded_files: list[Any]) -> dict[str, bytes]:
    names = [str(item.name) for item in uploaded_files]
    if len(names) != len(set(names)):
        raise ValueError("力场/参数上传包含重复文件名。")
    return normalize_forcefield_files(
        {str(item.name): item.getvalue() for item in uploaded_files}
    )


def _render_job_creation(
    *,
    store: MDJobStore,
    run_id: str,
    preflight: MDBackendPreflight | None,
    audit_callback: AuditCallback | None,
) -> str | None:
    st.subheader("1 冻结原始身份、参数化系统与技术烟测清单")
    st.caption(
        "PDBQT 不能作为 MD 的唯一化学来源。v0.6 真实执行只接受已经裁剪为"
        "所选链、无 altloc 的单模型受体 PDB，以及恰好一个 V2000 记录的配体 "
        "SDF；还必须提供匹配的 OpenMM System XML 和 topology PDB。"
    )

    with st.form(f"md-create-{run_id}", border=True):
        st.markdown("**原始化学来源与显式身份**")
        source_columns = st.columns(2)
        with source_columns[0]:
            receptor_file = st.file_uploader(
                "已选链单模型受体 PDB *",
                type=["pdb"],
                max_upload_size=50,
                key=f"md-receptor-{run_id}",
            )
            receptor_name = st.text_input(
                "受体来源名称 *",
                value="RCSB PDB / researcher-reviewed receptor",
                key=f"md-receptor-name-{run_id}",
            )
            receptor_accession = st.text_input(
                "受体 accession / PDB ID *",
                key=f"md-receptor-accession-{run_id}",
            )
            receptor_version = st.text_input(
                "受体版本 / revision *",
                key=f"md-receptor-version-{run_id}",
            )
        with source_columns[1]:
            ligand_file = st.file_uploader(
                "单记录 V2000 配体 SDF *",
                type=["sdf"],
                max_upload_size=25,
                key=f"md-ligand-{run_id}",
            )
            ligand_name = st.text_input(
                "配体来源名称 *",
                value="researcher-reviewed ligand",
                key=f"md-ligand-name-{run_id}",
            )
            ligand_accession = st.text_input(
                "配体 CID/InChIKey/内部 accession *",
                key=f"md-ligand-accession-{run_id}",
            )
            ligand_version = st.text_input(
                "配体记录/文件版本 *",
                key=f"md-ligand-version-{run_id}",
            )

        st.markdown("**化学人工确认门禁**")
        chemistry_columns = st.columns(2)
        with chemistry_columns[0]:
            reviewer = st.text_input(
                "化学复核人/角色 *",
                value="researcher",
                key=f"md-reviewer-{run_id}",
            )
            receptor_chains_text = st.text_area(
                "受体链（每行一个）*",
                value="A",
                key=f"md-chains-{run_id}",
            )
            receptor_protonation = st.text_input(
                "受体质子化假设 *",
                placeholder="例如：由研究者在 pH 7.4 条件下逐位点复核",
                key=f"md-receptor-protonation-{run_id}",
            )
            ligand_charge = st.number_input(
                "配体形式电荷 *",
                min_value=-20,
                max_value=20,
                value=0,
                step=1,
                key=f"md-ligand-charge-{run_id}",
            )
            ligand_protonation = st.text_input(
                "配体质子化状态 *",
                key=f"md-ligand-protonation-{run_id}",
            )
            ligand_tautomer = st.text_input(
                "配体互变异构体状态 *",
                key=f"md-ligand-tautomer-{run_id}",
            )
            ligand_stereo = st.text_input(
                "配体立体化学 *",
                key=f"md-ligand-stereo-{run_id}",
            )
        with chemistry_columns[1]:
            chemical_identity_confirmed = st.checkbox(
                "已核对化学身份",
                key=f"md-confirm-identity-{run_id}",
            )
            receptor_structure_reviewed = st.checkbox(
                "已核对受体结构和链",
                key=f"md-confirm-receptor-{run_id}",
            )
            formal_charge_confirmed = st.checkbox(
                "已核对形式电荷",
                key=f"md-confirm-charge-{run_id}",
            )
            protonation_confirmed = st.checkbox(
                "已核对质子化状态",
                key=f"md-confirm-protonation-{run_id}",
            )
            tautomer_confirmed = st.checkbox(
                "已核对互变异构体",
                key=f"md-confirm-tautomer-{run_id}",
            )
            stereochemistry_confirmed = st.checkbox(
                "已核对立体化学",
                key=f"md-confirm-stereo-{run_id}",
            )
            all_stereocenters_defined = st.checkbox(
                "已确认全部立体中心均有定义",
                key=f"md-confirm-centers-{run_id}",
            )
            metals_reviewed = st.checkbox(
                "已检查金属",
                key=f"md-confirm-metals-{run_id}",
            )
            covalent_links_reviewed = st.checkbox(
                "已检查共价连接",
                key=f"md-confirm-covalent-{run_id}",
            )
            unknown_residues_reviewed = st.checkbox(
                "已检查未知/非标准残基",
                key=f"md-confirm-unknown-{run_id}",
            )
        risk_columns = st.columns(2)
        metals_present_text = risk_columns[0].text_area(
            "已知金属（每行一个；应为空）",
            key=f"md-metals-present-{run_id}",
        )
        covalent_present_text = risk_columns[1].text_area(
            "已知共价连接（每行一个；应为空）",
            key=f"md-covalent-present-{run_id}",
        )
        unknown_present_text = risk_columns[0].text_area(
            "未知/非标准残基（每行一个；应为空）",
            key=f"md-unknown-present-{run_id}",
        )
        unsupported_text = risk_columns[1].text_area(
            "其他不受支持特征（每行一个；应为空）",
            key=f"md-unsupported-{run_id}",
        )
        st.caption(
            "v0.6 遇到金属、共价配体、未知/非标准残基或其他未解决化学特征会安全拒绝，"
            "不会勾选确认后强行执行。"
        )

        st.markdown("**已参数化 OpenMM 系统与原子映射**")
        prepared_columns = st.columns(2)
        with prepared_columns[0]:
            system_xml_file = st.file_uploader(
                "OpenMM System XML *",
                type=["xml"],
                max_upload_size=25,
                key=f"md-system-{run_id}",
            )
            topology_file = st.file_uploader(
                "匹配的 topology PDB *",
                type=["pdb"],
                max_upload_size=25,
                key=f"md-topology-{run_id}",
            )
            forcefield_uploads = st.file_uploader(
                "实际使用的力场/参数文件 *",
                accept_multiple_files=True,
                max_upload_size=25,
                key=f"md-forcefields-{run_id}",
                help="保存原始 ffxml/offxml/XML、frcmod、mol2、lib 或其他实际输入。",
            )
            parameterization_backend = st.text_input(
                "参数化工具/后端 *",
                placeholder="例如 openmmforcefields + OpenFF Toolkit",
                key=f"md-param-backend-{run_id}",
            )
            parameterization_version = st.text_input(
                "参数化工具版本 *",
                key=f"md-param-version-{run_id}",
            )
            prepared_by = st.text_input(
                "prepared_by / 参数化审核人 *",
                value="researcher",
                key=f"md-prepared-by-{run_id}",
            )
        with prepared_columns[1]:
            preparation_command_text = st.text_area(
                "参数化命令 argv（每行一个参数）*",
                placeholder=(
                    "python\nprepare_system.py\n--receptor\nreceptor.pdb\n"
                    "--ligand\nligand.sdf"
                ),
                key=f"md-command-{run_id}",
                help="逐行保存参数，不经 shell 重新解析。",
            )
            mapping_method = st.text_input(
                "映射方法 *",
                value="canonical_source_topology_identity_review",
                key=f"md-mapping-method-{run_id}",
            )
            receptor_indices_text = st.text_area(
                "受体→topology 的零基原子索引 *",
                placeholder="0-1999",
                key=f"md-receptor-indices-{run_id}",
            )
            ligand_indices_text = st.text_area(
                "配体→topology 的零基原子索引 *",
                placeholder="2000-2031",
                key=f"md-ligand-indices-{run_id}",
            )
            preparation_notes = st.text_area(
                "参数化备注（每行一项）",
                key=f"md-preparation-notes-{run_id}",
            )

        summary_columns = st.columns(4)
        particle_count = summary_columns[0].number_input(
            "System 粒子数 *",
            min_value=1,
            max_value=500_000,
            value=1,
            step=1,
            key=f"md-particles-{run_id}",
        )
        force_count = summary_columns[1].number_input(
            "force 数 *",
            min_value=0,
            max_value=128,
            value=1,
            step=1,
            key=f"md-forces-{run_id}",
        )
        constraint_count = summary_columns[2].number_input(
            "constraint 数 *",
            min_value=0,
            max_value=2_000_000,
            value=0,
            step=1,
            key=f"md-constraints-{run_id}",
        )
        periodic = summary_columns[3].checkbox(
            "使用周期性边界（v0.6 未开放）",
            value=False,
            disabled=True,
            help=(
                "周期体系必须同时核验 System 与 topology 的盒向量；"
                "该双向绑定尚未实现，因此 v0.6 安全拒绝。"
            ),
            key=f"md-periodic-{run_id}",
        )
        force_types_text = st.text_area(
            "force 类型（每行一个，数量必须与 force 数一致）*",
            value="NonbondedForce",
            key=f"md-force-types-{run_id}",
        )

        st.markdown("**技术烟测协议与硬件请求**")
        protocol_columns = st.columns(4)
        task_id = protocol_columns[0].text_input(
            "任务 ID *",
            value=default_md_task_id(run_id),
            key=f"md-task-id-{run_id}",
        )
        seed = protocol_columns[1].number_input(
            "随机种子 *",
            min_value=1,
            max_value=2_147_483_647,
            value=20260730,
            step=1,
            key=f"md-seed-{run_id}",
        )
        temperature = protocol_columns[2].number_input(
            "温度 (K) *",
            min_value=1.0,
            max_value=1000.0,
            value=300.0,
            step=1.0,
            key=f"md-temperature-{run_id}",
        )
        friction = protocol_columns[3].number_input(
            "摩擦系数 (1/ps) *",
            min_value=0.001,
            max_value=100.0,
            value=1.0,
            step=0.1,
            key=f"md-friction-{run_id}",
        )
        hardware_columns = st.columns(4)
        platform_name = hardware_columns[0].selectbox(
            "OpenMM 平台 *",
            ["CPU", "CUDA"],
            index=0,
            key=f"md-platform-{run_id}",
            help="保守默认 CPU；CUDA 必须在后端检查中真实存在。",
        )
        precision = hardware_columns[1].selectbox(
            "GPU 精度",
            ["mixed", "single", "double"],
            key=f"md-precision-{run_id}",
        )
        device_indices_text = hardware_columns[2].text_input(
            "GPU device index",
            placeholder="0",
            key=f"md-device-{run_id}",
        )
        gpu_required = hardware_columns[3].checkbox(
            "gpu_required",
            key=f"md-gpu-required-{run_id}",
            help="勾选后实际 Context 不是 GPU 平台就安全失败。",
        )
        execution_mode = st.selectbox(
            "后台启动方式",
            ["仅生成 dry-run 计划", "执行 technical_smoke"],
            index=0,
            key=f"md-execution-mode-{run_id}",
        )
        protocol_confirmed = st.checkbox(
            "我确认这是单重复、极短 technical_smoke；不把结果写成 NVT/NPT、"
            "生产期 MD、稳定结合或自由能证据。",
            key=f"md-protocol-confirmed-{run_id}",
        )
        submitted = st.form_submit_button(
            (
                "冻结清单并启动 dry-run"
                if execution_mode == "仅生成 dry-run 计划"
                else "冻结清单并启动真实 technical_smoke"
            ),
            type="primary",
            icon=":material/play_arrow:",
            width="stretch",
        )

    if not submitted:
        return st.session_state.get(_state_key(run_id, "current-job-id"))

    active_task_id = task_id.strip()
    input_summary = active_task_id or "invalid-task-id"
    try:
        if preflight is None or not preflight.execution_available:
            raise ValueError("OpenMM 执行后端不可用，未创建任务。")
        if any(
            item is None
            for item in (
                receptor_file,
                ligand_file,
                system_xml_file,
                topology_file,
            )
        ):
            raise ValueError(
                "必须上传原始受体、原始配体、System XML 和 topology PDB。"
            )
        if not forcefield_uploads:
            raise ValueError("必须上传实际使用的力场/参数文件。")
        if not protocol_confirmed:
            raise ValueError("必须显式确认 technical_smoke 的解释边界。")
        if platform_name == "CPU" and (gpu_required or device_indices_text.strip()):
            raise ValueError("CPU 请求不能同时指定 GPU device 或 gpu_required。")
        if (
            platform_name == "CUDA"
            and "CUDA" not in preflight.hardware.openmm_platforms
        ):
            raise ValueError("本机 OpenMM 预检未发现 CUDA 平台。")

        receptor_payload = receptor_file.getvalue()
        ligand_payload = ligand_file.getvalue()
        system_xml = system_xml_file.getvalue()
        topology_pdb = topology_file.getvalue()
        command = parse_preparation_command(preparation_command_text)
        receptor_indices = parse_atom_indices(
            receptor_indices_text,
            label="受体原子映射",
        )
        ligand_indices = parse_atom_indices(
            ligand_indices_text,
            label="配体原子映射",
        )
        device_indices = parse_atom_indices(
            device_indices_text,
            label="GPU device index",
            allow_empty=True,
            maximum_count=16,
        )
        chains = parse_nonempty_lines(
            receptor_chains_text,
            label="受体链",
            maximum_count=128,
        )
        force_types = parse_nonempty_lines(
            force_types_text,
            label="force 类型",
            maximum_count=128,
            allow_empty=int(force_count) == 0,
        )
        if len(force_types) != int(force_count):
            raise ValueError("force 类型数量必须与声明的 force 数一致。")
        forcefield_files = _uploaded_file_map(forcefield_uploads)
        now = datetime.now(timezone.utc)
        chemistry = MDChemistryConfirmation(
            reviewed_by=reviewer,
            confirmed_at=now,
            receptor_chain_selection=list(chains),
            receptor_protonation_assumption=receptor_protonation,
            ligand_formal_charge=int(ligand_charge),
            ligand_protonation_state=ligand_protonation,
            ligand_tautomer_state=ligand_tautomer,
            ligand_stereochemistry=ligand_stereo,
            chemical_identity_confirmed=chemical_identity_confirmed,
            receptor_structure_reviewed=receptor_structure_reviewed,
            formal_charge_confirmed=formal_charge_confirmed,
            protonation_confirmed=protonation_confirmed,
            tautomer_confirmed=tautomer_confirmed,
            stereochemistry_confirmed=stereochemistry_confirmed,
            all_stereocenters_defined=all_stereocenters_defined,
            metals_reviewed=metals_reviewed,
            covalent_links_reviewed=covalent_links_reviewed,
            unknown_residues_reviewed=unknown_residues_reviewed,
            metals_present=list(
                parse_nonempty_lines(
                    metals_present_text,
                    label="已知金属",
                    allow_empty=True,
                )
            ),
            covalent_links_present=list(
                parse_nonempty_lines(
                    covalent_present_text,
                    label="已知共价连接",
                    allow_empty=True,
                )
            ),
            unknown_residues_present=list(
                parse_nonempty_lines(
                    unknown_present_text,
                    label="未知/非标准残基",
                    allow_empty=True,
                )
            ),
            unsupported_features=list(
                parse_nonempty_lines(
                    unsupported_text,
                    label="其他不受支持特征",
                    allow_empty=True,
                )
            ),
        )
        manifest = build_md_manifest(
            task_id=active_task_id,
            receptor_payload=receptor_payload,
            receptor_source=MDInputSource(
                source_name=receptor_name,
                accession=receptor_accession,
                version=receptor_version,
                format=infer_source_format(
                    receptor_file.name,
                    role="receptor",
                ),
            ),
            ligand_payload=ligand_payload,
            ligand_source=MDInputSource(
                source_name=ligand_name,
                accession=ligand_accession,
                version=ligand_version,
                format=infer_source_format(
                    ligand_file.name,
                    role="ligand",
                ),
            ),
            chemistry_confirmation=chemistry,
            preset=MDPreset.TECHNICAL_SMOKE,
            forcefield=MDForceFieldParameters(
                temperature_kelvin=float(temperature),
                friction_per_ps=float(friction),
            ),
            hardware_request=MDHardwareRequest(
                platform=platform_name,
                device_indices=list(device_indices),
                precision=precision,
                gpu_required=bool(gpu_required),
            ),
            seeds=[int(seed)],
            protocol_approved_by_user=True,
        )
        job_path = store.job_path(active_task_id)
        if job_path.exists():
            record = store.load(active_task_id)
            if record.manifest.manifest_sha256 != manifest.manifest_sha256:
                raise ValueError(
                    "同名任务已存在且 manifest 不同；不可覆盖，请使用新的任务 ID。"
                )
            if record.prepared_system is not None:
                raise ValueError(
                    "同名任务的参数化系统已冻结；请在任务监控区加载并启动，"
                    "不可覆盖上传。"
                )
        else:
            record = store.enqueue(
                manifest,
                receptor_payload=receptor_payload,
                ligand_payload=ligand_payload,
            )
        mapping_evidence = build_mapping_evidence(
            manifest_sha256=manifest.manifest_sha256 or "",
            receptor_source_sha256=manifest.receptor_source.sha256 or "",
            ligand_source_sha256=manifest.ligand_source.sha256 or "",
            topology_pdb=topology_pdb,
            receptor_indices=receptor_indices,
            ligand_indices=ligand_indices,
            mapping_method=mapping_method,
            prepared_by=prepared_by,
            preparation_command=command,
            recorded_at=now,
        )
        record = store.save_prepared_system(
            active_task_id,
            system_xml=system_xml,
            topology_pdb=topology_pdb,
            parameterization_backend=parameterization_backend,
            parameterization_version=parameterization_version,
            forcefield_files=forcefield_files,
            preparation_command=command,
            prepared_by=prepared_by,
            declared_system_summary=MDSystemSummary(
                particle_count=int(particle_count),
                force_count=int(force_count),
                constraint_count=int(constraint_count),
                force_types=list(force_types),
                uses_periodic_boundary_conditions=bool(periodic),
            ),
            receptor_topology_atom_indices=receptor_indices,
            ligand_topology_atom_indices=ligand_indices,
            mapping_method=mapping_method,
            mapping_evidence=mapping_evidence,
            notes=parse_nonempty_lines(
                preparation_notes,
                label="参数化备注",
                allow_empty=True,
            ),
        )
        dry_run = execution_mode == "仅生成 dry-run 计划"
        st.session_state[_state_key(run_id, "current-job-id")] = record.job_id
        _record(
            audit_callback,
            tool_name="md.enqueue_prepared_system",
            input_summary=record.job_id,
            status="succeeded",
            output_summary="原始输入、manifest、System、topology、映射和力场文件已冻结",
            metadata={
                "job_id": record.job_id,
                "manifest_sha256": record.manifest.manifest_sha256 or "",
                "system_xml_sha256": (
                    record.prepared_system.system_xml_sha256
                    if record.prepared_system
                    else ""
                ),
                "topology_pdb_sha256": (
                    record.prepared_system.topology_pdb_sha256
                    if record.prepared_system
                    else ""
                ),
            },
        )
        _launch_background_worker(
            store=store,
            record=record,
            run_id=run_id,
            dry_run=dry_run,
            audit_callback=audit_callback,
        )
    except Exception as exc:
        _record(
            audit_callback,
            tool_name="md.enqueue_prepared_system",
            input_summary=input_summary,
            status="failed",
            error=str(exc),
        )
        st.error(f"MD 任务未启动：{exc}")
        if active_task_id:
            try:
                existing = store.load(active_task_id)
            except Exception:
                pass
            else:
                st.session_state[
                    _state_key(run_id, "current-job-id")
                ] = existing.job_id
                st.warning(
                    "原始任务记录已存在并保留；请在监控区查看状态。"
                    "系统不会覆盖已冻结输入。"
                )
        return st.session_state.get(_state_key(run_id, "current-job-id"))
    else:
        st.success(
            "任务已由独立 Python 子进程启动；Streamlit 页面只轮询持久化状态。"
        )
        return record.job_id


def _render_job_recovery(
    *,
    store: MDJobStore,
    run_id: str,
    current_job_id: str | None,
) -> str | None:
    st.subheader("2 加载已保存任务")
    listing = list_md_jobs(store)
    if listing.invalid_files:
        st.warning(
            "以下状态文件未通过校验，已跳过："
            + "、".join(listing.invalid_files)
        )
    if not listing.records:
        st.caption("尚无可核验的 MD 任务。")
        return current_job_id
    options = [record.job_id for record in listing.records]
    default_index = (
        options.index(current_job_id)
        if current_job_id in options
        else 0
    )
    selected = st.selectbox(
        "已有任务",
        options,
        index=default_index,
        key=f"md-existing-job-{run_id}",
    )
    if st.button(
        "加载任务",
        icon=":material/folder_open:",
        key=f"md-load-job-{run_id}",
    ):
        st.session_state[_state_key(run_id, "current-job-id")] = selected
        st.rerun()
    return current_job_id


def _status_label(state: MDJobState) -> str:
    return {
        MDJobState.QUEUED: "已排队",
        MDJobState.RUNNING: "后台运行中",
        MDJobState.CANCEL_REQUESTED: "正在请求取消",
        MDJobState.CANCELLED: "已取消",
        MDJobState.SUCCEEDED: "已完成",
        MDJobState.FAILED: "失败",
    }[state]


def _render_actual_metrics(record: MDJobRecord) -> None:
    if record.run_result is None:
        if record.dry_run_plan is not None:
            st.info(
                "本任务仅完成 dry-run 计划，没有创建 OpenMM Context、轨迹、"
                "实际平台、温度或势能数据。"
            )
            st.json(record.dry_run_plan.model_dump(mode="json"))
        elif record.state in _ACTIVE_STATES:
            st.caption(
                "运行中的可信进度来自已落盘 checkpoint；温度、势能和实际平台将在"
                "成功工件通过哈希校验后显示。"
            )
        return

    result = record.run_result
    audit = result.execution_audit
    platform_columns = st.columns(4)
    platform_columns[0].metric("实际 OpenMM 平台", audit.platform_name)
    platform_columns[1].metric("实际设备", audit.selected_device or "未报告")
    platform_columns[2].metric("精度", audit.precision)
    platform_columns[3].metric(
        "执行时长",
        f"{audit.duration_seconds:.2f} s",
    )
    st.caption(
        f"硬件指纹：{audit.hardware_fingerprint} · "
        f"驱动：{audit.driver_version or '未报告'}"
    )

    replica = result.analysis.replicas[0]
    metric_columns = st.columns(2)
    if replica.temperature_kelvin is not None:
        summary = build_job_progress(record).temperature
        assert summary is not None
        metric_columns[0].metric(
            "末次温度",
            f"{summary.latest:.3f} {summary.unit}",
        )
        st.line_chart(
            {
                "time_ps": replica.temperature_kelvin.times_ps,
                "temperature_kelvin": replica.temperature_kelvin.values,
            },
            x="time_ps",
            y="temperature_kelvin",
            x_label="时间 (ps)",
            y_label=f"温度 ({replica.temperature_kelvin.unit})",
        )
    if replica.potential_energy_kj_mol is not None:
        summary = build_job_progress(record).potential_energy
        assert summary is not None
        metric_columns[1].metric(
            "末次势能",
            f"{summary.latest:.3f} {summary.unit}",
        )
        st.line_chart(
            {
                "time_ps": replica.potential_energy_kj_mol.times_ps,
                "potential_energy": replica.potential_energy_kj_mol.values,
            },
            x="time_ps",
            y="potential_energy",
            x_label="时间 (ps)",
            y_label=f"势能 ({replica.potential_energy_kj_mol.unit})",
        )
    st.warning(
        "未生成的科研指标："
        + "、".join(result.analysis.reserved_metrics_not_produced)
        + "。这些字段不会用占位数值伪造。"
    )


def _render_artifact_downloads(
    *,
    store: MDJobStore,
    record: MDJobRecord,
    run_id: str,
) -> None:
    if record.run_result is None:
        return
    load_downloads = st.toggle(
        "加载、哈希复核并提供工件下载",
        key=f"md-load-artifacts-{run_id}-{record.job_id}",
        help="仅在需要下载时把工件读入页面，避免每次轮询重复读取轨迹。",
    )
    if not load_downloads:
        return
    try:
        with st.spinner("正在复核 MD 工件 SHA-256…"):
            downloads = verified_artifact_downloads(store, record)
    except Exception as exc:
        st.error(f"MD 工件校验失败：{exc}")
        return
    for artifact in downloads:
        st.download_button(
            f"下载 {artifact.role} · {artifact.filename}",
            data=artifact.payload,
            file_name=artifact.filename,
            mime=artifact.mime,
            key=(
                f"md-download-{run_id}-{record.job_id}-"
                f"{artifact.role}-{artifact.sha256[:12]}"
            ),
            width="stretch",
        )
        st.caption(
            f"{artifact.size_bytes:,} bytes · SHA-256 {artifact.sha256}"
        )


def _render_job_actions(
    *,
    store: MDJobStore,
    record: MDJobRecord,
    run_id: str,
    audit_callback: AuditCallback | None,
) -> None:
    process_key = _process_key(run_id, record.job_id)
    process = st.session_state.get(process_key)
    process_alive = _process_is_alive(process)
    with st.container(horizontal=True):
        if record.state is MDJobState.QUEUED:
            dry_run = st.toggle(
                "仅 dry-run",
                value=_queued_dry_run_default(run_id, record.job_id),
                key=f"md-queued-mode-{run_id}-{record.job_id}",
            )
            if st.button(
                (
                    "启动 dry-run"
                    if dry_run
                    else "启动真实 technical_smoke"
                ),
                icon=":material/play_arrow:",
                type="primary",
                disabled=process_alive,
                key=f"md-start-{run_id}-{record.job_id}",
            ):
                try:
                    _launch_background_worker(
                        store=store,
                        record=record,
                        run_id=run_id,
                        dry_run=bool(dry_run),
                        audit_callback=audit_callback,
                    )
                except Exception as exc:
                    _record(
                        audit_callback,
                        tool_name="md.worker_launch",
                        input_summary=record.job_id,
                        status="failed",
                        error=str(exc),
                    )
                    st.error(f"后台 worker 启动失败：{exc}")
                else:
                    st.rerun()
        if record.state in {MDJobState.QUEUED, MDJobState.RUNNING}:
            if st.button(
                "请求取消",
                icon=":material/cancel:",
                key=f"md-cancel-{run_id}-{record.job_id}",
            ):
                try:
                    if process_alive:
                        updated = cancel_md_worker(
                            store,
                            record.job_id,
                            process,
                            timeout_seconds=2.0,
                        )
                    else:
                        updated = store.request_cancel(record.job_id)
                except Exception as exc:
                    _record(
                        audit_callback,
                        tool_name="md.cancel",
                        input_summary=record.job_id,
                        status="failed",
                        error=str(exc),
                    )
                    st.error(f"取消请求失败：{exc}")
                else:
                    _record(
                        audit_callback,
                        tool_name="md.cancel",
                        input_summary=record.job_id,
                        status="succeeded",
                        output_summary=f"状态已更新为 {updated.state.value}",
                    )
                    st.rerun()
        if record.state in {MDJobState.FAILED, MDJobState.CANCELLED}:
            if record.checkpoint is not None:
                if st.button(
                    "从已核验 checkpoint 恢复",
                    icon=":material/restore:",
                    type="primary",
                    key=f"md-resume-{run_id}-{record.job_id}",
                ):
                    try:
                        queued = store.resume(record.job_id)
                        _launch_background_worker(
                            store=store,
                            record=queued,
                            run_id=run_id,
                            dry_run=False,
                            audit_callback=audit_callback,
                        )
                    except Exception as exc:
                        _record(
                            audit_callback,
                            tool_name="md.resume",
                            input_summary=record.job_id,
                            status="failed",
                            error=str(exc),
                        )
                        st.error(f"checkpoint 恢复失败：{exc}")
                    else:
                        _record(
                            audit_callback,
                            tool_name="md.resume",
                            input_summary=record.job_id,
                            status="succeeded",
                            output_summary="已核验 checkpoint 并启动独立 worker",
                            metadata={
                                "checkpoint_sha256": (
                                    record.checkpoint.checkpoint_sha256
                                ),
                                "checkpoint_step": record.checkpoint.step,
                            },
                        )
                        st.rerun()
            else:
                st.caption(
                    "没有已核验 checkpoint，不能假装续跑；请修正输入并使用新任务 ID。"
                )
        if st.button(
            "协调孤儿任务",
            icon=":material/sync_problem:",
            key=f"md-reconcile-{run_id}-{record.job_id}",
        ):
            try:
                reconciled = store.reconcile_stale_jobs()
            except Exception as exc:
                _record(
                    audit_callback,
                    tool_name="md.reconcile",
                    input_summary=record.job_id,
                    status="failed",
                    error=str(exc),
                )
                st.error(f"任务协调失败：{exc}")
            else:
                _record(
                    audit_callback,
                    tool_name="md.reconcile",
                    input_summary=record.job_id,
                    status="succeeded",
                    output_summary=f"已协调 {len(reconciled)} 个孤儿任务",
                )
                st.rerun()


def _render_job_record(
    *,
    store: MDJobStore,
    record: MDJobRecord,
    run_id: str,
    audit_callback: AuditCallback | None,
) -> None:
    progress = build_job_progress(record)
    if record.state not in _ACTIVE_STATES:
        terminal_key = _terminal_audit_key(run_id, record)
        if not st.session_state.get(terminal_key):
            result_sha256 = (
                record.run_result.result_manifest_sha256
                if record.run_result is not None
                else None
            )
            _record(
                audit_callback,
                tool_name="md.worker_terminal",
                input_summary=record.job_id,
                status=(
                    "failed"
                    if record.state is MDJobState.FAILED
                    else "succeeded"
                ),
                output_summary=(
                    f"MD job terminal state={record.state.value}"
                ),
                error=record.error if record.state is MDJobState.FAILED else None,
                metadata={
                    "event_id": (
                        f"{record.job_id}:{record.revision}:"
                        f"{record.state.value}"
                    ),
                    "job_id": record.job_id,
                    "job_state": record.state.value,
                    "manifest_sha256": (
                        record.manifest.manifest_sha256 or ""
                    ),
                    "result_manifest_sha256": result_sha256 or "",
                },
            )
            st.session_state[terminal_key] = True
    with st.container(border=True):
        st.subheader(f"任务 {record.job_id}")
        status_columns = st.columns(4)
        status_columns[0].metric("状态", _status_label(record.state))
        status_columns[1].metric(
            "可信进度",
            f"{progress.completed_steps}/{progress.total_steps} steps",
        )
        status_columns[2].metric(
            "请求平台",
            progress.requested_platform,
        )
        status_columns[3].metric(
            "实际平台",
            progress.actual_platform or "尚未写入",
        )
        st.progress(
            min(progress.completed_steps / progress.total_steps, 1.0),
            text=(
                f"checkpoint/结果已核验步数："
                f"{progress.completed_steps}/{progress.total_steps}"
            ),
        )
        st.caption(
            f"manifest SHA-256：{record.manifest.manifest_sha256} · "
            f"attempts：{record.attempts} · resume：{record.resume_count}"
        )
        if record.checkpoint is not None:
            st.caption(
                f"checkpoint step {record.checkpoint.step} · "
                f"SHA-256 {record.checkpoint.checkpoint_sha256}"
            )
        if record.error:
            st.error(record.error)

        _render_job_actions(
            store=store,
            record=record,
            run_id=run_id,
            audit_callback=audit_callback,
        )
        _render_actual_metrics(record)
        manifest_payload = json.dumps(
            record.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        st.download_button(
            "下载 MD manifest",
            data=manifest_payload,
            file_name=f"{record.job_id}-manifest.json",
            mime="application/json",
            key=f"md-manifest-download-{run_id}-{record.job_id}",
            width="stretch",
        )
        _render_artifact_downloads(
            store=store,
            record=record,
            run_id=run_id,
        )


@st.fragment(run_every="2s")
def _render_active_job_monitor(
    *,
    store_root: str,
    job_id: str,
    run_id: str,
    audit_callback: AuditCallback | None,
) -> None:
    """Poll only persisted job state; no simulation code is reachable here."""

    store = MDJobStore(Path(store_root))
    try:
        record = store.load(job_id)
    except Exception as exc:
        st.error(f"MD 任务状态校验失败：{exc}")
        return
    _render_job_record(
        store=store,
        record=record,
        run_id=run_id,
        audit_callback=audit_callback,
    )
    if record.state not in _ACTIVE_STATES:
        st.session_state.pop(_process_key(run_id, record.job_id), None)
        st.rerun()


def _render_job_monitor(
    *,
    store: MDJobStore,
    run_id: str,
    job_id: str | None,
    audit_callback: AuditCallback | None,
) -> None:
    st.subheader("3 后台状态、取消、恢复与工件")
    if not job_id:
        st.info("请创建或加载一个 MD 任务。")
        return
    try:
        record = store.load(job_id)
    except Exception as exc:
        st.error(f"MD 任务状态校验失败：{exc}")
        return
    if record.state in _ACTIVE_STATES:
        _render_active_job_monitor(
            store_root=str(store.root),
            job_id=record.job_id,
            run_id=run_id,
            audit_callback=audit_callback,
        )
    else:
        st.session_state.pop(_process_key(run_id, record.job_id), None)
        _render_job_record(
            store=store,
            record=record,
            run_id=run_id,
            audit_callback=audit_callback,
        )


def render_md_workbench(
    *,
    run_id: str,
    audit_callback: AuditCallback | None = None,
    store_root: str | Path | None = None,
) -> None:
    """Render the v0.6 auditable OpenMM technical-smoke workbench."""

    store = MDJobStore(Path(store_root) if store_root is not None else None)
    try:
        startup_reconciled = store.reconcile_stale_jobs()
    except Exception as exc:
        _record(
            audit_callback,
            tool_name="md.reconcile_startup",
            input_summary=str(store.root),
            status="failed",
            error=str(exc),
        )
        st.warning(f"启动时遗留任务校正失败：{exc}")
    else:
        if startup_reconciled:
            _record(
                audit_callback,
                tool_name="md.reconcile_startup",
                input_summary=str(store.root),
                status="succeeded",
                output_summary=(
                    f"启动时已协调 {len(startup_reconciled)} 个遗留任务"
                ),
                metadata={
                    "event_id": "startup-reconcile:"
                    + ",".join(
                        f"{item.job_id}:{item.revision}:{item.state.value}"
                        for item in startup_reconciled
                    )
                },
            )
            st.caption(
                f"启动时已自动协调 {len(startup_reconciled)} 个遗留 MD 任务。"
            )
    preflight = _render_scope_and_preflight(audit_callback)
    current_job_id = _render_job_creation(
        store=store,
        run_id=run_id,
        preflight=preflight,
        audit_callback=audit_callback,
    )
    current_job_id = _render_job_recovery(
        store=store,
        run_id=run_id,
        current_job_id=current_job_id,
    )
    st.divider()
    _render_job_monitor(
        store=store,
        run_id=run_id,
        job_id=current_job_id,
        audit_callback=audit_callback,
    )


__all__ = ["render_md_workbench"]
