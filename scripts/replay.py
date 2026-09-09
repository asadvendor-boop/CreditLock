#!/usr/bin/env python3
"""
CLI entry point for offline delivery package replay verification.

Usage:
    python scripts/replay.py <path_to_zip> --expected-release-digest <digest>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from creditlock.evidence.replay import replay_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay and verify a CreditLock delivery package offline."
    )
    parser.add_argument(
        "bundle_path",
        type=str,
        help="Path to the delivery package (.zip archive or directory).",
    )
    parser.add_argument(
        "--expected-release-digest",
        type=str,
        required=True,
        help="Mandatory trusted external SHA-256 digest of release_evidence.json.",
    )

    args = parser.parse_args()

    bundle_path = Path(args.bundle_path)
    if not bundle_path.exists():
        print(f"ERROR: Bundle path '{bundle_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    result = replay_bundle(bundle_path, expected_release_digest=args.expected_release_digest)

    print("\n" + "=" * 50)
    print(f"REPLAY RESULT: {result.status}")
    print("=" * 50)
    print(f"Expected Gate State : {result.expected_gate_state}")
    print(f"Replayed Gate State : {result.replayed_gate_state}")
    print(f"Expected Issues     : {result.expected_issues_count}")
    print(f"Replayed Issues     : {result.replayed_issues_count}")

    if result.divergence_reasons:
        print("\nDIVERGENCE REASONS:")
        for reason in result.divergence_reasons:
            print(f"  - {reason}")

    print("=" * 50 + "\n")

    if result.status == "MATCH":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
