"""
Exhaustive unit and contract tests for delivery packaging and offline replay.
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.models import (
    AbsolutePosition,
    CardPositionKind,
    CardType,
    CreditManifest,
    CreditSurface,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    SourceSpan,
    VisualObservation,
    VisualObservations,
)
from creditlock.evidence.delivery import assemble_delivery_package
from creditlock.evidence.replay import replay_bundle
from tests.fixtures.dummy_evidence import generate_dummy_evidence


def _make_production_data(prod_id: str = "prod-replay-1") -> dict[str, object]:
    manifest = CreditManifest(
        manifest_id="manifest-r1",
        production_id=prod_id,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-r1",
                contributor_id="contrib-r1",
                display_name="John Williams",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=prod_id,
                delivery_version_id="v1",
            )
        ],
    )
    obligation = Obligation(
        obligation_id="obl-r1",
        production_id=prod_id,
        credited_party_id="contrib-r1",
        required_display_text="John Williams",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=10, quote="John Williams"),
        source_hash="hash-1",
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        status=ObligationStatus.ACTIVE,
    )
    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)
    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem-r1",
                observation="Clear readable credit card",
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
        "contributor_registry": [{"contributor_id": "contrib-r1", "name": "John Williams"}],
        "identity_bindings": {},
        "authorizations": [],
        "proposals": [],
        "render_profile_version": "1.0",
        "projection": None,
    }


class TestDeliveryReplay:
    def test_clean_bundle_produces_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-match-1")
            res_pkg = assemble_delivery_package("prod-match-1", prod_data, tmpdir)

            res = replay_bundle(res_pkg.package_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "MATCH"
            assert res.replayed_gate_state == "READY_TO_EXPORT"
            assert res.divergence_reasons == []

    def test_modified_file_produces_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-mod-1")
            res_pkg = assemble_delivery_package("prod-mod-1", prod_data, tmpdir)

            # Modify a file inside the zip
            mod_zip_path = Path(tmpdir) / "tampered.zip"
            with (
                zipfile.ZipFile(res_pkg.package_path, "r") as z_in,
                zipfile.ZipFile(mod_zip_path, "w") as z_out,
            ):
                for item in z_in.infolist():
                    data = z_in.read(item.filename)
                    if item.filename == "manifest.json":
                        # Tamper with manifest bytes
                        data = data.replace(b"John Williams", b"Tampered Name")
                    z_out.writestr(item, data)

            res = replay_bundle(mod_zip_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "DIVERGENCE"
            assert any("hash divergence" in r for r in res.divergence_reasons)

    def test_deleted_file_produces_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-del-1")
            res_pkg = assemble_delivery_package("prod-del-1", prod_data, tmpdir)

            # Omit manifest.json
            mod_zip_path = Path(tmpdir) / "omitted.zip"
            with (
                zipfile.ZipFile(res_pkg.package_path, "r") as z_in,
                zipfile.ZipFile(mod_zip_path, "w") as z_out,
            ):
                for item in z_in.infolist():
                    if item.filename != "manifest.json":
                        z_out.writestr(item, z_in.read(item.filename))

            res = replay_bundle(mod_zip_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "DIVERGENCE"
            assert any("missing from package" in r for r in res.divergence_reasons)

    def test_path_traversal_entry_produces_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_zip_path = Path(tmpdir) / "path_traversal.zip"
            with zipfile.ZipFile(bad_zip_path, "w") as z_out:
                z_out.writestr("../../etc/passwd", b"root:x:0:0")

            res = replay_bundle(bad_zip_path, expected_release_digest="a" * 64)
            assert res.status == "DIVERGENCE"
            assert any("path traversal" in r.lower() for r in res.divergence_reasons)

    def test_modify_manifest_and_recompute_internal_bundle_hashes_diverges_against_external_expected_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-reseal-1")
            res_pkg = assemble_delivery_package("prod-reseal-1", prod_data, tmpdir)
            original_expected_release_digest = res_pkg.release_evidence_digest

            # Maliciously modify manifest, recompute release_evidence and bundle_index internal hashes, and reseal package
            mod_zip_path = Path(tmpdir) / "resealed_tampered.zip"
            with zipfile.ZipFile(res_pkg.package_path, "r") as z_in:
                manifest_dict = json.loads(z_in.read("manifest.json").decode())
                manifest_dict["entries"][0]["display_name"] = "Malicious Name"
                mod_manifest_bytes = canonical_json_bytes(manifest_dict)

                z_in.read("obligations.json")
                z_in.read("layout_evidence.json")
                z_in.read("visual_observations.json")
                z_in.read("contributor_registry.json")
                z_in.read("authorizations.json")
                z_in.read("proposals.json")

                # Re-build release evidence and bundle_index with tampered manifest
                rel_ev_dict = json.loads(z_in.read("release_evidence.json").decode())
                rel_ev_dict["manifest_hash"] = sha256_bytes_digest(mod_manifest_bytes)
                mod_rel_ev_bytes = canonical_json_bytes(rel_ev_dict)

                bundle_idx_dict = json.loads(z_in.read("bundle_index.json").decode())
                bundle_idx_dict["manifest_hash"] = sha256_bytes_digest(mod_manifest_bytes)
                bundle_idx_dict["files"]["manifest.json"] = sha256_bytes_digest(mod_manifest_bytes)
                bundle_idx_dict["files"]["release_evidence.json"] = sha256_bytes_digest(
                    mod_rel_ev_bytes
                )
                bundle_idx_dict["release_evidence_digest"] = sha256_bytes_digest(mod_rel_ev_bytes)
                mod_bundle_idx_bytes = canonical_json_bytes(bundle_idx_dict)

            with zipfile.ZipFile(res_pkg.package_path, "r") as z_in, zipfile.ZipFile(mod_zip_path, "w") as z_out:
                for item in z_in.infolist():
                    name = item.filename
                    if name == "manifest.json":
                        z_out.writestr(name, mod_manifest_bytes)
                    elif name == "release_evidence.json":
                        z_out.writestr(name, mod_rel_ev_bytes)
                    elif name == "bundle_index.json":
                        z_out.writestr(name, mod_bundle_idx_bytes)
                    else:
                        z_out.writestr(name, z_in.read(name))

            res = replay_bundle(
                mod_zip_path, expected_release_digest=original_expected_release_digest
            )
            assert res.status == "DIVERGENCE"
            assert any("digest mismatch" in r for r in res.divergence_reasons)

    def test_valid_untouched_bundle_with_correct_expected_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-correct-digest-1")
            res_pkg = assemble_delivery_package("prod-correct-digest-1", prod_data, tmpdir)

            res = replay_bundle(res_pkg.package_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "MATCH"
            assert res.divergence_reasons == []

    def test_valid_bundle_with_wrong_expected_digest_diverges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-wrong-digest-1")
            res_pkg = assemble_delivery_package("prod-wrong-digest-1", prod_data, tmpdir)

            wrong_digest = "0" * 64
            res = replay_bundle(res_pkg.package_path, expected_release_digest=wrong_digest)
            assert res.status == "DIVERGENCE"
            assert any("External release digest mismatch" in r for r in res.divergence_reasons)

    def test_altered_authorizations_or_proposals_produces_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-auth-mod-1")
            res_pkg = assemble_delivery_package("prod-auth-mod-1", prod_data, tmpdir)

            mod_zip_path = Path(tmpdir) / "mod_auth.zip"
            with (
                zipfile.ZipFile(res_pkg.package_path, "r") as z_in,
                zipfile.ZipFile(mod_zip_path, "w") as z_out,
            ):
                for item in z_in.infolist():
                    data = z_in.read(item.filename)
                    if item.filename == "authorizations.json":
                        data = b'[{"authorization_id": "forged"}]'
                    z_out.writestr(item, data)

            res = replay_bundle(mod_zip_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "DIVERGENCE"
            assert any("hash divergence" in r or "mismatch" in r for r in res.divergence_reasons)

    def test_altered_findings_or_final_gate_produces_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prod_data = _make_production_data("prod-gate-mod-1")
            res_pkg = assemble_delivery_package("prod-gate-mod-1", prod_data, tmpdir)

            mod_zip_path = Path(tmpdir) / "mod_gate.zip"
            with zipfile.ZipFile(res_pkg.package_path, "r") as z_in:
                rel_ev_dict = json.loads(z_in.read("release_evidence.json").decode())
                rel_ev_dict["final_gate_state"] = "BLOCKED"
                mod_rel_bytes = canonical_json_bytes(rel_ev_dict)

                bundle_idx_dict = json.loads(z_in.read("bundle_index.json").decode())
                bundle_idx_dict["expected_gate_state"] = "BLOCKED"
                bundle_idx_dict["files"]["release_evidence.json"] = sha256_bytes_digest(
                    mod_rel_bytes
                )
                mod_bundle_idx_bytes = canonical_json_bytes(bundle_idx_dict)

            with (
                zipfile.ZipFile(res_pkg.package_path, "r") as z_in,
                zipfile.ZipFile(mod_zip_path, "w") as z_out,
            ):
                for item in z_in.infolist():
                    if item.filename == "bundle_index.json":
                        z_out.writestr(item, mod_bundle_idx_bytes)
                    elif item.filename == "release_evidence.json":
                        z_out.writestr(item, mod_rel_bytes)
                    else:
                        z_out.writestr(item, z_in.read(item.filename))

            res = replay_bundle(mod_zip_path, expected_release_digest=res_pkg.release_evidence_digest)
            assert res.status == "DIVERGENCE"
            assert any("mismatch" in r or "divergence" in r for r in res.divergence_reasons)

    def test_replay_operates_completely_offline_without_socket_access(self) -> None:
        import socket

        # Monkeypatch socket.socket to fail if any network access is attempted
        def forbidden_socket(*args: object, **kwargs: object) -> object:
            raise RuntimeError("Network socket call forbidden during offline replay")

        old_socket = socket.socket
        socket.socket = forbidden_socket  # type: ignore[assignment]

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                prod_data = _make_production_data("prod-offline-1")
                res_pkg = assemble_delivery_package("prod-offline-1", prod_data, tmpdir)

                res = replay_bundle(res_pkg.package_path, expected_release_digest=res_pkg.release_evidence_digest)
                assert res.status == "MATCH"
                assert res.replayed_gate_state == "READY_TO_EXPORT"
        finally:
            socket.socket = old_socket
