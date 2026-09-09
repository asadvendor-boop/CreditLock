"""
Integration tests for two-stage render evidence lifecycle and Projector anchoring.
"""

from __future__ import annotations

import os
import tempfile
import uuid

from creditlock.domain.canonical import sha256_digest
from creditlock.domain.gate import GateState
from creditlock.domain.models import (
    CreditManifest,
    CreditSurface,
    IssueCode,
    ManifestEntry,
    VisualObservations,
)
from creditlock.events.models import (
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    ArtifactRenderedPayload,
    CreditLockEvent,
    CreditRollSubmittedPayload,
)
from creditlock.events.projection import Projector
from creditlock.evidence.release import build_release_evidence
from creditlock.evidence.storage import LocalDirectoryStore
from tests.fixtures.dummy_evidence import generate_dummy_evidence

PROD = "prod-render-lifecycle-1"
AT = "2026-08-01T00:00:00Z"


def _make_manifest() -> CreditManifest:
    return CreditManifest(
        manifest_id="manifest-rl-1",
        production_id=PROD,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-rl-1",
                contributor_id="contrib-rl-1",
                display_name="Alice Smith",
                role="Producer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=PROD,
                delivery_version_id="v1",
            )
        ],
    )


class TestRenderLifecycle:
    def test_render_lifecycle_emits_artifact_rendered_event(self) -> None:
        projector = Projector()
        manifest = _make_manifest()

        # 1. Submit credit roll (v1)
        sub_evt = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            production_id=PROD,
            aggregate_version=1,
            actor_id="producer-1",
            occurred_at=AT,
            payload=CreditRollSubmittedPayload(
                production_id=PROD,
                manifest_id=manifest.manifest_id,
                manifest_hash=sha256_digest(manifest.model_dump()),
                submitted_by="producer-1",
                at=AT,
            ),
        )
        assert projector.apply(sub_evt) is True

        state1 = projector.get(PROD)
        assert state1 is not None
        assert state1["gate"]["state"] == GateState.STALE.value
        assert state1["manifests"]["manifest_lifecycle"] == "PENDING_RENDER"

        # 2. Render manifest and audit artifact
        _layout_ev, _frames, artifact_index = generate_dummy_evidence(manifest)

        # 3. Create artifact.rendered event (v2)
        artifact_index_digest = sha256_digest(artifact_index.model_dump())
        render_evt = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type="artifact.rendered",
            production_id=PROD,
            aggregate_version=2,
            actor_id="system",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id=manifest.manifest_id,
                artifact_index_digest=artifact_index_digest,
                render_profile_version="1.0",
                at=AT,
            ),
        )

        # 4. Project artifact.rendered event
        assert projector.apply(render_evt) is True

        state2 = projector.get(PROD)
        assert state2 is not None
        assert state2["manifests"]["manifest_lifecycle"] == "RENDER_COMPLETE"
        assert state2["gate"]["artifact_index_digest"] == artifact_index_digest

        # Invariant check: artifact.rendered does NOT remove ARTIFACT_PENDING
        open_issue_codes = [i["type"] for i in state2["gate"]["open_issues"]]
        assert IssueCode.ARTIFACT_PENDING.value in open_issue_codes

    def test_release_evidence_binding(self) -> None:
        manifest = _make_manifest()
        _layout_ev, _frames, artifact_index = generate_dummy_evidence(manifest)

        artifact_index_digest = sha256_digest(artifact_index.model_dump())
        render_evt = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type="artifact.rendered",
            production_id=PROD,
            aggregate_version=2,
            actor_id="system",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id=manifest.manifest_id,
                artifact_index_digest=artifact_index_digest,
                render_profile_version="1.0",
                at=AT,
            ),
        )

        observations = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="gemini-3.6-flash",
            observations=[],
        )

        from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest

        manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
        visual_hash = sha256_bytes_digest(canonical_json_bytes(observations.model_dump()))

        rel_evidence = build_release_evidence(
            manifest_hash=manifest_hash,
            obligation_registry_version_hash="empty_orv",
            render_profile_version="v1.0",
            ordered_png_frame_hashes=[],
            layout_evidence_hash=artifact_index_digest,
            visual_observations_hash=visual_hash,
            deterministic_findings=[],
            identity_bindings={},
            authorizations=[],
            proposals=[],
            final_gate_state=GateState.READY_TO_EXPORT.value,
            artifact_index_digest=artifact_index_digest,
            artifact_rendered_event=render_evt,
        )

        assert rel_evidence.artifact_index_digest == artifact_index_digest
        assert rel_evidence.final_gate_state == GateState.READY_TO_EXPORT.value

        # Store in LocalDirectoryStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            rel_bytes = rel_evidence.model_dump_json().encode()
            uri = store.put("release_evidence.json", rel_bytes, if_generation_match=0)
            assert uri.startswith("file://")
            assert store.get("release_evidence.json") == rel_bytes

    import pytest

    @pytest.mark.real_chrome
    @pytest.mark.skipif(
        os.getenv("CREDITLOCK_RUN_REAL_CHROME") != "1",
        reason="set CREDITLOCK_RUN_REAL_CHROME=1 to run real Chrome integration",
    )
    def test_real_chrome_rendering_integration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import creditlock.renderer.render as render_module
        from creditlock.renderer.service import RendererService

        def synthetic_fallback(*args, **kwargs):
            raise AssertionError("Renderer silently fell back to synthetic PNG generation!")

        monkeypatch.setattr(render_module, "_create_synthetic_png_bytes", synthetic_fallback)

        manifest = _make_manifest()

        from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest

        result = RendererService().render_and_audit(manifest)

        assert len(result.frames) == 1
        frame = result.frames[0]

        assert frame.width == 1920
        assert frame.height == 1080
        assert frame.image_bytes is not None
        assert len(frame.image_bytes) > 0

        from creditlock.renderer.audit import audit_png_bytes

        valid, reason, w, h = audit_png_bytes(frame.image_bytes)
        assert valid is True
        assert reason is None
        assert w == 1920
        assert h == 1080

        png_digest = sha256_bytes_digest(frame.image_bytes)
        assert frame.image_hash == png_digest
        assert result.artifact_index["frames"][0]["frame_id"] == frame.frame_id
        assert result.artifact_index["frames"][0]["sha256"] == png_digest

        assert result.layout_evidence.manifest_id == manifest.manifest_id
        assert (
            result.layout_evidence.render_profile_version
            == result.artifact_index["render_profile_version"]
        )

        art_idx_bytes = canonical_json_bytes(result.artifact_index)
        assert result.artifact_index_digest == sha256_bytes_digest(art_idx_bytes)
