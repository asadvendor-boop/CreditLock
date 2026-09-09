"""
Renderer service orchestrating card rendering, PNG auditing, and artifact evidence creation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from creditlock.domain.canonical import sha256_digest
from creditlock.domain.models import CreditManifest, LayoutAssertion, LayoutEvidence
from creditlock.events.models import (
    EVENT_TYPE_ARTIFACT_RENDERED,
    ArtifactRenderedPayload,
    CreditLockEvent,
)
from creditlock.evidence.release import build_artifact_index
from creditlock.renderer.audit import audit_artifact_package
from creditlock.renderer.models import RenderProfile, RenderResult
from creditlock.renderer.render import render_manifest_frames


class RendererService:
    """Orchestrates credit roll rendering, layout evidence extraction, and PNG auditing."""

    def __init__(self, profile: RenderProfile | None = None) -> None:
        self.profile = profile or RenderProfile()

    def render_and_audit(
        self,
        manifest: CreditManifest,
    ) -> RenderResult:
        p = self.profile
        frames = render_manifest_frames(manifest, p)

        # Build LayoutEvidence using domain LayoutAssertion
        layout_assertions: list[LayoutAssertion] = []
        for frame in frames:
            for el in frame.elements:
                layout_assertions.append(
                    LayoutAssertion(
                        rendered_element_id=el.element_id,
                        visible_text=el.text,
                        computed_font_size_px=el.computed_font_size_px,
                        bounding_box=el.bounding_box,
                        frame_index=frame.frame_index,
                        group_id=None,
                    )
                )

        manifest_hash = sha256_digest(manifest.model_dump())
        layout_ev = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version=p.profile_version,
            assertions=layout_assertions,
        )
        layout_ev_hash = sha256_digest(layout_ev.model_dump())

        # Build ArtifactIndex
        art_idx_obj = build_artifact_index(
            manifest_hash=manifest_hash,
            render_profile_version=p.profile_version,
            frames=frames,
            layout_evidence_hash=layout_ev_hash,
        )
        art_idx_dict = art_idx_obj.model_dump()
        art_idx_digest = sha256_digest(art_idx_dict)

        # Audit artifact package
        audit_issues = audit_artifact_package(
            manifest=manifest,
            render_profile=p,
            frames=frames,
            layout_evidence=layout_ev,
            artifact_index=art_idx_dict,
        )
        if audit_issues:
            raise ValueError(f"Artifact audit failed: {[i.detail for i in audit_issues]}")

        return RenderResult(
            frames=frames,
            layout_evidence=layout_ev,
            artifact_index=art_idx_dict,
            artifact_index_digest=art_idx_digest,
            render_profile=p,
        )

    def create_artifact_rendered_event(
        self,
        production_id: str,
        manifest_id: str,
        artifact_index_digest: str,
        aggregate_version: int,
        actor_id: str = "renderer-service",
        at: str | None = None,
    ) -> CreditLockEvent:
        now_iso = at or datetime.now(UTC).isoformat()
        return CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=production_id,
            aggregate_version=aggregate_version,
            actor_id=actor_id,
            occurred_at=now_iso,
            payload=ArtifactRenderedPayload(
                production_id=production_id,
                manifest_id=manifest_id,
                artifact_index_digest=artifact_index_digest,
                render_profile_version=self.profile.profile_version,
                at=now_iso,
            ),
        )
