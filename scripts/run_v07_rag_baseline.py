from __future__ import annotations

import argparse
import json
from pathlib import Path

from vetevidence.v07_rag_evaluation import (
    V07RAGBaselineReport,
    load_v07_rag_evaluation,
    run_v07_local_hash_rag_baseline,
    v07_rag_deterministic_result_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "data" / "eval" / "v0.7" / "cases.json"
DEFAULT_MANIFEST = (
    PROJECT_ROOT / "data" / "eval" / "v0.7" / "rag_retrieval.json"
)
DEFAULT_BASELINE = (
    PROJECT_ROOT
    / "data"
    / "eval"
    / "v0.7"
    / "baselines"
    / "local_hash_rag_v1.json"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fully offline v0.7 local hash RAG Recall@3 baseline. "
            "No network, API key, LLM, or paid embedding is used."
        )
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="Re-run without overwriting and compare the deterministic snapshot.",
    )
    return parser.parse_args()


def _write_report(report: V07RAGBaselineReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _check_report(
    fresh: V07RAGBaselineReport,
    baseline_path: Path,
) -> bool:
    if not baseline_path.is_file():
        print(f"baseline missing: {baseline_path}")
        return False
    saved = V07RAGBaselineReport.model_validate_json(
        baseline_path.read_text(encoding="utf-8")
    )
    saved_recomputed_sha = v07_rag_deterministic_result_sha256(saved)
    fresh_recomputed_sha = v07_rag_deterministic_result_sha256(fresh)
    checks = {
        "saved_snapshot_integrity": (
            saved.deterministic_result_sha256 == saved_recomputed_sha
        ),
        "cases_sha256": saved.cases_sha256 == fresh.cases_sha256,
        "retrieval_manifest_sha256": (
            saved.retrieval_manifest_sha256
            == fresh.retrieval_manifest_sha256
        ),
        "implementation_sha256": (
            saved.system.implementation_sha256
            == fresh.system.implementation_sha256
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
    loaded = load_v07_rag_evaluation(args.cases, args.manifest)
    report = run_v07_local_hash_rag_baseline(
        loaded,
        project_root=PROJECT_ROOT,
    )
    print(
        "v0.7 local_hash_rag_v1 Recall@3; "
        f"evaluation_errors={report.summary.evaluation_errors}"
    )
    for mode, mode_summary in report.summary.retrieval_modes.items():
        recall = mode_summary.retrieval_recall_at_3
        print(f"{mode}: {recall.numerator}/{recall.denominator}")
        for slice_name, result in mode_summary.slices.items():
            print(
                f"  {slice_name}: {result.numerator}/{result.denominator}"
            )
    keyword_hits = report.summary.retrieval_modes[
        "keyword_only"
    ].retrieval_recall_at_3.numerator
    hybrid_hits = report.summary.retrieval_modes[
        "hybrid"
    ].retrieval_recall_at_3.numerator
    print(
        "hybrid_gain_over_keyword_hits="
        f"{hybrid_hits - keyword_hits:+d}; "
        "do_not_claim_hash_vector_gain"
    )
    print(f"cases_sha256={report.cases_sha256}")
    print(
        "deterministic_result_sha256="
        f"{report.deterministic_result_sha256}"
    )
    print(
        "network_calls=0 real_model_calls=0 input_tokens=0 output_tokens=0 "
        "llm_api_cost_cny=0 llm_api_cost_usd=0"
    )

    if args.check_baseline:
        return 0 if _check_report(report, args.json_output) else 2
    _write_report(report, args.json_output)
    print(f"json: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
