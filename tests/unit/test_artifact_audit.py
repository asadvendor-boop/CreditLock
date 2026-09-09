"""
Exhaustive unit tests for PNG auditing, artifact evidence validation, and content-addressed storage.
"""

from __future__ import annotations

import tempfile

import pytest

from creditlock.domain.models import CreditManifest, CreditSurface, ManifestEntry
from creditlock.evidence.storage import LocalDirectoryStore, StorageCollisionError
from creditlock.renderer.audit import audit_artifact_package, audit_png_bytes
from creditlock.renderer.models import RenderProfile
from creditlock.renderer.render import _create_synthetic_png_bytes
from tests.fixtures.dummy_evidence import generate_dummy_evidence


def _make_manifest() -> CreditManifest:
    return CreditManifest(
        manifest_id="manifest-audit-1",
        production_id="prod-audit-1",
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
                display_name="Jane Doe",
                role="Director",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id="prod-audit-1",
                delivery_version_id="v1",
            )
        ],
    )


class TestArtifactAudit:
    def test_audit_png_bytes_valid(self) -> None:
        valid_png = _create_synthetic_png_bytes(1920, 1080, "Test Label")
        valid, reason, w, h = audit_png_bytes(valid_png)
        assert valid is True
        assert reason is None
        assert w == 1920
        assert h == 1080

    def test_audit_png_bytes_corrupt_crc(self) -> None:
        valid_png = bytearray(_create_synthetic_png_bytes(1920, 1080, "Test Label"))
        # Corrupt a byte in the IHDR data
        valid_png[16] ^= 0xFF
        valid, reason, _w, _h = audit_png_bytes(bytes(valid_png))
        assert valid is False
        assert reason is not None and "CRC32 mismatch" in reason

    def test_audit_png_bytes_missing_iend(self) -> None:
        valid_png = _create_synthetic_png_bytes(1920, 1080, "Test Label")
        # Truncate end of PNG to remove IEND
        truncated = valid_png[:-12]
        valid, reason, _w, _h = audit_png_bytes(truncated)
        assert valid is False
        assert reason is not None

    def test_audit_artifact_package_valid(self) -> None:
        manifest = _make_manifest()
        layout_evidence, frames, artifact_index = generate_dummy_evidence(manifest)

        issues = audit_artifact_package(
            manifest=manifest,
            render_profile=RenderProfile(profile_version=layout_evidence.render_profile_version, viewport_width=1, viewport_height=1),
            frames=frames,
            layout_evidence=layout_evidence,
            artifact_index=artifact_index.model_dump(),
        )
        assert len(issues) == 0

    def test_audit_artifact_package_frame_count_mismatch(self) -> None:
        manifest = _make_manifest()
        layout_evidence, _frames, artifact_index = generate_dummy_evidence(manifest)

        # Remove frame
        issues = audit_artifact_package(
            manifest=manifest,
            render_profile=RenderProfile(profile_version=layout_evidence.render_profile_version, viewport_width=1, viewport_height=1),
            frames=[],
            layout_evidence=layout_evidence,
            artifact_index=artifact_index.model_dump(),
        )
        assert len(issues) == 1
        assert issues[0].code.value == "ARTIFACT_INTEGRITY_FAILURE"

    def test_storage_collision_error_on_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            path = "test/artifact.json"
            store.put(path, b"initial content", if_generation_match=0)

            # Second put with if_generation_match=0 must raise StorageCollisionError
            with pytest.raises(StorageCollisionError):
                store.put(path, b"overwrite content", if_generation_match=0)

    def test_audit_png_bytes_invalid_header_signature(self) -> None:
        invalid = b"NOT_A_PNG_FILE_DATA"
        valid, reason, _w, _h = audit_png_bytes(invalid)
        assert valid is False
        assert reason is not None and "PNG magic header" in reason

    def test_audit_png_bytes_wrong_dimensions(self) -> None:
        valid_png = _create_synthetic_png_bytes(1920, 1080, "Test Label")
        # Assert width=1280 fails when expected 1920
        valid, reason, _w, _h = audit_png_bytes(
            valid_png, expected_width=1280, expected_height=1080
        )
        assert valid is False
        assert reason is not None and "Dimension mismatch" in reason

    def test_audit_png_bytes_extra_bytes_after_iend(self) -> None:
        valid_png = _create_synthetic_png_bytes(1920, 1080, "Test Label")
        corrupted = valid_png + b"EXTRA_TRAILING_BYTES"
        valid, reason, _w, _h = audit_png_bytes(corrupted)
        assert valid is False
        assert reason is not None and "trailing" in reason.lower()

    def test_audit_artifact_package_unexpected_element_id(self) -> None:
        manifest = _make_manifest()
        layout_evidence, frames, artifact_index = generate_dummy_evidence(manifest)

        # Alter manifest entry rendered_element_id to cause mismatch
        manifest.entries[0].rendered_element_id = "unexpected-elem-id"

        issues = audit_artifact_package(
            manifest=manifest,
            render_profile=RenderProfile(profile_version=layout_evidence.render_profile_version, viewport_width=1, viewport_height=1),
            frames=frames,
            layout_evidence=layout_evidence,
            artifact_index=artifact_index.model_dump(),
        )
        assert len(issues) >= 1
        assert any(i.code.value == "ARTIFACT_INTEGRITY_FAILURE" for i in issues)
