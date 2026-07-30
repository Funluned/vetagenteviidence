from __future__ import annotations

import csv
import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import StringIO
from statistics import fmean, stdev
from typing import Literal

from pydantic import BaseModel, Field


FICI_REQUIRED_COLUMNS = (
    "drug_a",
    "drug_b",
    "population_or_strain",
    "drug_a_mic_alone",
    "drug_a_mic_combo",
    "drug_b_mic_alone",
    "drug_b_mic_combo",
)
GROWTH_CURVE_REQUIRED_COLUMNS = (
    "population_or_strain",
    "intervention",
    "comparator",
    "time",
    "group",
    "value",
)
FICI_NUMERIC_COLUMNS = (
    "drug_a_mic_alone",
    "drug_a_mic_combo",
    "drug_b_mic_alone",
    "drug_b_mic_combo",
)

FICIClassification = Literal[
    "synergy",
    "additive",
    "indifferent",
    "antagonism",
]
ExperimentAnalysisType = Literal["fici", "growth_curve"]

_NUMBER_PATTERN = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


class CSVRowAudit(BaseModel):
    """The original CSV row and every validation error associated with it."""

    row_number: int = Field(ge=2)
    raw_values: list[str] = Field(default_factory=list)
    raw_row: dict[str, str | None] = Field(default_factory=dict)
    valid: bool
    errors: list[str] = Field(default_factory=list)


class FICIRowResult(CSVRowAudit):
    drug_a: str | None = None
    drug_b: str | None = None
    population_or_strain: str | None = None
    drug_a_mic_alone: float | None = None
    drug_a_mic_combo: float | None = None
    drug_b_mic_alone: float | None = None
    drug_b_mic_combo: float | None = None
    fic_a: float | None = None
    fic_b: float | None = None
    fici: float | None = None
    classification: FICIClassification | None = None


class FICIAnalysisResult(BaseModel):
    analysis_type: Literal["fici"] = "fici"
    headers: list[str] = Field(default_factory=list)
    rows: list[FICIRowResult] = Field(default_factory=list)
    valid: bool
    valid_row_count: int = Field(ge=0)
    invalid_row_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    source_name: str | None = None
    input_sha256: str = Field(min_length=64, max_length=64)


class GrowthCurveObservation(CSVRowAudit):
    population_or_strain: str | None = None
    intervention: str | None = None
    comparator: str | None = None
    time: float | None = None
    group: str | None = None
    value: float | None = None


class GrowthTimepointSummary(BaseModel):
    group: str
    time: float
    mean: float
    sd: float | None
    n: int = Field(ge=1)
    source_row_numbers: list[int] = Field(default_factory=list)


class GrowthCurveAUC(BaseModel):
    group: str
    auc: float
    n_timepoints: int = Field(ge=1)
    start_time: float
    end_time: float


class GrowthCurveAnalysisResult(BaseModel):
    analysis_type: Literal["growth_curve"] = "growth_curve"
    headers: list[str] = Field(default_factory=list)
    rows: list[GrowthCurveObservation] = Field(default_factory=list)
    timepoints: list[GrowthTimepointSummary] = Field(default_factory=list)
    auc_by_group: list[GrowthCurveAUC] = Field(default_factory=list)
    valid: bool
    valid_row_count: int = Field(ge=0)
    invalid_row_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    source_name: str | None = None
    input_sha256: str = Field(min_length=64, max_length=64)


ExperimentAnalysisResult = FICIAnalysisResult | GrowthCurveAnalysisResult


@dataclass
class _RawCSVRow:
    row_number: int
    values: list[str]
    mapping: dict[str, str | None]
    errors: list[str] = field(default_factory=list)


