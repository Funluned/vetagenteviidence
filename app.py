from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from vetevidence.config import load_settings
from vetevidence.evaluation import EvaluationReport
from vetevidence.export import (
    evidence_to_csv,
    evidence_to_rows,
    research_to_markdown,
)
from vetevidence.models import PubMedArticle, ResearchResult
from vetevidence.pubmed import PubMedClient, PubMedError
from vetevidence.retrieval import run_research


def load_latest_evaluation() -> EvaluationReport | None:
    report_path = Path(__file__).parent / "data" / "eval" / "latest_results.json"
    if not report_path.exists():
        return None
    return EvaluationReport.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )


def render_articles(articles: list[PubMedArticle]) -> None:
    st.dataframe(
        [
            {
                "年份": article.year or "未报告",
                "标题": article.title,
                "期刊": article.journal or "未报告",
                "ISSN": article.issn or article.issn_linking or "未报告",
                "中科院分区（LetPub 2025年3月升级版）": (
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
                "DOI": article.doi or "未报告",
            }
            for article in articles
        ],
        width="stretch",
        hide_index=True,
    )

    for article in articles:
        st.subheader(article.title)
        metadata = [
            article.journal or "期刊未报告",
            str(article.year) if article.year else "年份未报告",
            f"PMID: {article.pmid}",
            f"DOI: {article.doi}" if article.doi else "DOI: 未报告",
        ]
        st.caption(" · ".join(metadata))
        if article.authors:
            st.write("作者：" + ", ".join(article.authors))
        ranking = article.journal_ranking
        if ranking:
            ranking_columns = st.columns(2)
            with ranking_columns[0]:
                st.markdown(f"**中科院分区（{ranking.cas_edition}）**")
                st.write(ranking.cas_display())
            with ranking_columns[1]:
                st.markdown(f"**JCR 分区（{ranking.jcr_edition}）**")
                st.write(ranking.jcr_display())
            source_links = []
            if ranking.cas_source_url:
                source_links.append(
                    f"[核查中科院分区来源]({ranking.cas_source_url})"
                )
            if ranking.jcr_source_url:
                source_links.append(
                    f"[核查 JCR 分区来源]({ranking.jcr_source_url})"
                )
            if source_links:
                st.markdown(" · ".join(source_links))
            if ranking.source_note:
                st.caption(ranking.source_note)
        st.markdown(f"[在 PubMed 核查原始记录]({article.source_url})")
        with st.expander("查看摘要", expanded=False):
            st.write(article.abstract or "摘要未提供。")


def evidence_to_display_rows(result: ResearchResult) -> list[dict[str, object]]:
    display_rows: list[dict[str, object]] = []
    for row in evidence_to_rows(result.evidence):
        display_rows.append(
            {
                "期刊": row.pop("journal"),
                (
                    f"中科院分区（{row.pop('cas_partition_edition')}）"
                ): row.pop("cas_partition"),
                (
                    f"JCR 分区（{row.pop('jcr_partition_edition')}）"
                ): row.pop("jcr_partition"),
                "ISSN": row.pop("issn"),
                "分区数据备注": row.pop("journal_ranking_note"),
                **row,
            }
        )
    return display_rows


st.set_page_config(page_title="VetEvidence AI", page_icon="🔬", layout="wide")

st.title("VetEvidence AI")
st.caption("兽医科研证据智能体 · 检索、证据提取、引用回答与导出")
st.warning("仅用于科研证据整理，不构成医疗或兽医诊断建议。")

with st.sidebar:
    st.header("检索设置")
    max_results = st.slider("最多返回文献数", min_value=1, max_value=20, value=5)
    st.markdown(
        "当前检索 PubMed 元数据与摘要。摘要未报告的信息不会由系统补造。"
    )
    st.caption("提取方式：透明规则 rules_v1（无需 LLM API Key）")
    st.caption(
        "期刊分区：按 ISSN 动态查询 LetPub；中科院采用 2025 年 3 月升级版，"
        "JCR 采用 WOS 的 JIF 分区。查询失败时使用 7 天缓存或本地回退表。"
    )

query = st.text_input(
    "输入病原菌、疾病模型、药物或科研问题",
    value="quercetin Streptococcus agalactiae mastitis",
)

if st.button("检索 PubMed", type="primary", width="stretch"):
    st.session_state.pop("research_result", None)
    client = PubMedClient(load_settings())
    try:
        with st.spinner("正在查询 NCBI PubMed…"):
            research_result = run_research(
                query,
                max_results=max_results,
                client=client,
            )
    except (PubMedError, ValueError) as exc:
        st.error(f"PubMed 检索失败：{exc}")
    else:
        st.session_state["research_result"] = research_result.model_dump(
            mode="json"
        )
    finally:
        client.close()

result_payload = st.session_state.get("research_result")
if result_payload:
    result = ResearchResult.model_validate(result_payload)
    if not result.articles:
        st.info("没有找到结果。请尝试缩短问题或改用英文关键词。")
    else:
        st.success(
            f"找到 {len(result.articles)} 篇文献，并生成 "
            f"{len(result.evidence)} 条结构化证据。"
        )
        st.caption(
            f"本次 NCBI 请求：{result.retrieval_request_count} 次 · "
            f"LLM 估算成本：${result.estimated_llm_cost_usd:.4f}"
        )
        (
            literature_tab,
            evidence_tab,
            answer_tab,
            evaluation_tab,
            export_tab,
        ) = st.tabs(
            ["文献列表", "证据表", "证据回答", "评测", "导出"]
        )

        with literature_tab:
            render_articles(result.articles)

        with evidence_tab:
            st.caption(
                "空值表示摘要未报告；期刊分区未匹配时显示“未收录”，"
                "系统不会自动补造。"
            )
            st.dataframe(
                evidence_to_display_rows(result),
                width="stretch",
                hide_index=True,
            )

        with answer_tab:
            st.markdown(result.answer.answer_markdown)
            with st.expander("查看引用原句", expanded=False):
                for citation in result.answer.citations:
                    st.markdown(
                        f"**PMID [{citation.pmid}]({citation.source_url})**"
                    )
                    st.write(citation.source_quote or "摘要未提供可引用原句。")

        with evaluation_tab:
            evaluation = load_latest_evaluation()
            if evaluation is None:
                st.info("尚未生成评测报告。")
            else:
                summary = evaluation.summary
                metric_columns = st.columns(4)
                metric_columns[0].metric("样本数", summary.total)
                metric_columns[1].metric("通过", summary.passed)
                metric_columns[2].metric("失败", summary.failed)
                metric_columns[3].metric(
                    "定向通过率",
                    f"{summary.pass_rate:.1%}",
                )
                st.dataframe(
                    [
                        {
                            "分类": category,
                            "样本数": metrics.total,
                            "通过": metrics.passed,
                            "失败": metrics.failed,
                            "通过率": f"{metrics.pass_rate:.1%}",
                        }
                        for category, metrics in summary.by_category.items()
                    ],
                    width="stretch",
                    hide_index=True,
                )
                st.warning(
                    "这是针对当前示范查询和受控边界场景的小样本工程检查，"
                    "不是通用模型准确率。"
                )

        with export_tab:
            markdown_report = research_to_markdown(result)
            csv_report = evidence_to_csv(result.evidence)
            st.download_button(
                "下载 Markdown 报告",
                data=markdown_report,
                file_name="vetevidence_report.md",
                mime="text/markdown",
                width="stretch",
            )
            st.download_button(
                "下载 CSV 证据表",
                data=csv_report.encode("utf-8-sig"),
                file_name="vetevidence_evidence.csv",
                mime="text/csv",
                width="stretch",
            )
