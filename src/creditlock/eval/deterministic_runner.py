"""
Deterministic compliance benchmark runner.

Discovers and evaluates all compliance fixtures under fixtures/benchmark/dev/ and fixtures/benchmark/sealed/.
Computes exact gate accuracy, false-clear rate, false-block rate, confusion matrix, and replay match rate.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import CreditManifest, LayoutEvidence, Obligation, VisualObservations
from creditlock.eval.models import DeterministicEvalResult
from creditlock.evidence.delivery import assemble_delivery_package
from creditlock.evidence.replay import replay_bundle


def run_deterministic_benchmark(
    base_dir: str | Path = "./fixtures/benchmark",
    force_false_clear: bool = False,
) -> DeterministicEvalResult:
    """
    Run deterministic compliance benchmark across all dev and sealed fixtures.

    Invariants:
    - Fails if any sealed fixture is skipped or missing.
    - False clear denominator is computed dynamically from fixture expectations (expected_gate != READY_TO_EXPORT).
    - If force_false_clear is True (negative control), forces all cases to evaluate as READY_TO_EXPORT.
    """
    base_path = Path(base_dir)
    dev_dir = base_path / "dev"
    sealed_dir = base_path / "sealed"

    dev_files = list(dev_dir.glob("*.json"))
    sealed_files = list(sealed_dir.glob("*.json"))

    if not sealed_files:
        raise RuntimeError(
            "Evaluation failed: no sealed benchmark fixtures found under fixtures/benchmark/sealed/."
        )

    if len(dev_files) < 8 or len(sealed_files) < 4:
        raise RuntimeError(
            f"Evaluation failed: incomplete benchmark fixture set (found {len(dev_files)} dev, {len(sealed_files)} sealed; expected 8 dev, 4 sealed)."
        )

    all_files = [(f, "dev") for f in dev_files] + [(f, "sealed") for f in sealed_files]

    total_cases = len(all_files)
    dev_count = len(dev_files)
    sealed_count = len(sealed_files)

    gate_matches = 0
    issue_set_matches = 0
    false_clear_count = 0
    false_block_count = 0
    replay_matches = 0
    replay_total = 0

    confusion_matrix: dict[str, int] = {}
    case_results: list[dict[str, Any]] = []

    total_negative_cases = 0
    total_positive_cases = 0

    for file_path, source in all_files:
        content = json.loads(file_path.read_bytes())
        case_id = content.get("case_id", file_path.stem)
        expected_gate = content.get("expected_gate", "UNKNOWN")
        expected_issues = set(content.get("expected_issues", []))

        if expected_gate != "READY_TO_EXPORT":
            total_negative_cases += 1
        else:
            total_positive_cases += 1

        manifest = CreditManifest.model_validate(content["manifest"])
        obligations = [Obligation.model_validate(o) for o in content.get("obligations", [])]
        layout = (
            LayoutEvidence.model_validate(content["layout_evidence"])
            if content.get("layout_evidence")
            else None
        )
        visuals = (
            VisualObservations.model_validate(content["visual_observations"])
            if content.get("visual_observations")
            else None
        )
        registry = content.get("contributor_registry", [])

        if force_false_clear:
            actual_gate = "READY_TO_EXPORT"
            gate_match = actual_gate == expected_gate
            if expected_gate != "READY_TO_EXPORT" and actual_gate == "READY_TO_EXPORT":
                false_clear_count += 1

            case_results.append(
                {
                    "case_id": case_id,
                    "source": source,
                    "expected_gate": expected_gate,
                    "actual_gate": actual_gate,
                    "gate_match": gate_match,
                    "issue_set_match": False,
                    "status": "NEGATIVE_CONTROL_FORCED_FALSE_CLEAR",
                    "reason": "Forced false clear for negative control",
                }
            )
            continue

        if (
            layout is None
            or visuals is None
            or "frames" not in content
            or "artifact_index" not in content
            or "render_profile_version" not in content
        ):
            # We catch validation failures from the pipeline via validation directly
            # or just mark it UNVERIFIED_INCOMPLETE_STORED_EVIDENCE right here
            # instead of synthesizing.
            missing = []
            if layout is None:
                missing.append("layout_evidence")
            if visuals is None:
                missing.append("visual_observations")
            if "frames" not in content:
                missing.append("frames")
            if "artifact_index" not in content:
                missing.append("artifact_index")
            if "render_profile_version" not in content:
                missing.append("render_profile_version")

            case_results.append(
                {
                    "case_id": case_id,
                    "source": source,
                    "expected_gate": expected_gate,
                    "actual_gate": "UNVERIFIED_INCOMPLETE_STORED_EVIDENCE",
                    "gate_match": False,
                    "issue_set_match": False,
                    "status": "UNVERIFIED_INCOMPLETE_STORED_EVIDENCE",
                    "reason": f"Missing: {', '.join(missing)}",
                }
            )
            continue

        checker_input = CheckerInput(
            obligations=obligations,
            manifest=manifest,
            layout_evidence=layout,
            visual_observations=visuals,
            contributor_registry=registry,
        )
        findings = evaluate_findings(checker_input)

        if force_false_clear:
            actual_gate_value = "READY_TO_EXPORT"
            actual_issues = set()
        else:
            folded_gate = fold_gate(findings, ProjectionStatus())
            actual_gate_value = folded_gate.value
            actual_issues = {f.code.value for f in findings}

        # Gate Accuracy
        if actual_gate_value == expected_gate:
            gate_matches += 1

        # Issue-set accuracy
        if actual_issues == expected_issues:
            issue_set_matches += 1

        # False clear: actual READY when expected != READY
        if actual_gate_value == "READY_TO_EXPORT" and expected_gate != "READY_TO_EXPORT":
            false_clear_count += 1

        # False block: actual != READY when expected == READY
        if actual_gate_value != "READY_TO_EXPORT" and expected_gate == "READY_TO_EXPORT":
            false_block_count += 1

        # Confusion Matrix
        pair_key = f"{expected_gate}->{actual_gate_value}"
        confusion_matrix[pair_key] = confusion_matrix.get(pair_key, 0) + 1

        # Replay evaluation on valid and tampered bundle
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = {
                "manifest": manifest,
                "obligations": obligations,
                "layout_evidence": layout,
                "visual_observations": visuals,
                "contributor_registry": registry,
                "authorizations": [],
                "proposals": [],
                "frames": content["frames"],
                "artifact_index": content["artifact_index"],
                "render_profile_version": content["render_profile_version"],
            }
            pkg_res = assemble_delivery_package(case_id, prod_data, tmpdir)
            valid_replay = replay_bundle(
                pkg_res.package_path, expected_release_digest=pkg_res.release_evidence_digest
            )

            replay_total += 1
            if valid_replay.status == "MATCH":
                replay_matches += 1

            # Test corrupted package
            tampered_path = Path(tmpdir) / "tampered.zip"
            with (
                zipfile.ZipFile(pkg_res.package_path, "r") as z_in,
                zipfile.ZipFile(tampered_path, "w") as z_out,
            ):
                for item in z_in.infolist():
                    data = z_in.read(item.filename)
                    if item.filename == "manifest.json":
                        data = data.replace(b"production_id", b"tampered_prod")
                    z_out.writestr(item, data)

            corrupt_replay = replay_bundle(
                tampered_path, expected_release_digest=pkg_res.release_evidence_digest
            )
            replay_total += 1
            if corrupt_replay.status == "DIVERGENCE":
                replay_matches += 1

        case_results.append(
            {
                "case_id": case_id,
                "source": source,
                "expected_gate": expected_gate,
                "actual_gate": actual_gate_value,
                "gate_match": actual_gate_value == expected_gate,
                "issue_set_match": actual_issues == expected_issues,
            }
        )

    # Check if we skipped any cases due to incomplete evidence
    skipped_cases = [
        c for c in case_results if c.get("status") == "UNVERIFIED_INCOMPLETE_STORED_EVIDENCE"
    ]
    false_clear_rate: float | None = None
    false_block_rate: float | None = None
    gate_accuracy: float | None = None
    issue_accuracy: float | None = None
    replay_match_rate: float | None = None
    opt_false_clear_count: int | None = None
    opt_false_block_count: int | None = None

    if skipped_cases:
        summary_status = "UNVERIFIED_INCOMPLETE_STORED_EVIDENCE"
    else:
        opt_false_clear_count = false_clear_count
        opt_false_block_count = false_block_count
        false_clear_rate = (
            (false_clear_count / total_negative_cases) if total_negative_cases > 0 else 0.0
        )
        false_block_rate = (
            (false_block_count / total_positive_cases) if total_positive_cases > 0 else 0.0
        )
        gate_accuracy = gate_matches / total_cases
        issue_accuracy = issue_set_matches / total_cases
        replay_match_rate = (replay_matches / replay_total) if replay_total > 0 else 0.0
        summary_status = "VERIFIED"

    return DeterministicEvalResult(
        total_cases=total_cases,
        dev_cases=dev_count,
        sealed_cases=sealed_count,
        gate_accuracy=gate_accuracy,
        exact_issue_code_set_accuracy=issue_accuracy,
        false_clear_count=opt_false_clear_count,
        false_clear_rate=false_clear_rate,
        false_block_count=opt_false_block_count,
        false_block_rate=false_block_rate,
        total_negative_cases=total_negative_cases,
        total_positive_cases=total_positive_cases,
        confusion_matrix=confusion_matrix,
        replay_match_rate=replay_match_rate,
        case_results=case_results,
        summary_status=summary_status,
    )
