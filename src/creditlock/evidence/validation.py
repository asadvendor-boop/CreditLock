from typing import Any

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.evidence.models import ArtifactIndex


class ArtifactEvidenceMissingError(ValueError):
    pass


class ArtifactEvidenceIntegrityError(ValueError):
    pass


def validate_artifact_evidence(prod: dict[str, Any]) -> tuple[str, str, str, str]:
    """
    Strict centralized validation for ArtifactIndex and associated evidence.
    Returns (manifest_hash, orv_hash, artifact_index_digest, visual_observations_hash).
    Raises ArtifactEvidenceMissingError for missing required evidence.
    Raises ArtifactEvidenceIntegrityError for invalid, stale, or tampered evidence.
    """
    manifest = prod.get("manifest")
    if manifest is None:
        raise ArtifactEvidenceMissingError("Missing manifest in production state.")

    layout_ev = prod.get("layout_evidence")
    if layout_ev is None:
        raise ArtifactEvidenceMissingError("Missing layout_evidence in production state.")

    visual_observations = prod.get("visual_observations")
    if visual_observations is None:
        raise ArtifactEvidenceMissingError("Missing visual_observations in production state.")

    frames = prod.get("frames")
    if not frames:
        raise ArtifactEvidenceMissingError("Missing or empty frames in production state.")

    art_index_raw = prod.get("artifact_index")
    if art_index_raw is None:
        raise ArtifactEvidenceMissingError("Missing artifact_index in production state.")

    stored_profile_version = prod.get("render_profile_version")
    if not stored_profile_version:
        raise ArtifactEvidenceMissingError("Missing render_profile_version in production state.")

    # Integrity Validations
    if (
        getattr(
            layout_ev,
            "manifest_id",
            layout_ev.get("manifest_id", None) if isinstance(layout_ev, dict) else None,
        )
        != manifest.manifest_id
    ):
        raise ArtifactEvidenceIntegrityError(
            "layout_evidence.manifest_id does not match manifest.manifest_id."
        )

    if (
        getattr(
            visual_observations,
            "manifest_id",
            visual_observations.get("manifest_id", None)
            if isinstance(visual_observations, dict)
            else None,
        )
        != manifest.manifest_id
    ):
        raise ArtifactEvidenceIntegrityError(
            "visual_observations.manifest_id does not match manifest.manifest_id."
        )

    layout_profile_version = getattr(
        layout_ev,
        "render_profile_version",
        layout_ev.get("render_profile_version", None) if isinstance(layout_ev, dict) else None,
    )
    if layout_profile_version != stored_profile_version:
        raise ArtifactEvidenceIntegrityError(
            "layout_evidence.render_profile_version does not match stored render_profile_version."
        )

    # Hashes
    manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
    orv_hash = sha256_bytes_digest(
        canonical_json_bytes([o.model_dump() for o in prod.get("obligations", [])])
    )
    visual_hash = sha256_bytes_digest(canonical_json_bytes(visual_observations.model_dump()))

    layout_ev_dict = layout_ev.model_dump() if hasattr(layout_ev, "model_dump") else layout_ev
    layout_ev_hash = sha256_bytes_digest(canonical_json_bytes(layout_ev_dict))

    if isinstance(art_index_raw, dict):
        from pydantic import ValidationError

        try:
            art_index = ArtifactIndex.model_validate(art_index_raw)
        except ValidationError as e:
            raise ArtifactEvidenceIntegrityError(f"Malformed ArtifactIndex schema: {e}") from e
    else:
        art_index = art_index_raw

    if art_index.manifest_hash != manifest_hash:
        raise ArtifactEvidenceIntegrityError(
            f"Stale artifact_index: manifest_hash mismatch ('{art_index.manifest_hash}' != '{manifest_hash}')."
        )

    if art_index.layout_evidence_hash != layout_ev_hash:
        raise ArtifactEvidenceIntegrityError(
            f"Stale artifact_index: layout_evidence_hash mismatch ('{art_index.layout_evidence_hash}' != '{layout_ev_hash}')."
        )

    if art_index.render_profile_version != stored_profile_version:
        raise ArtifactEvidenceIntegrityError(
            "Stale artifact_index: render_profile_version mismatch."
        )

    if len(art_index.frames) != len(frames):
        raise ArtifactEvidenceIntegrityError(
            f"Artifact index frame count mismatch: got {len(art_index.frames)}, expected {len(frames)}."
        )

    from creditlock.renderer.audit import audit_png_bytes

    seen_frame_ids = set()
    seen_idx_frame_ids = set()

    for idx, (frame, idx_frame) in enumerate(zip(frames, art_index.frames, strict=False)):
        frame_id = getattr(frame, "frame_id", None) or (
            frame.get("frame_id") if isinstance(frame, dict) else None
        )
        if not frame_id:
            raise ArtifactEvidenceIntegrityError(f"Missing frame_id for frame {idx}.")
        if frame_id in seen_frame_ids:
            raise ArtifactEvidenceIntegrityError(f"Duplicate frame_id '{frame_id}' in frames.")
        seen_frame_ids.add(frame_id)

        idx_frame_id = (
            idx_frame.get("frame_id") if isinstance(idx_frame, dict) else idx_frame.frame_id
        )
        if not idx_frame_id:
            raise ArtifactEvidenceIntegrityError(
                f"Missing frame_id for artifact index frame {idx}."
            )
        if idx_frame_id in seen_idx_frame_ids:
            raise ArtifactEvidenceIntegrityError(
                f"Duplicate frame_id '{idx_frame_id}' in artifact index."
            )
        seen_idx_frame_ids.add(idx_frame_id)

        if idx_frame_id != frame_id:
            raise ArtifactEvidenceIntegrityError(
                f"Stale artifact_index: frame_id mismatch at index {idx} ('{idx_frame_id}' != '{frame_id}')."
            )

        frame_index = (
            getattr(frame, "frame_index", None)
            if hasattr(frame, "frame_index")
            else frame.get("frame_index")
        )
        if frame_index != idx:
            raise ArtifactEvidenceIntegrityError(
                f"Non-contiguous frame_index at index {idx}: expected {idx}, got {frame_index}."
            )

        png_bytes = getattr(frame, "image_bytes", None) or (
            frame.get("image_bytes") if isinstance(frame, dict) else None
        )
        if not png_bytes:
            raise ArtifactEvidenceMissingError(f"Missing PNG bytes for frame {idx}.")

        valid, reason, w, h = audit_png_bytes(png_bytes)
        if not valid:
            raise ArtifactEvidenceIntegrityError(f"Frame {idx} failed PNG audit: {reason}")

        frame_w = getattr(frame, "width", None) or (
            frame.get("width") if isinstance(frame, dict) else None
        )
        frame_h = getattr(frame, "height", None) or (
            frame.get("height") if isinstance(frame, dict) else None
        )
        if w != frame_w or h != frame_h:
            raise ArtifactEvidenceIntegrityError(
                f"Frame {idx} dimensions mismatch: audited ({w}x{h}), stored ({frame_w}x{frame_h})."
            )

        png_digest = sha256_bytes_digest(png_bytes)
        frame_image_hash = getattr(frame, "image_hash", None) or (
            frame.get("image_hash") if isinstance(frame, dict) else None
        )

        if png_digest != frame_image_hash:
            raise ArtifactEvidenceIntegrityError(
                f"Stale frame: PNG bytes do not match FrameEvidence.image_hash at index {idx}."
            )

        idx_sha256 = idx_frame.get("sha256") if isinstance(idx_frame, dict) else idx_frame.sha256
        if png_digest != idx_sha256:
            raise ArtifactEvidenceIntegrityError(
                f"Stale artifact_index: PNG bytes do not match ArtifactIndex frame hash at index {idx}."
            )

    art_index_bytes = canonical_json_bytes(art_index.model_dump())
    artifact_index_digest = sha256_bytes_digest(art_index_bytes)

    return manifest_hash, orv_hash, artifact_index_digest, visual_hash
