from __future__ import annotations

import pytest
from pydantic import BaseModel

from vetevidence.experiment_analysis import (
    FICIAnalysisResult,
    GrowthCurveAnalysisResult,
    analyze_experiment_csv,
    analyze_fici_csv,
    analyze_growth_curve_csv,
)


FICI_HEADER = (
    "drug_a,drug_b,population_or_strain,"
    "drug_a_mic_alone,drug_a_mic_combo,"
    "drug_b_mic_alone,drug_b_mic_combo"
)
FICI_SCOPE = "quercetin,amoxicillin,Streptococcus agalactiae"
GROWTH_HEADER = (
    "population_or_strain,intervention,comparator,time,group,value"
)
GROWTH_SCOPE = "Streptococcus agalactiae,quercetin,amoxicillin"


def test_fici_calculates_values_and_all_boundary_classifications() -> None:
    payload = "\n".join(
        [
            FICI_HEADER,
            f"{FICI_SCOPE},4,1,8,2",
            f"{FICI_SCOPE},4,2,8,4",
            f"{FICI_SCOPE},2,3,2,3",
            f"{FICI_SCOPE},1,3,1,2",
        ]
    )

    result = analyze_fici_csv(payload)

    assert isinstance(result, FICIAnalysisResult)
    assert isinstance(result, BaseModel)
    assert result.valid
    assert result.valid_row_count == 4
    assert [row.classification for row in result.rows] == [
        "synergy",
        "additive",
        "indifferent",
        "antagonism",
    ]
    assert result.rows[0].fic_a == pytest.approx(0.25)
    assert result.rows[0].fic_b == pytest.approx(0.25)
    assert result.rows[0].fici == pytest.approx(0.5)
    assert result.rows[1].fici == pytest.approx(1)
    assert result.rows[2].fici == pytest.approx(3)
    assert result.rows[3].fici == pytest.approx(5)
    assert result.rows[0].row_number == 2
    assert result.rows[0].drug_a == "quercetin"
    assert result.rows[0].drug_b == "amoxicillin"
    assert (
        result.rows[0].population_or_strain
        == "Streptococcus agalactiae"
    )
    assert result.rows[0].raw_row["drug_a_mic_alone"] == "4"


def test_fici_rejects_non_numeric_non_finite_and_non_positive_mics() -> None:
    payload = "\n".join(
        [
            FICI_HEADER,
            f"{FICI_SCOPE},4,not-a-number,8,2",
            f"{FICI_SCOPE},0,1,8,2",
            f"{FICI_SCOPE},4,1,-8,2",
            f"{FICI_SCOPE},4,1,nan,2",
        ]
    )

    result = analyze_fici_csv(payload)

    assert not result.valid
    assert result.valid_row_count == 0
    assert result.invalid_row_count == 4
    assert all(not row.valid for row in result.rows)
    assert result.rows[0].raw_values == [
        "quercetin",
        "amoxicillin",
        "Streptococcus agalactiae",
        "4",
        "not-a-number",
        "8",
        "2",
    ]
    assert any("finite number" in error for error in result.rows[0].errors)
    assert any("greater than 0" in error for error in result.rows[1].errors)
    assert any("greater than 0" in error for error in result.rows[2].errors)
    assert any("finite number" in error for error in result.rows[3].errors)
    assert all(row.fici is None for row in result.rows)


def test_fici_reports_missing_columns_without_losing_input_rows() -> None:
    result = analyze_fici_csv("drug_a_mic_alone,drug_a_mic_combo\n4,1\n")

    assert not result.valid
    assert result.rows[0].raw_values == ["4", "1"]
    assert not result.rows[0].valid
    assert any("missing required columns" in error for error in result.errors)
    assert "drug_b_mic_alone" in result.errors[0]


def test_fici_requires_non_empty_scope_metadata_and_finite_calculation() -> None:
    blank_scope = analyze_fici_csv(
        FICI_HEADER + "\nquercetin,,Streptococcus agalactiae,4,1,8,2\n"
    )
    overflow = analyze_fici_csv(
        FICI_HEADER
        + f"\n{FICI_SCOPE},1e-308,1e308,1e-308,1e308\n"
    )

    assert not blank_scope.valid
    assert any(
        "drug_b is required" in error
        for error in blank_scope.rows[0].errors
    )
    assert not overflow.valid
    assert overflow.rows[0].fici is None
    assert any(
        "non-finite" in error or "overflowed" in error
        for error in overflow.rows[0].errors
    )


