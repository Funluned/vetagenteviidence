from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from vetevidence.models import EvidenceRecord, ResearchResult


def evidence_to_rows(
    evidence_records: Iterable[EvidenceRecord],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in evidence_records:
        row = record.model_dump()
        for key in ("outcomes", "mechanism", "limitations"):
            row[key] = "；".join(row[key])
        rows.append(row)
    return rows


def evidence_to_csv(evidence_records: list[EvidenceRecord]) -> str:
    output = io.StringIO(newline="")
    fieldnames = list(EvidenceRecord.model_fields)
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(evidence_to_rows(evidence_records))
    return output.getvalue()


def _value(value: object) -> str:
    if value is None or value == "" or value == []:
        return "未报告"
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    return str(value)


def research_to_markdown(result: ResearchResult) -> str:
    lines = [
        "# VetEvidence AI 科研证据报告",
        "",
        f"- 检索问题：{result.query}",
        f"- 文献数量：{len(result.articles)}",
        f"- 提取方式：{result.provider_name}",
        f"- 生成时间：{result.generated_at.isoformat()}",
        "",
        result.answer.answer_markdown,
        "",
        "## 结构化证据",
        "",
    ]

    if not result.evidence:
        lines.append("当前没有可导出的证据记录。")
        return "\n".join(lines)

    for index, record in enumerate(result.evidence, start=1):
        lines.extend(
            [
                f"### 证据 {index}：PMID {record.pmid}",
                "",
                f"- 病原体：{_value(record.pathogen)}",
                f"- 疾病或条件：{_value(record.disease_or_condition)}",
                f"- 物种：{_value(record.species)}",
                f"- 模型：{_value(record.model)}",
                f"- 样本量：{_value(record.sample_size)}",
                f"- 干预：{_value(record.intervention)}",
                f"- 药物：{_value(record.drug)}",
                f"- 剂量：{_value(record.dose)}",
                f"- 给药途径：{_value(record.route)}",
                f"- 时长：{_value(record.duration)}",
                f"- 对照：{_value(record.control)}",
                f"- 结果：{_value(record.outcomes)}",
                f"- 机制：{_value(record.mechanism)}",
                f"- 关键结论：{_value(record.key_result)}",
                f"- 期刊：{_value(record.journal)}",
                f"- ISSN：{_value(record.issn)}",
                (
                    f"- 中科院分区（{_value(record.cas_partition_edition)}）："
                    f"{_value(record.cas_partition)}"
                ),
                (
                    f"- JCR 分区（{_value(record.jcr_partition_edition)}）："
                    f"{_value(record.jcr_partition)}"
                ),
                f"- 分区数据备注：{_value(record.journal_ranking_note)}",
                f"- PMID：{record.pmid}",
                f"- DOI：{_value(record.doi)}",
                f"- 局限：{_value(record.limitations)}",
                "",
                "来源原句：",
                "",
                f"> {_value(record.source_quote)}",
                "",
            ]
        )
    return "\n".join(lines)
