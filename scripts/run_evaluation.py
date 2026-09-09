#!/usr/bin/env python3
"""
CLI Evaluation Runner for CreditLock Benchmark (V1 and V2).

Executes:
- V1: Legacy pipeline benchmark
- V2: Independent Component Evaluation Suite (Extractor, Resolver, Steward)

Outputs:
- Machine-readable JSON: <output_dir>/benchmark_report.json
- Human-readable Markdown: <output_dir>/benchmark_report.md
- Terminal summary
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from creditlock.eval.agent_runner import run_agent_benchmark
from creditlock.eval.deterministic_runner import run_deterministic_benchmark
from creditlock.eval.models import BenchmarkReport
from creditlock.eval.v2_runner import run_component_eval_v2


def main() -> None:
    parser = argparse.ArgumentParser(description="CreditLock Comprehensive Benchmark Runner")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports",
        help="Directory to write benchmark JSON and Markdown reports (default: reports)",
    )
    import os
    env_live = os.getenv("CREDITLOCK_RUN_LIVE_GEMINI", "0") in ("1", "true", "True")
    parser.add_argument(
        "--live-gemini",
        action="store_true",
        default=env_live,
        help="Run live Gemini API calls for AI extraction and precedence benchmark",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help="Run versioned V2 independent component benchmark suite",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Do not exit non-zero on quality gate failure (exploratory mode)",
    )
    args = parser.parse_args()

    reports_dir = Path(args.output_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    if args.v2 or "v2" in args.output_dir.lower():
        print("Running CreditLock Versioned V2 Independent Component Benchmark Suite...")
        if not args.live_gemini:
            from creditlock.agents.provider import FakeModelProvider
            ext_provider = FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}})
            res_provider = FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "mock"}})
            stw_provider = FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "mock", "proposed_operation": "SUBSTITUTE_TEXT", "proposed_value": "Hans Zimmer", "derivable": True}})
        else:
            ext_provider = None
            res_provider = None
            stw_provider = None

        v2_res = run_component_eval_v2(
            fixtures_dir=Path("./fixtures/agent_eval_v2"),
            extractor_provider=ext_provider,
            resolver_provider=res_provider,
            steward_provider=stw_provider,
        )

        json_path = reports_dir / "benchmark_report.json"
        json_bytes = v2_res.model_dump_json(indent=2)
        json_path.write_text(json_bytes)

        print("\n" + "=" * 60)
        print("MACHINE-READABLE V2 BENCHMARK REPORT SUMMARY (JSON):")
        print("=" * 60)
        print(json_bytes)
        print("=" * 60)
        print(f"\nReport written to:\n - {json_path}\n")

        if (
            v2_res.quality_gate_status != "PASSED_QUALITY_GATE"
            or v2_res.completeness_status != "COMPLETE_ALL_CASES"
        ) and not args.report_only:
            sys.exit(1)

        sys.exit(0)

    print("Running CreditLock Comprehensive Benchmark (V1)...")

    # 1. Deterministic Compliance Benchmark
    det_res = run_deterministic_benchmark(base_dir="./fixtures/benchmark")

    # 2. AI Extraction & Precedence Benchmark
    agent_res = run_agent_benchmark(base_dir="./fixtures/agent_eval", live_gemini=args.live_gemini)

    # 3. Negative Controls Verification
    print("Verifying Negative Controls...")
    nc_passed = True

    # Negative Control 1: Forced false clear must be detected and fail
    det_nc = run_deterministic_benchmark(base_dir="./fixtures/benchmark", force_false_clear=True)
    if det_nc.false_clear_count == 0:
        print("FAIL: Negative Control 1 failed (forced false clear was not detected).")
        nc_passed = False

    # Negative Control 2: Fabricated extractions must reduce precision
    agent_nc_prec = run_agent_benchmark(
        base_dir="./fixtures/agent_eval", live_gemini=args.live_gemini, fabricate_extractions=True
    )
    if agent_nc_prec.obligation_field_precision is not None and agent_nc_prec.obligation_field_precision >= 1.0:
        print("FAIL: Negative Control 2 failed (fabricated extractions did not reduce precision).")
        nc_passed = False

    # Negative Control 3: Date-only supersession failure must reduce abstention accuracy
    agent_nc_abs = run_agent_benchmark(
        base_dir="./fixtures/agent_eval", live_gemini=args.live_gemini, fail_date_abstention=True
    )
    if agent_nc_abs.abstention_accuracy is not None and agent_nc_abs.abstention_accuracy >= 1.0:
        print(
            "FAIL: Negative Control 3 failed (date-only supersession failure did not reduce abstention accuracy)."
        )
        nc_passed = False

    if nc_passed:
        print("PASS: All negative controls verified successfully.")

    gate_acc_str = (
        f"{det_res.gate_accuracy * 100:.1f}%" if det_res.gate_accuracy is not None else "N/A"
    )
    fc_count_str = (
        str(det_res.false_clear_count) if det_res.false_clear_count is not None else "N/A"
    )

    headline = (
        f"AI reads and recommends; deterministic code decides; authorized humans confirm. "
        f"Gate accuracy: {gate_acc_str}, False clears: {fc_count_str}."
    )

    report = BenchmarkReport(
        timestamp=datetime.now(UTC).isoformat(),
        deterministic_eval=det_res,
        agent_eval=agent_res,
        passed_negative_controls=nc_passed,
        summary_headline=headline,
    )

    json_path = reports_dir / "benchmark_report.json"
    md_path = reports_dir / "benchmark_report.md"

    json_bytes = report.model_dump_json(indent=2)
    json_path.write_text(json_bytes)

    md_content = report.to_markdown()
    md_path.write_text(md_content)

    print("\n" + "=" * 60)
    print("MACHINE-READABLE BENCHMARK REPORT SUMMARY (JSON):")
    print("=" * 60)
    print(json_bytes)
    print("=" * 60)
    print(f"\nReports written to:\n - {json_path}\n - {md_path}\n")

    if not nc_passed:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
