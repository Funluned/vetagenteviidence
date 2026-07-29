from __future__ import annotations

import argparse
import json
from pathlib import Path

from vetevidence.evaluation import (
    evaluate_research,
    evaluation_report_to_markdown,
    load_evaluation_cases,
)
from vetevidence.retrieval import run_research


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERY = "quercetin Streptococcus agalactiae mastitis"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the VetEvidence targeted evaluation set."
    )
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()

    cases_path = PROJECT_ROOT / "data" / "eval" / "cases.json"
    json_output = PROJECT_ROOT / "data" / "eval" / "latest_results.json"
    markdown_output = PROJECT_ROOT / "docs" / "EVALUATION.md"

    research_result = run_research(args.query, max_results=5)
    cases = load_evaluation_cases(cases_path)
    report = evaluate_research(research_result, cases)

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
        evaluation_report_to_markdown(report) + "\n",
        encoding="utf-8",
    )

    print(
        f"evaluation: {report.summary.passed}/{report.summary.total} passed; "
        f"failures={report.summary.failed}"
    )
    print(f"json: {json_output}")
    print(f"markdown: {markdown_output}")
    return 1 if report.summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
