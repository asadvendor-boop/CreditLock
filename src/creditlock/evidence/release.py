"""
Release evidence builder module.
"""
from __future__ import annotations

from typing import Any

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.models import GateState, Issue
from creditlock.events.models import CreditLockEvent
from creditlock.evidence.models import ArtifactIndex, ReleaseEvidence


def build_artifact_index(
    manifest_hash: str,
    render_profile_version: str,
    frames: list[Any],
    layout_evidence_hash: str,
) -> ArtifactIndex:
    """Build canonical Stage 1 ArtifactIndex."""
    frame_list = []
    for f in frames:
        if isinstance(f, dict):
            frame_list.append({"frame_id": str(f.get("frame_id", "")), "sha256": str(f.get("sha256") or f.get("image_hash", ""))})
        else:
            frame_list.append({"frame_id": f.frame_id, "sha256": f.image_hash})
    return ArtifactIndex(
        manifest_hash=manifest_hash,
        render_profile_version=render_profile_version,
        frames=frame_list,
        layout_evidence_hash=layout_evidence_hash,
    )


def build_release_evidence(
    manifest_hash: str,
    obligation_registry_version_hash: str,
    render_profile_version: str,
    ordered_png_frame_hashes: list[dict[str, str]],
    layout_evidence_hash: str,
    visual_observations_hash: str,
    deterministic_findings: list[Issue],
    identity_bindings: dict[str, str] | None,
    authorizations: list[Any],
    proposals: list[Any],
    final_gate_state: GateState | str,
    artifact_index_digest: str,
    artifact_rendered_event: CreditLockEvent | None = None,
) -> ReleaseEvidence:
    """Build canonical Stage 2 ReleaseEvidence."""
    det_findings_bytes = canonical_json_bytes([f.model_dump() for f in deterministic_findings])
    det_findings_hash = sha256_bytes_digest(det_findings_bytes)

    auth_list = [a.model_dump() if hasattr(a, "model_dump") else a for a in authorizations]
    prop_list = [p.model_dump() if hasattr(p, "model_dump") else p for p in proposals]
    bindings = identity_bindings or {}
    typed_gate = (
        GateState(final_gate_state) if isinstance(final_gate_state, str) else final_gate_state
    )

    return ReleaseEvidence(
        manifest_hash=manifest_hash,
        obligation_registry_version_hash=obligation_registry_version_hash,
        render_profile_version=render_profile_version,
        ordered_png_frame_hashes=ordered_png_frame_hashes,
        layout_evidence_hash=layout_evidence_hash,
        visual_observations_hash=visual_observations_hash,
        deterministic_findings_hash=det_findings_hash,
        identity_bindings=bindings,
        authorizations=auth_list,
        proposals=prop_list,
        final_gate_state=typed_gate,
        artifact_index_digest=artifact_index_digest,
        artifact_rendered_event=artifact_rendered_event,
        deterministic_findings=deterministic_findings,
    )
