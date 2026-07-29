from __future__ import annotations

from urllib.parse import quote

from vetevidence.models import CitedAnswer, EvidenceCitation, EvidenceRecord


def _citation_markdown(record: EvidenceRecord) -> str:
    pmid_link = f"[PMID {record.pmid}]({record.source_url})"
    if not record.doi:
        return pmid_link
    doi_link = f"[DOI {record.doi}](https://doi.org/{quote(record.doi, safe='/')})"
    return f"{pmid_link}；{doi_link}"


def build_cited_answer(
    question: str,
    evidence_records: list[EvidenceRecord],
) -> CitedAnswer:
    citations = [
        EvidenceCitation(
            pmid=record.pmid,
            doi=record.doi,
            source_quote=record.source_quote,
            source_url=record.source_url,
        )
        for record in evidence_records
    ]

    if not evidence_records:
        return CitedAnswer(
            question=question,
            answer_markdown=(
                "### 证据结论\n\n"
                "当前检索没有返回可用文献，因此不足以回答该问题。"
            ),
            citations=[],
        )

    lines = [
        "### 证据结论",
        "",
        "以下内容仅基于当前检索到的 PubMed 元数据和摘要：",
        "",
    ]
    for record in evidence_records:
        result = record.key_result or "摘要未报告明确结论。"
        lines.append(f"- {result}（{_citation_markdown(record)}）")

    mechanisms = list(
        dict.fromkeys(
            mechanism
            for record in evidence_records
            for mechanism in record.mechanism
        )
    )
    lines.extend(["", "### 机制判断", ""])
    if mechanisms:
        joined = "、".join(mechanisms)
        lines.append(
            f"当前摘要明确提及 {joined}。这支持“这些机制在所述实验模型中"
            "与干预结果相关”的摘要级表述，但不能据此直接外推临床疗效或完整因果链。"
        )
    else:
        lines.append(
            "当前摘要没有提供足够的机制信息，不能判断具体通路是否得到支持。"
        )

    lines.extend(
        [
            "",
            "### 局限",
            "",
            "- 尚未核对论文全文、方法细节和补充材料。",
            "- 动物或体外实验结果不能直接外推到临床。",
            "- 多篇论文结论如有冲突，应保留冲突并逐条核查来源。",
        ]
    )
    return CitedAnswer(
        question=question,
        answer_markdown="\n".join(lines),
        citations=citations,
    )
