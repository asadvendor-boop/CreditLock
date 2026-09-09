#!/usr/bin/env python3
"""
Single Frozen Generalization Challenge Live Evaluation Runner (Commit H).
Runs live Gemini models ONCE against frozen challenge fixtures and saves docs/evidence/live-v2-frozen-challenge.json.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("STORE_BACKEND", "memory_demo")
os.environ.setdefault("ALLOW_IN_MEMORY_DEMO", "true")
os.environ.setdefault("KAFKA_TRANSPORT", "memory")
os.environ.setdefault("ALLOW_IN_MEMORY_KAFKA", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-must-be-at-least-32-bytes-long-for-security")

from creditlock.eval.v2_runner import run_component_eval_v2


def main() -> None:
    if os.getenv("CREDITLOCK_RUN_LIVE_GEMINI", "0") not in ("1", "true", "True"):
        print("ERROR: CREDITLOCK_RUN_LIVE_GEMINI=1 is required to run the live frozen challenge.")
        sys.exit(1)

    print("Running Single Frozen Generalization Challenge with Live Gemini Models...")
    result = run_component_eval_v2(fixtures_dir=Path("./fixtures/agent_eval_v2"))

    output_path = Path("docs/evidence/live-v2-frozen-challenge.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    json_data = result.model_dump_json(indent=2)
    output_path.write_text(json_data)

    print(f"\nSaved report to {output_path}")
    print(f"Quality Gate Status: {result.quality_gate_status}")
    print(f"Completeness Status: {result.completeness_status}")

    print("\n--- DEV METRICS ---")
    print(result.dev_metrics)

    print("\n--- RECORDED_REGRESSION METRICS ---")
    print(result.recorded_regression_metrics)

    print("\n--- FROZEN_CHALLENGE METRICS ---")
    print(result.frozen_challenge_metrics)

    if result.quality_gate_status != "PASSED_QUALITY_GATE":
        print("\nQUALITY GATE FAILED ON FROZEN CHALLENGE RUN.")
        sys.exit(1)

    print("\nQUALITY GATE PASSED ON FROZEN CHALLENGE RUN!")
    sys.exit(0)


if __name__ == "__main__":
    main()
