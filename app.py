from __future__ import annotations

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
from vetevidence.literature_import import (
    LiteratureImportResult,
    parse_literature_export,
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
    LiteratureEvidenceGrade.OUT_OF_SCOPE: "无关",
    LiteratureEvidenceGrade.CONTEXTUAL: "间接背景",
    LiteratureEvidenceGrade.DIRECT_INTERACTION: "直接文献证据",
}
QUESTION_PRESETS = {
    "证据不足负例：槲皮素 + 阿莫西林 / 无乳链球菌": {
        "question": (
            "quercetin 与 amoxicillin 对 Streptococcus agalactiae "
            "是否具有值得进一步验证的协同作用？"
        ),
        "population": "Streptococcus agalactiae",
        "intervention": "quercetin",
        "comparator": "amoxicillin",
        "outcomes": "FICI, 生长曲线, 抑菌效应",
    },
    "公开直接文献证据正例：氟苯尼考 + 甲砜霉素 / 多杀性巴氏杆菌": {
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
    if isinstance(analysis, FICIAnalysisResult):
        st.subheader("FICI 结果")
        st.dataframe(
            [
                {
                    "CSV 行": row.row_number,
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
            "阈值：FICI ≤ 0.5 协同；≤ 1 相加；≤ 4 无关；> 4 拮抗。"
        )
    elif isinstance(analysis, GrowthCurveAnalysisResult):
        st.subheader("生长曲线结果")
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


st.set_page_config(
    page_title="VetResearch Workbench",
    page_icon="🧪",
    layout="wide",
)

st.title("VetResearch Workbench")
st.caption(
    "基于 VetEvidence AI v0.1 · 问题、文献、实验数据与人工复核的可审计科研决策闭环"
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
    report_tab,
    audit_tab,
) = st.tabs(
    ["1 问题与假设", "2 文献证据", "3 实验数据", "4 决策报告", "5 运行记录"]
)

with question_tab:
    st.header("定义科研问题")
    preset_name = st.selectbox(
        "公开验收案例",
        list(QUESTION_PRESETS),
        help=(
            "负例用于验证系统会明确报告证据不足；正例对应 PubMed "
            "PMID 31749775，用于验证直接文献证据准入。所有字段仍可人工修改。"
        ),
    )
    preset = QUESTION_PRESETS[preset_name]
    with st.form("research_question_form"):
        question_text = st.text_area(
            "科研问题",
            value=preset["question"],
        )
        question_columns = st.columns(3)
        population = question_columns[0].text_input(
            "病原体/研究对象",
            value=preset["population"],
        )
        intervention = question_columns[1].text_input(
            "候选干预",
            value=preset["intervention"],
        )
        comparator = question_columns[2].text_input(
            "对照/联合药物",
            value=preset["comparator"],
        )
        outcomes_text = st.text_input(
            "预设结局指标（逗号分隔）",
            value=preset["outcomes"],
        )
        create_task = st.form_submit_button(
            "创建或重置研究任务",
            type="primary",
            width="stretch",
        )

    if create_task and len(question_text.strip()) < 3:
        st.error("科研问题至少需要 3 个字符。")
    elif create_task:
        outcomes = [
            value.strip()
            for value in outcomes_text.replace("，", ",").split(",")
            if value.strip()
        ]
        run_id = (
            f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid4().hex}"
        )
        research_question = ResearchQuestion(
            id=f"rq-{uuid4().hex[:12]}",
            text=question_text,
            population=population or None,
            intervention=intervention or None,
            comparator=comparator or None,
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
            "直接、间接、无关稳定分桶后限制页面数量。"
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
            metric_columns[3].metric("无关/未评估", excluded_count)
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
                if is_demo_analysis:
                    st.success("分析完成。演示 CSV 为合成数据。")
                else:
                    st.success("分析完成。")
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
