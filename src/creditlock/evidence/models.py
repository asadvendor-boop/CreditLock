"""
Evidence models for Stage 1 artifact_index and Stage 2 release_evidence.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from creditlock.domain.models import GateState, Issue
from creditlock.events.models import CreditLockEvent


class ArtifactIndex(BaseModel):
    """
    Stage 1 artifact trust root (artifact_index.json).

    Contains ONLY:
    - manifest_hash
    - render_profile_version
    - frames (ordered list of frame_id and sha256)
    - layout_evidence_hash
    """

    model_config = {"extra": "forbid"}

    manifest_hash: str
    render_profile_version: str
    frames: list[dict[str, str]]
    layout_evidence_hash: str


class ReleaseEvidence(BaseModel):
    """
    Stage 2 release evidence packet (release_evidence.json).

    Authoritative release root binding manifest_hash, obligation_registry_version_hash,
    render_profile_version, ordered_png_frame_hashes, layout_evidence_hash,
    visual_observations_hash, deterministic_findings_hash, identity_bindings,
    authorizations, proposals, final_gate_state, and artifact_index_digest.
    """

    model_config = {"extra": "forbid"}

    manifest_hash: str
    obligation_registry_version_hash: str
    render_profile_version: str
    ordered_png_frame_hashes: list[dict[str, str]] = Field(default_factory=list)
    layout_evidence_hash: str
    visual_observations_hash: str
    deterministic_findings_hash: str
    identity_bindings: dict[str, str] = Field(default_factory=dict)
    authorizations: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    final_gate_state: GateState
    artifact_index_digest: str
    artifact_rendered_event: CreditLockEvent | None = None
    deterministic_findings: list[Issue] = Field(default_factory=list)
