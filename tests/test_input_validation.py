from __future__ import annotations

from vetevidence.input_validation import validate_synergy_question_input


def test_valid_synergy_question_has_no_errors() -> None:
    errors = validate_synergy_question_input(
        question_text=(
            "quercetin 与 amoxicillin 对 Streptococcus agalactiae 是否协同？"
        ),
        population="Streptococcus agalactiae",
        intervention="quercetin",
        comparator="amoxicillin",
        outcomes=["FICI", "生长曲线"],
    )

    assert errors == []


def test_missing_scope_and_outcomes_are_reported() -> None:
    errors = validate_synergy_question_input(
        question_text="候选组合是否协同？",
        population="",
        intervention="",
        comparator="",
        outcomes=[],
    )

    assert "病原体/研究对象不能为空。" in errors
    assert "候选干预不能为空。" in errors
    assert "对照/联合药物不能为空。" in errors
    assert "至少填写一个预设结局指标。" in errors


def test_question_text_must_match_structured_scope() -> None:
    errors = validate_synergy_question_input(
        question_text="vancomycin 与 rifampin 对 MRSA 是否协同？",
        population="Streptococcus agalactiae",
        intervention="quercetin",
        comparator="amoxicillin",
        outcomes=["FICI"],
    )

    assert len(errors) == 3
    assert all("正文与结构化范围必须一致" in error for error in errors)


def test_interventions_must_be_distinct_after_normalization() -> None:
    errors = validate_synergy_question_input(
        question_text="Drug-A 与 drug a 对 target 是否协同？",
        population="target",
        intervention="Drug-A",
        comparator="drug a",
        outcomes=["FICI"],
    )

    assert errors == ["候选干预与对照/联合药物必须是不同对象。"]


def test_punctuation_only_scientific_input_is_rejected() -> None:
    errors = validate_synergy_question_input(
        question_text="!!!",
        population="---",
        intervention="...",
        comparator="///",
        outcomes=["***"],
    )

    assert errors == [
        "科研问题必须包含文字或数字。",
        "病原体/研究对象必须包含文字或数字。",
        "候选干预必须包含文字或数字。",
        "对照/联合药物必须包含文字或数字。",
        "预设结局指标“***”必须包含文字或数字。",
    ]


def test_unicode_and_hyphenated_names_remain_valid() -> None:
    errors = validate_synergy_question_input(
        question_text="β-lactam 与 Drug-A 对耐药菌株-1 是否协同？",
        population="耐药菌株-1",
        intervention="β-lactam",
        comparator="Drug-A",
        outcomes=["FICI", "生长曲线"],
    )

    assert errors == []


def test_duplicate_outcomes_are_rejected_after_normalization() -> None:
    errors = validate_synergy_question_input(
        question_text="Drug-A 与 Drug-B 对 target 是否协同？",
        population="target",
        intervention="Drug-A",
        comparator="Drug-B",
        outcomes=["FICI", "fici", "F-I-C-I"],
    )

    assert errors == [
        "预设结局指标“fici”与“FICI”重复，请合并后重试。",
        "预设结局指标“F-I-C-I”与“FICI”重复，请合并后重试。",
    ]
