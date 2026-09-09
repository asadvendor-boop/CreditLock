"""
Offline replay engine for CreditLock delivery packages.

Verifies bundle integrity and recomputes gate state completely offline without network,
cloud credentials, or AI models.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import (
    Authorization,
    CreditManifest,
    GateState,
    LayoutEvidence,
    Obligation,
    VisualObservations,
)
from creditlock.evidence.models import ArtifactIndex, ReleaseEvidence
from creditlock.renderer.audit import audit_png_bytes


@dataclass(frozen=True)
class ReplayResult:
    """Result of offline delivery package replay."""

    status: Literal["MATCH", "DIVERGENCE"]
    expected_gate_state: str
    replayed_gate_state: str
    expected_issues_count: int
    replayed_issues_count: int
    recomputed_artifact_index_digest: str | None = None
    divergence_reasons: list[str] = field(default_factory=list)


def replay_bundle(
    bundle_path: str | Path,
    expected_release_digest: str,
) -> ReplayResult:
    """
    Replay a delivery package (.zip archive or directory) offline.

    Guarantees:
    - Zero network, zero credentials, zero Gemini calls required.
    - Mandatory non-empty 64-character hexadecimal external release digest validation.
    - Strict hash-binding verification for every contained file and root release evidence.
    - Full deterministic findings re-evaluation and gate fold.
    """
    divergence_reasons: list[str] = []

    # Mandatory expected_release_digest validation
    if (
        not expected_release_digest
        or not isinstance(expected_release_digest, str)
        or len(expected_release_digest) != 64
        or not all(c in "0123456789abcdefABCDEF" for c in expected_release_digest)
    ):
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state="UNKNOWN",
            replayed_gate_state="INVALID_EXPECTED_DIGEST",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=[
                f"Expected release digest must be a valid 64-character hexadecimal SHA-256 string (got '{expected_release_digest}')."
            ],
        )

    path = Path(bundle_path)
    if not path.exists():
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state="UNKNOWN",
            replayed_gate_state="UNKNOWN",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=[f"Bundle path '{bundle_path}' does not exist."],
        )

    file_contents: dict[str, bytes] = {}

    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for name in zf.namelist():
                    if name.startswith("/") or ".." in name or "\\" in name:
                        return ReplayResult(
                            status="DIVERGENCE",
                            expected_gate_state="UNKNOWN",
                            replayed_gate_state="SECURITY_ERROR",
                            expected_issues_count=0,
                            replayed_issues_count=0,
                            divergence_reasons=[
                                f"Security error: path traversal ZIP entry detected: '{name}'."
                            ],
                        )
                    file_contents[name] = zf.read(name)
        except Exception as ex:  # noqa: BLE001
            return ReplayResult(
                status="DIVERGENCE",
                expected_gate_state="UNKNOWN",
                replayed_gate_state="UNKNOWN",
                expected_issues_count=0,
                replayed_issues_count=0,
                divergence_reasons=[f"Corrupt or invalid zip archive: {ex}"],
            )
    elif path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                rel_p = p.relative_to(path).as_posix()
                file_contents[rel_p] = p.read_bytes()
    else:
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state="UNKNOWN",
            replayed_gate_state="UNKNOWN",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=[f"Invalid bundle path type: '{bundle_path}'."],
        )

    if "bundle_index.json" not in file_contents:
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state="UNKNOWN",
            replayed_gate_state="UNKNOWN",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=["bundle_index.json missing from delivery package."],
        )

    try:
        bundle_index = json.loads(file_contents["bundle_index.json"].decode())
    except Exception as ex:  # noqa: BLE001
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state="UNKNOWN",
            replayed_gate_state="UNKNOWN",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=[f"Malformed bundle_index.json: {ex}"],
        )

    expected_gate_state = bundle_index.get("expected_gate_state", "UNKNOWN")
    declared_files: dict[str, str] = bundle_index.get("files", {})

    # 1. Verify file presence and internal hash integrity
    for filename, expected_hash in declared_files.items():
        if filename not in file_contents:
            divergence_reasons.append(f"Bundled file '{filename}' missing from package.")
            continue
        actual_hash = sha256_bytes_digest(file_contents[filename])
        if actual_hash != expected_hash:
            divergence_reasons.append(
                f"File hash divergence in '{filename}': computed '{actual_hash}' != declared '{expected_hash}'."
            )

    # Required JSON files
    required_json_files = [
        "manifest.json",
        "obligations.json",
        "layout_evidence.json",
        "visual_observations.json",
        "contributor_registry.json",
        "authorizations.json",
        "proposals.json",
        "artifact_index.json",
        "deterministic_findings.json",
        "release_evidence.json",
    ]
    for r_file in required_json_files:
        if r_file not in file_contents:
            divergence_reasons.append(f"Required package file '{r_file}' missing from package.")

    if divergence_reasons:
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state=expected_gate_state,
            replayed_gate_state="DIVERGED_HASH",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=divergence_reasons,
        )

    # 2. Verify external expected release digest (anti-tamper resealing check)
    actual_release_digest = sha256_bytes_digest(file_contents["release_evidence.json"])
    if actual_release_digest.lower() != expected_release_digest.lower():
        divergence_reasons.append(
            f"External release digest mismatch: computed '{actual_release_digest}' != expected '{expected_release_digest}'."
        )

    # 3. Parse domain models
    try:
        manifest_dict = json.loads(file_contents["manifest.json"].decode())
        manifest = CreditManifest.model_validate(manifest_dict)

        obligations_list = json.loads(file_contents["obligations.json"].decode())
        obligations = [Obligation.model_validate(o) for o in obligations_list]

        layout_dict = json.loads(file_contents["layout_evidence.json"].decode())
        layout_evidence = LayoutEvidence.model_validate(layout_dict) if layout_dict else None

        visual_dict = json.loads(file_contents["visual_observations.json"].decode())
        visual_observations = (
            VisualObservations.model_validate(visual_dict) if visual_dict else None
        )

        registry_list = json.loads(file_contents["contributor_registry.json"].decode())
        contributor_registry = registry_list

        auth_list = json.loads(file_contents["authorizations.json"].decode())
        authorizations = [Authorization.model_validate(a) for a in auth_list]

        prop_list = json.loads(file_contents["proposals.json"].decode())
        proposals = prop_list

        art_index_dict = json.loads(file_contents["artifact_index.json"].decode())
        artifact_index = ArtifactIndex.model_validate(art_index_dict)

        release_ev_dict = json.loads(file_contents["release_evidence.json"].decode())
        release_evidence = ReleaseEvidence.model_validate(release_ev_dict)
    except Exception as ex:  # noqa: BLE001
        return ReplayResult(
            status="DIVERGENCE",
            expected_gate_state=expected_gate_state,
            replayed_gate_state="PARSE_ERROR",
            expected_issues_count=0,
            replayed_issues_count=0,
            divergence_reasons=[f"Failed to parse bundled domain models: {ex}"],
        )

    # 4. Verify artifact index & PNG frame bytes
    recomputed_manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
    recomputed_orv_hash = sha256_bytes_digest(canonical_json_bytes([o.model_dump() for o in obligations]))
    recomputed_layout_hash = sha256_bytes_digest(canonical_json_bytes(layout_evidence.model_dump() if layout_evidence else {}))
    recomputed_visual_hash = sha256_bytes_digest(canonical_json_bytes(visual_observations.model_dump() if visual_observations else {}))

    recomputed_art_index_digest = sha256_bytes_digest(canonical_json_bytes(artifact_index.model_dump()))

    if artifact_index.manifest_hash != recomputed_manifest_hash:
        divergence_reasons.append("ArtifactIndex manifest_hash mismatch.")
    if artifact_index.layout_evidence_hash != recomputed_layout_hash:
        divergence_reasons.append("ArtifactIndex layout_evidence_hash mismatch.")

    for f_info in artifact_index.frames:
        f_id = f_info["frame_id"]
        exp_f_hash = f_info["sha256"]
        f_path = f"frames/{f_id}.png"
        if f_path not in file_contents:
            divergence_reasons.append(f"Frame PNG file '{f_path}' missing from package.")
            continue
        png_bytes = file_contents[f_path]
        actual_f_hash = sha256_bytes_digest(png_bytes)
        if actual_f_hash != exp_f_hash:
            divergence_reasons.append(f"Frame PNG hash mismatch for '{f_path}'.")
        png_valid, png_reason, _, _ = audit_png_bytes(png_bytes)
        if not png_valid:
            divergence_reasons.append(f"PNG byte audit failed for '{f_path}': {png_reason}.")

    # 5. Verify Release Evidence Root Hash Bindings
    auth_bytes = canonical_json_bytes([a.model_dump() for a in authorizations])
    prop_bytes = canonical_json_bytes(
        [p if isinstance(p, dict) else p.model_dump() for p in proposals]
    )

    if release_evidence.manifest_hash != recomputed_manifest_hash:
        divergence_reasons.append(
            f"Release root manifest_hash mismatch: '{release_evidence.manifest_hash}' != '{recomputed_manifest_hash}'."
        )
    if release_evidence.obligation_registry_version_hash != recomputed_orv_hash:
        divergence_reasons.append(
            f"Release root obligation_registry_version_hash mismatch: '{release_evidence.obligation_registry_version_hash}' != '{recomputed_orv_hash}'."
        )
    if release_evidence.layout_evidence_hash != recomputed_layout_hash:
        divergence_reasons.append(
            f"Release root layout_evidence_hash mismatch: '{release_evidence.layout_evidence_hash}' != '{recomputed_layout_hash}'."
        )
    if release_evidence.visual_observations_hash != recomputed_visual_hash:
        divergence_reasons.append(
            f"Release root visual_observations_hash mismatch: '{release_evidence.visual_observations_hash}' != '{recomputed_visual_hash}'."
        )
    if release_evidence.artifact_index_digest != recomputed_art_index_digest:
        divergence_reasons.append(
            f"Release root artifact_index_digest mismatch: '{release_evidence.artifact_index_digest}' != '{recomputed_art_index_digest}'."
        )
    if release_evidence.render_profile_version != artifact_index.render_profile_version:
        divergence_reasons.append(
            f"Release root render_profile_version mismatch: '{release_evidence.render_profile_version}' != '{artifact_index.render_profile_version}'."
        )
    if release_evidence.ordered_png_frame_hashes != artifact_index.frames:
        divergence_reasons.append(
            "Release root ordered_png_frame_hashes mismatch with artifact_index frames."
        )

    bound_auth_bytes = canonical_json_bytes(release_evidence.authorizations)
    if sha256_bytes_digest(bound_auth_bytes) != sha256_bytes_digest(auth_bytes):
        divergence_reasons.append(
            "Release root authorizations mismatch with bundled authorizations."
        )

    bound_prop_bytes = canonical_json_bytes(release_evidence.proposals)
    if sha256_bytes_digest(bound_prop_bytes) != sha256_bytes_digest(prop_bytes):
        divergence_reasons.append("Release root proposals mismatch with bundled proposals.")

    # 6. Build identity_bindings from hash-bound CONFIRM_IDENTITY authorizations
    from creditlock.api.export import _build_identity_bindings
    raw_replayed_bindings = _build_identity_bindings(
        authorizations,
        recomputed_manifest_hash,
        recomputed_orv_hash,
        recomputed_art_index_digest,
        recomputed_visual_hash,
    )
    preliminary_input = CheckerInput(
        obligations=obligations,
        manifest=manifest,
        layout_evidence=layout_evidence,
        visual_observations=visual_observations,
        contributor_registry=contributor_registry,
    )
    preliminary_issues = evaluate_findings(preliminary_input)
    ambig_map = {
        i.issue_id: i.obligation_id
        for i in preliminary_issues
        if i.code.value == "AMBIGUOUS_IDENTITY" and i.obligation_id is not None
    }
    replayed_ob_bindings: dict[str, str] = {}
    if raw_replayed_bindings:
        for issue_id, sel_cid in raw_replayed_bindings.items():
            obl_id = ambig_map.get(issue_id)
            if obl_id:
                replayed_ob_bindings[obl_id] = sel_cid

    if (release_evidence.identity_bindings or {}) != replayed_ob_bindings:
        divergence_reasons.append(
            f"Release root identity_bindings mismatch: '{release_evidence.identity_bindings}' != replayed '{replayed_ob_bindings}'."
        )

    identity_bindings = replayed_ob_bindings

    # 7. Deterministic re-evaluation
    checker_input = CheckerInput(
        obligations=obligations,
        manifest=manifest,
        layout_evidence=layout_evidence,
        visual_observations=visual_observations,
        contributor_registry=contributor_registry,
        identity_bindings=identity_bindings if identity_bindings else None,
    )
    findings = evaluate_findings(checker_input)

    from creditlock.api.export import _resolve_open_issues

    open_issues = _resolve_open_issues(
        findings,
        authorizations,
        recomputed_manifest_hash,
        recomputed_orv_hash,
        recomputed_art_index_digest,
        recomputed_visual_hash,
    )

    replayed_gate = fold_gate(open_issues, ProjectionStatus())

    if replayed_gate.value != expected_gate_state:
        divergence_reasons.append(
            f"Gate state divergence: replayed state '{replayed_gate.value}' != expected state '{expected_gate_state}'."
        )

    exp_release_gate = (
        release_evidence.final_gate_state.value
        if isinstance(release_evidence.final_gate_state, GateState)
        else str(release_evidence.final_gate_state)
    )
    if exp_release_gate != replayed_gate.value:
        divergence_reasons.append(
            f"Release root final_gate_state mismatch: '{exp_release_gate}' != replayed '{replayed_gate.value}'."
        )

    recomputed_findings_hash = sha256_bytes_digest(
        canonical_json_bytes([f.model_dump() for f in open_issues])
    )
    if release_evidence.deterministic_findings_hash != recomputed_findings_hash:
        divergence_reasons.append(
            f"Release root deterministic_findings_hash mismatch: '{release_evidence.deterministic_findings_hash}' != '{recomputed_findings_hash}'."
        )

    status: Literal["MATCH", "DIVERGENCE"] = "MATCH" if not divergence_reasons else "DIVERGENCE"

    return ReplayResult(
        status=status,
        expected_gate_state=expected_gate_state,
        replayed_gate_state=replayed_gate.value,
        expected_issues_count=len(open_issues),
        replayed_issues_count=len(open_issues),
        recomputed_artifact_index_digest=recomputed_art_index_digest,
        divergence_reasons=divergence_reasons,
    )
