"""
Focused unit regression tests for Firestore-backed export and download persistence defect.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import (
    UninitializedProductionStore,
    set_production_store,
)
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
from creditlock.domain.store import FirestoreProductionStore, InMemoryProductionStore
from creditlock.evidence.replay import replay_bundle
from creditlock.evidence.storage import LocalDirectoryStore
from creditlock.settings import get_settings
from tests.fixtures.dummy_evidence import generate_dummy_evidence
from tests.unit.test_evidence_storage import FakeGCSClient
from tests.unit.test_firestore_store import FakeFirestoreClient, fake_transaction_runner


def _make_production_args(
    prod_id: str = "prod-fs-defect-1",
    auth_timestamp: str = "2026-09-08T12:00:00Z",
) -> dict[str, Any]:
    manifest = CreditManifest(
        manifest_id=f"manifest-{prod_id}",
        production_id=prod_id,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
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
        obligation_id=f"obl-{prod_id}",
        production_id=prod_id,
        credited_party_id="contrib-1",
        required_display_text="Jane Doe",
        role_label="Director",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=8, quote="Jane Doe"),
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
                rendered_element_id="elem-1",
                observation="Clear readable credit card",
                flagged=False,
            )
        ],
    )
    auths = [
        Authorization(
            authorization_id=f"auth-{prod_id}",
            production_id=prod_id,
            proposal_id=f"prop-{prod_id}",
            issue_id=f"issue-{prod_id}",
            proposer_id="reviewer-1",
            actor_id="approver-1",
            role="RELEASE_APPROVER",
            action=AuthorizationAction.CONFIRM_IDENTITY,
            reason="Verified against records",
            selected_contributor_id="contrib-1",
            manifest_hash=sha256_bytes_digest(canonical_json_bytes(manifest.model_dump())),
            obligation_registry_version_hash=sha256_bytes_digest(
                canonical_json_bytes([obligation.model_dump()])
            ),
            artifact_index_digest=sha256_bytes_digest(
                canonical_json_bytes(artifact_index.model_dump())
            ),
            visual_observations_hash=sha256_bytes_digest(
                canonical_json_bytes(visuals.model_dump())
            ),
            authorized_at=auth_timestamp,
        )
    ]
    return {
        "production_id": prod_id,
        "manifest": manifest,
        "obligations": [obligation],
        "layout_evidence": layout_ev,
        "frames": frames,
        "artifact_index": artifact_index,
        "visual_observations": visuals,
        "contributor_registry": [{"contributor_id": "contrib-1", "name": "Jane Doe"}],
        "authorizations": auths,
        "render_profile_version": "1.0",
    }


class TestFirestoreExportPersistenceRegression:
    """Proves the full regression suite required for the export persistence repair."""

    def test_firestore_backed_export_and_fresh_store_download_e2e(
        self, tmp_path: Path
    ) -> None:
        prod_id = "prod-fs-defect-1"
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )

        args = _make_production_args(prod_id)
        store.register(**args)
        set_production_store(store)

        approver_token = create_token("approver_1", "RELEASE_APPROVER", production_id=prod_id)
        client = TestClient(app, raise_server_exceptions=False)
        settings = get_settings()
        fake_gcs = FakeGCSClient()

        with (
            patch.object(settings, "enable_judge_demo", True),
            patch.object(settings, "evidence_bucket", "mock-evidence-bucket"),
            patch("google.cloud.storage.Client", return_value=fake_gcs),
        ):
            # 1. Firestore-backed export returns HTTP 200
            resp = client.post(
                f"/productions/{prod_id}/export",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert resp.status_code == 200, f"Export failed: {resp.text}"
            body = resp.json()
            rel_digest = body["release_digest"]
            delivery_path = body["delivery_package_path"]
            assert len(rel_digest) == 64
            assert delivery_path.startswith("gs://mock-evidence-bucket/")

            # 2. Inspect Firestore document directly
            doc_key = f"production_states/{prod_id}"
            assert doc_key in client_db._store_data, "Production document missing in Firestore"
            stored_doc = client_db._store_data[doc_key]

            # The Firestore document MUST contain release_digest and delivery_package_path
            assert "release_digest" in stored_doc
            assert stored_doc["release_digest"] == rel_digest
            assert "delivery_package_path" in stored_doc
            assert stored_doc["delivery_package_path"] == delivery_path

            # 3. Contains no ZIP or frame binary bytes
            for k, v in stored_doc.items():
                assert not isinstance(v, (bytes, bytearray)), f"Binary bytes stored in field '{k}'"
                if isinstance(v, str):
                    assert not v.startswith("PK\x03\x04"), f"ZIP binary bytes in field '{k}'"

            # 4. Construct fresh FirestoreProductionStore over same database
            fresh_store = FirestoreProductionStore(
                client=client_db,
                transaction_runner=fake_transaction_runner,
                evidence_storage=evidence_storage,
            )
            set_production_store(fresh_store)

            # Confirm fresh store returns the persisted fields
            fresh_prod = fresh_store.get(prod_id)
            assert fresh_prod is not None
            assert fresh_prod.get("release_digest") == rel_digest
            assert fresh_prod.get("delivery_package_path") == delivery_path

            # 5. Subsequent authenticated download through fresh store returns HTTP 200 and exact ZIP bytes
            dl_resp = client.get(
                f"/demo/api/export/download?production_id={prod_id}",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert dl_resp.status_code == 200, f"Download failed with status {dl_resp.status_code}: {dl_resp.text}"
            assert dl_resp.headers["content-type"] == "application/zip"
            zip_bytes = dl_resp.content
            assert len(zip_bytes) > 0

            # 6. Downloaded ZIP passes replay with MATCH against external release digest
            with tempfile.NamedTemporaryFile(suffix=".zip") as tf:
                tf.write(zip_bytes)
                tf.flush()
                replay_res = replay_bundle(tf.name, expected_release_digest=rel_digest)
                assert replay_res.status == "MATCH"
                assert replay_res.replayed_gate_state == "READY_TO_EXPORT"

    def test_identical_repeated_export_is_idempotent(self, tmp_path: Path) -> None:
        prod_id = "prod-fs-idemp-1"
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )

        args = _make_production_args(prod_id)
        store.register(**args)
        set_production_store(store)

        approver_token = create_token("approver_1", "RELEASE_APPROVER", production_id=prod_id)
        client = TestClient(app, raise_server_exceptions=False)
        settings = get_settings()
        fake_gcs = FakeGCSClient()

        with (
            patch.object(settings, "enable_judge_demo", True),
            patch.object(settings, "evidence_bucket", "mock-evidence-bucket"),
            patch("google.cloud.storage.Client", return_value=fake_gcs),
        ):
            # First export call
            resp1 = client.post(
                f"/productions/{prod_id}/export",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert resp1.status_code == 200
            body1 = resp1.json()

            # Second export call (identical repeated export)
            resp2 = client.post(
                f"/productions/{prod_id}/export",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert resp2.status_code == 200
            body2 = resp2.json()

            assert body1["release_digest"] == body2["release_digest"]
            assert body1["delivery_package_path"] == body2["delivery_package_path"]

            doc = client_db._store_data[f"production_states/{prod_id}"]
            assert doc["release_digest"] == body1["release_digest"]
            assert doc["delivery_package_path"] == body1["delivery_package_path"]

    def test_conflicting_export_metadata_fails_closed(self, tmp_path: Path) -> None:
        prod_id = "prod-fs-conflict-1"
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )

        args = _make_production_args(prod_id)
        store.register(**args)

        original_digest = "a" * 64
        original_path = "gs://bucket/deliveries/1.zip"
        store.save_export_result(prod_id, original_digest, original_path)

        # Identical call succeeds idempotently
        store.save_export_result(prod_id, original_digest, original_path)

        # Conflicting digest fails closed
        with pytest.raises(ValueError, match="conflicting export metadata"):
            store.save_export_result(prod_id, "b" * 64, original_path)

        # Conflicting path fails closed
        with pytest.raises(ValueError, match="conflicting export metadata"):
            store.save_export_result(prod_id, original_digest, "gs://bucket/deliveries/2.zip")

        # Stored metadata is unchanged
        doc = client_db._store_data[f"production_states/{prod_id}"]
        assert doc["release_digest"] == original_digest
        assert doc["delivery_package_path"] == original_path

    def test_firestore_persistence_failure_prevents_export_success(self, tmp_path: Path) -> None:
        prod_id = "prod-fs-fail-persist-1"
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )

        args = _make_production_args(prod_id)
        store.register(**args)
        set_production_store(store)

        approver_token = create_token("approver_1", "RELEASE_APPROVER", production_id=prod_id)
        client = TestClient(app, raise_server_exceptions=False)
        settings = get_settings()
        fake_gcs = FakeGCSClient()

        # Simulate Firestore write failure during save_export_result
        def failing_save(p_id: str, r_dig: str, p_path: str) -> None:
            raise RuntimeError("Simulated transient Firestore write failure with secret /path/key.json")

        with (
            patch.object(settings, "enable_judge_demo", True),
            patch.object(settings, "evidence_bucket", "mock-evidence-bucket"),
            patch("google.cloud.storage.Client", return_value=fake_gcs),
            patch.object(store, "save_export_result", side_effect=failing_save),
        ):
            resp = client.post(
                f"/productions/{prod_id}/export",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            # Export must fail closed with HTTP 500 and NOT claim success
            assert resp.status_code == 500
            error_detail = resp.json().get("detail", "")
            # Ensure error detail is sanitized
            assert error_detail == "Durable export metadata persistence failed."
            assert "secret" not in error_detail
            assert "key.json" not in error_detail

    def test_missing_export_metadata_before_export_returns_404(self, tmp_path: Path) -> None:
        prod_id = "prod-fs-not-exported-yet"
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )

        args = _make_production_args(prod_id)
        store.register(**args)
        set_production_store(store)

        approver_token = create_token("approver_1", "RELEASE_APPROVER", production_id=prod_id)
        client = TestClient(app, raise_server_exceptions=False)
        settings = get_settings()

        with patch.object(settings, "enable_judge_demo", True):
            dl_resp = client.get(
                f"/demo/api/export/download?production_id={prod_id}",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert dl_resp.status_code == 404
            assert "No exported delivery package found" in dl_resp.text

    def test_store_contract_input_validation(self, tmp_path: Path) -> None:
        client_db = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path / "evidence")
        fs_store = FirestoreProductionStore(
            client=client_db,
            transaction_runner=fake_transaction_runner,
            evidence_storage=evidence_storage,
        )
        mem_store = InMemoryProductionStore()
        uninit_store = UninitializedProductionStore()

        valid_digest = "f" * 64
        valid_path = "gs://bucket/deliveries/valid.zip"

        # 1. UninitializedProductionStore fails closed
        with pytest.raises(RuntimeError, match="uninitialized"):
            uninit_store.save_export_result("p1", valid_digest, valid_path)

        # 2. InMemoryProductionStore input validation
        # Non-existent production
        with pytest.raises(KeyError, match="not found"):
            mem_store.save_export_result("nonexistent", valid_digest, valid_path)

        # Invalid release_digest: uppercase, wrong length, non-hex
        with pytest.raises(ValueError, match="release_digest"):
            mem_store.save_export_result("p1", "F" * 64, valid_path)
        with pytest.raises(ValueError, match="release_digest"):
            mem_store.save_export_result("p1", "f" * 63, valid_path)
        with pytest.raises(ValueError, match="release_digest"):
            mem_store.save_export_result("p1", "z" * 64, valid_path)

        # Invalid delivery_package_path: empty, whitespace
        with pytest.raises(ValueError, match="delivery_package_path"):
            mem_store.save_export_result("p1", valid_digest, "")
        with pytest.raises(ValueError, match="delivery_package_path"):
            mem_store.save_export_result("p1", valid_digest, "   ")

        # 3. FirestoreProductionStore input validation
        # Non-existent production in Firestore
        with pytest.raises(KeyError, match="not found"):
            fs_store.save_export_result("nonexistent", valid_digest, valid_path)

        with pytest.raises(ValueError, match="release_digest"):
            fs_store.save_export_result("p1", "F" * 64, valid_path)
        with pytest.raises(ValueError, match="delivery_package_path"):
            fs_store.save_export_result("p1", valid_digest, "")

    def test_in_memory_production_store_idempotency_and_conflict(self) -> None:
        mem_store = InMemoryProductionStore()
        prod_id = "prod-mem-1"
        args = _make_production_args(prod_id)
        # Register in memory store
        mem_store.register(
            production_id=prod_id,
            obligations=args["obligations"],
            manifest=args["manifest"],
            layout_evidence=args["layout_evidence"],
            visual_observations=args["visual_observations"],
            contributor_registry=args["contributor_registry"],
            authorizations=args["authorizations"],
            frames=args["frames"],
            artifact_index=args["artifact_index"],
        )

        digest = "c" * 64
        path = "/tmp/pkg.zip"

        # First save
        mem_store.save_export_result(prod_id, digest, path)
        prod = mem_store.get(prod_id)
        assert prod is not None
        assert prod["release_digest"] == digest
        assert prod["delivery_package_path"] == path

        # Idempotent retry
        mem_store.save_export_result(prod_id, digest, path)
        assert mem_store.get(prod_id)["release_digest"] == digest

        # Conflicting attempt fails closed
        with pytest.raises(ValueError, match="conflicting export metadata"):
            mem_store.save_export_result(prod_id, "d" * 64, path)

        with pytest.raises(ValueError, match="conflicting export metadata"):
            mem_store.save_export_result(prod_id, digest, "/tmp/other.zip")

        assert mem_store.get(prod_id)["release_digest"] == digest