def test_growth_curve_aggregates_replicates_and_integrates_group_means() -> None:
    payload = f"""\
{GROWTH_HEADER},note
{GROWTH_SCOPE},0,control,0.0,r1
{GROWTH_SCOPE},0,control,0.2,r2
{GROWTH_SCOPE},1,control,1.0,r1
{GROWTH_SCOPE},1,control,1.2,r2
{GROWTH_SCOPE},2,control,2.0,r1
{GROWTH_SCOPE},2,control,2.2,r2
{GROWTH_SCOPE},0,treated,0.0,r1
{GROWTH_SCOPE},1,treated,0.5,r1
{GROWTH_SCOPE},2,treated,1.0,r1
"""

    result = analyze_growth_curve_csv(payload)

    assert isinstance(result, GrowthCurveAnalysisResult)
    assert result.valid
    assert len(result.timepoints) == 6
    first = result.timepoints[0]
    assert (first.group, first.time, first.n) == ("control", 0, 2)
    assert first.mean == pytest.approx(0.1)
    assert first.sd == pytest.approx(2**0.5 / 10)
    assert first.source_row_numbers == [2, 3]
    treated_first = next(
        item
        for item in result.timepoints
        if item.group == "treated" and item.time == 0
    )
    assert treated_first.sd is None
    auc = {item.group: item.auc for item in result.auc_by_group}
    assert auc["control"] == pytest.approx(2.2)
    assert auc["treated"] == pytest.approx(1.0)
    assert result.rows[0].population_or_strain == "Streptococcus agalactiae"
    assert result.rows[0].intervention == "quercetin"
    assert result.rows[0].comparator == "amoxicillin"
    assert result.rows[0].raw_row["note"] == "r1"


def test_growth_curve_audits_invalid_rows_and_excludes_them() -> None:
    payload = f"""\
{GROWTH_HEADER}
{GROWTH_SCOPE},0,control,0
{GROWTH_SCOPE},1,,1
{GROWTH_SCOPE},two,control,2
{GROWTH_SCOPE},2,control,inf
{GROWTH_SCOPE},3,control,3
"""

    result = analyze_growth_curve_csv(payload)

    assert not result.valid
    assert result.valid_row_count == 2
    assert result.invalid_row_count == 3
    assert any("group is required" in error for error in result.rows[1].errors)
    assert any("finite number" in error for error in result.rows[2].errors)
    assert any("finite number" in error for error in result.rows[3].errors)
    assert [
        (summary.time, summary.mean)
        for summary in result.timepoints
    ] == [(0, 0), (3, 3)]
    assert result.auc_by_group[0].auc == pytest.approx(4.5)


def test_growth_curve_reports_missing_columns_and_row_width_errors() -> None:
    missing = analyze_growth_curve_csv("time,value\n0,1\n")
    malformed = analyze_growth_curve_csv(
        GROWTH_HEADER
        + f"\n{GROWTH_SCOPE},0,control,1,unexpected\n"
    )

    assert not missing.valid
    assert any("missing required columns" in error for error in missing.errors)
    assert missing.rows[0].raw_values == ["0", "1"]
    assert not malformed.valid
    assert "__extra_column_1" in malformed.rows[0].raw_row
    assert any("expected 6" in error for error in malformed.rows[0].errors)
    assert malformed.timepoints == []


def test_growth_curve_requires_scope_and_two_distinct_timepoints_per_group() -> None:
    blank_scope = analyze_growth_curve_csv(
        GROWTH_HEADER + "\n,quercetin,amoxicillin,0,control,0\n"
    )
    single_timepoint = analyze_growth_curve_csv(
        GROWTH_HEADER
        + f"\n{GROWTH_SCOPE},0,control,0.1\n"
        + f"{GROWTH_SCOPE},0,control,0.2\n"
    )

    assert not blank_scope.valid
    assert any(
        "population_or_strain is required" in error
        for error in blank_scope.rows[0].errors
    )
    assert not single_timepoint.valid
    assert single_timepoint.auc_by_group == []
    assert any(
        "at least 2 distinct time points" in error
        for error in single_timepoint.errors
    )


def test_growth_curve_rejects_non_finite_summary_or_auc() -> None:
    result = analyze_growth_curve_csv(
        GROWTH_HEADER
        + f"\n{GROWTH_SCOPE},0,control,1e308\n"
        + f"{GROWTH_SCOPE},1e308,control,1e308\n"
    )

    assert not result.valid
    assert result.auc_by_group == []
    assert any(
        "non-finite" in error or "overflowed" in error
        for error in result.errors
    )


def test_dispatcher_accepts_bom_bytes_and_returns_requested_model() -> None:
    payload = (
        f"\ufeff{GROWTH_HEADER}\n"
        f"{GROWTH_SCOPE},0,control,0\n"
        f"{GROWTH_SCOPE},1,control,1\n"
    ).encode("utf-8")

    result = analyze_experiment_csv(
        payload,
        analysis_type="growth_curve",
    )

    assert isinstance(result, GrowthCurveAnalysisResult)
    assert result.valid
    assert result.auc_by_group[0].auc == pytest.approx(0.5)


def test_header_only_csv_is_invalid_for_both_analyses() -> None:
    fici = analyze_fici_csv(FICI_HEADER + "\n")
    growth = analyze_growth_curve_csv(GROWTH_HEADER + "\n")

    assert not fici.valid
    assert fici.valid_row_count == 0
    assert any("no data rows" in error for error in fici.errors)
    assert not growth.valid
    assert growth.valid_row_count == 0
    assert any("no data rows" in error for error in growth.errors)


def test_analysis_preserves_source_name_and_input_hash() -> None:
    payload = (FICI_HEADER + f"\n{FICI_SCOPE},4,1,8,2\n").encode("utf-8")

    result = analyze_fici_csv(payload, source_name="checkerboard.csv")

    assert result.source_name == "checkerboard.csv"
    assert len(result.input_sha256) == 64
    assert result.input_sha256 == analyze_fici_csv(
        payload,
        source_name="renamed.csv",
    ).input_sha256
