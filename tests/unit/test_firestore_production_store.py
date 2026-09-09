"""
Adversarial contract and security regression tests for FirestoreProductionStore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.domain.gate import ProjectionStatus
from creditlock.domain.models import (
    AuthorizationAction,
    CreditManifest,
    Obligation,
    Proposal,
)
from creditlock.domain.store import FirestoreProductionStore
from creditlock.evidence.models import ArtifactIndex
from creditlock.evidence.storage import LocalDirectoryStore, StorageCollisionError
from creditlock.renderer.models import FrameElement, FrameEvidence
from creditlock.renderer.render import _create_synthetic_png_bytes
from tests.unit.test_firestore_store import FakeDocRef, FakeFirestoreClient, fake_transaction_runner


def _sample_production_args(prod_id: str = "prod-fs-contract") -> dict[str, Any]:
    obl = Obligation(
        obligation_id="obl-1",
        production_id=prod_id,
        status="CANDIDATE",
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        credited_party_id="p1",
        required_display_text="Director Alice Smith",
        role_label="Director",
        credit_surface="END_CARDS",
        card_type="SOLO",
        source_document_id="doc-1",
        source_document_version=1,
        source_hash="h1",
        agent_reported_confidence=0.9,
        source_span={"quote": "Director Alice Smith", "start_char": 0, "end_char": 20},
    )
    manifest = CreditManifest(
        manifest_id="man-1",
        production_id=prod_id,
        delivery_version_id="v1",
        entries=[],
    )
    return {
        "production_id": prod_id,
        "obligations": [obl],
        "manifest": manifest,
        "contributor_registry": [{"id": "p1", "name": "Alice Smith"}],
        "render_profile_version": "v1",
    }


def _sample_frame(frame_index: int = 0, frame_id: str = "frame-0") -> FrameEvidence:
    png_bytes = _create_synthetic_png_bytes(1920, 1080, f"Frame {frame_index}")
    image_hash = sha256_bytes_digest(png_bytes)
    return FrameEvidence(
        frame_index=frame_index,
        frame_id=frame_id,
        image_bytes=png_bytes,
        image_hash=image_hash,
        width=1920,
        height=1080,
        elements=[
            FrameElement(
                element_id=f"elem-{frame_index}",
                text=f"Frame {frame_index}",
                computed_font_size_px=24.0,
                bounding_box={"x": 100.0, "y": 100.0, "width": 200.0, "height": 50.0},
            )
        ],
    )


class TestFirestoreProductionStoreContract:
    def test_clear_removes_destructive_clear_and_raises_runtimeerror(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)

        store.register(**_sample_production_args("prod-1"))
        store.register(**_sample_production_args("prod-2"))

        # Save proposal for prod-1
        prop = Proposal(
            proposal_id="prop-clear-1",
            production_id="prod-1",
            issue_id="iss-1",
            proposer_id="user-1",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Testing clear failure",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        store.save_proposal(prop)

        # clear() must fail closed with RuntimeError and perform zero deletions
        with pytest.raises(RuntimeError, match="Global Firestore clearing is disabled"):
            store.clear()

        # Assert both production states and subcollections remain intact
        assert store.get("prod-1") is not None
        assert store.get("prod-2") is not None
        props = store.get_proposals("prod-1")
        assert len(props) == 1
        assert props[0].proposal_id == "prop-clear-1"

    def test_save_proposal_is_transactional(self) -> None:
        client = FakeFirestoreClient()
        runner_called = False

        def tracking_runner(cl: Any, txn_func: Any) -> Any:
            nonlocal runner_called
            runner_called = True
            txn = cl.transaction()
            res = txn_func(txn)
            txn.commit()
            return res

        store = FirestoreProductionStore(client=client, transaction_runner=tracking_runner)
        store.register(**_sample_production_args())

        prop = Proposal(
            proposal_id="prop-txn-1",
            production_id="prod-fs-contract",
            issue_id="iss-1",
            proposer_id="user-1",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Testing transactional save",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )

        store.save_proposal(prop)
        assert runner_called is True
        props = store.get_proposals("prod-fs-contract")
        assert len(props) == 1

    def test_save_proposal_missing_production_raises_keyerror_and_zero_writes(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)

        prop = Proposal(
            proposal_id="prop-missing",
            production_id="prod-nonexistent",
            issue_id="iss-1",
            proposer_id="user-1",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Testing missing prod",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        with pytest.raises(KeyError, match="not registered"):
            store.save_proposal(prop)

        # Zero writes to proposals subcollection
        assert "production_states/prod-nonexistent/proposals/prop-missing" not in client._store_data

    def test_save_proposal_identical_retry_creates_one_document(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)
        store.register(**_sample_production_args())

        prop1 = Proposal(
            proposal_id="prop-1",
            production_id="prod-fs-contract",
            issue_id="iss-1",
            proposer_id="user-1",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Reason 1",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        store.save_proposal(prop1)

        # Identical save succeeds idempotently
        store.save_proposal(prop1)
        proposals = store.get_proposals("prod-fs-contract")
        assert len(proposals) == 1

    def test_save_proposal_competing_proposals_same_id_conflict(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)
        store.register(**_sample_production_args())

        prop1 = Proposal(
            proposal_id="prop-1",
            production_id="prod-fs-contract",
            issue_id="iss-1",
            proposer_id="user-1",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Reason 1",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        store.save_proposal(prop1)

        # Conflicting reuse of same proposal_id raises ValueError
        prop2 = Proposal(
            proposal_id="prop-1",
            production_id="prod-fs-contract",
            issue_id="iss-1",
            proposer_id="user-2",  # Different proposer
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Reason 2",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="already exists with different data"):
            store.save_proposal(prop2)

    def test_firestore_state_stores_frame_metadata_never_raw_png_bytes(
        self, tmp_path: Path
    ) -> None:
        client = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path)
        store = FirestoreProductionStore(client=client, evidence_storage=evidence_storage)

        frame0 = _sample_frame(0, "frame-0")
        frame1 = _sample_frame(1, "frame-1")
        art_index = ArtifactIndex(
            manifest_hash="mhash123",
            render_profile_version="v1",
            frames=[
                {"frame_id": "frame-0", "sha256": frame0.image_hash},
                {"frame_id": "frame-1", "sha256": frame1.image_hash},
            ],
            layout_evidence_hash="lhash123",
        )

        args = _sample_production_args("prod-persist-frames")
        args["frames"] = [frame0, frame1]
        args["artifact_index"] = art_index
        store.register(**args)

        # Inspect raw document in FakeFirestoreClient
        raw_doc = client._store_data["production_states/prod-persist-frames"]
        assert "frames" in raw_doc
        assert len(raw_doc["frames"]) == 2

        for idx, f_meta in enumerate(raw_doc["frames"]):
            assert f_meta["frame_index"] == idx
            assert f_meta["frame_id"] == f"frame-{idx}"
            assert "storage_path" in f_meta
            assert "image_hash" in f_meta
            assert "width" in f_meta
            assert "height" in f_meta
            assert "elements" in f_meta
            # Must NEVER store raw image_bytes in Firestore document
            assert "image_bytes" not in f_meta
            assert "png_bytes" not in f_meta

            # Verify the storage path contains production_id, frame_id, and hash
            assert "prod-persist-frames" in f_meta["storage_path"]
            assert f_meta["frame_id"] in f_meta["storage_path"]
            assert f_meta["image_hash"] in f_meta["storage_path"]

            # Verify bytes exist in LocalDirectoryStore
            stored_bytes = evidence_storage.get(f_meta["storage_path"])
            assert stored_bytes is not None
            assert sha256_bytes_digest(stored_bytes) == f_meta["image_hash"]

    def test_fresh_store_rehydrates_exact_frame_bytes_and_metadata(
        self, tmp_path: Path
    ) -> None:
        client = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path)
        store1 = FirestoreProductionStore(client=client, evidence_storage=evidence_storage)

        frame0 = _sample_frame(0, "frame-0")
        frame1 = _sample_frame(1, "frame-1")
        art_index = ArtifactIndex(
            manifest_hash="mhash123",
            render_profile_version="v1",
            frames=[
                {"frame_id": "frame-0", "sha256": frame0.image_hash},
                {"frame_id": "frame-1", "sha256": frame1.image_hash},
            ],
            layout_evidence_hash="lhash123",
        )

        args = _sample_production_args("prod-rehydrate-frames")
        args["frames"] = [frame0, frame1]
        args["artifact_index"] = art_index
        store1.register(**args)

        # A fresh store instance rehydrates through evidence storage
        store2 = FirestoreProductionStore(client=client, evidence_storage=evidence_storage)
        state = store2.get("prod-rehydrate-frames")
        assert state is not None
        rehydrated_frames = state["frames"]
        assert len(rehydrated_frames) == 2
        for idx, (orig, rehydrated) in enumerate(zip([frame0, frame1], rehydrated_frames, strict=True)):
            assert isinstance(rehydrated, FrameEvidence)
            assert rehydrated.frame_index == orig.frame_index == idx
            assert rehydrated.frame_id == orig.frame_id
            assert rehydrated.image_bytes == orig.image_bytes
            assert rehydrated.image_hash == orig.image_hash
            assert rehydrated.width == orig.width == 1920
            assert rehydrated.height == orig.height == 1080
            assert len(rehydrated.elements) == len(orig.elements)
            assert rehydrated.elements[0].element_id == orig.elements[0].element_id
            assert rehydrated.elements[0].text == orig.elements[0].text

    def test_missing_or_tampered_stored_frame_data_fails_closed(
        self, tmp_path: Path
    ) -> None:
        client = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path)
        store = FirestoreProductionStore(client=client, evidence_storage=evidence_storage)

        frame0 = _sample_frame(0, "frame-0")
        art_index = ArtifactIndex(
            manifest_hash="mhash123",
            render_profile_version="v1",
            frames=[{"frame_id": "frame-0", "sha256": frame0.image_hash}],
            layout_evidence_hash="lhash123",
        )

        args = _sample_production_args("prod-tamper-frames")
        args["frames"] = [frame0]
        args["artifact_index"] = art_index
        store.register(**args)

        raw_doc = client._store_data["production_states/prod-tamper-frames"]
        stored_path = raw_doc["frames"][0]["storage_path"]
        target_file = tmp_path / stored_path

        # Case A: Missing object in storage fails closed on get()
        target_file.unlink()
        with pytest.raises(ValueError, match="missing|Malformed"):
            store.get("prod-tamper-frames")

        # Case B: Tampered bytes in storage fails closed on get()
        corrupt_bytes = _create_synthetic_png_bytes(1920, 1080, "Tampered Content")
        target_file.write_bytes(corrupt_bytes)
        with pytest.raises(ValueError, match="Corrupt|digest|mismatch|Malformed"):
            store.get("prod-tamper-frames")

        # Case C: Tampered metadata in Firestore document fails closed on get()
        target_file.write_bytes(frame0.image_bytes)
        raw_doc["frames"][0]["image_hash"] = "0" * 64
        with pytest.raises(ValueError, match="Corrupt|digest|mismatch|Malformed"):
            store.get("prod-tamper-frames")

    def test_no_usable_evidence_storage_with_nonempty_frames_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from creditlock.settings import Settings

        # Ensure no bucket configured in settings
        monkeypatch.setattr(
            "creditlock.settings.get_settings",
            lambda: Settings(evidence_bucket="", google_cloud_project="test-proj"),
        )
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, evidence_storage=None)

        frame0 = _sample_frame(0, "frame-0")
        args = _sample_production_args("prod-no-storage")
        args["frames"] = [frame0]

        with pytest.raises(ValueError, match="evidence"):
            store.register(**args)

        # Zero writes to Firestore
        assert "production_states/prod-no-storage" not in client._store_data
        assert store.get("prod-no-storage") is None

    def test_identical_registration_retry_safe_and_divergent_content_fails(
        self, tmp_path: Path
    ) -> None:
        client = FakeFirestoreClient()
        evidence_storage = LocalDirectoryStore(tmp_path)
        store = FirestoreProductionStore(client=client, evidence_storage=evidence_storage)

        frame0 = _sample_frame(0, "frame-0")
        art_index = ArtifactIndex(
            manifest_hash="mhash123",
            render_profile_version="v1",
            frames=[{"frame_id": "frame-0", "sha256": frame0.image_hash}],
            layout_evidence_hash="lhash123",
        )

        args = _sample_production_args("prod-retry-frames")
        args["frames"] = [frame0]
        args["artifact_index"] = art_index

        # First registration succeeds
        store.register(**args)
        state1 = store.get("prod-retry-frames")
        assert state1 is not None
        assert len(state1["frames"]) == 1

        # Identical registration retry succeeds idempotently
        store.register(**args)
        state2 = store.get("prod-retry-frames")
        assert state2 is not None
        assert len(state2["frames"]) == 1
        assert state2["frames"][0].image_bytes == frame0.image_bytes

        # Divergent frame content with conflicting data fails closed and does not overwrite
        divergent_png = _create_synthetic_png_bytes(1920, 1080, "Divergent Content")
        divergent_frame = FrameEvidence(
            frame_index=0,
            frame_id="frame-0",
            image_bytes=divergent_png,
            image_hash=frame0.image_hash,  # Claiming frame0's hash but supplying different bytes
            width=1920,
            height=1080,
            elements=[],
        )
        args_conflict = _sample_production_args("prod-retry-frames")
        args_conflict["frames"] = [divergent_frame]
        args_conflict["artifact_index"] = art_index

        with pytest.raises((ValueError, StorageCollisionError)):
            store.register(**args_conflict)

        # Verify stored evidence was not overwritten
        re_read = store.get("prod-retry-frames")
        assert re_read is not None
        assert re_read["frames"][0].image_bytes == frame0.image_bytes

    def test_no_frame_behavior_preserved_for_states_without_frames(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client)
        args = _sample_production_args("prod-no-frames")
        args["frames"] = None
        store.register(**args)

        state = store.get("prod-no-frames")
        assert state is not None
        assert state["frames"] == []


    def test_validate_every_persisted_structured_field(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client)
        args = _sample_production_args("prod-struct-val")

        # 1. Valid ArtifactIndex
        art_index = ArtifactIndex(
            manifest_hash="mhash123",
            render_profile_version="v1",
            frames=[{"frame_id": "f1", "sha256": "hash1"}],
            layout_evidence_hash="lhash123",
        )
        args["artifact_index"] = art_index
        store.register(**args)

        state = store.get("prod-struct-val")
        assert state is not None
        assert isinstance(state["artifact_index"], ArtifactIndex)
        assert state["artifact_index"].manifest_hash == "mhash123"
        assert state["artifact_index_digest"] is not None

        # 2. Reject malformed ArtifactIndex at register
        args["artifact_index"] = {"invalid": "missing_required_fields"}
        with pytest.raises(ValueError, match="invalid ArtifactIndex"):
            store.register(**args)

        # 3. Reject malformed contributor_registry at register
        args["artifact_index"] = art_index
        args["contributor_registry"] = "not_a_list"  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="contributor_registry must be a list"):
            store.register(**args)

        # 4. Reject malformed render_profile_version at register
        args["contributor_registry"] = [{"id": "p1", "name": "Alice"}]
        args["render_profile_version"] = "   "
        with pytest.raises(ValueError, match="render_profile_version must be a non-empty string"):
            store.register(**args)

        # 5. Reject document with None or non-dict body at get boundary
        client._store_data["production_states/prod-corrupt-1"] = None  # type: ignore[assignment]

        client._store_data["production_states/prod-corrupt-2"] = {"some": "data"}
        store._client = client
        with pytest.raises(ValueError, match="Malformed production state"):
            client._store_data["production_states/prod-corrupt-3"] = "not_a_dict"  # type: ignore[assignment]
            store.get("prod-corrupt-3")

    def test_phase1a2_rehydration_contract_red_cases(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)

        def _valid_persisted_dict(prod_id: str) -> dict[str, Any]:
            obl = Obligation(
                obligation_id="obl-1",
                production_id=prod_id,
                status="ACTIVE",
                extraction_model_id="m1",
                prompt_version="v1",
                credited_party_id="p1",
                required_display_text="Alice",
                role_label="Director",
                credit_surface="END_CARDS",
                card_type="SOLO",
                source_document_id="doc1",
                source_document_version=1,
                source_hash="h1",
                agent_reported_confidence=0.9,
                source_span={"quote": "Alice", "start_char": 0, "end_char": 5},
            )
            manifest = CreditManifest(
                manifest_id="m1",
                production_id=prod_id,
                delivery_version_id="v1",
                entries=[],
            )
            return {
                "obligations": [obl.model_dump()],
                "manifest": manifest.model_dump(),
                "layout_evidence": None,
                "visual_observations": None,
                "contributor_registry": [],
                "projection": {"artifact_pending": False, "stale_manifest": False},
                "artifact_index": None,
                "artifact_index_digest": None,
                "render_profile_version": "v1",
            }

        # 1. projection = "not-a-projection"
        d1 = _valid_persisted_dict("prod-red-1")
        d1["projection"] = "not-a-projection"
        client._store_data["production_states/prod-red-1"] = d1
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-1")

        # 2. projection booleans "false" and 0
        d2a = _valid_persisted_dict("prod-red-2a")
        d2a["projection"] = {"artifact_pending": "false", "stale_manifest": False}
        client._store_data["production_states/prod-red-2a"] = d2a
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-2a")

        d2b = _valid_persisted_dict("prod-red-2b")
        d2b["projection"] = {"artifact_pending": 0, "stale_manifest": False}
        client._store_data["production_states/prod-red-2b"] = d2b
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-2b")

        # 3. projection missing a key or containing an extra key
        d3a = _valid_persisted_dict("prod-red-3a")
        d3a["projection"] = {"artifact_pending": False}
        client._store_data["production_states/prod-red-3a"] = d3a
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-3a")

        d3b = _valid_persisted_dict("prod-red-3b")
        d3b["projection"] = {"artifact_pending": False, "stale_manifest": False, "extra": True}
        client._store_data["production_states/prod-red-3b"] = d3b
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-3b")

        # 4. contributor_registry = "not-a-list"
        d4 = _valid_persisted_dict("prod-red-4")
        d4["contributor_registry"] = "not-a-list"
        client._store_data["production_states/prod-red-4"] = d4
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-4")

        # 5. contributor_registry containing a non-dict element
        d5 = _valid_persisted_dict("prod-red-5")
        d5["contributor_registry"] = ["not-a-dict"]
        client._store_data["production_states/prod-red-5"] = d5
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-5")

        # 6. missing render_profile_version
        d6 = _valid_persisted_dict("prod-red-6")
        del d6["render_profile_version"]
        client._store_data["production_states/prod-red-6"] = d6
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-6")

        # 7. blank render_profile_version
        d7 = _valid_persisted_dict("prod-red-7")
        d7["render_profile_version"] = "   "
        client._store_data["production_states/prod-red-7"] = d7
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-7")

        # 8. ArtifactIndex with missing digest
        d8 = _valid_persisted_dict("prod-red-8")
        art_index = ArtifactIndex(
            manifest_hash="mhash",
            render_profile_version="v1",
            frames=[],
            layout_evidence_hash="lhash",
        )
        d8["artifact_index"] = art_index.model_dump()
        d8["artifact_index_digest"] = None
        client._store_data["production_states/prod-red-8"] = d8
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-8")

        # 9. digest without ArtifactIndex
        d9 = _valid_persisted_dict("prod-red-9")
        d9["artifact_index"] = None
        d9["artifact_index_digest"] = "a" * 64
        client._store_data["production_states/prod-red-9"] = d9
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-9")

        # 10. digest mismatch
        d10 = _valid_persisted_dict("prod-red-10")
        d10["artifact_index"] = art_index.model_dump()
        d10["artifact_index_digest"] = "b" * 64
        client._store_data["production_states/prod-red-10"] = d10
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-10")

        # 11. falsey malformed layout_evidence {}
        d11 = _valid_persisted_dict("prod-red-11")
        d11["layout_evidence"] = {}
        client._store_data["production_states/prod-red-11"] = d11
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-11")

        # 12. falsey malformed visual_observations {}
        d12 = _valid_persisted_dict("prod-red-12")
        d12["visual_observations"] = {}
        client._store_data["production_states/prod-red-12"] = d12
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-12")

        # 13. proposal subcollection document with a non-dict body
        d13 = _valid_persisted_dict("prod-red-13")
        client._store_data["production_states/prod-red-13"] = d13
        client._store_data["production_states/prod-red-13/proposals/prop-1"] = "not-a-dict"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-13")

        # 14. authorization subcollection document with an empty dictionary
        d14 = _valid_persisted_dict("prod-red-14")
        client._store_data["production_states/prod-red-14"] = d14
        client._store_data["production_states/prod-red-14/authorizations/auth-1"] = {}
        with pytest.raises(ValueError, match="Malformed production state"):
            store.get("prod-red-14")

    def test_phase1a3_validate_projection_before_persistence(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProductionStore(client=client)

        corrupt_projections: list[tuple[str, Any]] = [
            ("prod-proj-1", {"artifact_pending": "false", "stale_manifest": False}),
            ("prod-proj-2", {"artifact_pending": 0, "stale_manifest": False}),
            ("prod-proj-3", {"artifact_pending": False}),
            ("prod-proj-4", {"artifact_pending": False, "stale_manifest": False, "extra": True}),
            ("prod-proj-5", "not-a-projection"),
            ("prod-proj-6", ProjectionStatus(artifact_pending="false", stale_manifest=False)),
        ]

        for prod_id, bad_proj in corrupt_projections:
            args = _sample_production_args(prod_id)
            args["projection"] = bad_proj
            with pytest.raises(ValueError, match="projection"):
                store.register(**args)
            assert f"production_states/{prod_id}" not in client._store_data, f"Document was persisted for bad projection in {prod_id}"

        # Valid cases persist and rehydrate cleanly
        # 1. projection=None
        args_none = _sample_production_args("prod-proj-none")
        args_none["projection"] = None
        store.register(**args_none)
        state_none = store.get("prod-proj-none")
        assert state_none is not None
        assert state_none["projection"] == ProjectionStatus(artifact_pending=False, stale_manifest=False)
        assert client._store_data["production_states/prod-proj-none"]["projection"] == {
            "artifact_pending": False,
            "stale_manifest": False,
        }

        # 2. projection=ProjectionStatus
        args_status = _sample_production_args("prod-proj-status")
        args_status["projection"] = ProjectionStatus(artifact_pending=True, stale_manifest=False)
        store.register(**args_status)
        state_status = store.get("prod-proj-status")
        assert state_status is not None
        assert state_status["projection"] == ProjectionStatus(artifact_pending=True, stale_manifest=False)

        # 3. projection=dict with exact bool fields
        args_dict = _sample_production_args("prod-proj-dict")
        args_dict["projection"] = {"artifact_pending": False, "stale_manifest": True}
        store.register(**args_dict)
        state_dict = store.get("prod-proj-dict")
        assert state_dict is not None
        assert state_dict["projection"] == ProjectionStatus(artifact_pending=False, stale_manifest=True)

    def test_confirm_proposal_contention_normalization_red_cases(self) -> None:
        client = FakeFirestoreClient()
        prod_args = _sample_production_args("prod-contention")
        hashes = ("m1", "o1", "a1", "v1")

        # Setup production and pending proposal
        prop = Proposal(
            proposal_id="prop-contention-1",
            production_id="prod-contention",
            issue_id="iss-1",
            proposer_id="user-proposer",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Testing contention normalization",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )

        # A. Competing confirmation committed, transaction runner raises SDK ValueError
        def competing_runner(cl: Any, txn_func: Any) -> Any:
            # Simulate competing thread committing first
            prop_data = client._store_data.get("production_states/prod-contention/proposals/prop-contention-1")
            if prop_data:
                prop_data["status"] = "CONFIRMED"
            client._store_data["production_states/prod-contention/authorizations/auth-competing"] = {
                "authorization_id": "auth-competing",
                "production_id": "prod-contention",
                "proposal_id": "prop-contention-1",
                "issue_id": "iss-1",
                "proposer_id": "user-proposer",
                "actor_id": "approver-competing",
                "role": "RELEASE_APPROVER",
                "action": "WAIVE_OBLIGATION",
                "reason": "Competing confirmation",
                "manifest_hash": "m1",
                "obligation_registry_version_hash": "o1",
                "artifact_index_digest": "a1",
                "visual_observations_hash": "v1",
                "authorized_at": "2026-08-01T00:00:01Z",
            }
            raise ValueError("Failed to commit transaction in 5 attempts.")

        store_a = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)
        store_a.register(**prod_args)
        store_a.save_proposal(prop)
        store_a._transaction_runner = competing_runner

        with pytest.raises(ValueError, match="is not pending \\(status: CONFIRMED\\)") as exc_info:
            store_a.confirm_proposal(
                production_id="prod-contention",
                proposal_id="prop-contention-1",
                approver_id="user-approver-loser",
                current_hashes=hashes,
            )
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "Failed to commit transaction in 5 attempts." in str(exc_info.value.__cause__)

        # Exactly one authorization exists (the competing one)
        auths = store_a.get_authorizations("prod-contention")
        assert len(auths) == 1
        assert auths[0].authorization_id == "auth-competing"

        # B. Transaction runner raises ValueError while proposal remains PENDING -> original exception unchanged
        def failing_pending_runner(cl: Any, txn_func: Any) -> Any:
            raise ValueError("Firestore network failure during transaction")

        client_b = FakeFirestoreClient()
        store_b = FirestoreProductionStore(client=client_b, transaction_runner=fake_transaction_runner)
        store_b.register(**_sample_production_args("prod-contention-b"))
        prop_b = Proposal(
            proposal_id="prop-pending-1",
            production_id="prod-contention-b",
            issue_id="iss-1",
            proposer_id="user-proposer",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Testing pending runner failure",
            manifest_hash="m1",
            obligation_registry_version_hash="o1",
            artifact_index_digest="a1",
            visual_observations_hash="v1",
            created_at="2026-08-01T00:00:00Z",
        )
        store_b.save_proposal(prop_b)
        store_b._transaction_runner = failing_pending_runner

        with pytest.raises(ValueError, match="Firestore network failure during transaction") as exc_info_b:
            store_b.confirm_proposal(
                production_id="prod-contention-b",
                proposal_id="prop-pending-1",
                approver_id="user-approver",
                current_hashes=hashes,
            )
        assert exc_info_b.value.__cause__ is None
        assert str(exc_info_b.value) == "Firestore network failure during transaction"

        # C. Reread failure must not be converted into "not pending"
        def failing_reread_runner(cl: Any, txn_func: Any) -> Any:
            # Corrupt store so reread fails or raises
            del client_b._store_data["production_states/prod-contention-b/proposals/prop-pending-1"]
            raise ValueError("Failed to commit transaction in 5 attempts.")

        store_c = FirestoreProductionStore(client=client_b, transaction_runner=failing_reread_runner)
        with pytest.raises(ValueError, match="Failed to commit transaction in 5 attempts.") as exc_info_c:
            store_c.confirm_proposal(
                production_id="prod-contention-b",
                proposal_id="prop-pending-1",
                approver_id="user-approver",
                current_hashes=hashes,
            )
        assert exc_info_c.value.__cause__ is None

    def test_phase1a5_reread_error_preservation_and_validated_terminal_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeFirestoreClient()
        prod_args = _sample_production_args("prod-p1a5")
        hashes = ("m1", "o1", "a1", "v1")

        def make_prop(prop_id: str, status: str = "PENDING") -> Proposal:
            return Proposal(
                proposal_id=prop_id,
                production_id="prod-p1a5",
                issue_id="iss-1",
                proposer_id="user-proposer",
                proposer_role="REVIEWER",
                action=AuthorizationAction.WAIVE_OBLIGATION,
                reason="Testing Phase 1A.5",
                manifest_hash="m1",
                obligation_registry_version_hash="o1",
                artifact_index_digest="a1",
                visual_observations_hash="v1",
                created_at="2026-08-01T00:00:00Z",
                status=status,
            )

        store = FirestoreProductionStore(client=client, transaction_runner=fake_transaction_runner)
        store.register(**prod_args)

        # 1. prop_ref.get() raises ValueError("REREAD_FAILURE")
        prop1 = make_prop("prop-1")
        store.save_proposal(prop1)

        def runner_fail_1(cl: Any, txn_func: Any) -> Any:
            raise ValueError("ORIGINAL_TXN_VAL_ERR")

        store._transaction_runner = runner_fail_1

        orig_get = FakeDocRef.get

        def mock_get_val_err(self_ref: Any, transaction: Any = None) -> Any:
            if transaction is None and self_ref.path.endswith("prop-1"):
                raise ValueError("REREAD_FAILURE")
            return orig_get(self_ref, transaction=transaction)

        monkeypatch.setattr(FakeDocRef, "get", mock_get_val_err)

        with pytest.raises(ValueError, match="ORIGINAL_TXN_VAL_ERR") as exc1:
            store.confirm_proposal("prod-p1a5", "prop-1", "approver-1", hashes)
        assert exc1.value.__cause__ is None
        assert str(exc1.value) == "ORIGINAL_TXN_VAL_ERR"

        # 2. prop_ref.get() raises non-ValueError exception (e.g. RuntimeError)
        prop2 = make_prop("prop-2")
        store._transaction_runner = fake_transaction_runner
        store.save_proposal(prop2)
        store._transaction_runner = runner_fail_1

        def mock_get_runtime_err(self_ref: Any, transaction: Any = None) -> Any:
            if transaction is None and self_ref.path.endswith("prop-2"):
                raise RuntimeError("REREAD_RUNTIME_ERR")
            return orig_get(self_ref, transaction=transaction)

        monkeypatch.setattr(FakeDocRef, "get", mock_get_runtime_err)

        with pytest.raises(ValueError, match="ORIGINAL_TXN_VAL_ERR") as exc2:
            store.confirm_proposal("prod-p1a5", "prop-2", "approver-1", hashes)
        assert exc2.value.__cause__ is None
        assert str(exc2.value) == "ORIGINAL_TXN_VAL_ERR"

        monkeypatch.setattr(FakeDocRef, "get", orig_get)

        # 3. Reread returns malformed status values: 123, "BROKEN", "", missing status, malformed dict
        malformed_statuses: list[Any] = [123, "BROKEN", "", None]
        for idx, bad_status in enumerate(malformed_statuses):
            p_id = f"prop-mal-{idx}"
            p_obj = make_prop(p_id)
            store._transaction_runner = fake_transaction_runner
            store.save_proposal(p_obj)
            store._transaction_runner = runner_fail_1

            p_path = f"production_states/prod-p1a5/proposals/{p_id}"
            if bad_status is None:
                del client._store_data[p_path]["status"]
            else:
                client._store_data[p_path]["status"] = bad_status

            with pytest.raises(ValueError, match="ORIGINAL_TXN_VAL_ERR") as exc3:
                store.confirm_proposal("prod-p1a5", p_id, "approver-1", hashes)
            assert exc3.value.__cause__ is None
            assert str(exc3.value) == "ORIGINAL_TXN_VAL_ERR"

        # Malformed/empty proposal dictionary
        p_id_empty = "prop-mal-empty"
        store._transaction_runner = fake_transaction_runner
        store.save_proposal(make_prop(p_id_empty))
        store._transaction_runner = runner_fail_1
        client._store_data[f"production_states/prod-p1a5/proposals/{p_id_empty}"] = {}
        with pytest.raises(ValueError, match="ORIGINAL_TXN_VAL_ERR") as exc3_empty:
            store.confirm_proposal("prod-p1a5", p_id_empty, "approver-1", hashes)
        assert exc3_empty.value.__cause__ is None
        assert str(exc3_empty.value) == "ORIGINAL_TXN_VAL_ERR"

        # 4. Fully valid CONFIRMED proposal
        prop4 = make_prop("prop-4", status="CONFIRMED")
        store._transaction_runner = fake_transaction_runner
        store.save_proposal(prop4)
        store._transaction_runner = runner_fail_1
        with pytest.raises(ValueError, match="is not pending \\(status: CONFIRMED\\)") as exc4:
            store.confirm_proposal("prod-p1a5", "prop-4", "approver-1", hashes)
        assert isinstance(exc4.value.__cause__, ValueError)
        assert str(exc4.value.__cause__) == "ORIGINAL_TXN_VAL_ERR"

        # 5. Fully valid REJECTED proposal
        prop5 = make_prop("prop-5", status="REJECTED")
        store._transaction_runner = fake_transaction_runner
        store.save_proposal(prop5)
        store._transaction_runner = runner_fail_1
        with pytest.raises(ValueError, match="is not pending \\(status: REJECTED\\)") as exc5:
            store.confirm_proposal("prod-p1a5", "prop-5", "approver-1", hashes)
        assert isinstance(exc5.value.__cause__, ValueError)
        assert str(exc5.value.__cause__) == "ORIGINAL_TXN_VAL_ERR"

        # 6. Fully valid PENDING proposal
        prop6 = make_prop("prop-6", status="PENDING")
        store._transaction_runner = fake_transaction_runner
        store.save_proposal(prop6)
        store._transaction_runner = runner_fail_1
        with pytest.raises(ValueError, match="ORIGINAL_TXN_VAL_ERR") as exc6:
            store.confirm_proposal("prod-p1a5", "prop-6", "approver-1", hashes)
        assert exc6.value.__cause__ is None
        assert str(exc6.value) == "ORIGINAL_TXN_VAL_ERR"
