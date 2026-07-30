from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from vetevidence.config import load_settings
from vetevidence.connector_artifacts import (
    ConnectorArchiveError,
    ConnectorArtifactStore,
)
from vetevidence.database_connectors import (
    ConnectorError,
    ConnectorResult,
    ConnectorStatus,
    DAVIDConnector,
    DEFAULT_DAVID_CATEGORIES,
    NCBIConnector,
    PubChemConnector,
    RCSBConnector,
    STRINGConnector,
    UniProtConnector,
    export_connector_result,
)
from vetevidence.evidence_network import (
    EvidenceNetwork,
    build_evidence_network,
)
from vetevidence.evaluation import EvaluationReport
from vetevidence.experiment_analysis import (
    FICIAnalysisResult,
    GrowthCurveAnalysisResult,
    analyze_experiment_csv,
)
from vetevidence.input_validation import validate_synergy_question_input
from vetevidence.literature_import import (
    LiteratureImportResult,
    parse_literature_export,
)
from vetevidence.mechanism_prediction import (
    MechanismPredictionBundle,
    SourceProvenance,
    VinaParameters,
    build_vina_manifest,
    parse_vina_output,
    require_docking_scope,
    require_network_scope,
    validate_pdbqt_bytes,
)
from vetevidence.network_files import (
    MAX_NETWORK_FILE_BYTES,
    analyze_network_pharmacology_files,
    compound_target_template_docx,
    compound_target_template_xlsx,
    network_result_to_docx,
    network_result_to_xlsx,
    target_pathway_template_docx,
    target_pathway_template_xlsx,
)
from vetevidence.openbabel_execution import (
    OpenBabelExecutableInfo,
    OpenBabelExecutionError,
    OpenBabelPreparationArtifacts,
    OpenBabelPreparationOptions,
    discover_openbabel,
    prepare_ligand_pdbqt,
)
from vetevidence.models import PubMedArticle
from vetevidence.pubmed import PubMedClient, PubMedError
from vetevidence.run_store import (
    RunStore,
    WorkbenchRunSnapshot,
    build_tool_call,
)
from vetevidence.vina_artifacts import VinaArtifactStore
from vetevidence.vina_execution import (
    VinaExecutableInfo,
    VinaExecutionError,
    discover_vina,
    execute_vina,
)
from vetevidence.workbench import (
    EvidenceAdmissionStatus,
    HumanReview,
    LiteratureEvidenceGrade,
    ResearchQuestion,
    ReviewDecision,
    TaskStatus,
    build_task_event,
    decompose_research_question,
    summarize_task_status,
)
from vetevidence.workbench_pipeline import (
    EvidenceAssessment,
    ExperimentCondition,
    build_decision_report,
    build_experiment_conditions,
    decision_report_to_markdown,
    experiment_condition_rows,
    experiment_analysis_matches_question,
    generate_search_queries,
    report_content_sha256,
    run_multi_query_research,
    assess_evidence,
)


PROJECT_ROOT = Path(__file__).parent
RUN_STATE_KEY = "vetresearch_run_snapshot"
OPENBABEL_LIGAND_STATE_KEY = "openbabel_prepared_ligand"
DATABASE_RESULTS_STATE_KEY = "database_connector_results"
RUN_STORE = RunStore()
VINA_ARTIFACT_STORE = VinaArtifactStore()
CONNECTOR_ARTIFACT_STORE = ConnectorArtifactStore()


@st.cache_data(ttl=300, show_spinner=False)
def discover_vina_for_ui(
    vina_executable: str,
    local_appdata: str,
    path_value: str,
) -> tuple[VinaExecutableInfo | None, str | None]:
    """Cache both successful and failed discovery across Streamlit reruns."""

    environment = {
        "VINA_EXECUTABLE": vina_executable,
        "LOCALAPPDATA": local_appdata,
        "PATH": path_value,
    }
    try:
        return discover_vina(environment=environment), None
    except VinaExecutionError as exc:
        return None, str(exc)


@st.cache_data(ttl=300, show_spinner=False)
def discover_openbabel_for_ui(
    openbabel_executable: str,
    path_value: str,
) -> tuple[OpenBabelExecutableInfo | None, str | None]:
    """Cache bounded Open Babel discovery without weakening execution checks."""

    environment = {
        "OPENBABEL_EXECUTABLE": openbabel_executable,
        "PATH": path_value,
    }
    try:
        return (
            discover_openbabel(
                environment=environment,
                project_root=PROJECT_ROOT,
            ),
            None,
        )
    except OpenBabelExecutionError as exc:
        return None, str(exc)


