from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from vetevidence.config import load_settings
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
    analyze_network_pharmacology_csv,
    build_vina_manifest,
    parse_vina_output,
    require_docking_scope,
    require_network_scope,
    validate_pdbqt_bytes,
)
from vetevidence.models import PubMedArticle
from vetevidence.pubmed import PubMedClient, PubMedError
from vetevidence.run_store import (
    RunStore,
    WorkbenchRunSnapshot,
    build_tool_call,
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
RUN_STORE = RunStore()
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
) -> WorkbenchRunSnapshot:
    retry_of = next(
        (
            call.call_id
            for call in reversed(snapshot.tool_calls)
            if call.tool_name == tool_name and call.status == "failed"
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
    )
    return snapshot.model_copy(
        update={"tool_calls": [*snapshot.tool_calls, call]}
    )


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


def render_mechanism_prediction(bundle: MechanismPredictionBundle) -> None:
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

    if bundle.prepared_manifests:
        st.subheader("AutoDock Vina 任务清单")
        completed_ids = {
            run.manifest.task_id for run in bundle.docking_runs
        }
        st.dataframe(
            [
                {
                    "任务 ID": manifest.task_id,
                    "状态": (
                        "已导入真实输出"
                        if manifest.task_id in completed_ids
                        else "待运行，无分数"
                    ),
                    "配体": manifest.compound_name,
                    "配体 accession": manifest.ligand_accession,
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
        st.subheader("已解析的用户导入 Vina 输出")
        st.warning(
            "系统只校验输出格式、版本和内容哈希，不能证明文件确由 Vina "
            "实际运行产生。对接得分仍是计算预测，不是结合实验证据，也不能"
            "单独证明抗菌活性或协同。"
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


st.set_page_config(
    page_title="VetResearch Workbench",
    page_icon="🧪",
    layout="wide",
)

st.title("VetResearch Workbench")
st.caption(
    "VetResearch Workbench v0.3 · 文献、实验、网络药理学、"
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
        "用户导入题录与 CSV 只在本机处理；未报告字段保持为空，不由系统补造。"
    )
    st.caption(
        "当前版本仅支持可信的单用户本机运行，不具备共享部署所需的账号与对象授权。"
    )
    st.caption("透明规则工作流，无需 LLM API Key。")

(
    question_tab,
    literature_tab,
    experiment_tab,
    mechanism_tab,
    report_tab,
    audit_tab,
) = st.tabs(
    [
        "1 问题与假设",
        "2 文献证据",
        "3 实验数据",
        "4 机制预测",
        "5 决策报告",
        "6 运行记录",
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
            "两个 CSV 都必须包含 organism，并与当前科研问题完全匹配。"
        )
        template_columns = st.columns(2)
        template_columns[0].download_button(
            "下载化合物—靶点模板",
            data=(
                PROJECT_ROOT / "data" / "templates"
                / "compound_target_template.csv"
            ).read_bytes(),
            file_name="compound_target_template.csv",
            mime="text/csv",
            width="stretch",
        )
        template_columns[1].download_button(
            "下载靶点—通路模板",
            data=(
                PROJECT_ROOT / "data" / "templates"
                / "target_pathway_template.csv"
            ).read_bytes(),
            file_name="target_pathway_template.csv",
            mime="text/csv",
            width="stretch",
        )
        st.info(
            "化合物—靶点必填：compound、compound_accession、organism、"
            "target、target_accession。靶点—通路必填：organism、target、"
            "target_accession、pathway、pathway_accession。"
        )
        network_upload_columns = st.columns(2)
        compound_target_file = network_upload_columns[0].file_uploader(
            "上传化合物—靶点 CSV",
            type=["csv"],
            key=f"compound-target-{snapshot.run_id}",
        )
        target_pathway_file = network_upload_columns[1].file_uploader(
            "上传靶点—通路 CSV",
            type=["csv"],
            key=f"target-pathway-{snapshot.run_id}",
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
                network_input_summary = "合成网络药理学演示"
            else:
                compound_payload = compound_target_file.getvalue()
                pathway_payload = target_pathway_file.getvalue()
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
                network_result = analyze_network_pharmacology_csv(
                    compound_payload,
                    pathway_payload,
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
        st.subheader("AutoDock Vina：准备可复现任务")
        st.caption(
            "本机未捆绑 Vina。这里先保存 PDBQT 内容哈希、来源、搜索框和"
            "版本，下载任务清单后可在合规环境运行，再把带任务清单哈希的"
            "标准输出导回。格式校验不能代替运行环境审计。"
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
            ligand_source_name = st.text_input(
                "配体结构来源",
                value="用户提供的 PDBQT",
                key=f"ligand-source-name-{snapshot.run_id}",
            )
            ligand_source_version = st.text_input(
                "配体结构来源版本",
                value="user-provided",
                key=f"ligand-source-version-{snapshot.run_id}",
            )
            ligand_file = st.file_uploader(
                "上传配体 PDBQT",
                type=["pdbqt"],
                key=f"ligand-pdbqt-{snapshot.run_id}",
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
            )

        engine_version = st.text_input(
            "AutoDock Vina 版本",
            value="1.2.5",
            key=f"vina-version-{snapshot.run_id}",
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
        prepare_manifest = st.button(
            "生成并保存 Vina 任务清单",
            disabled=ligand_file is None or receptor_file is None,
            width="stretch",
        )
        if prepare_manifest and ligand_file and receptor_file:
            try:
                ligand_sha256 = validate_pdbqt_bytes(
                    ligand_file.getvalue(),
                    role="ligand",
                )
                receptor_sha256 = validate_pdbqt_bytes(
                    receptor_file.getvalue(),
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
                        source_name=(
                            f"{ligand_source_name}；文件={ligand_file.name}"
                        ),
                        accession=ligand_accession,
                        version=ligand_source_version,
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
                    "docking.prepare",
                    f"{docking_compound} × {receptor_name}",
                    status="failed",
                    error=str(exc),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.FAILED,
                    f"Vina 任务清单校验失败：{exc}",
                )
                save_snapshot(snapshot)
                st.error(str(exc))
            else:
                snapshot = current_snapshot()
                bundle = snapshot.mechanism_prediction.model_copy(
                    update={
                        "prepared_manifests": [
                            *snapshot.mechanism_prediction.prepared_manifests,
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
                snapshot = append_tool_call(
                    snapshot,
                    "docking.prepare",
                    f"{docking_compound} × {receptor_name}",
                    status="succeeded",
                    output_summary=(
                        f"生成任务 {manifest.task_id}；尚无对接分数"
                    ),
                )
                snapshot = append_event(
                    snapshot,
                    TaskStatus.RUNNING,
                    "AutoDock Vina 可复现任务清单已生成，等待外部运行。",
                )
                save_snapshot(snapshot)
                st.success("任务清单已保存；未导入真实输出前不会显示分数。")

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
            vina_output_file = st.file_uploader(
                "上传 Vina 标准输出文本",
                type=["txt", "log"],
                key=f"vina-output-{snapshot.run_id}",
            )
            import_vina_output = st.button(
                "校验并导入 Vina 输出",
                disabled=vina_output_file is None,
                width="stretch",
            )
            if import_vina_output and vina_output_file:
                try:
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

        render_mechanism_prediction(current_snapshot().mechanism_prediction)

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