@dataclass
class _ParsedCSV:
    headers: list[str] = field(default_factory=list)
    rows: list[_RawCSVRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _decode_payload(payload: bytes | str) -> tuple[str, str | None]:
    if isinstance(payload, str):
        return payload.lstrip("\ufeff"), None
    try:
        return payload.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        try:
            return payload.decode("gb18030"), None
        except UnicodeDecodeError as exc:
            return "", f"CSV encoding is not valid UTF-8 or GB18030: {exc}"


def _payload_sha256(payload: bytes | str) -> str:
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _parse_csv(payload: bytes | str) -> _ParsedCSV:
    text, decoding_error = _decode_payload(payload)
    if decoding_error:
        return _ParsedCSV(errors=[decoding_error])
    if not text.strip():
        return _ParsedCSV(errors=["CSV input is empty."])

    reader = csv.reader(StringIO(text, newline=""), strict=True)
    try:
        raw_headers = next(reader)
    except StopIteration:
        return _ParsedCSV(errors=["CSV input is empty."])
    except csv.Error as exc:
        return _ParsedCSV(errors=[f"CSV header could not be parsed: {exc}"])

    headers = [header.strip() for header in raw_headers]
    parsed = _ParsedCSV(headers=headers)
    if not headers or all(not header for header in headers):
        parsed.errors.append("CSV header is empty.")
    empty_positions = [
        str(index + 1) for index, header in enumerate(headers) if not header
    ]
    if empty_positions:
        parsed.errors.append(
            "CSV header contains unnamed columns at positions "
            + ", ".join(empty_positions)
            + "."
        )
    duplicates = sorted(
        header
        for header, count in Counter(headers).items()
        if header and count > 1
    )
    if duplicates:
        parsed.errors.append(
            "CSV header contains duplicate columns: "
            + ", ".join(duplicates)
            + "."
        )

    try:
        for values in reader:
            if not values:
                continue
            row_number = reader.line_num
            mapping = {
                header: values[index] if index < len(values) else None
                for index, header in enumerate(headers)
                if header
            }
            if len(values) > len(headers):
                for index, value in enumerate(
                    values[len(headers) :],
                    start=1,
                ):
                    mapping[f"__extra_column_{index}"] = value
            row_errors = []
            if len(values) != len(headers):
                row_errors.append(
                    f"row has {len(values)} columns; expected {len(headers)}."
                )
            parsed.rows.append(
                _RawCSVRow(
                    row_number=row_number,
                    values=values,
                    mapping=mapping,
                    errors=row_errors,
                )
            )
    except csv.Error as exc:
        parsed.errors.append(
            f"CSV parsing failed near line {reader.line_num}: {exc}"
        )
    return parsed


def _missing_columns(
    headers: list[str],
    required: tuple[str, ...],
) -> list[str]:
    return [column for column in required if column not in headers]


def _parse_number(
    raw_value: str | None,
    *,
    column: str,
) -> tuple[float | None, str | None]:
    if raw_value is None or not raw_value.strip():
        return None, f"{column} is required."
    normalized = raw_value.strip()
    if not _NUMBER_PATTERN.fullmatch(normalized):
        return None, f"{column} must be a finite number; got {raw_value!r}."
    value = float(normalized)
    if not math.isfinite(value):
        return None, f"{column} must be a finite number; got {raw_value!r}."
    return value, None


def _parse_required_text(
    raw_value: str | None,
    *,
    column: str,
) -> tuple[str | None, str | None]:
    if raw_value is None or not raw_value.strip():
        return None, f"{column} is required."
    return raw_value.strip(), None


def _classify_fici(fici: float) -> FICIClassification:
    if fici <= 0.5:
        return "synergy"
    if fici <= 1:
        return "additive"
    if fici <= 4:
        return "indifferent"
    return "antagonism"


def _collect_result_errors(
    file_errors: list[str],
    rows: list[CSVRowAudit],
) -> list[str]:
    errors = list(file_errors)
    for row in rows:
        errors.extend(
            f"row {row.row_number}: {message}" for message in row.errors
        )
    return errors


def analyze_fici_csv(
    payload: bytes | str,
    *,
    source_name: str | None = None,
) -> FICIAnalysisResult:
    """Validate and calculate row-level FIC/FICI values from a CSV payload."""

    parsed = _parse_csv(payload)
    file_errors = list(parsed.errors)
    missing = _missing_columns(parsed.headers, FICI_REQUIRED_COLUMNS)
    if missing:
        file_errors.append(
            "missing required columns: " + ", ".join(missing) + "."
        )
    if not parsed.rows:
        file_errors.append("CSV contains no data rows.")
    can_calculate = not file_errors

    results: list[FICIRowResult] = []
    for raw_row in parsed.rows:
        row_errors = list(raw_row.errors)
        numeric_values: dict[str, float | None] = {
            column: None for column in FICI_NUMERIC_COLUMNS
        }
        text_values: dict[str, str | None] = {
            column: None
            for column in ("drug_a", "drug_b", "population_or_strain")
        }
        if can_calculate:
            for column in text_values:
                value, error = _parse_required_text(
                    raw_row.mapping.get(column),
                    column=column,
                )
                text_values[column] = value
                if error:
                    row_errors.append(error)
            for column in FICI_NUMERIC_COLUMNS:
                value, error = _parse_number(
                    raw_row.mapping.get(column),
                    column=column,
                )
                numeric_values[column] = value
                if error:
                    row_errors.append(error)
                elif value is not None and value <= 0:
                    row_errors.append(
                        f"{column} must be greater than 0; got {value}."
                    )

        fic_a: float | None = None
        fic_b: float | None = None
        fici: float | None = None
        classification: FICIClassification | None = None
        if not row_errors and can_calculate:
            drug_a_alone = numeric_values["drug_a_mic_alone"]
            drug_a_combo = numeric_values["drug_a_mic_combo"]
            drug_b_alone = numeric_values["drug_b_mic_alone"]
            drug_b_combo = numeric_values["drug_b_mic_combo"]
            if (
                drug_a_alone is not None
                and drug_a_combo is not None
                and drug_b_alone is not None
                and drug_b_combo is not None
            ):
                try:
                    calculated_fic_a = drug_a_combo / drug_a_alone
                    calculated_fic_b = drug_b_combo / drug_b_alone
                    calculated_fici = calculated_fic_a + calculated_fic_b
                except OverflowError:
                    row_errors.append("FICI calculation overflowed.")
                else:
                    if not all(
                        math.isfinite(value)
                        for value in (
                            calculated_fic_a,
                            calculated_fic_b,
                            calculated_fici,
                        )
                    ):
                        row_errors.append(
                            "FICI calculation produced a non-finite result."
                        )
                    else:
                        fic_a = calculated_fic_a
                        fic_b = calculated_fic_b
                        fici = calculated_fici
                        classification = _classify_fici(fici)

        valid = not row_errors and can_calculate
        results.append(
            FICIRowResult(
                row_number=raw_row.row_number,
                raw_values=raw_row.values,
                raw_row=raw_row.mapping,
                valid=valid,
                errors=row_errors,
                **text_values,
                **numeric_values,
                fic_a=fic_a,
                fic_b=fic_b,
                fici=fici,
                classification=classification,
            )
        )

    errors = _collect_result_errors(file_errors, results)
    valid_count = sum(row.valid for row in results)
    return FICIAnalysisResult(
        headers=parsed.headers,
        rows=results,
        valid=not errors,
        valid_row_count=valid_count,
        invalid_row_count=len(results) - valid_count,
        errors=errors,
        source_name=source_name,
        input_sha256=_payload_sha256(payload),
    )


def analyze_growth_curve_csv(
    payload: bytes | str,
    *,
    source_name: str | None = None,
) -> GrowthCurveAnalysisResult:
    """Validate observations, aggregate replicates, and integrate mean curves."""

    parsed = _parse_csv(payload)
    file_errors = list(parsed.errors)
    missing = _missing_columns(parsed.headers, GROWTH_CURVE_REQUIRED_COLUMNS)
    if missing:
        file_errors.append(
            "missing required columns: " + ", ".join(missing) + "."
        )
    if not parsed.rows:
        file_errors.append("CSV contains no data rows.")
    can_calculate = not file_errors

    observations: list[GrowthCurveObservation] = []
    for raw_row in parsed.rows:
        row_errors = list(raw_row.errors)
        population_or_strain: str | None = None
        intervention: str | None = None
        comparator: str | None = None
        time: float | None = None
        value: float | None = None
        group: str | None = None
        if can_calculate:
            population_or_strain, population_error = _parse_required_text(
                raw_row.mapping.get("population_or_strain"),
                column="population_or_strain",
            )
            intervention, intervention_error = _parse_required_text(
                raw_row.mapping.get("intervention"),
                column="intervention",
            )
            comparator, comparator_error = _parse_required_text(
                raw_row.mapping.get("comparator"),
                column="comparator",
            )
            time, time_error = _parse_number(
                raw_row.mapping.get("time"),
                column="time",
            )
            value, value_error = _parse_number(
                raw_row.mapping.get("value"),
                column="value",
            )
            if time_error:
                row_errors.append(time_error)
            if value_error:
                row_errors.append(value_error)
            for error in (
                population_error,
                intervention_error,
                comparator_error,
            ):
                if error:
                    row_errors.append(error)

            raw_group = raw_row.mapping.get("group")
            if raw_group is None or not raw_group.strip():
                row_errors.append("group is required.")
            else:
                group = raw_group.strip()

        valid = not row_errors and can_calculate
        observations.append(
            GrowthCurveObservation(
                row_number=raw_row.row_number,
                raw_values=raw_row.values,
                raw_row=raw_row.mapping,
                valid=valid,
                errors=row_errors,
                population_or_strain=population_or_strain,
                intervention=intervention,
                comparator=comparator,
                time=time,
                group=group,
                value=value,
            )
        )

    grouped: defaultdict[
        str,
        defaultdict[float, list[GrowthCurveObservation]],
    ] = defaultdict(lambda: defaultdict(list))
    for observation in observations:
        if (
            observation.valid
            and observation.group is not None
            and observation.time is not None
            and observation.value is not None
        ):
            grouped[observation.group][observation.time].append(observation)

    timepoints: list[GrowthTimepointSummary] = []
    calculation_errors: list[str] = []
    for group in sorted(grouped):
        for time in sorted(grouped[group]):
            source_rows = grouped[group][time]
            values = [
                observation.value
                for observation in source_rows
                if observation.value is not None
            ]
            try:
                mean = fmean(values)
                sd = stdev(values) if len(values) > 1 else None
            except (OverflowError, ValueError) as exc:
                calculation_errors.append(
                    f"group {group!r} at time {time:g} could not be "
                    f"summarized: {exc}"
                )
                continue
            if not math.isfinite(mean) or (
                sd is not None and not math.isfinite(sd)
            ):
                calculation_errors.append(
                    f"group {group!r} at time {time:g} produced a "
                    "non-finite summary."
                )
                continue
            timepoints.append(
                GrowthTimepointSummary(
                    group=group,
                    time=time,
                    mean=mean,
                    sd=sd,
                    n=len(values),
                    source_row_numbers=[
                        observation.row_number for observation in source_rows
                    ],
                )
            )

    auc_by_group: list[GrowthCurveAUC] = []
    summaries_by_group: defaultdict[str, list[GrowthTimepointSummary]] = (
        defaultdict(list)
    )
    for summary in timepoints:
        summaries_by_group[summary.group].append(summary)
    for group in sorted(summaries_by_group):
        summaries = summaries_by_group[group]
        if len(summaries) < 2:
            calculation_errors.append(
                f"group {group!r} requires at least 2 distinct time points."
            )
            continue
        try:
            auc = sum(
                (right.time - left.time) * (left.mean + right.mean) / 2
                for left, right in zip(summaries, summaries[1:])
            )
        except OverflowError:
            calculation_errors.append(
                f"group {group!r} AUC calculation overflowed."
            )
            continue
        if not math.isfinite(auc):
            calculation_errors.append(
                f"group {group!r} AUC calculation produced a non-finite result."
            )
            continue
        auc_by_group.append(
            GrowthCurveAUC(
                group=group,
                auc=auc,
                n_timepoints=len(summaries),
                start_time=summaries[0].time,
                end_time=summaries[-1].time,
            )
        )

    errors = _collect_result_errors(
        [*file_errors, *calculation_errors],
        observations,
    )
    valid_count = sum(row.valid for row in observations)
    return GrowthCurveAnalysisResult(
        headers=parsed.headers,
        rows=observations,
        timepoints=timepoints,
        auc_by_group=auc_by_group,
        valid=not errors,
        valid_row_count=valid_count,
        invalid_row_count=len(observations) - valid_count,
        errors=errors,
        source_name=source_name,
        input_sha256=_payload_sha256(payload),
    )


def analyze_experiment_csv(
    payload: bytes | str,
    *,
    analysis_type: ExperimentAnalysisType,
    source_name: str | None = None,
) -> ExperimentAnalysisResult:
    """Dispatch to one of the two explicitly supported experiment analyses."""

    if analysis_type == "fici":
        return analyze_fici_csv(payload, source_name=source_name)
    if analysis_type == "growth_curve":
        return analyze_growth_curve_csv(
            payload,
            source_name=source_name,
        )
    raise ValueError(f"unsupported experiment analysis type: {analysis_type}")