def openbabel_preparation_fingerprint(
    payload: bytes,
    options: OpenBabelPreparationOptions,
    executable: OpenBabelExecutableInfo,
) -> str:
    """Bind an ephemeral prepared ligand to input, controls and executable."""

    digest = hashlib.sha256()
    digest.update(payload)
    digest.update(
        json.dumps(
            {
                "options": options.model_dump(mode="json"),
                "executable_sha256": executable.sha256,
                "executable_version": executable.version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def load_openbabel_preparation(
    run_id: str,
    expected_fingerprint: str,
) -> OpenBabelPreparationArtifacts | None:
    payload = st.session_state.get(OPENBABEL_LIGAND_STATE_KEY)
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != run_id
        or payload.get("fingerprint") != expected_fingerprint
    ):
        return None
    try:
        artifacts = OpenBabelPreparationArtifacts.model_validate(
            payload.get("artifacts")
        )
        output_sha256 = validate_pdbqt_bytes(
            artifacts.output_pdbqt,
            role="ligand",
        )
        if output_sha256 != artifacts.metadata.output_pdbqt_sha256:
            raise ValueError("会话中的 Open Babel 输出哈希不一致。")
        return artifacts
    except (ValidationError, TypeError, ValueError):
        st.session_state.pop(OPENBABEL_LIGAND_STATE_KEY, None)
        return None


EVIDENCE_GRADE_LABELS = {
    LiteratureEvidenceGrade.UNASSESSED: "未评估",
    LiteratureEvidenceGrade.OUT_OF_SCOPE: "主题不匹配",
    LiteratureEvidenceGrade.CONTEXTUAL: "间接背景",
    LiteratureEvidenceGrade.DIRECT_INTERACTION: "直接文献证据",
}
QUESTION_PRESETS = {
    "验收示例：槲皮素 + 阿莫西林 / 无乳链球菌": {
        "question": (
            "quercetin 与 amoxicillin 对 Streptococcus agalactiae "
            "是否具有值得进一步验证的协同作用？"
        ),
        "population": "Streptococcus agalactiae",
        "intervention": "quercetin",
        "comparator": "amoxicillin",
        "outcomes": "FICI, 生长曲线, 抑菌效应",
    },
    "验收示例：氟苯尼考 + 甲砜霉素 / 多杀性巴氏杆菌": {
        "question": (
            "florfenicol 与 thiamphenicol 对 Pasteurella multocida "
            "是否存在值得进一步验证的协同抗菌作用？"
        ),
        "population": "Pasteurella multocida",
        "intervention": "florfenicol",
        "comparator": "thiamphenicol",
        "outcomes": "FICI, time-kill, 抑菌效应",
    },
}


def build_synthetic_network_demo(
    question: ResearchQuestion,
) -> tuple[bytes, bytes]:
    """Build visibly synthetic network rows bound to the active question."""

    compound_buffer = io.StringIO(newline="")
    compound_writer = csv.writer(compound_buffer)
    compound_writer.writerow(
        [
            "compound",
            "compound_accession",
            "organism",
            "target",
            "target_accession",
        ]
    )
    compounds = (
        (question.intervention or "", "SYNTHETIC:CMPD:001"),
        (question.comparator or "", "SYNTHETIC:CMPD:002"),
    )
    for compound, accession in compounds:
        for target_index in (1, 2):
            compound_writer.writerow(
                [
                    compound,
                    accession,
                    question.population or "",
                    f"SYNTHETIC_TARGET_{target_index}",
                    f"SYNTHETIC:TGT:{target_index:03d}",
                ]
            )

    pathway_buffer = io.StringIO(newline="")
    pathway_writer = csv.writer(pathway_buffer)
    pathway_writer.writerow(
        [
            "organism",
            "target",
            "target_accession",
            "pathway",
            "pathway_accession",
        ]
    )
    for target_index in (1, 2):
        pathway_writer.writerow(
            [
                question.population or "",
                f"SYNTHETIC_TARGET_{target_index}",
                f"SYNTHETIC:TGT:{target_index:03d}",
                f"SYNTHETIC_PATHWAY_{target_index}",
                f"SYNTHETIC:PATH:{target_index:03d}",
            ]
        )
    return (
        compound_buffer.getvalue().encode("utf-8"),
        pathway_buffer.getvalue().encode("utf-8"),
    )


def load_latest_evaluation() -> EvaluationReport | None:
    report_path = PROJECT_ROOT / "data" / "eval" / "latest_results.json"
    if not report_path.exists():
        return None
    try:
        return EvaluationReport.model_validate(
            json.loads(report_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


def current_snapshot() -> WorkbenchRunSnapshot | None:
    payload = st.session_state.get(RUN_STATE_KEY)
    if not payload:
        return None
    try:
        return WorkbenchRunSnapshot.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        st.session_state.pop(RUN_STATE_KEY, None)
        st.error("当前会话状态无法校验，已清除；可用完整运行 ID 恢复历史快照。")
        return None


def save_snapshot(snapshot: WorkbenchRunSnapshot) -> None:
    st.session_state[RUN_STATE_KEY] = snapshot.model_dump(mode="json")
    try:
        RUN_STORE.save(snapshot)
    except OSError as exc:
        st.warning(f"当前会话可继续，但本地运行快照保存失败：{exc}")


def append_event(
    snapshot: WorkbenchRunSnapshot,
    status: TaskStatus,
    message: str,
    *,
    actor: str = "system",
    metadata: dict[str, object] | None = None,
) -> WorkbenchRunSnapshot:
    event = build_task_event(
        snapshot.run_id,
        status,
        message,
        actor=actor,
        metadata=metadata,
    )
    return snapshot.model_copy(
        update={"task_events": [*snapshot.task_events, event]}
    )


def append_tool_call(
    snapshot: WorkbenchRunSnapshot,
    tool_name: str,
    input_summary: str,
    *,
    status: str,
    output_summary: str | None = None,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> WorkbenchRunSnapshot:
    retry_of = next(
        (
            call.call_id
            for call in reversed(snapshot.tool_calls)
            if (
                call.tool_name == tool_name
                and call.status == "failed"
                and call.input_summary == input_summary
            )
        ),
        None,
    )
    call = build_tool_call(
        tool_name,
        input_summary,
        status=status,
        output_summary=output_summary,
        error=error,
        retry_of=retry_of,
        metadata=metadata,
    )
    return snapshot.model_copy(
        update={"tool_calls": [*snapshot.tool_calls, call]}
    )


DATABASE_SOURCE_SLUGS = {
    "PubChem": "pubchem",
    "UniProt": "uniprot",
    "NCBI Gene": "ncbi-gene",
    "GenBank": "genbank",
    "RCSB PDB": "rcsb-pdb",
    "STRING": "string",
    "DAVID": "david",
    "STRING + DAVID 证据网络": "evidence-network",
    "数据库输入": "input-validation",
    "数据库结果归档": "archive-validation",
}
DATABASE_HOME_URLS = {
    "PubChem": "https://pubchem.ncbi.nlm.nih.gov/",
    "UniProt": "https://www.uniprot.org/",
    "NCBI Gene": "https://www.ncbi.nlm.nih.gov/gene/",
    "GenBank": "https://www.ncbi.nlm.nih.gov/genbank/",
    "RCSB PDB": "https://www.rcsb.org/",
    "STRING": "https://string-db.org/",
    "DAVID": "https://davidbioinformatics.nih.gov/",
}
DATABASE_STATUS_LABELS = {
    ConnectorStatus.OK: "可用",
    ConnectorStatus.NO_RESULTS: "无结果",
    ConnectorStatus.DEGRADED: "降级",
    ConnectorStatus.OFFLINE_EXPORT: "未发送 / 离线导出",
}
DATABASE_EVIDENCE_TYPE_LABELS = {
    "experimental": "实验验证",
    "curated_database": "数据库整理",
    "text_mined": "文本推断",
    "computational_prediction": "模型或计算预测",
}
MAX_DATABASE_HISTORY = 100


def split_database_identifiers(
    value: str,
    *,
    label: str,
    limit: int,
) -> tuple[str, ...]:
    """Parse line/semicolon-delimited identifiers without splitting commas."""

    normalized = value.replace(";", "\n").replace("；", "\n")
    identifiers = tuple(
        dict.fromkeys(
            item.strip()
            for item in normalized.splitlines()
            if item.strip()
        )
    )
    if len(identifiers) > limit:
        raise ValueError(f"{label} 最多允许 {limit} 项，当前为 {len(identifiers)} 项。")
    return identifiers


def database_state(run_id: str) -> dict[str, object]:
    """Return bounded, per-session connector state for the active run only."""

    state = st.session_state.get(DATABASE_RESULTS_STATE_KEY)
    if (
        not isinstance(state, dict)
        or state.get("run_id") != run_id
        or not isinstance(state.get("entries"), list)
    ):
        state = {
            "run_id": run_id,
            "entries": [],
            "network": None,
        }
        st.session_state[DATABASE_RESULTS_STATE_KEY] = state
    return state


def redact_database_error(
    error: Exception,
    *,
    sensitive_values: tuple[str | None, ...] = (),
) -> str:
    """Keep UI and audit errors useful without echoing credentials or emails."""

    text = str(error).strip() or error.__class__.__name__
    settings = load_settings()
    redactions = (
        *sensitive_values,
        settings.ncbi_email,
        settings.ncbi_api_key,
        os.getenv("DAVID_EMAIL"),
    )
    for value in redactions:
        if value:
            text = text.replace(value, "[已脱敏]")
            text = text.replace(quote(value, safe=""), "[已脱敏]")
    if len(text) > 1500:
        text = text[:1497] + "..."
    return text


def record_database_failure(
    snapshot: WorkbenchRunSnapshot,
    *,
    source: str,
    input_summary: str,
    error: Exception,
    sensitive_values: tuple[str | None, ...] = (),
) -> WorkbenchRunSnapshot:
    """Expose and persist a failed connector/UI boundary operation."""

    safe_error = redact_database_error(
        error,
        sensitive_values=sensitive_values,
    )
    slug = DATABASE_SOURCE_SLUGS.get(source, "ui")
    snapshot = append_tool_call(
        snapshot,
        f"database.{slug}",
        input_summary,
        status="failed",
        error=safe_error,
        metadata={"source": source},
    )
    snapshot = append_event(
        snapshot,
        TaskStatus.FAILED,
        f"{source} 数据库操作失败：{safe_error}",
        metadata={"source": source},
    )
    save_snapshot(snapshot)
    st.error(f"{source}：{safe_error}")
    return snapshot


def run_database_query(
    snapshot: WorkbenchRunSnapshot,
    state: dict[str, object],
    *,
    source: str,
    input_summary: str,
    query: Callable[[], ConnectorResult],
    sensitive_values: tuple[str | None, ...] = (),
) -> tuple[WorkbenchRunSnapshot, bool]:
    """Execute, archive and audit one connector result as an atomic UI step."""

    slug = DATABASE_SOURCE_SLUGS[source]
    try:
        result = query()
        if not isinstance(result, ConnectorResult):
            raise TypeError("连接器没有返回 ConnectorResult。")
        query_id = f"{slug}-{uuid4().hex}"
        archive_path = CONNECTOR_ARTIFACT_STORE.save(
            snapshot.run_id,
            query_id,
            result,
        )
    except Exception as exc:
        return (
            record_database_failure(
                snapshot,
                source=source,
                input_summary=input_summary,
                error=exc,
                sensitive_values=sensitive_values,
            ),
            False,
        )

    try:
        archive_display = archive_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        archive_display = str(archive_path)
    entry = {
        "source": source,
        "query_id": query_id,
        "input_summary": input_summary,
        "archive_path": archive_display,
        "result": result,
    }
    entries = state["entries"]
    if not isinstance(entries, list):
        entries = []
    entries.append(entry)
    state["entries"] = entries[-MAX_DATABASE_HISTORY:]
    st.session_state[DATABASE_RESULTS_STATE_KEY] = state

    metadata: dict[str, object] = {
        "source": source,
        "query_id": query_id,
        "connector_status": result.status.value,
        "record_count": len(result.records),
        "raw_response_count": len(result.artifacts),
        "archive_path": archive_display,
        "raw_response_sha256": [
            item.provenance.raw_response_sha256 for item in result.artifacts
        ],
    }
    if result.offline_request:
        metadata["offline_request_sha256"] = result.offline_request.sha256
    snapshot = append_tool_call(
        snapshot,
        f"database.{slug}",
        input_summary,
        status="succeeded",
        output_summary=(
            f"连接器状态 {result.status.value}；"
            f"{len(result.records)} 条标准化记录；"
            f"{len(result.artifacts)} 份原始响应"
        ),
        metadata=metadata,
    )
    snapshot = append_event(
        snapshot,
        TaskStatus.RUNNING,
        (
            f"{source} 查询已归档：{query_id}；"
            f"状态 {result.status.value}，记录 {len(result.records)} 条。"
        ),
        metadata=metadata,
    )
    save_snapshot(snapshot)
    return snapshot, True


def run_database_query_group(
    snapshot: WorkbenchRunSnapshot,
    state: dict[str, object],
    *,
    source: str,
    connector_factory: Callable[[], object],
    operations: tuple[
        tuple[str, Callable[[object], ConnectorResult]],
        ...,
    ],
    sensitive_values: tuple[str | None, ...] = (),
) -> tuple[WorkbenchRunSnapshot, int]:
    """Reuse one rate-limited connector while isolating each query result."""

    if not operations:
        return snapshot, 0
    succeeded = 0
    try:
        connector = connector_factory()
        with connector:
            for input_summary, operation in operations:
                snapshot, archived = run_database_query(
                    snapshot,
                    state,
                    source=source,
                    input_summary=input_summary,
                    query=lambda operation=operation: operation(connector),
                    sensitive_values=sensitive_values,
                )
                succeeded += int(archived)
    except Exception as exc:
        snapshot = record_database_failure(
            snapshot,
            source=source,
            input_summary=f"{len(operations)} 项批量查询的连接器初始化或关闭",
            error=exc,
            sensitive_values=sensitive_values,
        )
    return snapshot, succeeded


def build_database_network(
    snapshot: WorkbenchRunSnapshot,
    state: dict[str, object],
) -> WorkbenchRunSnapshot:
    """Build the latest STRING + DAVID evidence network and audit the result."""

    latest: dict[str, ConnectorResult] = {}
    entries = state.get("entries")
    if isinstance(entries, list):
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            result = entry.get("result")
            if (
                source in {"STRING", "DAVID"}
                and source not in latest
                and isinstance(result, ConnectorResult)
            ):
                latest[str(source)] = result

    string_result = latest.get("STRING")
    david_result = latest.get("DAVID")
    try:
        network = build_evidence_network(
            string_result.records if string_result else (),
            string_provenance=(
                string_result.provenance[-1]
                if string_result and string_result.provenance
                else None
            ),
            string_mappings=(
                string_result.mappings if string_result else ()
            ),
            enrichment_records=david_result.records if david_result else (),
            enrichment_provenance=(
                david_result.provenance[-1]
                if david_result and david_result.provenance
                else None
            ),
        )
    except Exception as exc:
        state["network"] = None
        st.session_state[DATABASE_RESULTS_STATE_KEY] = state
        return record_database_failure(
            snapshot,
            source="STRING + DAVID 证据网络",
            input_summary="最新 STRING 相互作用与 DAVID 富集标准化记录",
            error=exc,
        )

    state["network"] = network
    st.session_state[DATABASE_RESULTS_STATE_KEY] = state
    metadata = {
        "node_count": len(network.nodes),
        "evidence_edge_count": len(network.edges),
        "ranking_count": len(network.rankings),
        "enrichment_count": len(network.enrichment),
        "string_query_present": string_result is not None,
        "david_query_present": david_result is not None,
    }
    snapshot = append_tool_call(
        snapshot,
        "database.evidence-network",
        "最新 STRING 相互作用与 DAVID 富集标准化记录",
        status="succeeded",
        output_summary=(
            f"{len(network.nodes)} 个节点，{len(network.edges)} 条证据边，"
            f"{len(network.enrichment)} 条富集结果"
        ),
        metadata=metadata,
    )
    snapshot = append_event(
        snapshot,
        TaskStatus.RUNNING,
        "STRING + DAVID 证据网络已更新；combined score 仅用于排序。",
        metadata=metadata,
    )
    save_snapshot(snapshot)
    return snapshot


def database_display_value(value: object) -> object:
    """Keep table previews compact; complete data remains in JSON downloads."""

    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text if len(text) <= 500 else text[:497] + "..."


def connector_source_url(
    result: ConnectorResult,
    *,
    source: str | None = None,
) -> str:
    for record in result.records:
        source_url = record.get("source_url")
        if source_url:
            return str(source_url)
    for provenance in reversed(result.provenance):
        if provenance.citation_url:
            return provenance.citation_url
        if provenance.endpoint_url:
            return provenance.endpoint_url
    return DATABASE_HOME_URLS.get(source or "", "")


def render_database_network(network: EvidenceNetwork) -> None:
    st.subheader("STRING + DAVID 证据网络")
    st.caption(
        "边按实验、数据库整理、文本推断和计算预测分层；"
        "STRING combined score 只用于排序，不被当作独立证据。"
    )
    metrics = st.columns(4)
    metrics[0].metric("节点", len(network.nodes))
    metrics[1].metric("分层证据边", len(network.edges))
    metrics[2].metric("排序关系", len(network.rankings))
    metrics[3].metric("富集结果", len(network.enrichment))

    if network.edges:
        st.markdown("**分层证据边**")
        st.dataframe(
            [
                {
                    "起点": edge.source_node_id,
                    "终点": edge.target_node_id,
                    "关系": edge.relationship,
                    "证据类型": DATABASE_EVIDENCE_TYPE_LABELS[
                        edge.evidence_type.value
                    ],
                    "证据通道": edge.evidence_channel,
                    "通道分数": edge.channel_score,
                    "combined 排序分数": edge.ranking_score,
                    "来源版本": edge.trace.source_version or "",
                    "来源 URL": edge.trace.source_url,
                    "原始响应 SHA-256": edge.trace.raw_response_sha256 or "",
                }
                for edge in network.edges
            ],
            column_config={
                "来源 URL": st.column_config.LinkColumn("来源 URL"),
            },
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("当前 STRING 结果没有可分层的证据通道。")

    if network.rankings:
        st.markdown("**combined score 排序（不是证据边）**")
        st.dataframe(
            [
                {
                    "起点": item.source_node_id,
                    "终点": item.target_node_id,
                    "combined score": item.combined_score,
                    "用途": "仅排序",
                    "来源 URL": item.trace.source_url,
                }
                for item in network.rankings
            ],
            column_config={
                "来源 URL": st.column_config.LinkColumn("来源 URL"),
            },
            hide_index=True,
            width="stretch",
        )

    if network.enrichment:
        st.markdown("**DAVID 富集与多重检验校正**")
        st.dataframe(
            [
                {
                    "类别": item.category,
                    "条目 ID": item.term_id,
                    "条目名称": item.term_name,
                    "命中基因": "；".join(item.gene_ids),
                    "命中数": item.hit_count,
                    "输入总数": item.input_total,
                    "背景命中数": item.background_hit_count,
                    "背景总数": item.background_total,
                    "P 值": item.p_value,
                    "BH 校正 P 值": item.bh_adjusted_p_value,
                    "校正来源": item.correction_source,
                    "富集倍数": item.fold_enrichment,
                    "TaxID": item.taxon_id,
                    "证据类型": DATABASE_EVIDENCE_TYPE_LABELS[
                        item.evidence_type.value
                    ],
                    "来源 URL": item.trace.source_url,
                }
                for item in network.enrichment
            ],
            column_config={
                "来源 URL": st.column_config.LinkColumn("来源 URL"),
            },
            hide_index=True,
            width="stretch",
        )

    for warning in network.warnings:
        st.warning(warning)
    st.download_button(
        "下载证据网络 JSON",
        data=json.dumps(
            network.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        file_name="string-david-evidence-network.json",
        mime="application/json",
        key="download-database-evidence-network",
        on_click="ignore",
        width="stretch",
    )


def render_database_results(
    snapshot: WorkbenchRunSnapshot,
    state: dict[str, object],
) -> WorkbenchRunSnapshot:
    run_id = snapshot.run_id
    entries = state.get("entries")
    valid_entries = [
        entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("result"), ConnectorResult)
    ]
    if not valid_entries:
        st.info("尚无数据库查询结果。提交上方表单后，结果与原始响应将写入本地归档。")
        return snapshot

    st.subheader("连接器结果与可追溯归档")
    st.dataframe(
        [
            {
                "数据源": entry["source"],
                "查询 ID": entry["query_id"],
                "状态": DATABASE_STATUS_LABELS[entry["result"].status],
                "标准化记录": len(entry["result"].records),
                "原始响应": len(entry["result"].artifacts),
                "来源版本": next(
                    (
                        provenance.source_version
                        for provenance in reversed(entry["result"].provenance)
                        if provenance.source_version
                    ),
                    "",
                ),
                "访问时间 UTC": next(
                    (
                        provenance.retrieved_at_utc.isoformat()
                        for provenance in reversed(entry["result"].provenance)
                    ),
                    "",
                ),
                "来源 URL": connector_source_url(
                    entry["result"],
                    source=str(entry["source"]),
                ),
                "归档目录": entry["archive_path"],
            }
            for entry in valid_entries
        ],
        column_config={
            "来源 URL": st.column_config.LinkColumn("来源 URL"),
        },
        hide_index=True,
        width="stretch",
    )

    st.caption(
        f"当前会话显示最近 {min(len(valid_entries), MAX_DATABASE_HISTORY)} 次查询；"
        "完整原始响应、清单与 SHA-256 校验文件保存在 .workbench/connectors/。"
    )
    for entry in reversed(valid_entries[-20:]):
        result = entry["result"]
        query_id = str(entry["query_id"])
        with st.expander(
            f"{entry['source']} · {query_id} · "
            f"{DATABASE_STATUS_LABELS[result.status]}"
        ):
            if result.warnings:
                for warning in result.warnings:
                    st.warning(warning)

            if result.records:
                record_rows = []
                for record in result.records:
                    row = {
                        key: database_display_value(value)
                        for key, value in record.items()
                        if key != "source_url"
                    }
                    row["来源 URL"] = str(record.get("source_url") or "")
                    record_rows.append(row)
                st.markdown("**标准化记录预览**")
                st.dataframe(
                    record_rows,
                    column_config={
                        "来源 URL": st.column_config.LinkColumn("来源 URL"),
                    },
                    hide_index=True,
                    width="stretch",
                )

            if result.mappings:
                st.markdown("**标识符映射**")
                st.dataframe(
                    [
                        {
                            "输入标识符": mapping.input_identifier,
                            "命名空间": mapping.namespace,
                            "规范标识符": mapping.canonical_identifier or "",
                            "候选项": database_display_value(
                                [
                                    candidate.model_dump(mode="json")
                                    for candidate in mapping.candidates
                                ]
                            ),
                            "是否歧义": mapping.ambiguous,
                            "映射方法": mapping.mapping_method,
                            "TaxID": mapping.taxon_id,
                            "警告": mapping.warning or "",
                        }
                        for mapping in result.mappings
                    ],
                    hide_index=True,
                    width="stretch",
                )

            if result.provenance:
                st.markdown("**来源与原始响应哈希**")
                st.dataframe(
                    [
                        {
                            "数据源": provenance.source_name,
                            "方法": provenance.method,
                            "端点 URL": provenance.endpoint_url,
                            "HTTP": provenance.http_status,
                            "来源版本": provenance.source_version or "",
                            "发布日期": provenance.source_release_date or "",
                            "访问时间 UTC": provenance.retrieved_at_utc.isoformat(),
                            "稳定 ID": "；".join(provenance.stable_ids),
                            "请求 SHA-256": provenance.request_sha256,
                            "响应 SHA-256": provenance.raw_response_sha256,
                            "引用 URL": provenance.citation_url or "",
                        }
                        for provenance in result.provenance
                    ],
                    column_config={
                        "端点 URL": st.column_config.LinkColumn("端点 URL"),
                        "引用 URL": st.column_config.LinkColumn("引用 URL"),
                    },
                    hide_index=True,
                    width="stretch",
                )

            archive_valid = True
            try:
                CONNECTOR_ARTIFACT_STORE.load_manifest(run_id, query_id)
            except (ConnectorArchiveError, OSError, ValueError) as exc:
                archive_valid = False
                safe_error = redact_database_error(exc)
                st.warning(
                    "原始响应归档缺失或完整性校验失败，已禁用 ZIP 下载；"
                    f"标准化 JSON 与离线请求仍可下载。详情：{safe_error}"
                )
                if not entry.get("archive_error_logged"):
                    snapshot = record_database_failure(
                        snapshot,
                        source="数据库结果归档",
                        input_summary=f"校验连接器归档 {query_id}",
                        error=exc,
                    )
                    entry["archive_error_logged"] = True
                    st.session_state[DATABASE_RESULTS_STATE_KEY] = state

            exported = export_connector_result(result)
            with st.container(horizontal=True):
                st.download_button(
                    "下载标准化结果 JSON",
                    data=exported.content,
                    file_name=f"{query_id}-result.json",
                    mime=exported.media_type,
                    key=f"download-database-result-{query_id}",
                    on_click="ignore",
                )

                def build_archive(
                    active_run_id: str = run_id,
                    active_query_id: str = query_id,
                ) -> bytes:
                    return CONNECTOR_ARTIFACT_STORE.build_zip(
                        active_run_id,
                        active_query_id,
                    )

                if archive_valid:
                    st.download_button(
                        "下载原始响应归档 ZIP",
                        data=build_archive,
                        file_name=f"{query_id}-archive.zip",
                        mime="application/zip",
                        key=f"download-database-archive-{query_id}",
                        on_click="ignore",
                    )
                if result.offline_request:
                    st.download_button(
                        "下载离线请求 JSON",
                        data=result.offline_request.content,
                        file_name=f"{query_id}-offline-request.json",
                        mime=result.offline_request.media_type,
                        key=f"download-database-offline-{query_id}",
                        on_click="ignore",
                    )

    network = state.get("network")
    if isinstance(network, EvidenceNetwork):
        st.divider()
        render_database_network(network)
    return snapshot


def render_articles(
    articles: list[PubMedArticle],
    conditions: list[ExperimentCondition] | None = None,
) -> None:
    qualification_by_pmid = {
        condition.pmid: condition.qualification
        for condition in (conditions or [])
        if condition.pmid
    }
    rows = []
    for article in articles:
        qualification = qualification_by_pmid.get(article.pmid)
        rows.append(
            {
                "证据等级": (
                    EVIDENCE_GRADE_LABELS[qualification.grade]
                    if qualification
                    else "未评估"
                ),
                "准入理由": (
                    "；".join(qualification.reasons) if qualification else ""
                ),
                "年份": article.year,
                "标题": article.title,
                "期刊": article.journal or "",
                "中科院分区（LetPub）": (
                    article.journal_ranking.cas_display()
                    if article.journal_ranking
                    else "未获取"
                ),
                "JCR 分区（LetPub/JIF）": (
                    article.journal_ranking.jcr_display()
                    if article.journal_ranking
                    else "未获取"
                ),
                "PMID": article.pmid,
                "DOI": article.doi or "",
            }
        )
    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
    )
    for article in articles:
        qualification = qualification_by_pmid.get(article.pmid)
        with st.expander(article.title):
            st.caption(
                " · ".join(
                    [
                        article.journal or "期刊未报告",
                        str(article.year) if article.year else "年份未报告",
                        f"PMID {article.pmid}",
                        f"DOI {article.doi}" if article.doi else "DOI 未报告",
                    ]
                )
            )
            if article.authors:
                st.write("作者：" + ", ".join(article.authors))
            if qualification:
                grade_label = EVIDENCE_GRADE_LABELS[qualification.grade]
                message = f"{grade_label}：{'；'.join(qualification.reasons)}"
                if (
                    qualification.grade
                    is LiteratureEvidenceGrade.DIRECT_INTERACTION
                ):
                    st.success(message)
                elif qualification.grade is LiteratureEvidenceGrade.CONTEXTUAL:
                    st.warning(message)
                else:
                    st.info(message)
                if qualification.supporting_quote:
                    st.caption("证据判定匹配原句：" + qualification.supporting_quote)
            ranking = article.journal_ranking
            if ranking:
                columns = st.columns(2)
                columns[0].markdown(
                    f"**中科院分区（{ranking.cas_edition}）**\n\n"
                    f"{ranking.cas_display()}"
                )
                columns[1].markdown(
                    f"**JCR 分区（{ranking.jcr_edition}）**\n\n"
                    f"{ranking.jcr_display()}"
                )
                links = []
                if ranking.cas_source_url:
                    links.append(
                        f"[核查中科院分区来源]({ranking.cas_source_url})"
                    )
                if ranking.jcr_source_url:
                    links.append(
                        f"[核查 JCR 分区来源]({ranking.jcr_source_url})"
                    )
                if links:
                    st.markdown(" · ".join(links))
                if ranking.source_note:
                    st.caption(ranking.source_note)
            st.markdown(f"[在 PubMed 核查原始记录]({article.source_url})")
            st.write(article.abstract or "摘要未提供。")


def render_assessment(assessment: EvidenceAssessment) -> None:
    admission = assessment.evidence_admission
    if admission.status is EvidenceAdmissionStatus.ADMITTED:
        st.success(admission.reason)
    else:
        st.warning(admission.reason)
    st.subheader("一致性、冲突与证据空白")
    if assessment.consistencies:
        for item in assessment.consistencies:
            st.success(item)
    else:
        st.info("当前没有达到规则阈值的多来源一致性信号。")
    if assessment.conflicts:
        for conflict in assessment.conflicts:
            st.warning(f"{conflict.description} {conflict.impact}")
    else:
        st.caption("当前未检测到满足规则定义的显式方向冲突。")
    if assessment.gaps:
        st.dataframe(
            [
                {
                    "字段": gap.topic,
                    "证据空白": gap.missing_evidence,
                    "影响": gap.impact,
                    "建议动作": gap.recommended_action,
                }
                for gap in assessment.gaps
            ],
            width="stretch",
            hide_index=True,
        )


def render_analysis(snapshot: WorkbenchRunSnapshot) -> None:
    analysis = snapshot.analysis
    if analysis and analysis.valid:
        if experiment_analysis_matches_question(snapshot.question, analysis):
            st.success("CSV 身份与当前科研问题一致，可以进入报告候选。")
        else:
            st.error(
                "CSV 虽可计算，但药物或病原体/菌株与当前科研问题不一致，"
                "已阻断其进入报告。"
            )
    if isinstance(analysis, FICIAnalysisResult):
        st.subheader("FICI 结果")
        st.dataframe(
            [
                {
                    "CSV 行": row.row_number,
                    "药物 A": row.drug_a or "",
                    "药物 B": row.drug_b or "",
                    "病原体/菌株": row.population_or_strain or "",
                    "FIC A": row.fic_a,
                    "FIC B": row.fic_b,
                    "FICI": row.fici,
                    "分类": row.classification,
                    "有效": row.valid,
                    "错误": "；".join(row.errors),
                }
                for row in analysis.rows
            ],
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "阈值：FICI ≤ 0.5 协同；≤ 1 相加；≤ 4 无相互作用"
            "（indifferent）；> 4 拮抗。"
        )
    elif isinstance(analysis, GrowthCurveAnalysisResult):
        st.subheader("生长曲线结果")
        valid_observations = [row for row in analysis.rows if row.valid]
        if valid_observations:
            first_scope = valid_observations[0]
            st.caption(
                f"范围：{first_scope.intervention} + {first_scope.comparator} / "
                f"{first_scope.population_or_strain}"
            )
        chart_rows = [
            {
                "time": row.time,
                "group": row.group,
                "mean": row.mean,
            }
            for row in analysis.timepoints
        ]
        if chart_rows:
            st.line_chart(chart_rows, x="time", y="mean", color="group")
        st.dataframe(
            [
                {
                    "组别": row.group,
                    "时间": row.time,
                    "均值": row.mean,
                    "标准差": row.sd,
                    "重复数": row.n,
                    "来源行": ",".join(map(str, row.source_row_numbers)),
                }
                for row in analysis.timepoints
            ],
            width="stretch",
            hide_index=True,
        )
        st.dataframe(
            [
                {
                    "组别": row.group,
                    "AUC": row.auc,
                    "时间点数": row.n_timepoints,
                    "起点": row.start_time,
                    "终点": row.end_time,
                }
                for row in analysis.auc_by_group
            ],
            width="stretch",
            hide_index=True,
        )
    if analysis and analysis.errors:
        for error in analysis.errors:
            st.error(error)


def render_mechanism_prediction(
    bundle: MechanismPredictionBundle,
    *,
    run_id: str | None = None,
) -> None:
    """Render predictions separately from literature and experimental evidence."""

    if bundle.network is not None:
        network = bundle.network
        st.subheader("网络药理学结果")
        metrics = st.columns(4)
        metrics[0].metric("输入化合物", network.summary.input_compound_count)
        metrics[1].metric("研究对象", network.summary.input_organism_count)
        metrics[2].metric("交集靶点", network.summary.intersection_target_count)
        metrics[3].metric("交集通路", network.summary.intersection_pathway_count)
        st.caption(
            "透明排名规则：compound_degree × pathway_degree。"
            "它只描述导入关系的网络拓扑，不证明靶点有效或药物协同。"
        )
        st.dataframe(
            [
                {
                    "排名": item.rank,
                    "研究对象": item.organism,
                    "靶点": item.target,
                    "靶点 accession": item.target_accession,
                    "化合物数": item.compound_degree,
                    "通路数": item.pathway_degree,
                    "网络分数": item.network_score,
                    "化合物": "；".join(
                        f"{link.compound} ({link.compound_accession})"
                        for link in item.compounds
                    ),
                    "化合物 accession": "；".join(item.compound_accessions),
                    "通路": "；".join(
                        f"{link.pathway} ({link.pathway_accession})"
                        for link in item.pathways
                    ),
                    "通路 accession": "；".join(item.pathway_accessions),
                }
                for item in network.ranked_targets
            ],
            width="stretch",
            hide_index=True,
        )
        st.dataframe(
            [
                {
                    "输入来源": source.source_name,
                    "accession": source.accession,
                    "版本": source.version,
                    "SHA-256": source.sha256 or "",
                }
                for source in network.sources
            ],
            width="stretch",
            hide_index=True,
        )
        export_columns = st.columns(2)
        export_columns[0].download_button(
            "下载 Excel 结果",
            data=network_result_to_xlsx(network),
            file_name="network_pharmacology_targets_pathways.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            key=f"download-network-xlsx-{run_id or 'current'}",
            width="stretch",
        )
        export_columns[1].download_button(
            "下载 Word 报告",
            data=network_result_to_docx(network),
            file_name="network_pharmacology_targets_pathways.docx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            key=f"download-network-docx-{run_id or 'current'}",
            width="stretch",
        )

    if bundle.prepared_manifests:
        st.subheader("AutoDock Vina 任务清单")
        runs_by_task = {
            run.manifest.task_id: run for run in bundle.docking_runs
        }
        st.dataframe(
            [
                {
                    "任务 ID": manifest.task_id,
                    "状态": (
                        (
                            "本机 Vina 已执行（可审计）"
                            if runs_by_task[
                                manifest.task_id
                            ].execution_audit is not None
                            else "已导入用户输出（未认证运行真实性）"
                        )
                        if manifest.task_id in runs_by_task
                        else "待运行，无分数"
                    ),
                    "配体": manifest.compound_name,
                    "配体 accession": manifest.ligand_accession,
                    "配体结构来源": manifest.ligand_source.source_name,
                    "配体来源版本": manifest.ligand_source.version,
                    "配体 PDBQT SHA-256": manifest.ligand_source.sha256,
                    "受体": manifest.receptor_name,
                    "受体 accession": manifest.receptor_accession,
                    "研究对象": manifest.receptor_organism,
                    "引擎版本": manifest.engine_version,
                    "任务清单 SHA-256": manifest.manifest_sha256,
                }
                for manifest in bundle.prepared_manifests
            ],
            width="stretch",
            hide_index=True,
        )
        for manifest in bundle.prepared_manifests:
            st.caption(
                f"任务 {manifest.task_id} 的输出必须保留绑定标记："
            )
            st.code(
                "VetEvidence-Manifest-SHA256: "
                f"{manifest.manifest_sha256}",
                language="text",
            )
            st.download_button(
                f"下载任务清单 {manifest.task_id}",
                data=json.dumps(
                    manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                file_name=f"{manifest.task_id}.json",
                mime="application/json",
                key=f"download-manifest-{manifest.task_id}",
                width="stretch",
            )

    if bundle.docking_runs:
        st.subheader("AutoDock Vina 对接结果")
        if any(run.execution_audit is None for run in bundle.docking_runs):
            st.warning(
                "用户导入输出只经过格式、版本和内容哈希校验，系统不能证明"
                "文件确由 Vina 实际运行产生。"
            )
        if any(run.execution_audit is not None for run in bundle.docking_runs):
            st.info(
                "本机结果记录了 Vina 可执行文件、参数、退出码和输出哈希；"
                "这些记录仍不能替代结构准备审查或结合实验。"
            )
        st.caption(
            "所有对接得分都属于计算预测，不是结合实验证据，也不能单独证明"
            "抗菌活性或药物协同。"
        )
        for run in bundle.docking_runs:
            with st.expander(
                f"{run.manifest.compound_name} × {run.manifest.receptor_name} · "
                f"最佳 {run.best_affinity_kcal_mol:g} kcal/mol"
            ):
                st.caption(
                    f"任务 {run.manifest.task_id} · AutoDock Vina "
                    f"{run.manifest.engine_version} · "
                    f"输出 SHA-256 {run.output_source.sha256}"
                )
                if run.execution_audit is not None:
                    audit = run.execution_audit
                    st.caption(
                        "VetEvidence Agent 本机执行 · "
                        f"退出码 {audit.exit_code} · "
                        f"{audit.duration_seconds:.3f} 秒 · "
                        f"可执行文件 SHA-256 {audit.executable_sha256}"
                    )
                st.dataframe(
                    [
                        {
                            "模式": pose.mode,
                            "affinity (kcal/mol)": pose.affinity_kcal_mol,
                            "RMSD lower": pose.rmsd_lower_bound,
                            "RMSD upper": pose.rmsd_upper_bound,
                        }
                        for pose in run.poses
                    ],
                    width="stretch",
                    hide_index=True,
                )
                if run.execution_audit is not None and run_id is not None:
                    try:
                        artifacts = VINA_ARTIFACT_STORE.load(
                            run_id,
                            run.manifest.task_id,
                            expected_manifest_sha256=(
                                run.manifest.manifest_sha256
                            ),
                        )
                    except (OSError, ValueError) as exc:
                        st.caption(f"本地 Vina 产物当前不可下载：{exc}")
                    else:
                        with st.container(horizontal=True):
                            st.download_button(
                                "下载 Vina 绑定日志",
                                data=artifacts.bound_log,
                                file_name=f"{run.manifest.task_id}.log",
                                mime="text/plain",
                                key=f"download-vina-log-{run.manifest.task_id}",
                            )
                            st.download_button(
                                "下载对接构象 PDBQT",
                                data=artifacts.output_pdbqt,
                                file_name=f"{run.manifest.task_id}-poses.pdbqt",
                                mime="chemical/x-pdb",
                                key=f"download-vina-poses-{run.manifest.task_id}",
                            )


st.set_page_config(
    page_title="VetResearch Workbench",
    page_icon="🧪",
    layout="wide",
)

st.title("VetResearch Workbench")
st.caption(
    "VetResearch Workbench v0.4 · 文献、实验、数据库证据、网络药理学、"
    "分子对接与人工复核的可审计科研决策闭环"
)
st.warning(
    "仅用于科研证据整理与实验设计支持，不构成医疗、兽医诊断、处方或临床建议。"
)

with st.sidebar:
    st.header("工作台设置")
    max_results = st.slider("最多保留文献数", 1, 20, 8)
    max_queries = st.slider("自动检索轮数", 1, 3, 3)
    st.caption(
        "检索使用 NCBI PubMed；期刊分区按 ISSN 查询 LetPub 并同时显示"
        "中科院 2025 年 3 月升级版和 WOS JIF 分区。"
    )
    st.caption(
        "用户导入题录、CSV、Excel 与 Word 文件只在本机处理；"
        "未报告字段保持为空，不由系统补造。"
    )
    st.caption(
        "当前版本仅支持可信的单用户本机运行，不具备共享部署所需的账号与对象授权。"
    )
    st.caption("透明规则工作流，无需 LLM API Key。")

(
    question_tab,
    literature_tab,
    experiment_tab,
    database_tab,
    mechanism_tab,
    report_tab,
    audit_tab,
) = st.tabs(
    [
        "1 问题与假设",
        "2 文献证据",
        "3 实验数据",
        "4 数据库证据",
        "5 网络与对接",
        "6 决策报告",
        "7 运行记录",
    ]
)

with question_tab:
    st.header("定义科研问题")
    preset_name = st.selectbox(
        "公开验收案例",
        list(QUESTION_PRESETS),
        help=(
            "这些内容只用于验收流程，不代表系统预设答案。PubMed 是实时"
            "数据源，证据数量和结论可能随检索日期变化。所有字段仍可修改。"
        ),
    )
    preset = QUESTION_PRESETS[preset_name]
    with st.form("research_question_form"):
        question_text = st.text_area(
            "科研问题 *",
            value=preset["question"],
            help="正文必须与下方三个结构化范围字段一致。",
        )
        question_columns = st.columns(3)
        population = question_columns[0].text_input(
            "病原体/研究对象 *",
            value=preset["population"],
        )
        intervention = question_columns[1].text_input(
            "候选干预 *",
            value=preset["intervention"],
        )
        comparator = question_columns[2].text_input(
            "对照/联合药物 *",
            value=preset["comparator"],
        )
        outcomes_text = st.text_input(
            "预设结局指标（逗号分隔）*",
            value=preset["outcomes"],
        )
        create_task = st.form_submit_button(
            "创建或重置研究任务",
            type="primary",
            width="stretch",
        )

    if create_task:
        outcomes = [
            value.strip()
            for value in outcomes_text.replace("，", ",").split(",")
            if value.strip()
        ]
        input_errors = validate_synergy_question_input(
            question_text=question_text,
            population=population,
            intervention=intervention,
            comparator=comparator,
            outcomes=outcomes,
        )
        if input_errors:
            for error in input_errors:
                st.error(error)
        else:
            run_id = (
                f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
                f"{uuid4().hex}"
            )
            research_question = ResearchQuestion(
                id=f"rq-{uuid4().hex[:12]}",
                text=question_text,
                population=population,
                intervention=intervention,
                comparator=comparator,
                outcomes=outcomes,
                context="候选药物与抗生素协同作用的首个垂直场景",
            )
            hypotheses = decompose_research_question(research_question)
            snapshot = WorkbenchRunSnapshot(
                run_id=run_id,
                question=research_question,
                query_plan=generate_search_queries(
                    research_question,
                    max_queries=max_queries,
                ),
                hypotheses=hypotheses,
                task_events=[
                    build_task_event(
                        run_id,
                        TaskStatus.PENDING,
                        "研究任务已创建，等待执行文献检索。",
                        actor="user",
                    )
                ],
                tool_calls=[
                    build_tool_call(
                        "question.decompose",
                        question_text,
                        status="succeeded",
                        output_summary=f"生成 {len(hypotheses)} 条可检验假设",
                    )
                ],
            )
            save_snapshot(snapshot)
            st.success(f"已创建任务 {run_id}")

    snapshot = current_snapshot()
    if snapshot:
        st.subheader("可检验假设（可人工修改）")
        hypothesis_rows = [
            {
                "编号": hypothesis.id,
                "类型": hypothesis.kind.value,
                "假设": hypothesis.statement,
                "验证方法": hypothesis.verification_method,
                "成功标准": hypothesis.success_criteria,
                "生成规则": hypothesis.rule_id,
            }
            for hypothesis in snapshot.hypotheses
        ]
        edited_rows = st.data_editor(
            hypothesis_rows,
            disabled=["编号", "类型", "验证方法", "成功标准", "生成规则"],
            width="stretch",
            hide_index=True,
            key=f"hypotheses-{snapshot.run_id}",
        )
        if st.button("保存假设修改", width="stretch"):
            try:
                updated_hypotheses = [
                    type(hypothesis).model_validate(
                        {
                            **hypothesis.model_dump(mode="python"),
                            "statement": edited_rows[index]["假设"],
                        }
                    )
                    for index, hypothesis in enumerate(snapshot.hypotheses)
                ]
            except (ValidationError, ValueError) as exc:
                st.error(f"假设修改无效：{exc}")
            else:
                snapshot = snapshot.model_copy(
                    update={
                        "hypotheses": updated_hypotheses,
                        "report": None,
                    }
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.PENDING,
                    "用户保存了假设修改，旧报告及复核已失效。",
                    actor="user",
                )
                save_snapshot(snapshot)
                st.success("假设修改已写入运行记录，需重新生成并复核报告。")

        st.subheader("自动生成的 PubMed 检索式")
        for index, query in enumerate(snapshot.query_plan.queries, start=1):
            st.code(f"{index}. {query}", language=None)
    else:
        st.info("先创建研究任务，系统会生成 2—4 条可检验假设和最多 3 轮检索式。")

with literature_tab:
    snapshot = current_snapshot()
    if not snapshot:
        st.info("请先在“问题与假设”创建任务。")
    else:
        st.header("多轮 PubMed 检索")
        st.caption(
            "系统扩大各轮候选池，保留相关性顺序并按轮公平去重，再按"
            "直接、间接、主题不匹配稳定分桶后限制页面数量。"
        )
        if st.button(
            f"执行 {len(snapshot.query_plan.queries)} 轮 PubMed 检索",
            type="primary",
            width="stretch",
        ):
            snapshot = append_event(
                snapshot,
                TaskStatus.RUNNING,
                "开始执行多轮 PubMed 检索。",
            )
            snapshot = snapshot.model_copy(update={"report": None})
            save_snapshot(snapshot)
            client = PubMedClient(load_settings())
            try:
                with st.spinner("正在调用 NCBI 并核查期刊分区…"):
                    multi_result = run_multi_query_research(
                        snapshot.question,
                        max_results=max_results,
                        max_queries=max_queries,
                        client=client,
                    )
            except (PubMedError, ValueError) as exc:
                snapshot = append_tool_call(
                    snapshot,
                    "pubmed.multi_search",
                    f"{len(snapshot.query_plan.queries)} 个检索式",
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"PubMed 检索失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(f"PubMed 检索失败：{exc}")
            else:
                conditions = build_experiment_conditions(
                    multi_result.research,
                    snapshot.literature_import,
                    question=snapshot.question,
                )
                assessment = assess_evidence(
                    conditions,
                    snapshot.analysis,
                    question=snapshot.question,
                )
                snapshot = snapshot.model_copy(
                    update={
                        "query_plan": multi_result.query_plan,
                        "research": multi_result.research,
                        "conditions": conditions,
                        "assessment": assessment,
                        "report": None,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    "pubmed.multi_search",
                    f"{len(multi_result.query_plan.queries)} 个检索式",
                    status="succeeded",
                    output_summary=(
                        "公平合并后获得 "
                        f"{len(multi_result.research.articles)} 个唯一 PMID"
                    ),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.RUNNING,
                    (
                        f"PubMed 检索完成，获得 "
                        f"{len(multi_result.research.articles)} 篇唯一文献。"
                    ),
                )
                save_snapshot(snapshot)
                st.success("多轮检索和证据提取完成。")
            finally:
                client.close()

        snapshot = current_snapshot()
        if snapshot.research:
            direct_count = sum(
                condition.qualification.grade
                is LiteratureEvidenceGrade.DIRECT_INTERACTION
                for condition in snapshot.conditions
                if condition.source_type == "pubmed"
            )
            contextual_count = sum(
                condition.qualification.grade
                is LiteratureEvidenceGrade.CONTEXTUAL
                for condition in snapshot.conditions
                if condition.source_type == "pubmed"
            )
            excluded_count = sum(
                condition.qualification.grade
                in {
                    LiteratureEvidenceGrade.OUT_OF_SCOPE,
                    LiteratureEvidenceGrade.UNASSESSED,
                }
                for condition in snapshot.conditions
                if condition.source_type == "pubmed"
            )
            metric_columns = st.columns(5)
            metric_columns[0].metric("唯一文献", len(snapshot.research.articles))
            metric_columns[1].metric("直接文献", direct_count)
            metric_columns[2].metric("间接背景", contextual_count)
            metric_columns[3].metric("主题不匹配/未评估", excluded_count)
            metric_columns[4].metric(
                "NCBI 请求", snapshot.research.retrieval_request_count
            )
            render_articles(snapshot.research.articles, snapshot.conditions)
        else:
            st.info("尚未执行 PubMed 检索。")

        st.divider()
        st.header("导入 RIS / EndNote / RefWorks")
        st.caption(
            "这是知网等平台导出文件的本地导入接口，不会绕过平台权限自动抓取。"
        )
        import_file = st.file_uploader(
            "上传题录导出文件",
            type=["ris", "enw", "txt"],
            key=f"literature-import-{snapshot.run_id}",
        )
        import_columns = st.columns(2)
        parse_uploaded = import_columns[0].button(
            "解析上传文件",
            disabled=import_file is None,
            width="stretch",
        )
        load_import_demo = import_columns[1].button(
            "加载合成 RIS 演示",
            width="stretch",
        )
        import_payload: bytes | None = None
        import_input = ""
        if parse_uploaded and import_file:
            import_payload = import_file.getvalue()
            import_input = import_file.name
        elif load_import_demo:
            import_payload = (
                PROJECT_ROOT / "data" / "demo" / "cnki_export_demo.ris"
            ).read_bytes()
            import_input = "合成演示数据 cnki_export_demo.ris"

        if import_payload is not None:
            snapshot = snapshot.model_copy(update={"report": None})
            save_snapshot(snapshot)
            try:
                imported = parse_literature_export(
                    import_payload,
                    pubmed_articles=(
                        snapshot.research.articles if snapshot.research else []
                    ),
                )
            except ValueError as exc:
                snapshot = append_tool_call(
                    snapshot,
                    "literature.import",
                    import_input,
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"文献导入失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(str(exc))
            else:
                conditions = build_experiment_conditions(
                    snapshot.research,
                    imported,
                    question=snapshot.question,
                )
                assessment = assess_evidence(
                    conditions,
                    snapshot.analysis,
                    question=snapshot.question,
                )
                snapshot = snapshot.model_copy(
                    update={
                        "literature_import": imported,
                        "conditions": conditions,
                        "assessment": assessment,
                        "report": None,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    "literature.import",
                    import_input,
                    status="succeeded",
                    output_summary=(
                        f"保留 {len(imported.records)} 条，"
                        f"排除 {len(imported.duplicates)} 条重复记录"
                    ),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.RUNNING,
                    "文献导入、规范化和去重完成。",
                )
                save_snapshot(snapshot)
                st.success("导入完成。演示文件中的内容是合成数据，不是科研事实。")

        snapshot = current_snapshot()
        if snapshot.literature_import:
            imported = snapshot.literature_import
            st.dataframe(
                [
                    {
                        "来源 ID": record.source_id,
                        "题名": record.title,
                        "年份": record.year,
                        "期刊": record.journal or "",
                        "DOI": record.doi or "",
                        "PMID": "",
                        "警告": "；".join(record.warnings),
                    }
                    for record in imported.records
                ],
                width="stretch",
                hide_index=True,
            )
            if imported.duplicates:
                st.info(f"已排除 {len(imported.duplicates)} 条重复记录。")

with experiment_tab:
    snapshot = current_snapshot()
    if not snapshot:
        st.info("请先创建研究任务。")
    else:
        st.header("实验条件矩阵")
        if snapshot.conditions:
            st.caption("空值表示来源未报告；用户导入文献不会伪造 PMID。")
            st.dataframe(
                experiment_condition_rows(snapshot.conditions),
                width="stretch",
                hide_index=True,
            )
            if snapshot.assessment:
                render_assessment(snapshot.assessment)
        else:
            st.info("完成文献检索或导入后，这里会比较物种、剂量、时间和指标。")

        st.divider()
        st.header("实验 CSV 分析")
        analysis_label = st.radio(
            "分析类型",
            ["FICI", "生长曲线"],
            horizontal=True,
            key=f"analysis-type-{snapshot.run_id}",
        )
        analysis_type = "fici" if analysis_label == "FICI" else "growth_curve"
        template_name = (
            "fici_template.csv"
            if analysis_type == "fici"
            else "growth_curve_template.csv"
        )
        demo_name = (
            "fici_demo.csv"
            if analysis_type == "fici"
            else "growth_curve_demo.csv"
        )
        if analysis_type == "fici":
            st.info(
                "上传前检查：必填 drug_a、drug_b、population_or_strain、"
                "drug_a_mic_alone、drug_a_mic_combo、drug_b_mic_alone、"
                "drug_b_mic_combo；MIC 必须为大于 0 的有限数值。"
            )
        else:
            st.info(
                "上传前检查：必填 population_or_strain、intervention、"
                "comparator、time、group、value；每组至少需要两个不同时间点。"
            )
        st.download_button(
            f"下载{analysis_label} CSV 模板",
            data=(PROJECT_ROOT / "data" / "templates" / template_name).read_bytes(),
            file_name=template_name,
            mime="text/csv",
            width="stretch",
        )
        experiment_file = st.file_uploader(
            "上传 CSV",
            type=["csv"],
            key=f"experiment-upload-{snapshot.run_id}-{analysis_type}",
        )
        analysis_columns = st.columns(2)
        analyze_uploaded = analysis_columns[0].button(
            "分析上传 CSV",
            disabled=experiment_file is None,
            width="stretch",
        )
        analyze_demo = analysis_columns[1].button(
            f"分析合成{analysis_label}演示",
            width="stretch",
        )
        analysis_payload: bytes | None = None
        analysis_input = ""
        is_demo_analysis = False
        if analyze_uploaded and experiment_file:
            analysis_payload = experiment_file.getvalue()
            analysis_input = experiment_file.name
        elif analyze_demo:
            analysis_payload = (
                PROJECT_ROOT / "data" / "demo" / demo_name
            ).read_bytes()
            analysis_input = f"合成演示数据 {demo_name}"
            is_demo_analysis = True

        if analysis_payload is not None:
            snapshot = snapshot.model_copy(update={"report": None})
            save_snapshot(snapshot)
            analysis = analyze_experiment_csv(
                analysis_payload,
                analysis_type=analysis_type,
                source_name=(
                    demo_name
                    if is_demo_analysis
                    else analysis_input
                ),
            )
            if analysis.valid:
                assessment = assess_evidence(
                    snapshot.conditions,
                    analysis,
                    question=snapshot.question,
                )
                snapshot = snapshot.model_copy(
                    update={
                        "analysis": analysis,
                        "assessment": assessment,
                        "report": None,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    f"csv.{analysis_type}",
                    analysis_input,
                    status="succeeded",
                    output_summary=(
                        f"{analysis.valid_row_count} 行有效，"
                        f"{analysis.invalid_row_count} 行无效"
                    ),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.RUNNING,
                    f"{analysis_label} CSV 描述性分析完成。",
                )
                save_snapshot(snapshot)
                if not experiment_analysis_matches_question(
                    snapshot.question,
                    analysis,
                ):
                    st.error(
                        "计算已完成，但 CSV 范围与当前科研问题不匹配，"
                        "不会进入报告。请核对药物和病原体/菌株列。"
                    )
                elif is_demo_analysis:
                    st.success(
                        "分析完成且范围匹配。演示 CSV 为合成数据，"
                        "不可作为科研证据。"
                    )
                else:
                    st.success("分析完成且范围匹配，可以进入报告候选。")
            else:
                assessment = assess_evidence(
                    snapshot.conditions,
                    analysis,
                    question=snapshot.question,
                )
                snapshot = snapshot.model_copy(
                    update={
                        "analysis": analysis,
                        "assessment": assessment,
                        "report": None,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    f"csv.{analysis_type}",
                    analysis_input,
                    status="failed",
                    error="；".join(analysis.errors),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"{analysis_label} CSV 校验失败，可修正后重试。",
                )
                save_snapshot(snapshot)

        render_analysis(current_snapshot())

with database_tab:
    snapshot = current_snapshot()
    if not snapshot:
        st.info("请先创建研究任务。")
    else:
        st.header("公开数据库证据")
        st.warning(
            "数据库记录、PPI 与富集分析属于可追溯的外部资料或计算证据，"
            "不等同于论文中的直接实验结论；物种必须用 NCBI TaxID 明确限定。"
        )
        st.caption(
            "所有外部请求都只在提交表单后执行。每次结果会保存请求参数、"
            "数据库版本、访问时间、来源 URL、原始响应及 SHA-256；"
            "联系方式和 API Key 不写入结果或审计摘要。"
        )
        settings = load_settings()
        state = database_state(snapshot.run_id)

        with st.form(
            f"database-connectors-{snapshot.run_id}",
            enter_to_submit=False,
        ):
            st.subheader("共同范围与联系信息")
            shared_columns = st.columns(3)
            taxon_id = int(
                shared_columns[0].number_input(
                    "NCBI TaxID",
                    min_value=1,
                    value=9606,
                    step=1,
                    key=f"database-taxon-{snapshot.run_id}",
                    help="所有蛋白、基因、PPI 和富集结果共用这一物种范围。",
                )
            )
            ncbi_email = shared_columns[1].text_input(
                "NCBI 联系邮箱（可选）",
                value=settings.ncbi_email or "",
                key=f"database-ncbi-email-{snapshot.run_id}",
                help="未提供时 NCBI Gene/GenBank 不发送请求，只生成离线请求文件。",
            ).strip()
            david_email = shared_columns[2].text_input(
                "DAVID 注册邮箱（可选）",
                value=os.getenv("DAVID_EMAIL", ""),
                key=f"database-david-email-{snapshot.run_id}",
                help="DAVID Web Service 需要已注册邮箱；该值不会写入审计摘要。",
            ).strip()

            with st.expander("化合物与蛋白", expanded=True):
                compound_columns = st.columns(2)
                with compound_columns[0]:
                    pubchem_namespace_label = st.selectbox(
                        "PubChem 输入类型",
                        ["名称", "CID", "InChIKey"],
                        key=f"database-pubchem-namespace-{snapshot.run_id}",
                    )
                    pubchem_input = st.text_area(
                        "PubChem 化合物（最多 10 项）",
                        key=f"database-pubchem-{snapshot.run_id}",
                        help=(
                            "每行或分号分隔一项；名称模式不会按逗号切分，"
                            "以免破坏系统命名。"
                        ),
                        placeholder="quercetin",
                    )
                    pubchem_include_3d = st.checkbox(
                        "同时获取可用的 PubChem 3D SDF",
                        key=f"database-pubchem-3d-{snapshot.run_id}",
                    )
                with compound_columns[1]:
                    uniprot_input = st.text_area(
                        "UniProt accession（最多 20 项）",
                        key=f"database-uniprot-{snapshot.run_id}",
                        help="一行一个标识符，也可用中文或英文分号分隔。",
                        placeholder="P69905",
                    )

            with st.expander("NCBI Gene、GenBank 与 RCSB PDB"):
                ncbi_columns = st.columns(3)
                with ncbi_columns[0]:
                    gene_identifier_label = st.selectbox(
                        "NCBI Gene 输入类型",
                        ["GeneID", "基因符号"],
                        key=f"database-gene-mode-{snapshot.run_id}",
                    )
                    gene_input = st.text_area(
                        "NCBI Gene（最多 20 项）",
                        key=f"database-gene-{snapshot.run_id}",
                        help="一行一个标识符，也可用中文或英文分号分隔。",
                        placeholder="3043",
                    )
                with ncbi_columns[1]:
                    genbank_input = st.text_area(
                        "GenBank accession.version（最多 10 项）",
                        key=f"database-genbank-{snapshot.run_id}",
                        help="优先输入带版本号的 accession.version。",
                        placeholder="NM_000518.5",
                    )
                with ncbi_columns[2]:
                    pdb_input = st.text_area(
                        "RCSB PDB ID（最多 10 项）",
                        key=f"database-pdb-{snapshot.run_id}",
                        help="一行一个标识符，也可用中文或英文分号分隔。",
                        placeholder="1IEP",
                    )
                    pdb_download_mmcif = st.checkbox(
                        "保存 mmCIF 坐标原始文件",
                        value=True,
                        key=f"database-pdb-mmcif-{snapshot.run_id}",
                    )

            with st.expander("STRING PPI"):
                string_input = st.text_area(
                    "蛋白标识符（最多 50 项）",
                    key=f"database-string-{snapshot.run_id}",
                    help="可输入 UniProt accession、基因名或 STRING ID；物种由 TaxID 限定。",
                    placeholder="P69905\nP68871",
                )
                string_columns = st.columns(2)
                string_network_label = string_columns[0].selectbox(
                    "网络类型",
                    ["功能关联", "物理相互作用"],
                    key=f"database-string-type-{snapshot.run_id}",
                )
                string_required_score = int(
                    string_columns[1].slider(
                        "最低 STRING 分数",
                        min_value=0,
                        max_value=1000,
                        value=400,
                        step=50,
                        key=f"database-string-score-{snapshot.run_id}",
                    )
                )
                string_consent = st.checkbox(
                    "我同意把上述蛋白标识符和 TaxID 提交给 STRING 公共服务",
                    key=f"database-string-consent-{snapshot.run_id}",
                    help="未勾选时不会联网提交，只生成可下载的离线请求 JSON。",
                )

            with st.expander("DAVID 富集"):
                david_columns = st.columns(2)
                with david_columns[0]:
                    david_target_input = st.text_area(
                        "目标基因（最多 500 项）",
                        key=f"database-david-targets-{snapshot.run_id}",
                        help="一行一个标识符，也可用中文或英文分号分隔。",
                        placeholder="3043\n3040",
                    )
                    david_id_type = st.selectbox(
                        "DAVID 标识符类型",
                        [
                            "ENTREZ_GENE_ID",
                            "UNIPROT_ACCESSION",
                            "OFFICIAL_GENE_SYMBOL",
                        ],
                        key=f"database-david-id-type-{snapshot.run_id}",
                    )
                with david_columns[1]:
                    david_background_input = st.text_area(
                        "明确背景基因集（最多 1000 项）",
                        key=f"database-david-background-{snapshot.run_id}",
                        help="背景必须包含全部目标基因，不能留空或使用隐式全基因组背景。",
                        placeholder="3043\n3040\n3039",
                    )
                    david_categories = st.multiselect(
                        "注释类别",
                        options=list(DEFAULT_DAVID_CATEGORIES),
                        default=list(DEFAULT_DAVID_CATEGORIES),
                        key=f"database-david-categories-{snapshot.run_id}",
                    )
                david_threshold_columns = st.columns(2)
                david_max_p_value = float(
                    david_threshold_columns[0].number_input(
                        "最大 EASE P 值",
                        min_value=0.000001,
                        max_value=1.0,
                        value=0.1,
                        step=0.01,
                        format="%.6f",
                        key=f"database-david-p-{snapshot.run_id}",
                    )
                )
                david_min_count = int(
                    david_threshold_columns[1].number_input(
                        "最少命中基因数",
                        min_value=1,
                        value=2,
                        step=1,
                        key=f"database-david-min-count-{snapshot.run_id}",
                    )
                )
                david_consent = st.checkbox(
                    "我同意把上述目标、背景基因、TaxID 和类别提交给 DAVID 公共服务",
                    key=f"database-david-consent-{snapshot.run_id}",
                    help="未勾选时不会联网提交，只生成可下载的离线请求 JSON。",
                )

            database_submitted = st.form_submit_button(
                "查询并归档数据库证据",
                type="primary",
                icon=":material/database_search:",
                width="stretch",
            )

        if database_submitted:
            pubchem_namespace = {
                "名称": "name",
                "CID": "cid",
                "InChIKey": "inchikey",
            }[pubchem_namespace_label]
            gene_identifier_type = (
                "gene_id" if gene_identifier_label == "GeneID" else "symbol"
            )
            string_network_type = (
                "functional"
                if string_network_label == "功能关联"
                else "physical"
            )
            try:
                pubchem_identifiers = split_database_identifiers(
                    pubchem_input,
                    label="PubChem 化合物",
                    limit=10,
                )
                uniprot_accessions = split_database_identifiers(
                    uniprot_input,
                    label="UniProt accession",
                    limit=20,
                )
                gene_identifiers = split_database_identifiers(
                    gene_input,
                    label="NCBI Gene",
                    limit=20,
                )
                genbank_accessions = split_database_identifiers(
                    genbank_input,
                    label="GenBank accession.version",
                    limit=10,
                )
                pdb_ids = split_database_identifiers(
                    pdb_input,
                    label="RCSB PDB ID",
                    limit=10,
                )
                string_identifiers = split_database_identifiers(
                    string_input,
                    label="STRING 蛋白标识符",
                    limit=50,
                )
                david_targets = split_database_identifiers(
                    david_target_input,
                    label="DAVID 目标基因",
                    limit=500,
                )
                david_background = split_database_identifiers(
                    david_background_input,
                    label="DAVID 背景基因",
                    limit=1000,
                )
                if bool(david_targets) != bool(david_background):
                    raise ValueError("DAVID 目标基因和明确背景基因集必须同时提供。")
                if david_targets and not set(david_targets).issubset(
                    david_background
                ):
                    raise ValueError("DAVID 明确背景基因集必须包含全部目标基因。")
                if david_targets and not david_categories:
                    raise ValueError("DAVID 至少选择一个注释类别。")
                planned_calls = (
                    len(pubchem_identifiers)
                    + len(uniprot_accessions)
                    + len(gene_identifiers)
                    + len(genbank_accessions)
                    + len(pdb_ids)
                    + int(bool(string_identifiers))
                    + int(bool(david_targets))
                )
                if planned_calls == 0:
                    raise ValueError("至少填写一个数据库查询输入。")
            except Exception as exc:
                snapshot = record_database_failure(
                    snapshot,
                    source="数据库输入",
                    input_summary="数据库批量查询表单校验",
                    error=exc,
                    sensitive_values=(ncbi_email, david_email),
                )
            else:
                query_status = st.status(
                    f"正在执行并归档 {planned_calls} 项数据库查询…",
                    expanded=True,
                )
                successful_calls = 0
                network_result_archived = False

                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="PubChem",
                    connector_factory=PubChemConnector,
                    operations=tuple(
                        (
                            (
                                f"namespace={pubchem_namespace}; "
                                f"input_sha256="
                                f"{hashlib.sha256(identifier.encode('utf-8')).hexdigest()}"
                            ),
                            lambda connector, identifier=identifier: (
                                connector.fetch_compound(
                                    identifier,
                                    namespace=pubchem_namespace,
                                    include_3d=pubchem_include_3d,
                                )
                            ),
                        )
                        for identifier in pubchem_identifiers
                    ),
                )
                successful_calls += completed
                if pubchem_identifiers:
                    query_status.write(
                        f"PubChem：已归档 {completed}/{len(pubchem_identifiers)} 项。"
                    )

                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="UniProt",
                    connector_factory=UniProtConnector,
                    operations=tuple(
                        (
                            (
                                "UniProt accession; "
                                f"input_sha256="
                                f"{hashlib.sha256(accession.encode('utf-8')).hexdigest()}; "
                                f"TaxID={taxon_id}"
                            ),
                            lambda connector, accession=accession: (
                                connector.fetch_protein(
                                    accession,
                                    taxon_id=taxon_id,
                                )
                            ),
                        )
                        for accession in uniprot_accessions
                    ),
                )
                successful_calls += completed
                if uniprot_accessions:
                    query_status.write(
                        f"UniProt：已归档 {completed}/{len(uniprot_accessions)} 项。"
                    )

                ncbi_sensitive = (
                    ncbi_email,
                    settings.ncbi_api_key,
                )
                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="NCBI Gene",
                    connector_factory=lambda: NCBIConnector(
                        email=ncbi_email or None,
                        api_key=settings.ncbi_api_key,
                    ),
                    operations=tuple(
                        (
                            (
                                f"identifier_type={gene_identifier_type}; "
                                f"input_sha256="
                                f"{hashlib.sha256(identifier.encode('utf-8')).hexdigest()}; "
                                f"TaxID={taxon_id}"
                            ),
                            lambda connector, identifier=identifier: (
                                connector.fetch_gene(
                                    identifier,
                                    taxon_id=taxon_id,
                                    identifier_type=gene_identifier_type,
                                )
                            ),
                        )
                        for identifier in gene_identifiers
                    ),
                    sensitive_values=ncbi_sensitive,
                )
                successful_calls += completed
                if gene_identifiers:
                    query_status.write(
                        f"NCBI Gene：已归档 {completed}/{len(gene_identifiers)} 项。"
                    )

                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="GenBank",
                    connector_factory=lambda: NCBIConnector(
                        email=ncbi_email or None,
                        api_key=settings.ncbi_api_key,
                    ),
                    operations=tuple(
                        (
                            (
                                "GenBank accession.version; "
                                f"input_sha256="
                                f"{hashlib.sha256(accession.encode('utf-8')).hexdigest()}; "
                                f"TaxID={taxon_id}"
                            ),
                            lambda connector, accession=accession: (
                                connector.fetch_nucleotide(
                                    accession,
                                    taxon_id=taxon_id,
                                )
                            ),
                        )
                        for accession in genbank_accessions
                    ),
                    sensitive_values=ncbi_sensitive,
                )
                successful_calls += completed
                if genbank_accessions:
                    query_status.write(
                        f"GenBank：已归档 {completed}/{len(genbank_accessions)} 项。"
                    )

                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="RCSB PDB",
                    connector_factory=RCSBConnector,
                    operations=tuple(
                        (
                            (
                                "PDB ID; "
                                f"input_sha256="
                                f"{hashlib.sha256(pdb_id.encode('utf-8')).hexdigest()}"
                            ),
                            lambda connector, pdb_id=pdb_id: (
                                connector.fetch_structure(
                                    pdb_id,
                                    download_mmcif=pdb_download_mmcif,
                                )
                            ),
                        )
                        for pdb_id in pdb_ids
                    ),
                )
                successful_calls += completed
                if pdb_ids:
                    query_status.write(
                        f"RCSB PDB：已归档 {completed}/{len(pdb_ids)} 项。"
                    )

                string_input_hash = hashlib.sha256(
                    "\n".join(string_identifiers).encode("utf-8")
                ).hexdigest()
                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="STRING",
                    connector_factory=lambda: STRINGConnector(
                        caller_identity=(
                            os.getenv("STRING_CALLER_IDENTITY")
                            or "VetEvidenceAI"
                        )
                    ),
                    operations=(
                        (
                            (
                                f"{len(string_identifiers)} identifiers; "
                                f"input_sha256={string_input_hash}; "
                                f"TaxID={taxon_id}; type={string_network_type}; "
                                f"required_score={string_required_score}"
                            ),
                            lambda connector: connector.fetch_network(
                                string_identifiers,
                                taxon_id=taxon_id,
                                consent_external_submission=string_consent,
                                required_score=string_required_score,
                                network_type=string_network_type,
                            ),
                        ),
                    )
                    if string_identifiers
                    else (),
                )
                successful_calls += completed
                network_result_archived = network_result_archived or completed > 0
                if string_identifiers:
                    query_status.write(f"STRING：已归档 {completed}/1 项。")

                david_input_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "targets": david_targets,
                            "background": david_background,
                            "categories": david_categories,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                snapshot, completed = run_database_query_group(
                    snapshot,
                    state,
                    source="DAVID",
                    connector_factory=lambda: DAVIDConnector(
                        registered_email=david_email or None,
                    ),
                    operations=(
                        (
                            (
                                f"{len(david_targets)} targets; "
                                f"{len(david_background)} background identifiers; "
                                f"input_sha256={david_input_hash}; TaxID={taxon_id}; "
                                f"id_type={david_id_type}"
                            ),
                            lambda connector: connector.enrich(
                                david_targets,
                                taxon_id=taxon_id,
                                background=david_background,
                                consent_external_submission=david_consent,
                                id_type=david_id_type,
                                categories=tuple(david_categories),
                                max_ease_p_value=david_max_p_value,
                                min_count=david_min_count,
                            ),
                        ),
                    )
                    if david_targets
                    else (),
                    sensitive_values=(david_email,),
                )
                successful_calls += completed
                network_result_archived = network_result_archived or completed > 0
                if david_targets:
                    query_status.write(f"DAVID：已归档 {completed}/1 项。")

                if network_result_archived:
                    snapshot = build_database_network(snapshot, state)
                    query_status.write("STRING + DAVID 证据网络已重建。")

                failed_calls = planned_calls - successful_calls
                query_status.update(
                    label=(
                        f"数据库查询完成：已归档 {successful_calls}/{planned_calls} 项；"
                        f"失败 {failed_calls} 项。"
                    ),
                    state="error" if failed_calls else "complete",
                    expanded=bool(failed_calls),
                )

        try:
            snapshot = render_database_results(snapshot, state)
        except (
            ConnectorArchiveError,
            ConnectorError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            latest_snapshot = current_snapshot()
            if latest_snapshot:
                record_database_failure(
                    latest_snapshot,
                    source="数据库结果归档",
                    input_summary="校验并渲染当前运行的数据库结果",
                    error=exc,
                )

with mechanism_tab:
    snapshot = current_snapshot()
    if not snapshot:
        st.info("请先创建研究任务。")
    else:
        st.header("网络药理学与分子对接")
        st.warning(
            "本页全部结果属于计算预测层，不会作为直接文献证据或实验协同"
            "证据。只有真实输入、来源 accession、版本和 SHA-256 完整时才保存。"
        )

        st.subheader("网络药理学：化合物—靶点—通路")
        st.caption(
            "系统只分析用户合法取得并上传的关系表，不自动猜测靶点。"
            "两端均支持 CSV、Excel（.xlsx）和 Word（.docx）；"
            "都必须包含 organism，并与当前科研问题完全匹配。"
        )
        template_columns = st.columns(2)
        with template_columns[0]:
            st.markdown("**化合物—靶点模板**")
            with st.container(horizontal=True):
                st.download_button(
                    "CSV",
                    data=(
                        PROJECT_ROOT
                        / "data"
                        / "templates"
                        / "compound_target_template.csv"
                    ).read_bytes(),
                    file_name="compound_target_template.csv",
                    mime="text/csv",
                )
                st.download_button(
                    "Excel",
                    data=compound_target_template_xlsx(),
                    file_name="compound_target_template.xlsx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                )
                st.download_button(
                    "Word",
                    data=compound_target_template_docx(),
                    file_name="compound_target_template.docx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
        with template_columns[1]:
            st.markdown("**靶点—通路模板**")
            with st.container(horizontal=True):
                st.download_button(
                    "CSV",
                    data=(
                        PROJECT_ROOT
                        / "data"
                        / "templates"
                        / "target_pathway_template.csv"
                    ).read_bytes(),
                    file_name="target_pathway_template.csv",
                    mime="text/csv",
                )
                st.download_button(
                    "Excel",
                    data=target_pathway_template_xlsx(),
                    file_name="target_pathway_template.xlsx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                )
                st.download_button(
                    "Word",
                    data=target_pathway_template_docx(),
                    file_name="target_pathway_template.docx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
        st.info(
            "化合物—靶点必填：compound、compound_accession、organism、"
            "target、target_accession。靶点—通路必填：organism、target、"
            "target_accession、pathway、pathway_accession。"
        )
        network_upload_columns = st.columns(2)
        compound_target_file = network_upload_columns[0].file_uploader(
            "上传化合物—靶点文件",
            type=["csv", "xlsx", "docx"],
            key=f"compound-target-{snapshot.run_id}",
            max_upload_size=MAX_NETWORK_FILE_BYTES // (1024 * 1024),
        )
        target_pathway_file = network_upload_columns[1].file_uploader(
            "上传靶点—通路文件",
            type=["csv", "xlsx", "docx"],
            key=f"target-pathway-{snapshot.run_id}",
            max_upload_size=MAX_NETWORK_FILE_BYTES // (1024 * 1024),
        )
        provenance_columns = st.columns(2)
        with provenance_columns[0]:
            compound_source_name = st.text_input(
                "化合物—靶点来源名称",
                value="用户导出的化合物—靶点数据",
                key=f"compound-source-name-{snapshot.run_id}",
            )
            compound_source_accession = st.text_input(
                "化合物—靶点数据集 accession",
                value="user-dataset:compound-target",
                key=f"compound-source-accession-{snapshot.run_id}",
            )
            compound_source_version = st.text_input(
                "化合物—靶点数据版本",
                value="user-provided",
                key=f"compound-source-version-{snapshot.run_id}",
            )
        with provenance_columns[1]:
            pathway_source_name = st.text_input(
                "靶点—通路来源名称",
                value="用户导出的靶点—通路数据",
                key=f"pathway-source-name-{snapshot.run_id}",
            )
            pathway_source_accession = st.text_input(
                "靶点—通路数据集 accession",
                value="user-dataset:target-pathway",
                key=f"pathway-source-accession-{snapshot.run_id}",
            )
            pathway_source_version = st.text_input(
                "靶点—通路数据版本",
                value="user-provided",
                key=f"pathway-source-version-{snapshot.run_id}",
            )
        network_action_columns = st.columns(2)
        analyze_network = network_action_columns[0].button(
            "分析上传的网络药理学数据",
            disabled=(
                compound_target_file is None
                or target_pathway_file is None
            ),
            width="stretch",
        )
        analyze_network_demo = network_action_columns[1].button(
            "加载合成网络演示",
            width="stretch",
        )
        if analyze_network or analyze_network_demo:
            if analyze_network_demo:
                compound_payload, pathway_payload = (
                    build_synthetic_network_demo(snapshot.question)
                )
                compound_provenance = SourceProvenance(
                    source_name="合成演示化合物—靶点数据",
                    accession="synthetic-demo:compound-target",
                    version="v1",
                )
                pathway_provenance = SourceProvenance(
                    source_name="合成演示靶点—通路数据",
                    accession="synthetic-demo:target-pathway",
                    version="v1",
                )
                compound_filename = "synthetic_compound_target.csv"
                pathway_filename = "synthetic_target_pathway.csv"
                network_input_summary = "合成网络药理学演示"
            else:
                compound_payload = compound_target_file.getvalue()
                pathway_payload = target_pathway_file.getvalue()
                compound_filename = compound_target_file.name
                pathway_filename = target_pathway_file.name
                compound_provenance = SourceProvenance(
                    source_name=compound_source_name,
                    accession=compound_source_accession,
                    version=compound_source_version,
                )
                pathway_provenance = SourceProvenance(
                    source_name=pathway_source_name,
                    accession=pathway_source_accession,
                    version=pathway_source_version,
                )
                network_input_summary = (
                    f"{compound_target_file.name} + "
                    f"{target_pathway_file.name}"
                )
            try:
                network_result = analyze_network_pharmacology_files(
                    compound_payload,
                    pathway_payload,
                    compound_target_filename=compound_filename,
                    target_pathway_filename=pathway_filename,
                    compound_target_source=compound_provenance,
                    target_pathway_source=pathway_provenance,
                )
                require_network_scope(
                    network_result,
                    expected_compounds=[
                        snapshot.question.intervention or "",
                        snapshot.question.comparator or "",
                    ],
                    expected_organism=snapshot.question.population or "",
                )
            except (ValidationError, ValueError) as exc:
                snapshot = append_tool_call(
                    snapshot,
                    "network_pharmacology.analyze",
                    network_input_summary,
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"网络药理学数据校验失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(str(exc))
            else:
                network_changed = (
                    snapshot.mechanism_prediction.network != network_result
                )
                invalidated_docking = network_changed and bool(
                    snapshot.mechanism_prediction.prepared_manifests
                    or snapshot.mechanism_prediction.docking_runs
                )
                bundle_updates: dict[str, object] = {
                    "network": network_result,
                }
                if invalidated_docking:
                    bundle_updates.update(
                        {
                            "prepared_manifests": [],
                            "docking_runs": [],
                        }
                    )
                bundle = snapshot.mechanism_prediction.model_copy(
                    update=bundle_updates
                )
                snapshot = snapshot.model_copy(
                    update={
                        "mechanism_prediction": bundle,
                        "report": None,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    "network_pharmacology.analyze",
                    network_input_summary,
                    status="succeeded",
                    output_summary=(
                        f"{network_result.summary.intersection_target_count} "
                        "个交集靶点"
                    ),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.RUNNING,
                    (
                        "网络药理学透明网络分析完成；输入变化，旧对接任务和"
                        "结果已安全失效。"
                        if invalidated_docking
                        else "网络药理学透明网络分析完成。"
                    ),
                )
                save_snapshot(snapshot)
                if analyze_network_demo:
                    st.success(
                        "合成网络演示已运行，只用于验证流程，不代表真实靶点。"
                    )
                else:
                    st.success("网络药理学数据已通过范围校验并保存。")

        st.divider()
        st.subheader("AutoDock Vina：准备并执行可复现任务")
        st.caption(
            "系统保存 PDBQT 内容哈希、来源、搜索框和版本；检测到本机 Vina "
            "时可由 Agent 受控执行，也可以只生成任务清单后导入外部输出。"
            "所有得分都属于计算预测，不能替代结构准备审查或结合实验。"
        )
        local_vina, local_vina_error = discover_vina_for_ui(
            os.environ.get("VINA_EXECUTABLE", ""),
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("PATH", ""),
        )
        if local_vina is None:
            st.warning(f"本机 Vina 当前不可用：{local_vina_error}")
        else:
            st.success(
                f"已连接 AutoDock Vina {local_vina.version}："
                f"{local_vina.path}"
            )
            st.caption(
                f"可执行文件 SHA-256：{local_vina.sha256}。"
                "执行前会再次核验版本和哈希。"
            )
        local_openbabel, local_openbabel_error = discover_openbabel_for_ui(
            os.environ.get("OPENBABEL_EXECUTABLE", ""),
            os.environ.get("PATH", ""),
        )
        current_bundle = current_snapshot().mechanism_prediction
        network_target = (
            current_bundle.network.ranked_targets[0]
            if (
                current_bundle.network is not None
                and current_bundle.network.ranked_targets
            )
            else None
        )
        ligand_payload: bytes | None = None
        ligand_file_name = ""
        ligand_source_name_for_manifest = ""
        ligand_source_version_for_manifest = ""
        docking_columns = st.columns(2)
        with docking_columns[0]:
            docking_compound = st.selectbox(
                "配体对应当前干预",
                [
                    snapshot.question.intervention or "",
                    snapshot.question.comparator or "",
                ],
                key=f"docking-compound-{snapshot.run_id}",
            )
            ligand_accession = st.text_input(
                "配体 accession（例如 PubChem CID）",
                key=f"ligand-accession-{snapshot.run_id}",
            )
            ligand_input_mode = st.radio(
                "配体输入方式",
                ["上传已准备的 PDBQT", "使用 Open Babel 准备"],
                horizontal=True,
                key=f"ligand-input-mode-{snapshot.run_id}",
            )
            ligand_source_name = st.text_input(
                "原始配体结构来源",
                value="用户提供的结构文件",
                key=f"ligand-source-name-{snapshot.run_id}",
            )
            ligand_source_version = st.text_input(
                "原始配体结构来源版本",
                value="user-provided",
                key=f"ligand-source-version-{snapshot.run_id}",
            )
            if ligand_input_mode == "上传已准备的 PDBQT":
                ligand_file = st.file_uploader(
                    "上传配体 PDBQT",
                    type=["pdbqt"],
                    key=f"ligand-pdbqt-{snapshot.run_id}",
                    max_upload_size=25,
                )
                if ligand_file is not None:
                    ligand_payload = ligand_file.getvalue()
                    ligand_file_name = ligand_file.name
                    ligand_source_name_for_manifest = (
                        f"{ligand_source_name}；文件={ligand_file.name}"
                    )
                    ligand_source_version_for_manifest = ligand_source_version
            else:
                if local_openbabel is None:
                    st.warning(
                        "本机 Open Babel 当前不可用："
                        f"{local_openbabel_error}"
                    )
                else:
                    st.success(
                        f"已连接 Open Babel {local_openbabel.version}"
                    )
                    st.caption(
                        f"可执行文件 SHA-256：{local_openbabel.sha256}。"
                        "仅准备配体；受体仍须上传经人工核查的 PDBQT。"
                    )
                raw_ligand_file = st.file_uploader(
                    "上传单个原始配体结构",
                    type=["smi", "smiles", "sdf", "mol", "mol2", "pdb"],
                    key=f"openbabel-ligand-input-{snapshot.run_id}",
                    max_upload_size=10,
                    help=(
                        "一次只允许一个配体；不接受压缩包、多记录 SDF、"
                        "多行 SMILES 或含多个 MODEL 的 PDB。"
                    ),
                )
                generate_3d = st.checkbox(
                    "生成或重建三维坐标",
                    value=True,
                    key=f"openbabel-gen3d-{snapshot.run_id}",
                )
                protonate_ligand = st.checkbox(
                    "按指定 pH 添加氢",
                    value=True,
                    key=f"openbabel-protonate-{snapshot.run_id}",
                )
                protonation_ph = st.number_input(
                    "配体质子化 pH",
                    min_value=0.0,
                    max_value=14.0,
                    value=7.4,
                    step=0.1,
                    disabled=not protonate_ligand,
                    key=f"openbabel-ph-{snapshot.run_id}",
                )
                st.caption(
                    "部分电荷固定使用 Gasteiger；自动准备结果仍需人工检查"
                    "互变异构体、立体化学、质子化和可旋转键。"
                )
                preparation_options = OpenBabelPreparationOptions(
                    input_format=(
                        Path(raw_ligand_file.name).suffix
                        if raw_ligand_file is not None
                        else "smi"
                    ),
                    generate_3d=generate_3d,
                    protonation_ph=(
                        float(protonation_ph)
                        if protonate_ligand
                        else None
                    ),
                )
                raw_ligand_payload = (
                    raw_ligand_file.getvalue()
                    if raw_ligand_file is not None
                    else None
                )
                preparation_input_sha256 = (
                    hashlib.sha256(raw_ligand_payload).hexdigest()
                    if raw_ligand_payload is not None
                    else None
                )
                preparation_fingerprint = (
                    openbabel_preparation_fingerprint(
                        raw_ligand_payload,
                        preparation_options,
                        local_openbabel,
                    )
                    if (
                        raw_ligand_payload is not None
                        and local_openbabel is not None
                    )
                    else None
                )
                preparation_input_summary = (
                    f"{raw_ligand_file.name}；准备指纹="
                    f"{preparation_fingerprint}"
                    if (
                        raw_ligand_file is not None
                        and preparation_fingerprint is not None
                    )
                    else "未提供配体文件"
                )
                prepared_ligand = (
                    load_openbabel_preparation(
                        snapshot.run_id,
                        preparation_fingerprint,
                    )
                    if preparation_fingerprint is not None
                    else None
                )
                prepare_openbabel_ligand = st.button(
                    "用 Open Babel 生成配体 PDBQT",
                    type="primary",
                    disabled=(
                        raw_ligand_payload is None
                        or local_openbabel is None
                    ),
                    width="stretch",
                    key=f"prepare-openbabel-ligand-{snapshot.run_id}",
                )
                if prepare_openbabel_ligand:
                    status_box = st.status(
                        "正在由 VetEvidence Agent 准备配体…",
                        expanded=True,
                    )
                    try:
                        if (
                            raw_ligand_file is None
                            or raw_ligand_payload is None
                            or local_openbabel is None
                            or preparation_fingerprint is None
                        ):
                            raise OpenBabelExecutionError(
                                "Open Babel 配体准备输入不完整。"
                            )
                        status_box.write(
                            "正在复核 Open Babel 版本、二进制哈希和参数目录。"
                        )
                        prepared_ligand = prepare_ligand_pdbqt(
                            raw_ligand_payload,
                            options=preparation_options,
                            executable=local_openbabel,
                        )
                        status_box.write(
                            "正在校验单分子数量、可解析且非退化的坐标和 "
                            "PDBQT 内容哈希。"
                        )
                    except (
                        OSError,
                        ValidationError,
                        ValueError,
                        OpenBabelExecutionError,
                    ) as exc:
                        prepared_ligand = None
                        st.session_state.pop(
                            OPENBABEL_LIGAND_STATE_KEY,
                            None,
                        )
                        failed_snapshot = append_tool_call(
                            current_snapshot(),
                            "structure.openbabel_prepare",
                            preparation_input_summary,
                            status="failed",
                            error=str(exc),
                            metadata={
                                "input_sha256": preparation_input_sha256 or "",
                                "options": preparation_options.model_dump(
                                    mode="json"
                                ),
                                "executable_version": (
                                    local_openbabel.version
                                    if local_openbabel is not None
                                    else ""
                                ),
                                "executable_sha256": (
                                    local_openbabel.sha256
                                    if local_openbabel is not None
                                    else ""
                                ),
                            },
                        )
                        failed_snapshot = append_event(
                            failed_snapshot,
                            TaskStatus.FAILED,
                            f"Open Babel 配体准备失败：{exc}",
                        )
                        save_snapshot(failed_snapshot)
                        status_box.update(
                            label="Open Babel 配体准备失败",
                            state="error",
                            expanded=True,
                        )
                        st.error(
                            "没有生成可交给 Vina 的配体 PDBQT："
                            f"{exc}"
                        )
                    else:
                        st.session_state[OPENBABEL_LIGAND_STATE_KEY] = {
                            "run_id": snapshot.run_id,
                            "fingerprint": preparation_fingerprint,
                            "artifacts": prepared_ligand.model_dump(
                                mode="python"
                            ),
                        }
                        preparation_metadata = (
                            prepared_ligand.metadata.model_dump(
                                mode="json",
                                exclude={
                                    "executable_path",
                                    "data_directory",
                                    "stdout",
                                    "stderr",
                                },
                            )
                        )
                        succeeded_snapshot = append_tool_call(
                            current_snapshot(),
                            "structure.openbabel_prepare",
                            preparation_input_summary,
                            status="succeeded",
                            output_summary=(
                                "生成配体 PDBQT；SHA-256 "
                                f"{prepared_ligand.metadata.output_pdbqt_sha256}"
                            ),
                            metadata=preparation_metadata,
                        )
                        succeeded_snapshot = append_event(
                            succeeded_snapshot,
                            TaskStatus.RUNNING,
                            (
                                "Open Babel 已生成并校验单个配体 PDBQT，"
                                "可直接用于当前 Vina 任务。"
                            ),
                            metadata={
                                "input_sha256": (
                                    prepared_ligand.metadata.input_sha256
                                ),
                                "output_pdbqt_sha256": (
                                    prepared_ligand.metadata
                                    .output_pdbqt_sha256
                                ),
                                "executable_version": (
                                    prepared_ligand.metadata
                                    .executable_version
                                ),
                                "executable_sha256": (
                                    prepared_ligand.metadata
                                    .executable_sha256
                                ),
                            },
                        )
                        save_snapshot(succeeded_snapshot)
                        status_box.update(
                            label="Open Babel 配体准备完成",
                            state="complete",
                            expanded=False,
                        )
                        st.success(
                            "配体 PDBQT 已通过内容及可解析、非退化坐标校验；"
                            "这不代表构象已通过科研人工复核。"
                        )
                if prepared_ligand is not None:
                    ligand_payload = prepared_ligand.output_pdbqt
                    ligand_file_name = (
                        f"{Path(raw_ligand_file.name).stem}.pdbqt"
                        if raw_ligand_file is not None
                        else "ligand.pdbqt"
                    )
                    ligand_source_name_for_manifest = (
                        f"{ligand_source_name}；Open Babel 配体准备；"
                        f"原始文件={raw_ligand_file.name}"
                    )
                    ligand_source_version_for_manifest = (
                        f"{ligand_source_version}；Open Babel "
                        f"{prepared_ligand.metadata.executable_version}"
                    )
                    st.caption(
                        "已准备输出 SHA-256："
                        f"{prepared_ligand.metadata.output_pdbqt_sha256}"
                    )
                    st.download_button(
                        "下载已准备的配体 PDBQT",
                        prepared_ligand.output_pdbqt,
                        file_name=ligand_file_name,
                        mime="chemical/x-pdbqt",
                        width="stretch",
                        key=f"download-openbabel-ligand-{snapshot.run_id}",
                    )
        with docking_columns[1]:
            receptor_name = st.text_input(
                "受体名称",
                value=network_target.target if network_target else "",
                key=f"receptor-name-{snapshot.run_id}",
            )
            receptor_accession = st.text_input(
                "受体结构 accession（例如 PDB ID；不要填写 UniProt 靶点号）",
                value="",
                key=f"receptor-accession-{snapshot.run_id}",
            )
            receptor_organism = st.text_input(
                "受体研究对象",
                value=snapshot.question.population or "",
                key=f"receptor-organism-{snapshot.run_id}",
            )
            receptor_source_name = st.text_input(
                "受体结构来源",
                value="用户提供的 PDBQT",
                key=f"receptor-source-name-{snapshot.run_id}",
            )
            receptor_source_version = st.text_input(
                "受体结构来源版本",
                value="user-provided",
                key=f"receptor-source-version-{snapshot.run_id}",
            )
            receptor_file = st.file_uploader(
                "上传受体 PDBQT",
                type=["pdbqt"],
                key=f"receptor-pdbqt-{snapshot.run_id}",
                max_upload_size=25,
            )

        engine_version = st.text_input(
            "AutoDock Vina 版本",
            value=local_vina.version if local_vina is not None else "1.2.7",
            key=f"vina-version-v2-{snapshot.run_id}",
        )
        center_columns = st.columns(3)
        center_x = center_columns[0].number_input(
            "center_x", value=0.0, key=f"center-x-{snapshot.run_id}"
        )
        center_y = center_columns[1].number_input(
            "center_y", value=0.0, key=f"center-y-{snapshot.run_id}"
        )
        center_z = center_columns[2].number_input(
            "center_z", value=0.0, key=f"center-z-{snapshot.run_id}"
        )
        size_columns = st.columns(3)
        size_x = size_columns[0].number_input(
            "size_x", min_value=0.1, value=20.0, key=f"size-x-{snapshot.run_id}"
        )
        size_y = size_columns[1].number_input(
            "size_y", min_value=0.1, value=20.0, key=f"size-y-{snapshot.run_id}"
        )
        size_z = size_columns[2].number_input(
            "size_z", min_value=0.1, value=20.0, key=f"size-z-{snapshot.run_id}"
        )
        run_parameter_columns = st.columns(3)
        exhaustiveness = run_parameter_columns[0].number_input(
            "exhaustiveness",
            min_value=1,
            value=8,
            step=1,
            key=f"exhaustiveness-{snapshot.run_id}",
        )
        num_modes = run_parameter_columns[1].number_input(
            "num_modes",
            min_value=1,
            value=9,
            step=1,
            key=f"num-modes-{snapshot.run_id}",
        )
        seed = run_parameter_columns[2].number_input(
            "seed",
            value=42,
            step=1,
            key=f"seed-{snapshot.run_id}",
        )
        with st.container(horizontal=True):
            prepare_manifest = st.button(
                "仅生成 Vina 任务清单",
                disabled=ligand_payload is None or receptor_file is None,
                width="stretch",
            )
            execute_local_vina = st.button(
                "由 Agent 运行本机 Vina",
                type="primary",
                disabled=(
                    ligand_payload is None
                    or receptor_file is None
                    or local_vina is None
                ),
                width="stretch",
            )
        if (
            (prepare_manifest or execute_local_vina)
            and ligand_payload is not None
            and receptor_file
        ):
            operation_name = (
                "docking.vina_execute"
                if execute_local_vina
                else "docking.prepare"
            )
            try:
                receptor_payload = receptor_file.getvalue()
                ligand_sha256 = validate_pdbqt_bytes(
                    ligand_payload,
                    role="ligand",
                )
                receptor_sha256 = validate_pdbqt_bytes(
                    receptor_payload,
                    role="receptor",
                )
                manifest = build_vina_manifest(
                    task_id=f"dock-{uuid4().hex[:12]}",
                    compound_name=docking_compound,
                    ligand_accession=ligand_accession,
                    receptor_name=receptor_name,
                    receptor_accession=receptor_accession,
                    receptor_organism=receptor_organism,
                    ligand_source=SourceProvenance(
                        source_name=ligand_source_name_for_manifest,
                        accession=ligand_accession,
                        version=ligand_source_version_for_manifest,
                        sha256=ligand_sha256,
                    ),
                    receptor_source=SourceProvenance(
                        source_name=(
                            f"{receptor_source_name}；文件={receptor_file.name}"
                        ),
                        accession=receptor_accession,
                        version=receptor_source_version,
                        sha256=receptor_sha256,
                    ),
                    parameters=VinaParameters(
                        center_x=center_x,
                        center_y=center_y,
                        center_z=center_z,
                        size_x=size_x,
                        size_y=size_y,
                        size_z=size_z,
                        exhaustiveness=int(exhaustiveness),
                        num_modes=int(num_modes),
                        seed=int(seed),
                    ),
                    engine_version=engine_version,
                )
                require_docking_scope(
                    manifest,
                    expected_compounds=[
                        snapshot.question.intervention or "",
                        snapshot.question.comparator or "",
                    ],
                    expected_organism=snapshot.question.population or "",
                )
            except (ValidationError, ValueError) as exc:
                snapshot = append_tool_call(
                    current_snapshot(),
                    operation_name,
                    f"{docking_compound} × {receptor_name}",
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"Vina 任务准备失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(str(exc))
            else:
                snapshot = current_snapshot()
                previous_manifests = [
                    item
                    for item in snapshot.mechanism_prediction.prepared_manifests
                    if item.task_id != manifest.task_id
                ]
                bundle = snapshot.mechanism_prediction.model_copy(
                    update={
                        "prepared_manifests": [
                            *previous_manifests,
                            manifest,
                        ]
                    }
                )
                snapshot = snapshot.model_copy(
                    update={
                        "mechanism_prediction": bundle,
                        "report": None,
                    }
                )
                if not execute_local_vina:
                    snapshot = append_tool_call(
                        snapshot,
                        operation_name,
                        f"{docking_compound} × {receptor_name}",
                        status="succeeded",
                        output_summary=(
                            f"生成任务 {manifest.task_id}；尚无对接分数"
                        ),
                        metadata={
                            "manifest_sha256": manifest.manifest_sha256,
                        },
                    )
                    snapshot = append_event(
                        snapshot,
                        TaskStatus.RUNNING,
                        "AutoDock Vina 可复现任务清单已生成，等待外部运行。",
                    )
                    save_snapshot(snapshot)
                    st.success(
                        "任务清单已保存；未导入真实输出前不会显示分数。"
                    )
                else:
                    snapshot = append_event(
                        snapshot,
                        TaskStatus.RUNNING,
                        (
                            "VetEvidence Agent 准备调用本机 AutoDock Vina；"
                            "任务清单已先行保存。"
                        ),
                        metadata={
                            "task_id": manifest.task_id,
                            "manifest_sha256": manifest.manifest_sha256,
                        },
                    )
                    save_snapshot(snapshot)
                    status_box = st.status(
                        "正在由 VetEvidence Agent 运行本机 AutoDock Vina…",
                        expanded=True,
                    )
                    try:
                        if local_vina is None:
                            raise VinaExecutionError(
                                "本机 Vina 在执行前不可用。"
                            )
                        status_box.write("再次核验 Vina 版本和可执行文件哈希。")
                        execution = execute_vina(
                            manifest,
                            ligand_payload,
                            receptor_payload,
                            executable=local_vina,
                        )
                        audit = execution.docking_run.execution_audit
                        if audit is None:
                            raise ValueError("本机 Vina 执行缺少审计记录。")
                        status_box.write("Vina 已退出，正在校验日志与构象文件。")
                        execution_metadata = execution.metadata.model_dump(
                            mode="json",
                            exclude={"executable_path"},
                        )
                        stored_artifacts = VINA_ARTIFACT_STORE.save(
                            run_id=snapshot.run_id,
                            task_id=manifest.task_id,
                            manifest_sha256=manifest.manifest_sha256,
                            bound_log=execution.bound_log,
                            output_pdbqt=execution.output_pdbqt,
                            execution=execution_metadata,
                        )
                        if (
                            stored_artifacts.metadata.log_sha256
                            != execution.docking_run.output_source.sha256
                        ):
                            raise ValueError(
                                "保存后的 Vina 日志哈希与解析来源不一致。"
                            )
                    except (
                        OSError,
                        ValidationError,
                        ValueError,
                        VinaExecutionError,
                    ) as exc:
                        status_box.update(
                            label="本机 Vina 执行失败",
                            state="error",
                            expanded=True,
                        )
                        snapshot = append_tool_call(
                            snapshot,
                            operation_name,
                            f"{docking_compound} × {receptor_name}",
                            status="failed",
                            error=str(exc),
                            metadata={
                                "manifest_sha256": manifest.manifest_sha256,
                            },
                        )
                        snapshot = append_event(
                            snapshot,
                            TaskStatus.FAILED,
                            f"本机 AutoDock Vina 执行失败：{exc}",
                        )
                        save_snapshot(snapshot)
                        st.error(
                            "任务清单已保留，但没有保存对接分数："
                            f"{exc}"
                        )
                    else:
                        previous_runs = [
                            run
                            for run in bundle.docking_runs
                            if run.manifest.task_id != manifest.task_id
                        ]
                        bundle = bundle.model_copy(
                            update={
                                "docking_runs": [
                                    *previous_runs,
                                    execution.docking_run,
                                ]
                            }
                        )
                        snapshot = snapshot.model_copy(
                            update={
                                "mechanism_prediction": bundle,
                                "report": None,
                            }
                        )
                        snapshot = append_tool_call(
                            snapshot,
                            operation_name,
                            f"{docking_compound} × {receptor_name}",
                            status="succeeded",
                            output_summary=(
                                f"{len(execution.docking_run.poses)} 个模式；"
                                f"最佳 "
                                f"{execution.docking_run.best_affinity_kcal_mol:g} "
                                "kcal/mol"
                            ),
                            metadata={
                                "manifest_sha256": manifest.manifest_sha256,
                                "bound_log_sha256": (
                                    stored_artifacts.metadata.log_sha256
                                ),
                                **audit.model_dump(mode="json"),
                            },
                        )
                        snapshot = append_event(
                            snapshot,
                            TaskStatus.RUNNING,
                            (
                                "VetEvidence Agent 已调用本机 AutoDock Vina，"
                                "并保存版本、参数、退出码和输出哈希。"
                            ),
                            metadata={
                                "task_id": manifest.task_id,
                                "manifest_sha256": manifest.manifest_sha256,
                                "executable_sha256": audit.executable_sha256,
                                "output_pdbqt_sha256": (
                                    audit.output_pdbqt_sha256
                                ),
                            },
                        )
                        save_snapshot(snapshot)
                        status_box.update(
                            label="本机 AutoDock Vina 执行完成",
                            state="complete",
                            expanded=False,
                        )
                        st.success(
                            "真实 Vina 进程已完成；日志、构象文件和哈希已保存。"
                        )

        st.subheader("导入用户提供的 AutoDock Vina 文本输出")
        snapshot = current_snapshot()
        manifests = snapshot.mechanism_prediction.prepared_manifests
        if manifests:
            selected_task_id = st.selectbox(
                "选择任务清单",
                [manifest.task_id for manifest in manifests],
                key=f"vina-manifest-select-{snapshot.run_id}",
            )
            selected_manifest = next(
                manifest
                for manifest in manifests
                if manifest.task_id == selected_task_id
            )
            selected_run = next(
                (
                    run
                    for run in snapshot.mechanism_prediction.docking_runs
                    if run.manifest.task_id == selected_task_id
                ),
                None,
            )
            has_local_audit = (
                selected_run is not None
                and selected_run.execution_audit is not None
            )
            if has_local_audit:
                st.info(
                    "该任务已有 VetEvidence Agent 本机执行审计。为避免审计"
                    "记录与当前结果矛盾，不能用用户上传日志覆盖；请生成新"
                    "任务清单后再导入。"
                )
            vina_output_file = st.file_uploader(
                "上传 Vina 标准输出文本",
                type=["txt", "log"],
                key=f"vina-output-{snapshot.run_id}",
            )
            import_vina_output = st.button(
                "校验并导入 Vina 输出",
                disabled=vina_output_file is None or has_local_audit,
                width="stretch",
            )
            if import_vina_output and vina_output_file:
                try:
                    if has_local_audit:
                        raise ValueError(
                            "已有本机 Vina 执行审计的任务不能被外部输出覆盖。"
                        )
                    docking_run = parse_vina_output(
                        vina_output_file.getvalue(),
                        manifest=selected_manifest,
                        output_source=SourceProvenance(
                            source_name=vina_output_file.name,
                            accession=f"file:{vina_output_file.name}",
                            version=selected_manifest.engine_version,
                        ),
                    )
                except (ValidationError, ValueError) as exc:
                    snapshot = append_tool_call(
                        snapshot,
                        "docking.vina_import",
                        selected_task_id,
                        status="failed",
                        error=str(exc),
                    )
                    snapshot = append_event(
                        snapshot,
                        TaskStatus.FAILED,
                        f"Vina 输出校验失败：{exc}",
                    )
                    save_snapshot(snapshot)
                    st.error(str(exc))
                else:
                    previous_runs = [
                        run
                        for run in snapshot.mechanism_prediction.docking_runs
                        if run.manifest.task_id != selected_task_id
                    ]
                    bundle = snapshot.mechanism_prediction.model_copy(
                        update={
                            "docking_runs": [
                                *previous_runs,
                                docking_run,
                            ]
                        }
                    )
                    snapshot = snapshot.model_copy(
                        update={
                            "mechanism_prediction": bundle,
                            "report": None,
                        }
                    )
                    snapshot = append_tool_call(
                        snapshot,
                        "docking.vina_import",
                        selected_task_id,
                        status="succeeded",
                        output_summary=(
                            f"{len(docking_run.poses)} 个模式；最佳 "
                            f"{docking_run.best_affinity_kcal_mol:g} kcal/mol"
                        ),
                    )
                    snapshot = append_event(
                        snapshot,
                        TaskStatus.RUNNING,
                        "用户提供的 AutoDock Vina 输出已完成格式、版本与"
                        "内容哈希校验，并导入计算预测层。",
                    )
                    save_snapshot(snapshot)
                    st.success(
                        "Vina 输出已导入；系统无法认证其运行真实性，"
                        "结果仍属于计算预测。"
                    )
        else:
            st.info("请先上传配体和受体 PDBQT，生成一个任务清单。")

        active_snapshot = current_snapshot()
        render_mechanism_prediction(
            active_snapshot.mechanism_prediction,
            run_id=active_snapshot.run_id,
        )

with report_tab:
    snapshot = current_snapshot()
    if not snapshot:
        st.info("请先创建研究任务。")
    elif not snapshot.conditions:
        st.info("至少完成一次文献检索并形成可追溯来源后才能生成报告。")
    else:
        st.header("带证据、风险和下一步的科研决策报告")
        if st.button("生成或刷新决策报告", type="primary", width="stretch"):
            review_event = build_task_event(
                snapshot.run_id,
                TaskStatus.AWAITING_REVIEW,
                "决策报告已生成，等待人工复核。",
            )
            try:
                report = build_decision_report(
                    snapshot.question,
                    conditions=snapshot.conditions,
                    task_events=[*snapshot.task_events, review_event],
                    analysis=snapshot.analysis,
                    assessment=snapshot.assessment,
                    mechanism_prediction=snapshot.mechanism_prediction,
                    hypotheses=snapshot.hypotheses,
                    human_review=None,
                )
            except ValueError as exc:
                snapshot = append_tool_call(
                    snapshot,
                    "report.generate",
                    snapshot.question.text,
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"报告生成失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(str(exc))
            else:
                review_event = review_event.model_copy(
                    update={
                        "metadata": {
                            "report_id": report.id,
                            "report_content_sha256": report_content_sha256(report),
                        }
                    }
                )
                snapshot = snapshot.model_copy(
                    update={
                        "task_events": [*snapshot.task_events, review_event],
                        "report": report,
                    }
                )
                snapshot = append_tool_call(
                    snapshot,
                    "report.generate",
                    snapshot.question.text,
                    status="succeeded",
                    output_summary=(
                        f"{len(report.conclusions)} 条可追溯结论，"
                        f"{len(report.evidence_gaps)} 类证据空白，"
                        f"准入状态 {report.evidence_admission.status}"
                    ),
                )
                save_snapshot(snapshot)
                if (
                    report.evidence_admission.status
                    is EvidenceAdmissionStatus.BLOCKED_NO_DIRECT_EVIDENCE
                ):
                    st.warning(
                        "文献层面证据不足：当前没有直接文献协同证据。"
                        "匹配的实验数据如有，将按独立证据链呈现。"
                    )
                else:
                    st.success("报告已生成，必须经过人工复核。")

        snapshot = current_snapshot()
        if snapshot.report:
            markdown_report = decision_report_to_markdown(snapshot.report)
            st.markdown(markdown_report)
            st.download_button(
                "下载 Markdown 报告",
                data=markdown_report,
                file_name="vetresearch_decision_report.md",
                mime="text/markdown",
                width="stretch",
            )
            st.download_button(
                "下载 JSON 审计报告",
                data=json.dumps(
                    snapshot.report.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                file_name="vetresearch_decision_report.json",
                mime="application/json",
                width="stretch",
            )

            st.subheader("人工复核")
            with st.form(f"human-review-{snapshot.run_id}"):
                reviewer = st.text_input("复核人", value="孙奇")
                decision_label = st.selectbox(
                    "复核决定",
                    ["通过", "要求修改", "拒绝"],
                )
                review_comment = st.text_area("复核意见")
                submit_review = st.form_submit_button(
                    "保存人工复核",
                    width="stretch",
                )
            if submit_review:
                decision_map = {
                    "通过": ReviewDecision.APPROVED,
                    "要求修改": ReviewDecision.CHANGES_REQUESTED,
                    "拒绝": ReviewDecision.REJECTED,
                }
                decision = decision_map[decision_label]
                try:
                    review = HumanReview(
                        id=snapshot.report.human_review.id,
                        decision=decision,
                        reviewer=reviewer,
                        comments=[review_comment] if review_comment else [],
                        requested_at=snapshot.report.human_review.requested_at,
                        reviewed_at=datetime.now(timezone.utc),
                    )
                except ValidationError as exc:
                    st.error(f"人工复核记录无效：{exc}")
                else:
                    if decision is ReviewDecision.APPROVED:
                        status = TaskStatus.COMPLETED
                        message = "人工复核通过，任务完成。"
                    elif decision is ReviewDecision.REJECTED:
                        status = TaskStatus.FAILED
                        message = "人工复核拒绝，任务结束。"
                    else:
                        status = TaskStatus.AWAITING_REVIEW
                        message = "人工复核要求修改，任务返回待复核状态。"
                    snapshot = append_event(
                        snapshot,
                        status,
                        message,
                        actor=reviewer,
                        metadata={
                            "report_id": snapshot.report.id,
                            "report_generated_at": (
                                snapshot.report.generated_at.isoformat()
                            ),
                            "report_content_sha256": report_content_sha256(
                                snapshot.report
                            ),
                            "report_snapshot": snapshot.report.model_dump(
                                mode="json"
                            ),
                            "review_id": review.id,
                            "decision": decision.value,
                            "comments": review.comments,
                        },
                    )
                    report = type(snapshot.report).model_validate(
                        {
                            **snapshot.report.model_dump(mode="python"),
                            "human_review": review,
                            "task_status": summarize_task_status(
                                snapshot.task_events
                            ),
                        }
                    )
                    snapshot = snapshot.model_copy(update={"report": report})
                    save_snapshot(snapshot)
                    st.toast("人工复核决定已写入审计记录。")
                    st.rerun()
        else:
            st.info("尚未生成决策报告。")

with audit_tab:
    snapshot = current_snapshot()
    st.header("任务状态、工具调用、失败与人工记录")
    if not snapshot:
        st.info("尚无运行记录。")
    else:
        summary = summarize_task_status(snapshot.task_events)
        metrics = st.columns(4)
        metrics[0].metric("当前状态", summary.current_status.value)
        metrics[1].metric("事件数", summary.event_count)
        metrics[2].metric("工具调用", len(snapshot.tool_calls))
        metrics[3].metric("失败记录", len(summary.failure_messages))
        st.caption(
            f"运行 ID：{snapshot.run_id} · 本地保存："
            f".workbench/runs/{snapshot.run_id}.json"
        )
        st.subheader("任务事件")
        st.dataframe(
            [
                {
                    "时间": event.occurred_at.isoformat(),
                    "状态": event.status.value,
                    "事件": event.event_type.value,
                    "操作者": event.actor,
                    "消息": event.message,
                    "详情": (
                        json.dumps(event.metadata, ensure_ascii=False)
                        if event.metadata
                        else ""
                    ),
                }
                for event in snapshot.task_events
            ],
            width="stretch",
            hide_index=True,
        )
        st.subheader("工具调用")
        st.dataframe(
            [
                {
                    "调用 ID": call.call_id,
                    "工具": call.tool_name,
                    "状态": call.status,
                    "输入摘要": call.input_summary,
                    "输出摘要": call.output_summary or "",
                    "错误": call.error or "",
                    "重试自": call.retry_of or "",
                    "详情": (
                        json.dumps(call.metadata, ensure_ascii=False)
                        if call.metadata
                        else ""
                    ),
                }
                for call in snapshot.tool_calls
            ],
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "下载完整运行快照",
            data=json.dumps(
                snapshot.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            file_name=f"{snapshot.run_id}.json",
            mime="application/json",
            width="stretch",
        )

    st.subheader("恢复历史运行")
    st.caption(
        "为避免在共享页面枚举其他会话的本地数据，请输入创建任务时显示或"
        "已下载快照中的完整运行 ID；该 ID 在当前单用户本机模式中相当于"
        "访问凭证，请勿泄露。"
    )
    selected_run = st.text_input("完整运行 ID")
    if st.button(
        "恢复指定运行",
        disabled=not selected_run.strip(),
        width="stretch",
    ):
        try:
            restored = RUN_STORE.load(selected_run.strip())
        except (OSError, ValueError) as exc:
            st.error(f"无法恢复该运行：{exc}")
        else:
            st.session_state[RUN_STATE_KEY] = restored.model_dump(mode="json")
            st.success(f"已恢复 {selected_run.strip()}")
            st.rerun()

    evaluation = load_latest_evaluation()
    if evaluation:
        with st.expander("继承自 VetEvidence AI v0.1 的定向评测"):
            st.write(
                f"{evaluation.summary.passed}/{evaluation.summary.total} 通过；"
                "这是受控工程检查，不是通用模型准确率。"
            )
