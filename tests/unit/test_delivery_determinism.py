"""
Tests for deterministic delivery packaging and repeatable export persistence (P1-R1).
"""
from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.models import (
    AbsolutePosition,
    Authorization,
    AuthorizationAction,
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
from creditlock.settings import get_settings
from tests.fixtures.dummy_evidence import generate_dummy_evidence
from tests.unit.test_evidence_storage import FakeGCSClient


def _make_production_data(
    prod_id: str = "prod-det-1",
    auth_timestamp: str | None = "2026-09-08T12:00:00Z",
) -> dict[str, Any]:
    manifest = CreditManifest(
        manifest_id="manifest-det-1",
        production_id=prod_id,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-det-1",
                contributor_id="contrib-det-1",
                display_name="Jane Doe",
                role="Director",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=prod_id,
                delivery_version_id="v1",
            )
        ],
    )
    obligation = Obligation(
        obligation_id="obl-det-1",
        production_id=prod_id,
        credited_party_id="contrib-det-1",
        required_display_text="Jane Doe",
        role_label="Director",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-det-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=8, quote="Jane Doe"),
        source_hash="hash-det-1",
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
                rendered_element_id="elem-det-1",
                observation="Clear readable credit card",
                flagged=False,
            )
        ],
    )

    auths: list[Authorization] = []
    if auth_timestamp:
        auths.append(
            Authorization(
                authorization_id="auth-det-1",
                production_id=prod_id,
                proposal_id="prop-det-1",
                issue_id="issue-det-1",
                proposer_id="reviewer-1",
                actor_id="approver-1",
                role="RELEASE_APPROVER",
                action=AuthorizationAction.CONFIRM_IDENTITY,
                reason="Verified against master records",
                selected_contributor_id="contrib-det-1",
                manifest_hash=sha256_bytes_digest(canonical_json_bytes(manifest.model_dump())),
                obligation_registry_version_hash=sha256_bytes_digest(canonical_json_bytes([obligation.model_dump()])),
                artifact_index_digest=sha256_bytes_digest(canonical_json_bytes(artifact_index.model_dump())),
                visual_observations_hash=sha256_bytes_digest(canonical_json_bytes(visuals.model_dump())),
                authorized_at=auth_timestamp,
            )
        )

    return {
        "manifest": manifest,
        "obligations": [obligation],
        "layout_evidence": layout_ev,
        "frames": frames,
        "artifact_index": artifact_index.model_dump(),
        "visual_observations": visuals,
        "contributor_registry": [{"contributor_id": "contrib-det-1", "name": "Jane Doe"}],
        "identity_bindings": {},
        "authorizations": auths,
        "proposals": [],
        "render_profile_version": "1.0",
        "projection": None,
    }


def test_assemble_same_snapshot_twice_produces_identical_zip_bytes_and_sha256() -> None:
    """
    Assemble the same snapshot twice into different directories and assert
    identical ZIP bytes and SHA-256.
    """
    prod_data = _make_production_data("prod-det-1", auth_timestamp="2026-09-08T12:00:00Z")

    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        res1 = assemble_delivery_package("prod-det-1", prod_data, tmpdir1)
        zip_path_1 = Path(res1.package_path)
        assert zip_path_1.exists()
        zip_bytes_1 = zip_path_1.read_bytes()
        digest_1 = res1.release_evidence_digest

        # Introduce a delay so any live timestamp call would diverge
        time.sleep(0.05)

        res2 = assemble_delivery_package("prod-det-1", prod_data, tmpdir2)
        zip_path_2 = Path(res2.package_path)
        assert zip_path_2.exists()
        zip_bytes_2 = zip_path_2.read_bytes()
        digest_2 = res2.release_evidence_digest

        # 1. First zip bytes were captured prior to second assembly
        assert len(zip_bytes_1) > 0
        assert len(zip_bytes_2) > 0

        # 2. Release digests match
        assert digest_1 == digest_2

        # 3. Exact byte equality and sha256 equality
        assert sha256_bytes_digest(zip_bytes_1) == sha256_bytes_digest(zip_bytes_2)
        assert zip_bytes_1 == zip_bytes_2

        # 4. Verify explicit ZipInfo metadata: fixed timestamp, sorted order, permissions, platform marker
        with zipfile.ZipFile(zip_path_1, "r") as zf:
            infolist = zf.infolist()
            filenames = [info.filename for info in infolist]
            assert filenames == sorted(filenames), "ZIP members are not in sorted order"

            for info in infolist:
                assert info.date_time == (1980, 1, 1, 0, 0, 0), (
                    f"Member {info.filename} has non-fixed timestamp: {info.date_time}"
                )
                assert info.create_system == 3, f"Member {info.filename} platform marker is not UNIX (3)"
                assert (info.external_attr >> 16) & 0o777 == 0o644, (
                    f"Member {info.filename} permissions not 0o644: {oct((info.external_attr >> 16) & 0o777)}"
                )
                assert info.compress_type == zipfile.ZIP_DEFLATED


