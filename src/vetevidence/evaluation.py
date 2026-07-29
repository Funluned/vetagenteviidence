from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from vetevidence.answering import build_cited_answer
from vetevidence.models import EvidenceRecord, ResearchResult


class EvaluationCase(BaseModel):
    id: str
    category: str
    question: str
    check: str
    expected: Any
    pmid: str | None = None
    field: str | None = None


class EvaluationCaseResult(BaseModel):
    id: str
    category: str
    question: str
    passed: bool
    expected: Any
    actual: Any
    error_type: str | None = None


class CategoryMetrics(BaseModel):
    total: int
    passed: int
    failed: int
    pass_rate: float


class EvaluationSummary(BaseModel):
    total: int
    passed: int
    failed: int
    pass_rate: float
    by_category: dict[str, CategoryMetrics] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    query: str
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    summary: EvaluationSummary
    results: list[EvaluationCaseResult]


def load_evaluation_cases(path: Path) -> list[EvaluationCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [EvaluationCase.model_validate(item) for item in payload]


def _find_article(result: ResearchResult, pmid: str | None) -> object:
    match = next(
        (article for article in result.articles if article.pmid == pmid),
        None,
    )
    if match is None:
        raise LookupError(f"没有找到 PMID {pmid} 的文献。")
    return match


def _find_evidence(result: ResearchResult, pmid: str | None) -> EvidenceRecord:
    match = next(
        (record for record in result.evidence if record.pmid == pmid),
        None,
    )
    if match is None:
        raise LookupError(f"没有找到 PMID {pmid} 的证据记录。")
    return match


def _get_field(item: object, field: str | None) -> Any:
    if not field:
        raise ValueError("当前检查缺少 field。")
    return getattr(item, field)


def _controlled_conflict_check() -> bool:
    reduced = EvidenceRecord(
        pmid="CONTROLLED-1",
        source_url="https://example.invalid/controlled-1",
        key_result="The intervention reduced the outcome.",
        source_quote="The intervention reduced the outcome.",
    )
    increased = EvidenceRecord(
        pmid="CONTROLLED-2",
        source_url="https://example.invalid/controlled-2",
        key_result="The intervention increased the outcome.",
        source_quote="The intervention increased the outcome.",
    )
    answer = build_cited_answer("结果是否一致？", [reduced, increased])
    return (
        reduced.key_result in answer.answer_markdown
        and increased.key_result in answer.answer_markdown
    )


def _run_check(
    case: EvaluationCase,
    result: ResearchResult,
) -> tuple[Any, bool]:
    if case.check == "article_present":
        actual = any(article.pmid == case.pmid for article in result.articles)
        return actual, actual == case.expected

    if case.check == "result_count_at_least":
        actual = len(result.articles)
        return actual, actual >= int(case.expected)

    if case.check.startswith("article_field_"):
        value = _get_field(_find_article(result, case.pmid), case.field)
    elif case.check.startswith("evidence_field_"):
        value = _get_field(_find_evidence(result, case.pmid), case.field)
    else:
        value = None

    if case.check == "answer_contains":
        actual = str(case.expected) in result.answer.answer_markdown
        return actual, actual

    if case.check.endswith("_equals"):
        return value, value == case.expected

    if case.check.endswith("_contains"):
        if isinstance(value, list):
            searchable = " ".join(str(item) for item in value)
        else:
            searchable = "" if value is None else str(value)
        actual = str(case.expected).casefold() in searchable.casefold()
        return actual, actual

    if case.check == "evidence_field_is_none":
        actual = value is None
        return actual, actual == case.expected

    if case.check == "claims_traceable":
        actual = all(
            (not record.key_result)
            or (
                record.key_result in result.answer.answer_markdown
                and record.pmid in result.answer.answer_markdown
            )
            for record in result.evidence
        )
        return actual, actual == case.expected

    if case.check == "citation_count_matches":
        actual = len(result.answer.citations)
        expected_count = len(result.evidence)
        return actual, actual == expected_count

    if case.check == "source_quote_supports":
        record = _find_evidence(result, case.pmid)
        quote_text = (record.source_quote or "").casefold()
        expected_terms = [str(item) for item in case.expected]
        missing = [
            term
            for term in expected_terms
            if term.casefold() not in quote_text
        ]
        return {"missing_terms": missing}, not missing

    if case.check == "insufficient_answer":
        answer = build_cited_answer(case.question, [])
        actual = (
            "不足以回答" in answer.answer_markdown
            and not answer.citations
        )
        return actual, actual == case.expected

    if case.check == "conflict_preserved":
        actual = _controlled_conflict_check()
        return actual, actual == case.expected

    raise ValueError(f"未知检查类型：{case.check}")


def evaluate_research(
    result: ResearchResult,
    cases: list[EvaluationCase],
) -> EvaluationReport:
    case_results: list[EvaluationCaseResult] = []
    for case in cases:
        try:
            actual, passed = _run_check(case, result)
            error_type = None if passed else "expectation_mismatch"
        except (AttributeError, LookupError, TypeError, ValueError) as exc:
            actual = str(exc)
            passed = False
            error_type = "evaluation_error"

        case_results.append(
            EvaluationCaseResult(
                id=case.id,
                category=case.category,
                question=case.question,
                passed=passed,
                expected=case.expected,
                actual=actual,
                error_type=error_type,
            )
        )

    category_results: dict[str, list[EvaluationCaseResult]] = defaultdict(list)
    for case_result in case_results:
        category_results[case_result.category].append(case_result)

    by_category = {}
    for category, items in sorted(category_results.items()):
        passed = sum(item.passed for item in items)
        by_category[category] = CategoryMetrics(
            total=len(items),
            passed=passed,
            failed=len(items) - passed,
            pass_rate=passed / len(items),
        )

    total_passed = sum(item.passed for item in case_results)
    summary = EvaluationSummary(
        total=len(case_results),
        passed=total_passed,
        failed=len(case_results) - total_passed,
        pass_rate=total_passed / len(case_results) if case_results else 0,
        by_category=by_category,
    )
    return EvaluationReport(
        query=result.query,
        summary=summary,
        results=case_results,
    )


def evaluation_report_to_markdown(report: EvaluationReport) -> str:
    summary = report.summary
    lines = [
        "# VetEvidence AI 评测报告",
        "",
        f"- 运行时间：{report.generated_at.isoformat()}",
        f"- 真实检索词：`{report.query}`",
        f"- 样本数：{summary.total}",
        f"- 通过：{summary.passed}",
        f"- 失败：{summary.failed}",
        f"- 定向检查通过率：{summary.pass_rate:.1%}",
        "",
        "> 本报告是针对当前示范查询和受控边界场景的小样本工程检查，"
        "不是通用模型准确率，也不能替代人工全文核查。",
        "",
        "## 分类结果",
        "",
        "| 分类 | 样本数 | 通过 | 失败 | 通过率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, metrics in summary.by_category.items():
        lines.append(
            f"| {category} | {metrics.total} | {metrics.passed} | "
            f"{metrics.failed} | {metrics.pass_rate:.1%} |"
        )

    lines.extend(
        [
            "",
            "## 逐条结果",
            "",
            "| ID | 分类 | 结果 | 问题 | 实际值 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in report.results:
        status = "通过" if item.passed else "失败"
        actual = json.dumps(item.actual, ensure_ascii=False)
        actual = actual.replace("|", "\\|").replace("\n", " ")[:160]
        question = item.question.replace("|", "\\|")
        lines.append(
            f"| {item.id} | {item.category} | {status} | "
            f"{question} | {actual} |"
        )

    failures = [item for item in report.results if not item.passed]
    lines.extend(["", "## 失败与错误分类", ""])
    if not failures:
        lines.append(
            "本次定向检查没有失败项；仍需增加不同病原、药物、物种和全文"
            "样本，不能据此声称系统已达到通用准确率。"
        )
    else:
        for failure in failures:
            lines.append(
                f"- `{failure.id}` `{failure.error_type}`："
                f"{failure.question}；实际值 `{failure.actual}`。"
            )

    lines.extend(
        [
            "",
            "## 已知局限",
            "",
            "- 大部分字段检查集中在一个真实示范查询，覆盖面有限。",
            "- 当前提取器是透明规则，不理解未列入规则的新表达方式。",
            "- 引用检查验证了 PMID、DOI、来源原句和结论的机械对应，"
            "尚未完成人工语义支持度复核。",
            "- PubMed 摘要不等于论文全文，剂量、对照和局限可能缺失。",
        ]
    )
    return "\n".join(lines)
