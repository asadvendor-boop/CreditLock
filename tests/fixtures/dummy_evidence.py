import base64

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.models import CreditManifest, LayoutAssertion, LayoutEvidence
from creditlock.evidence.models import ArtifactIndex
from creditlock.evidence.release import build_artifact_index
from creditlock.renderer.models import FrameElement, FrameEvidence

# 1x1 transparent PNG
DUMMY_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

def generate_dummy_evidence(manifest: CreditManifest) -> tuple[LayoutEvidence, list[FrameEvidence], ArtifactIndex]:
    assertions = []
    frames = []

    png_digest = sha256_bytes_digest(DUMMY_PNG_BYTES)

    for i, entry in enumerate(manifest.entries):
        frame_id = f"frame-{i}"
        assertions.append(
            LayoutAssertion(
                rendered_element_id=entry.rendered_element_id,
                visible_text=entry.display_name,
                computed_font_size_px=24.0,
                bounding_box={"x": 10.0, "y": 10.0, "w": 100.0, "h": 20.0},
                frame_index=i,
                group_id=entry.group_id,
            )
        )

        frames.append(
            FrameEvidence(
                frame_index=i,
                frame_id=frame_id,
                image_bytes=DUMMY_PNG_BYTES,
                image_hash=png_digest,
                width=1,
                height=1,
                elements=[
                    FrameElement(
                        element_id=entry.rendered_element_id,
                        text=entry.display_name,
                        computed_font_size_px=24.0,
                        bounding_box={"x": 10.0, "y": 10.0, "w": 100.0, "h": 20.0},
                    )
                ],
            )
        )

    if not frames:
        frames.append(
            FrameEvidence(
                frame_index=0,
                frame_id="frame-0",
                image_bytes=DUMMY_PNG_BYTES,
                image_hash=png_digest,
                width=1,
                height=1,
                elements=[],
            )
        )

    layout = LayoutEvidence(
        manifest_id=manifest.manifest_id,
        render_profile_version="1.0",
        assertions=assertions,
    )

    manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
    layout_hash = sha256_bytes_digest(canonical_json_bytes(layout.model_dump()))

    artifact_index = build_artifact_index(
        manifest_hash=manifest_hash,
        render_profile_version=layout.render_profile_version,
        frames=frames,
        layout_evidence_hash=layout_hash,
    )

    return layout, frames, artifact_index