def test_assemble_without_authorizations_uses_deterministic_fallback() -> None:
    """
    Assemble without authorizations and verify it deterministically uses fallback.
    """
    prod_data = _make_production_data("prod-det-fallback", auth_timestamp=None)

    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        res1 = assemble_delivery_package("prod-det-fallback", prod_data, tmpdir1)
        zip_bytes_1 = Path(res1.package_path).read_bytes()

        time.sleep(0.05)

        res2 = assemble_delivery_package("prod-det-fallback", prod_data, tmpdir2)
        zip_bytes_2 = Path(res2.package_path).read_bytes()

        assert zip_bytes_1 == zip_bytes_2
        assert sha256_bytes_digest(zip_bytes_1) == sha256_bytes_digest(zip_bytes_2)


def test_call_export_twice_with_mock_evidence_storage_succeeds_both_times() -> None:
    """
    Call the same export twice with local/mock evidence storage and assert
    both return 200 with the same release digest, package URI, and stored bytes.
    Ensures first ZIP bytes are captured before second assembly.
    """
    clear_productions()

    prod_id = "prod-export-idemp"
    prod_data = _make_production_data(prod_id, auth_timestamp="2026-09-08T15:30:00Z")

    register_production(
        prod_id,
        prod_data["obligations"],
        prod_data["manifest"],
        layout_evidence=prod_data["layout_evidence"],
        visual_observations=prod_data["visual_observations"],
        contributor_registry=prod_data["contributor_registry"],
        frames=prod_data["frames"],
        artifact_index=prod_data["artifact_index"],
        render_profile_version="1.0",
        authorizations=prod_data["authorizations"],
    )

    approver_token = create_token("approver_1", "RELEASE_APPROVER", production_id=prod_id)
    fake_client = FakeGCSClient()
    client = TestClient(app, raise_server_exceptions=False)

    settings = get_settings()
    with (
        patch.object(settings, "evidence_bucket", "mock-evidence-bucket"),
        patch("google.cloud.storage.Client", return_value=fake_client),
    ):
        # First export call
        resp1 = client.post(
            f"/productions/{prod_id}/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp1.status_code == 200, resp1.text
        body1 = resp1.json()

        digest_1 = body1["release_digest"]
        uri_1 = body1["delivery_package_path"]
        assert uri_1.startswith("gs://mock-evidence-bucket/")

        # CAPTURE the first ZIP bytes before second assembly
        gcs_rel_path = f"deliveries/{prod_id}/{digest_1}.zip"
        bucket = fake_client.bucket("mock-evidence-bucket")
        assert gcs_rel_path in bucket._bucket_data, "Delivery package was not stored in mock GCS"
        stored_bytes_1 = bytes(bucket._bucket_data[gcs_rel_path])
        assert len(stored_bytes_1) > 0

        # Small delay
        time.sleep(0.05)

        # Second export call (identical request)
        resp2 = client.post(
            f"/productions/{prod_id}/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()

        digest_2 = body2["release_digest"]
        uri_2 = body2["delivery_package_path"]
        stored_bytes_2 = bytes(bucket._bucket_data[gcs_rel_path])

        # Assertions
        assert digest_1 == digest_2
        assert uri_1 == uri_2
        assert stored_bytes_1 == stored_bytes_2
        assert sha256_bytes_digest(stored_bytes_1) == sha256_bytes_digest(stored_bytes_2)
