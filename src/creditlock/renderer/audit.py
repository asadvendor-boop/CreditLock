"""
Strict PNG audit and artifact evidence verifier.

Validates PNG binary integrity (magic header, chunk CRC32, IEND termination),
dimension match, frame count/order match, and artifact_index hash consistency.

Emits ARTIFACT_INTEGRITY_FAILURE on any corruption, truncation, dimension mismatch,
or hash divergence.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

from creditlock.domain.canonical import sha256_bytes_digest, sha256_digest
from creditlock.domain.checker import _stable_issue_id
from creditlock.domain.models import CreditManifest, Issue, IssueCode
from creditlock.renderer.models import FrameEvidence, RenderProfile

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def audit_png_bytes(
    data: bytes,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> tuple[bool, str | None, int, int]:
    """
    Strictly decode PNG bytes and verify chunk CRC32 checksums, IEND chunk, and optional dimensions.

    Returns:
        (valid: bool, rejection_reason: str | None, width: int, height: int)
    """
    if len(data) < 8 or not data.startswith(PNG_MAGIC):
        return False, "PNG magic header missing or invalid", 0, 0

    offset = 8
    width = 0
    height = 0
    found_ihdr = False
    found_iend = False

    while offset < len(data):
        if offset + 8 > len(data):
            return False, "Truncated PNG chunk header", 0, 0

        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        offset += 8

        if offset + length + 4 > len(data):
            return (
                False,
                f"Truncated PNG chunk data for type {chunk_type.decode(errors='ignore')}",
                0,
                0,
            )

        chunk_data = data[offset : offset + length]
        offset += length

        stored_crc = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

        # CRC32 is calculated on chunk_type + chunk_data
        calculated_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if calculated_crc != stored_crc:
            return False, f"CRC32 mismatch in {chunk_type.decode(errors='ignore')} chunk", 0, 0

        if not found_ihdr:
            if chunk_type != b"IHDR":
                return False, "First chunk is not IHDR", 0, 0
            if length < 8:
                return False, "Invalid IHDR chunk length", 0, 0
            width, height = struct.unpack(">II", chunk_data[:8])
            found_ihdr = True

        if chunk_type == b"IEND":
            found_iend = True
            break

    if not found_ihdr:
        return False, "Missing IHDR chunk", 0, 0
    if not found_iend:
        return False, "Missing or unreached IEND chunk", 0, 0
    if offset < len(data):
        return (
            False,
            f"Extra trailing bytes found after IEND chunk ({len(data) - offset} bytes)",
            width,
            height,
        )

    if expected_width is not None and width != expected_width:
        return (
            False,
            f"Dimension mismatch: width {width} != expected {expected_width}",
            width,
            height,
        )
    if expected_height is not None and height != expected_height:
        return (
            False,
            f"Dimension mismatch: height {height} != expected {expected_height}",
            width,
            height,
        )

    return True, None, width, height


def audit_artifact_package(
    manifest: CreditManifest,
    render_profile: RenderProfile,
    frames: list[FrameEvidence],
    layout_evidence: Any,
    artifact_index: dict[str, Any],
) -> list[Issue]:
    """
    Perform fail-closed audit of rendered frames, layout evidence, and artifact_index.

    Returns a list of Issue objects. If any check fails, includes an ARTIFACT_INTEGRITY_FAILURE issue.
    """
    issues: list[Issue] = []

    def _integrity_error(detail: str, discriminator: str) -> Issue:
        issue_id = _stable_issue_id(
            "artifact-integrity", IssueCode.ARTIFACT_INTEGRITY_FAILURE, discriminator
        )
        return Issue(
            issue_id=issue_id,
            code=IssueCode.ARTIFACT_INTEGRITY_FAILURE,
            obligation_id=None,
            manifest_refs=[manifest.manifest_id],
            detail=detail,
        )

    # 1. Check frame count matches manifest card count
    if len(frames) != len(manifest.entries):
        issues.append(
            _integrity_error(
                f"Frame count mismatch: manifest has {len(manifest.entries)} entries, but rendered {len(frames)} frames.",
                "frame-count-mismatch",
            )
        )
        return issues

    # 2. Check artifact_index manifest_hash and profile match
    expected_manifest_hash = sha256_digest(manifest.model_dump())
    if artifact_index.get("manifest_hash") != expected_manifest_hash:
        issues.append(
            _integrity_error(
                f"Artifact index manifest_hash mismatch: got '{artifact_index.get('manifest_hash')}', expected '{expected_manifest_hash}'.",
                "manifest-hash-mismatch",
            )
        )

    if artifact_index.get("render_profile_version") != render_profile.profile_version:
        issues.append(
            _integrity_error(
                f"Render profile version mismatch: got '{artifact_index.get('render_profile_version')}', expected '{render_profile.profile_version}'.",
                "profile-version-mismatch",
            )
        )

    # 3. Check layout_evidence hash matches artifact_index
    layout_dict = (
        layout_evidence.model_dump() if hasattr(layout_evidence, "model_dump") else layout_evidence
    )
    expected_layout_hash = sha256_digest(layout_dict)
    if artifact_index.get("layout_evidence_hash") != expected_layout_hash:
        issues.append(
            _integrity_error(
                f"Layout evidence hash mismatch in artifact index: got '{artifact_index.get('layout_evidence_hash')}', expected '{expected_layout_hash}'.",
                "layout-hash-mismatch",
            )
        )

    # 4. Audit each PNG frame
    index_frames = artifact_index.get("frames", [])
    if len(index_frames) != len(frames):
        issues.append(
            _integrity_error(
                f"Artifact index frame count {len(index_frames)} does not match rendered frame count {len(frames)}.",
                "index-frame-count-mismatch",
            )
        )
        return issues

    for idx, (frame, idx_frame) in enumerate(zip(frames, index_frames, strict=False)):
        # Decode PNG bytes strictly
        valid, reason, w, h = audit_png_bytes(frame.image_bytes)
        if not valid:
            issues.append(
                _integrity_error(
                    f"Frame {idx} ({frame.frame_id}) failed PNG audit: {reason}.",
                    f"frame-corrupt-{idx}",
                )
            )
            continue

        # Check width/height against profile and layout evidence
        if w != render_profile.viewport_width or h != render_profile.viewport_height:
            issues.append(
                _integrity_error(
                    f"Frame {idx} dimensions {w}x{h} do not match render profile {render_profile.viewport_width}x{render_profile.viewport_height}.",
                    f"frame-dim-{idx}",
                )
            )

        # Check hash against artifact_index
        calc_hash = sha256_bytes_digest(frame.image_bytes)
        if calc_hash != frame.image_hash:
            issues.append(
                _integrity_error(
                    f"Frame {idx} image hash inconsistency: computed '{calc_hash}' != declared '{frame.image_hash}'.",
                    f"frame-hash-internal-{idx}",
                )
            )

        if idx_frame.get("sha256") != calc_hash:
            issues.append(
                _integrity_error(
                    f"Frame {idx} sha256 in artifact_index '{idx_frame.get('sha256')}' != computed '{calc_hash}'.",
                    f"frame-hash-index-{idx}",
                )
            )

        if idx_frame.get("frame_id") != frame.frame_id:
            issues.append(
                _integrity_error(
                    f"Frame {idx} frame_id in artifact_index '{idx_frame.get('frame_id')}' != '{frame.frame_id}'.",
                    f"frame-id-mismatch-{idx}",
                )
            )

    return issues
