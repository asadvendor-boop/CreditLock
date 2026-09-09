"""
Delivery package assembler.

Assembles an independently replayable, self-contained zip archive containing all
immutable inputs, artifact index, PNG frames, release evidence, and hashes required
to reproduce gate state offline.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate

_FALLBACK_ASSEMBLED_AT = "1970-01-01T00:00:00Z"
_ZIP_FIXED_TIMESTAMP: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
_ZIP_MEMBER_PERMISSIONS = 0o644


class DeliveryPackageResult(BaseModel):
    model_config = {"extra": "forbid"}

    package_path: str
    release_evidence_digest: str


def assemble_delivery_package(
    production_id: str,
    prod_data: dict[str, Any],
    output_dir: str | Path,
) -> DeliveryPackageResult:
    """
    Assemble a zip delivery package containing all offline replay inputs.

    Returns DeliveryPackageResult(package_path, release_evidence_digest).
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    zip_filename = f"{production_id}_delivery_package.zip"
    zip_filepath = out_path / zip_filename

    # Extract components
    manifest = prod_data.get("manifest")
    if manifest is None:
        raise ValueError("Missing manifest in production data.")

    obligations = prod_data.get("obligations", [])
    layout_evidence = prod_data.get("layout_evidence")
    visual_observations = prod_data.get("visual_observations")
    contributor_registry = prod_data.get("contributor_registry", [])
    authorizations = prod_data.get("authorizations", [])
    proposals = prod_data.get("proposals", [])
    frames = prod_data.get("frames", [])
    art_index_raw = prod_data.get("artifact_index")

    # Missing layout evidence is an error — NO RendererService fallback
    if layout_evidence is None:
        raise ValueError(
            "Missing required immutable evidence (layout_evidence). Missing evidence cannot be repaired."
        )

    # Serializations
    manifest_bytes = canonical_json_bytes(manifest.model_dump())
    obligations_bytes = canonical_json_bytes([o.model_dump() for o in obligations])
    layout_bytes = canonical_json_bytes(layout_evidence.model_dump() if layout_evidence else {})
    visual_bytes = canonical_json_bytes(
        visual_observations.model_dump() if visual_observations else {}
    )
    registry_bytes = canonical_json_bytes(
        [c.model_dump() if hasattr(c, "model_dump") else c for c in contributor_registry]
    )
    auth_bytes = canonical_json_bytes(
        [a.model_dump() if hasattr(a, "model_dump") else a for a in authorizations]
    )
    proposals_bytes = canonical_json_bytes(
        [p.model_dump() if hasattr(p, "model_dump") else p for p in proposals]
    )

    # Validate evidence completely and obtain hashes
    from creditlock.evidence.validation import validate_artifact_evidence
    manifest_hash, orv_hash, art_index_digest, visual_hash = validate_artifact_evidence(prod_data)

    if isinstance(art_index_raw, dict):
        from creditlock.evidence.models import ArtifactIndex
        art_index_obj = ArtifactIndex.model_validate(art_index_raw)
    elif art_index_raw is not None:
        art_index_obj = art_index_raw
    else:
        raise ValueError("artifact_index cannot be None")

    art_index_bytes = canonical_json_bytes(art_index_obj.model_dump())

    # Use authorized snapshot values if provided in prod_data, otherwise evaluate
    identity_bindings = prod_data.get("identity_bindings")
    if identity_bindings is None:
        from creditlock.api.export import _build_identity_bindings
        raw_bindings = _build_identity_bindings(
            authorizations,
            manifest_hash,
            orv_hash,
            art_index_digest,
            visual_hash,
        )
        identity_bindings = raw_bindings or {}

    open_issues = prod_data.get("open_issues")
    if open_issues is None:
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
            manifest_hash,
            orv_hash,
            art_index_digest,
            visual_hash,
        )

    gate = prod_data.get("gate")
    if gate is None:
        projection = prod_data.get("projection") or ProjectionStatus()
        gate = fold_gate(open_issues, projection)

    det_findings_bytes = canonical_json_bytes([f.model_dump() for f in open_issues])

    ordered_png_frame_hashes = art_index_obj.frames

    from creditlock.evidence.release import build_release_evidence

    layout_hash = sha256_bytes_digest(layout_bytes)

    release_ev = build_release_evidence(
        manifest_hash=manifest_hash,
        obligation_registry_version_hash=orv_hash,
        render_profile_version=art_index_obj.render_profile_version,
        ordered_png_frame_hashes=ordered_png_frame_hashes,
        layout_evidence_hash=layout_hash,
        visual_observations_hash=visual_hash,
        deterministic_findings=open_issues,
        identity_bindings=identity_bindings,
        authorizations=authorizations,
        proposals=proposals,
        final_gate_state=gate,
        artifact_index_digest=art_index_digest,
    )
    release_ev_bytes = canonical_json_bytes(release_ev.model_dump())
    release_digest = sha256_bytes_digest(release_ev_bytes)

    # Process frame PNG bytes - NO PLACEHOLDER PNG FALLBACK!
    frame_files: dict[str, bytes] = {}
    for idx, f in enumerate(frames):
        frame_id = None
        png_bytes = None
        if hasattr(f, "frame_id"):
            frame_id = f.frame_id
            png_bytes = getattr(f, "image_bytes", None) or getattr(f, "png_bytes", None)
        elif isinstance(f, dict):
            frame_id = f.get("frame_id")
            png_bytes = f.get("image_bytes") or f.get("png_bytes")

        if not frame_id or not png_bytes:
            raise ValueError(
                f"Missing frame_id or PNG bytes for frame at index {idx}. Missing evidence cannot be repaired."
            )
        frame_files[f"frames/{frame_id}.png"] = png_bytes

    files_dict = {
        "manifest.json": sha256_bytes_digest(manifest_bytes),
        "obligations.json": sha256_bytes_digest(obligations_bytes),
        "layout_evidence.json": sha256_bytes_digest(layout_bytes),
        "visual_observations.json": sha256_bytes_digest(visual_bytes),
        "contributor_registry.json": sha256_bytes_digest(registry_bytes),
        "authorizations.json": sha256_bytes_digest(auth_bytes),
        "proposals.json": sha256_bytes_digest(proposals_bytes),
        "artifact_index.json": art_index_digest,
        "deterministic_findings.json": sha256_bytes_digest(det_findings_bytes),
        "release_evidence.json": release_digest,
    }
    for f_path, f_bytes in frame_files.items():
        files_dict[f_path] = sha256_bytes_digest(f_bytes)

    gate_val = gate.value if hasattr(gate, "value") else str(gate)

    auth_timestamps: list[str] = []
    for a in authorizations:
        ts = getattr(a, "authorized_at", None) if not isinstance(a, dict) else a.get("authorized_at")
        if isinstance(ts, str) and ts.strip():
            auth_timestamps.append(ts.strip())

    deterministic_assembled_at = max(auth_timestamps) if auth_timestamps else _FALLBACK_ASSEMBLED_AT

    bundle_index = {
        "production_id": production_id,
        "delivery_version_id": manifest.delivery_version_id,
        "manifest_hash": manifest_hash,
        "obligation_registry_version_hash": orv_hash,
        "artifact_index_digest": art_index_digest,
        "visual_observations_hash": visual_hash,
        "authorization_log_hash": sha256_bytes_digest(auth_bytes),
        "release_evidence_digest": release_digest,
        "expected_gate_state": gate_val,
        "assembled_at": deterministic_assembled_at,
        "files": files_dict,
    }
    bundle_index_bytes = canonical_json_bytes(bundle_index)

    # Collect package files and write in sorted order with explicit deterministic ZipInfo metadata
    package_members: dict[str, bytes] = {
        "bundle_index.json": bundle_index_bytes,
        "manifest.json": manifest_bytes,
        "obligations.json": obligations_bytes,
        "layout_evidence.json": layout_bytes,
        "visual_observations.json": visual_bytes,
        "contributor_registry.json": registry_bytes,
        "authorizations.json": auth_bytes,
        "proposals.json": proposals_bytes,
        "artifact_index.json": art_index_bytes,
        "deterministic_findings.json": det_findings_bytes,
        "release_evidence.json": release_ev_bytes,
    }
    for f_path, f_bytes in frame_files.items():
        package_members[f_path] = f_bytes

    with zipfile.ZipFile(zip_filepath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for member_name in sorted(package_members.keys()):
            zinfo = zipfile.ZipInfo(
                filename=member_name,
                date_time=_ZIP_FIXED_TIMESTAMP,
            )
            zinfo.create_system = 3  # UNIX platform marker
            zinfo.external_attr = (0o100000 | _ZIP_MEMBER_PERMISSIONS) << 16  # standard -rw-r--r--
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zinfo, package_members[member_name])

    return DeliveryPackageResult(
        package_path=str(zip_filepath.resolve()),
        release_evidence_digest=release_digest,
    )
