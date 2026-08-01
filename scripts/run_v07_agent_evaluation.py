from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from vetevidence.v07_agent_comparison import (
    V07AgentComparisonReport,
    run_v07_agent_comparison,
)
from vetevidence.v07_agent_evaluation import build_v07_agent_fixtures
from vetevidence.v07_agent_fake import V07ContractSmokeProvider
from vetevidence.v07_evaluation import (
    V07BaselineReport,
    V07EvaluationCase,
    load_v07_evaluation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V07_ROOT = PROJECT_ROOT / "data" / "eval" / "v0.7"
DEFAULT_CASES = V07_ROOT / "cases.json"
DEFAULT_EXPECTED = V07_ROOT / "expected.json"
DEFAULT_RULES_BASELINE = V07_ROOT / "baselines" / "rules_v1.json"
DEFAULT_FAKE_BASELINE = V07_ROOT / "baselines" / "agent_fake_v1.json"
DEFAULT_RESULTS_DIR = V07_ROOT / "results"

DEEPSEEK_MODELS = ("deepseek-v4-pro", "deepseek-v4-flash")
TYPICAL_MODEL_CALLS_PER_CASE = 3
HARD_MAX_MODEL_CALLS_PER_CASE = 7
SYNTHETIC_BOUNDARY = (
    "All selected cases are synthetic engineering fixtures. A real API call "
    "validates provider and Agent execution only; it does not establish "
    "clinical or scientific truth."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the v0.7 synthetic single-Agent/dual-Agent comparison. "
            "The default contract-smoke run is offline, key-free, and cost-free."
        )
    )
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--model", choices=DEEPSEEK_MODELS, default="deepseek-v4-pro")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--case-id",
        action="append",
        default=[],
        metavar="ID",
        help="Select one case; repeat the option to select more cases.",
    )
    selection.add_argument(
        "--all-cases",
        action="store_true",
        help="Explicitly select all 27 cases (required for a full DeepSeek run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the paid-run envelope without reading a key or constructing a provider.",
    )
    parser.add_argument(
        "--confirm-paid-run",
        action="store_true",
        help="Explicit acknowledgement required before a real DeepSeek run.",
    )
    parser.add_argument(
        "--max-cost-cny",
        metavar="CNY",
        help="Shared hard CNY ceiling for every DeepSeek call in this command.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument(
        "--rules-baseline",
        type=Path,
        default=DEFAULT_RULES_BASELINE,
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help=(
            "Output path. The default full fake run writes agent_fake_v1.json; "
            "real and subset runs get a unique file under results/."
        ),
    )
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="Compare a full fake run with the saved output without overwriting it.",
    )
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _parse_cost(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("--max-cost-cny must be a decimal number") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("--max-cost-cny must be greater than zero")
    return amount


def _selected_cases(
    cases: Sequence[V07EvaluationCase],
    requested_ids: Sequence[str],
    *,
    all_cases: bool,
) -> tuple[V07EvaluationCase, ...]:
    if all_cases or not requested_ids:
        return tuple(cases)
    if len(requested_ids) != len(set(requested_ids)):
        raise ValueError("--case-id values must be unique")
    by_id = {case.id: case for case in cases}
    unknown = [identifier for identifier in requested_ids if identifier not in by_id]
    if unknown:
        raise ValueError("unknown --case-id: " + ", ".join(unknown))
    requested = set(requested_ids)
    return tuple(case for case in cases if case.id in requested)


def _unique_result_path(provider: str, model: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_model = model.replace("/", "-")
    return DEFAULT_RESULTS_DIR / f"agent_{provider}_{safe_model}_{timestamp}.json"


def _write_report(
    report: V07AgentComparisonReport,
    output: Path,
    *,
    refuse_overwrite: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if refuse_overwrite else "w"
    with output.open(mode, encoding="utf-8", newline="\n") as handle:
        json.dump(
            report.model_dump(mode="json"),
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")


def _check_saved_report(
    fresh: V07AgentComparisonReport,
    output: Path,
) -> int:
    if not output.is_file():
        print(f"baseline missing: {output}", file=sys.stderr)
        return 3
    try:
        saved = V07AgentComparisonReport.model_validate_json(
            output.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(f"baseline invalid: {exc}", file=sys.stderr)
        return 3
    matched = saved.result_sha256 == fresh.result_sha256
    print(
        json.dumps(
            {
                "status": "match" if matched else "mismatch",
                "result_sha256": fresh.result_sha256,
                "saved_result_sha256": saved.result_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if matched else 3


def _dry_run_payload(
    *,
    model: str,
    case_count: int,
    max_cost_cny: Decimal | None,
) -> dict[str, object]:
    return {
        "provider": "deepseek",
        "model": model,
        "case_count": case_count,
        "typical_model_calls_per_case": TYPICAL_MODEL_CALLS_PER_CASE,
        "typical_model_calls_total": case_count * TYPICAL_MODEL_CALLS_PER_CASE,
        "hard_max_model_calls_per_case": HARD_MAX_MODEL_CALLS_PER_CASE,
        "hard_max_model_calls_total": case_count * HARD_MAX_MODEL_CALLS_PER_CASE,
        "max_cost_cny": str(max_cost_cny) if max_cost_cny is not None else None,
        "synthetic_boundary": SYNTHETIC_BOUNDARY,
        "will_read_api_key": False,
        "will_construct_provider": False,
        "will_use_network": False,
    }


def _api_key_configured() -> bool:
    """Check only for presence; never return or print the credential value."""

    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        max_cost_cny = _parse_cost(args.max_cost_cny)
    except ValueError as exc:
        return _error(str(exc))

    if args.dry_run and args.provider != "deepseek":
        return _error("--dry-run is only available with --provider deepseek")
    if args.check_baseline and args.provider != "fake":
        return _error("--check-baseline is only available with --provider fake")
    if args.check_baseline and args.dry_run:
        return _error("--check-baseline cannot be combined with --dry-run")

    # Load only immutable local fixtures before the paid-run gate.  In
    # particular, no provider object is constructed and no environment value
    # is read in this section.
    try:
        loaded = load_v07_evaluation(args.cases, args.expected)
        selected = _selected_cases(
            loaded.dataset.cases,
            args.case_id,
            all_cases=args.all_cases,
        )
    except (OSError, ValueError) as exc:
        return _error(str(exc))

    is_full_selection = len(selected) == len(loaded.dataset.cases)
    if args.provider == "deepseek" and not (args.all_cases or args.case_id):
        return _error(
            "DeepSeek requires explicit --case-id (repeatable) or --all-cases"
        )
    if args.check_baseline and not is_full_selection:
        return _error("--check-baseline requires the complete fake case set")

    if args.dry_run:
        print(
            json.dumps(
                _dry_run_payload(
                    model=args.model,
                    case_count=len(selected),
                    max_cost_cny=max_cost_cny,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.provider == "deepseek":
        # Both confirmations are checked before output handling, provider
        # construction, credential lookup, and any possible network request.
        if not args.confirm_paid_run:
            return _error("DeepSeek requires --confirm-paid-run")
        if max_cost_cny is None:
            return _error("DeepSeek requires a positive --max-cost-cny")

    if args.check_baseline:
        output = args.json_output or DEFAULT_FAKE_BASELINE
    elif args.json_output is not None:
        output = args.json_output
    elif args.provider == "fake" and is_full_selection:
        output = DEFAULT_FAKE_BASELINE
    else:
        output = _unique_result_path(args.provider, args.model)
    # Only the versioned default fake baseline is intentionally refreshable.
    # A caller-supplied path may name source data or another valuable file, so
    # it must never be silently overwritten even for a full fake run.
    refuse_overwrite = (
        args.provider == "deepseek"
        or args.json_output is not None
        or not is_full_selection
    )
    if not args.check_baseline and refuse_overwrite and output.exists():
        return _error(f"refusing to overwrite existing result: {output}")

    if args.provider == "deepseek":
        # The credential is only checked after every non-paid gate has passed.
        if not _api_key_configured():
            return _error("DEEPSEEK_API_KEY is not configured")

    try:
        baseline = V07BaselineReport.model_validate_json(
            args.rules_baseline.read_text(encoding="utf-8")
        )
        fixtures = build_v07_agent_fixtures(selected)
    except (OSError, ValueError) as exc:
        return _error(str(exc))

    expected = {case.id: loaded.expected[case.id] for case in selected}
    if args.provider == "fake":
        fixtures_by_id = {fixture.case_id: fixture for fixture in fixtures}

        def provider_factory(case: V07EvaluationCase, _role: str):
            return V07ContractSmokeProvider(fixtures_by_id[case.id])

        research_provider = provider_factory
        reviewer_provider = provider_factory
        execution_mode = "fake"
    else:
        # Lazy import preserves the dry-run and pre-confirmation guarantee even
        # if the real provider module later gains import-time dependencies.
        from vetevidence.deepseek_provider import DeepSeekProvider, DeepSeekRunBudget

        assert max_cost_cny is not None
        shared_budget = DeepSeekRunBudget(max_cost_cny)
        provider = DeepSeekProvider(
            model_name=args.model,
            budget=shared_budget,
            max_retries=0,
        )
        research_provider = provider
        reviewer_provider = provider
        execution_mode = "real"

    try:
        report = run_v07_agent_comparison(
            selected,
            expected,
            fixtures,
            rules_baseline=baseline,
            research_provider=research_provider,
            reviewer_provider=reviewer_provider,
            execution_mode=execution_mode,
            gold_review_status=loaded.review_status,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return _error(f"evaluation failed: {exc}")

    if args.provider == "fake":
        usage = report.actual_spend.total_actual
        if (
            usage.real_model_calls != 0
            or usage.logical_network_calls != 0
            or usage.actual_http_attempts != 0
            or any(amount != 0 for amount in usage.costs_by_currency.values())
        ):
            return _error("fake provider violated the zero-key/zero-network/zero-cost contract")

    if args.check_baseline:
        return _check_saved_report(report, output)

    try:
        _write_report(report, output, refuse_overwrite=refuse_overwrite)
    except FileExistsError:
        return _error(f"refusing to overwrite existing result: {output}")
    except OSError as exc:
        return _error(f"could not write result: {exc}")

    usage = report.actual_spend.total_actual
    print(
        json.dumps(
            {
                "status": "written",
                "provider": args.provider,
                "model": (
                    V07ContractSmokeProvider.model_name
                    if args.provider == "fake"
                    else args.model
                ),
                "case_count": len(selected),
                "result_sha256": report.result_sha256,
                "output": str(output),
                "real_model_calls": usage.real_model_calls,
                "network_calls": usage.actual_http_attempts,
                "costs_by_currency": {
                    currency: str(amount)
                    for currency, amount in usage.costs_by_currency.items()
                },
                "synthetic_boundary": SYNTHETIC_BOUNDARY,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
