from __future__ import annotations

import argparse
import json
from pathlib import Path

from vetevidence.v07_evaluation import (
    V07BaselineReport,
    load_v07_evaluation,
    run_v07_rule_baseline,
    v07_baseline_to_markdown,
    v07_deterministic_result_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "data" / "eval" / "v0.7" / "cases.json"
DEFAULT_EXPECTED = PROJECT_ROOT / "data" / "eval" / "v0.7" / "expected.json"
DEFAULT_BASELINE = (
    PROJECT_ROOT
    / "data"
    / "eval"
    / "v0.7"
    / "baselines"
    / "rules_v1.json"
)
DEFAULT_MARKDOWN = PROJECT_ROOT / "docs" / "V0.7_EVALUATION.md"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fully offline v0.7 synthetic evaluation with rules_v1. "
            "Case failures are baseline data; evaluator errors are command failures."
        )
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="Re-run without overwriting and compare the deterministic snapshot.",
    )
    return parser.parse_args()


def _write_report(
    report: V07BaselineReport,
    *,
    json_output: Path,
    markdown_output: Path,
) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(
        v07_baseline_to_markdown(report) + "\n",
        encoding="utf-8",
    )


def _check_report(report: V07BaselineReport, baseline_path: Path) -> bool:
    if not baseline_path.is_file():
        print(f"baseline missing: {baseline_path}")
        return False
    saved = V07BaselineReport.model_validate_json(
        baseline_path.read_text(encoding="utf-8")
    )
    saved_recomputed_sha = v07_deterministic_result_sha256(saved)
    fresh_recomputed_sha = v07_deterministic_result_sha256(report)
    checks = {
        "saved_snapshot_integrity": (
            saved.deterministic_result_sha256 == saved_recomputed_sha
        ),
        "dataset_sha256": saved.dataset_sha256 == report.dataset_sha256,
        "implementation_sha256": (
            saved.system.implementation_sha256
            == report.system.implementation_sha256
        ),
        "deterministic_result_sha256": (
            saved_recomputed_sha == fresh_recomputed_sha
        ),
    }
    for name, matched in checks.items():
        print(f"{name}: {'match' if matched else 'MISMATCH'}")
    return all(checks.values())


def main() -> int:
    args = _parse_args()
    loaded = load_v07_evaluation(args.cases, args.expected)
    report = run_v07_rule_baseline(loaded, project_root=PROJECT_ROOT)

    print(
        f"v0.7 rules_v1 baseline: {report.summary.passed}/"
        f"{report.summary.total} match gold; "
        f"evaluation_errors={report.summary.evaluation_errors}"
    )
    print(f"dataset_sha256={report.dataset_sha256}")
    print(f"deterministic_result_sha256={report.deterministic_result_sha256}")
    print("network_calls=0 model_calls=0 llm_api_cost_usd=0")

    if report.summary.evaluation_errors:
        return 1
    if args.check_baseline:
        return 0 if _check_report(report, args.json_output) else 2

    _write_report(
        report,
        json_output=args.json_output,
        markdown_output=args.markdown_output,
    )
    print(f"json: {args.json_output}")
    print(f"markdown: {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
