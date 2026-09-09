#!/usr/bin/env python3
"""
V2 Frozen Challenge Live Runner.

Evaluates ONLY FROZEN_CHALLENGE_V2 fixtures using real Gemini calls.
Saves evidence to docs/evidence/live-v2-frozen-challenge-v2.json.

Exit codes:
  0: quality gate passed
  1: quality gate failed (report still saved)
  2: pre-flight failure (dirty worktree, missing fixtures, etc.)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pydantic import ValidationError

from creditlock.eval.v2_models import FrozenChallengeManifest
from creditlock.eval.v2_runner import (
    compute_frozen_challenge_v2_digest,
    get_agent_prompt_template_hashes,
    run_component_eval_v2,
    validate_fixtures_only,
)
from creditlock.settings import get_settings


def check_clean_worktree(root_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=root_dir,
        check=False,
    )
    return len(result.stdout.strip()) == 0


def check_commit_exists(sha: str, root_dir: Path) -> bool:
    res = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        cwd=root_dir,
        check=False,
    )
    return res.returncode == 0


def check_agents_unchanged(sha: str, root_dir: Path) -> bool:
    res = subprocess.run(
        ["git", "diff", "--exit-code", f"{sha}..HEAD", "--", "src/creditlock/agents"],
        capture_output=True,
        cwd=root_dir,
        check=False,
    )
    return res.returncode == 0


def get_v2_fixture_counts(fixtures_dir: Path) -> dict[str, int]:
    counts = {}
    for agent in ("extractor", "resolver", "steward"):
        count = 0
        for f in sorted((fixtures_dir / agent).glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(f"Malformed fixture file {f.name}: {e}") from e
            if data.get("split") == "FROZEN_CHALLENGE_V2":
                count += 1
        counts[agent] = count
    return counts


def run_preflight(
    manifest_path: Path,
    fixtures_dir: Path,
    root_dir: Path,
    evidence_path: Path,
) -> tuple[bool, str]:
    if not check_clean_worktree(root_dir):
        return False, "Git worktree is dirty. Commit all changes before live run."

    if not manifest_path.exists():
        return False, f"Manifest file missing at {manifest_path}"

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = FrozenChallengeManifest.model_validate(manifest_data)
    except (json.JSONDecodeError, OSError, ValidationError) as e:
        return False, f"Manifest is malformed or invalid: {e}"

    agent_code_sha = manifest.agent_code_commit_sha
    if not check_commit_exists(agent_code_sha, root_dir):
        return False, f"agent_code_commit_sha {agent_code_sha} does not exist."

    if not check_agents_unchanged(agent_code_sha, root_dir):
        return False, f"Code under src/creditlock/agents has changed since {agent_code_sha}."

    errors = validate_fixtures_only(fixtures_dir, target_split="FROZEN_CHALLENGE_V2")
    if errors:
        msg = "Fixture validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        return False, msg

    frozen_hashes = manifest.frozen_prompt_template_hashes
    runtime_hashes = get_agent_prompt_template_hashes()

    if set(frozen_hashes.keys()) != set(runtime_hashes.keys()):
        return False, "Prompt hash key sets mismatch."

    for agent, f_hash in frozen_hashes.items():
        if runtime_hashes.get(agent) != f_hash:
            return False, f"Prompt hash mismatch for {agent}"

    import os

    os.environ.setdefault(
        "JWT_SECRET", "test-secret-must-be-at-least-32-bytes-long-for-security"
    )
    os.environ.setdefault("STORE_BACKEND", "memory_demo")
    os.environ.setdefault("ALLOW_IN_MEMORY_DEMO", "true")

    settings = get_settings()
    configured_primary = manifest.configured_primary_models
    expected_primary = {
        "extractor": settings.gemini_extractor_model,
        "resolver": settings.gemini_resolver_model,
        "steward": settings.gemini_steward_model,
    }
    if configured_primary != expected_primary:
        return False, f"Configured primary models mismatch. Expected {expected_primary}, got {configured_primary}"

    configured_fallback = manifest.configured_fallback_models
    expected_fallback = {
        "extractor": settings.gemini_extractor_fallback_model,
        "resolver": settings.gemini_resolver_fallback_model,
        "steward": settings.gemini_steward_fallback_model,
    }
    if configured_fallback != expected_fallback:
        return False, f"Configured fallback models mismatch. Expected {expected_fallback}, got {configured_fallback}"

    runtime_counts = get_v2_fixture_counts(fixtures_dir)
    manifest_counts = manifest.fixture_counts
    if manifest_counts != runtime_counts:
        return False, f"Fixture count mismatch: expected {manifest_counts}, got {runtime_counts}"

    if manifest.semantic_uniqueness_counts != manifest_counts:
        return False, f"Semantic uniqueness counts mismatch: expected {manifest_counts}, got {manifest.semantic_uniqueness_counts}"

    frozen_digest = manifest.fixture_set_digest
    runtime_digest = compute_frozen_challenge_v2_digest(fixtures_dir)
    if frozen_digest != runtime_digest:
        return False, f"Fixture digest mismatch: runtime={runtime_digest} vs frozen={frozen_digest}"

    return True, manifest.agent_code_commit_sha


def run_live(
    manifest_path: Path,
    fixtures_dir: Path,
    root_dir: Path,
    evidence_path: Path,
    provider_factory: Any = None,
) -> int:
    if evidence_path.exists():
        print("ERROR: V2 evidence file already exists. Refusing to overwrite.", file=sys.stderr)
        return 2

    ok, msg = run_preflight(manifest_path, fixtures_dir, root_dir, evidence_path)
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    agent_code_sha = msg

    if provider_factory is not None:
        provider_factory()

    print("Pre-flight checks passed. Running live evaluation...")
    print("Target: FROZEN_CHALLENGE_V2 only")

    result = run_component_eval_v2(
        fixtures_dir=fixtures_dir,
        target_split="FROZEN_CHALLENGE_V2",
        agent_code_commit_sha=agent_code_sha,
    )

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = result.model_dump_json(indent=2)
    evidence_path.write_text(report_json, encoding="utf-8")
    print(f"Evidence saved: {evidence_path}")

    gate = result.quality_gate_status
    print(f"Quality gate: {gate}")
    print(f"Evidence class: {result.evidence_class}")
    print(f"Execution status: {result.execution_status}")

    if gate == "PASSED_QUALITY_GATE":
        print("RESULT: PASSED")
        return 0
    else:
        print("RESULT: FAILED")
        return 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="V2 Frozen Challenge Live Runner.")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        default=False,
        help="Run preflight checks only and exit 0 if valid.",
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        default=False,
        help="Run live evaluation using Gemini API.",
    )

    args = parser.parse_args(argv)

    evidence_path = (
        PROJECT_ROOT / "docs" / "evidence" / "live-v2-frozen-challenge-v2.json"
    )
    manifest_path = (
        PROJECT_ROOT / "docs" / "evidence" / "frozen-challenge-v2-manifest.json"
    )
    fixtures_dir = PROJECT_ROOT / "fixtures" / "agent_eval_v2"

    if args.execute_live:
        if os.environ.get("CREDITLOCK_RUN_LIVE_GEMINI") != "1":
            print(
                "ERROR: --execute-live requires CREDITLOCK_RUN_LIVE_GEMINI=1 environment variable.",
                file=sys.stderr,
            )
            return 2
        return run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path)
    else:
        ok, msg = run_preflight(
            manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path
        )
        if not ok:
            print(f"ERROR: {msg}", file=sys.stderr)
            return 2
        print(f"Preflight passed cleanly for agent code commit SHA: {msg}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
