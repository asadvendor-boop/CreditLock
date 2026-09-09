"""
Adversarial unit tests for evidence and replay security contracts.
"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import pytest

from creditlock.domain.models import (
    CreditManifest,
    CreditSurface,
    ManifestEntry,
    Obligation,
    SourceSpan,
)
from creditlock.evidence.delivery import assemble_delivery_package
from creditlock.evidence.replay import replay_bundle
from tests.fixtures.dummy_evidence import generate_dummy_evidence


def _create_test_production() -> dict:
    manifest = CreditManifest(
        manifest_id="mfst-adv-1",
        production_id="prod-adv-1",
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
                display_name="Alice Smith",
                role="Director",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id="prod-adv-1",
                delivery_version_id="v1",
            )
        ],
    )
    obligation = Obligation(
        obligation_id="obl-adv-1",
        production_id="prod-adv-1",
        credited_party_id="contrib-1",
        required_display_text="Alice Smith",
        role_label="Director",
        credit_surface=CreditSurface.END_CARDS,
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=11, quote="Alice Smith"),
        source_hash="hash-1",
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        status="ACTIVE",
    )
    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)

    from creditlock.domain.models import VisualObservation, VisualObservations
    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem-1",
                observation="Looks good",
                flagged=False,
            )
        ],
    )

    return {
        "manifest": manifest,
        "obligations": [obligation],
        "layout_evidence": layout_ev,
        "frames": frames,
        "artifact_index": artifact_index.model_dump(),
        "visual_observations": visuals,
        "contributor_registry": [{"contributor_id": "contrib-1", "name": "Alice Smith"}],
        "identity_bindings": {},
        "authorizations": [],
        "proposals": [],
        "render_profile_version": "1.0",
    }


class TestAdversarialReplay:
    def test_omission_of_expected_digest_fails(self) -> None:
        prod_data = _create_test_production()
        with tempfile.TemporaryDirectory() as tmpdir:
            res_pkg = assemble_delivery_package("prod-adv-1", prod_data, tmpdir)
            with pytest.raises((TypeError, ValueError)):
                replay_bundle(res_pkg.package_path)  # type: ignore[call-arg]

    def test_malformed_expected_digest_fails(self) -> None:
        prod_data = _create_test_production()
        with tempfile.TemporaryDirectory() as tmpdir:
            res_pkg = assemble_delivery_package("prod-adv-1", prod_data, tmpdir)
            res = replay_bundle(res_pkg.package_path, expected_release_digest="invalid_short_hex")
            assert res.status == "DIVERGENCE"

    def test_untouched_export_package_and_digest_returns_match(self) -> None:
        prod_data = _create_test_production()
        with tempfile.TemporaryDirectory() as tmpdir:
            res_pkg = assemble_delivery_package("prod-adv-1", prod_data, tmpdir)
            res = replay_bundle(
                res_pkg.package_path, expected_release_digest=res_pkg.release_evidence_digest
            )
            assert res.status == "MATCH"
            assert isinstance(res.replayed_gate_state, str)

    def test_change_png_bytes_causes_divergence(self) -> None:
        prod_data = _create_test_production()
        with tempfile.TemporaryDirectory() as tmpdir:
            res_pkg = assemble_delivery_package("prod-adv-1", prod_data, tmpdir)
            corrupt_path = Path(tmpdir) / "corrupt_png.zip"

            with zipfile.ZipFile(res_pkg.package_path, "r") as z_in, zipfile.ZipFile(
                corrupt_path, "w"
            ) as z_out:
                for item in z_in.infolist():
                    data = z_in.read(item.filename)
                    if item.filename.startswith("frames/"):
                        data = b"\x89PNG\r\n\x1a\nCorrupted PNG Data"
                    z_out.writestr(item, data)

            res = replay_bundle(
                corrupt_path, expected_release_digest=res_pkg.release_evidence_digest
            )
            assert res.status == "DIVERGENCE"

    def test_remove_artifact_index_causes_divergence(self) -> None:
        prod_data = _create_test_production()
        with tempfile.TemporaryDirectory() as tmpdir:
            res_pkg = assemble_delivery_package("prod-adv-1", prod_data, tmpdir)
            missing_idx_path = Path(tmpdir) / "missing_idx.zip"

            with zipfile.ZipFile(res_pkg.package_path, "r") as z_in, zipfile.ZipFile(
                missing_idx_path, "w"
            ) as z_out:
                for item in z_in.infolist():
                    if item.filename == "artifact_index.json":
                        continue
                    z_out.writestr(item, z_in.read(item.filename))

            res = replay_bundle(
                missing_idx_path, expected_release_digest=res_pkg.release_evidence_digest
            )
            assert res.status == "DIVERGENCE"
