"""Input contracts for the workbench's scoped synergy workflow."""

from __future__ import annotations

import re
from collections.abc import Sequence


def _compact(value: str | None) -> str:
    """Normalize punctuation and spacing while preserving Unicode letters."""

    return re.sub(r"[^\w]+", "", (value or "").casefold())


def _has_letter_or_number(value: str | None) -> bool:
    """Return whether the value contains a meaningful Unicode character."""

    return any(character.isalnum() for character in (value or ""))


def validate_synergy_question_input(
    *,
    question_text: str,
    population: str,
    intervention: str,
    comparator: str,
    outcomes: Sequence[str],
) -> list[str]:
    """Return user-facing errors for the supported synergy question contract.

    Structured fields define the scientific scope.  Requiring the same labels
    in the visible question prevents retrieval, CSV gates and the report from
    silently referring to different interventions or organisms.
    """

    errors: list[str] = []
    if len(question_text.strip()) < 3:
        errors.append("科研问题至少需要 3 个字符。")
    elif not _has_letter_or_number(question_text):
        errors.append("科研问题必须包含文字或数字。")

    required_fields = (
        ("病原体/研究对象", population),
        ("候选干预", intervention),
        ("对照/联合药物", comparator),
    )
    for label, value in required_fields:
        if not value.strip():
            errors.append(f"{label}不能为空。")
        elif not _has_letter_or_number(value):
            errors.append(f"{label}必须包含文字或数字。")

    intervention_key = _compact(intervention)
    comparator_key = _compact(comparator)
    if (
        intervention_key
        and comparator_key
        and intervention_key == comparator_key
    ):
        errors.append("候选干预与对照/联合药物必须是不同对象。")

    question_key = _compact(question_text)
    if question_key:
        for label, value in required_fields:
            value_key = _compact(value)
            if value_key and value_key not in question_key:
                errors.append(
                    f"科研问题正文未包含{label}“{value.strip()}”；"
                    "正文与结构化范围必须一致。"
                )

    nonempty_outcomes = [value for value in outcomes if value.strip()]
    if not nonempty_outcomes:
        errors.append("至少填写一个预设结局指标。")
    else:
        for value in nonempty_outcomes:
            if not _has_letter_or_number(value):
                errors.append(
                    f"预设结局指标“{value.strip()}”必须包含文字或数字。"
                )
    return errors
