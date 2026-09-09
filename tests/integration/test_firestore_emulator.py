"""
Integration test for FirestoreProjectionStore against the local Cloud Firestore Emulator.

Gated cleanly by:
  CREDITLOCK_RUN_FIRESTORE_EMULATOR=1 and FIRESTORE_EMULATOR_HOST

If either environment variable is missing or not set to "1", this test module skips cleanly.
Never contacts live Firestore in any mode.
"""

from __future__ import annotations

import os
import uuid

import pytest

from creditlock.domain.gate import GateState
from creditlock.events.firestore_store import FirestoreProjectionStore
from creditlock.events.models import (
    EVENT_TYPE_ARTIFACT_RENDERED,
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    ArtifactRenderedPayload,
    CreditLockEvent,
    CreditRollSubmittedPayload,
)

_run_emulator = os.environ.get("CREDITLOCK_RUN_FIRESTORE_EMULATOR") == "1"
_host_present = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
_should_run = _run_emulator and _host_present

pytestmark = pytest.mark.skipif(
    not _should_run,
    reason=(
        "Firestore emulator integration test skipped — "
        "set CREDITLOCK_RUN_FIRESTORE_EMULATOR=1 and FIRESTORE_EMULATOR_HOST to run"
    ),
)


class TestFirestoreEmulatorIntegration:
    def test_apply_events_to_emulator(self) -> None:
        store = FirestoreProjectionStore.from_settings()

        prod_id = f"emul-test-{uuid.uuid4().hex[:8]}"
        eid1 = str(uuid.uuid4())
        eid2 = str(uuid.uuid4())
        at = "2026-07-31T00:00:00Z"

        evt1 = CreditLockEvent(
            event_id=eid1,
            event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            production_id=prod_id,
            aggregate_version=1,
            actor_id="emul-actor",
            occurred_at=at,
            payload=CreditRollSubmittedPayload(
                production_id=prod_id,
                manifest_id="m-emul-1",
                manifest_hash="mhash-emul-1",
                submitted_by="emul-actor",
                at=at,
            ),
        )

        evt2 = CreditLockEvent(
            event_id=eid2,
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=prod_id,
            aggregate_version=2,
            actor_id="emul-actor",
            occurred_at=at,
            payload=ArtifactRenderedPayload(
                production_id=prod_id,
                manifest_id="m-emul-1",
                artifact_index_digest="digest-emul-123",
                render_profile_version="v1",
                at=at,
            ),
        )

        assert store.apply(evt1) is True
        assert store.apply(evt2) is True

        state = store.get(prod_id)
        assert state is not None
        assert state["gate"]["state"] == GateState.STALE.value
        assert state["gate"]["artifact_index_digest"] == "digest-emul-123"
        assert state["manifests"]["manifest_lifecycle"] == "RENDER_COMPLETE"

        # Verify processed-event document for credit_roll.submitted
        doc1_snap = (
            store.client.collection("productions")
            .document(prod_id)
            .collection("processed_events")
            .document(eid1)
            .get()
        )
        assert doc1_snap.exists
        doc1_data = doc1_snap.to_dict()
        assert doc1_data["event_id"] == eid1
        assert doc1_data["event_type"] == EVENT_TYPE_CREDIT_ROLL_SUBMITTED
        assert doc1_data["aggregate_version"] == 1

        # Verify processed-event document for artifact.rendered
        doc2_snap = (
            store.client.collection("productions")
            .document(prod_id)
            .collection("processed_events")
            .document(eid2)
            .get()
        )
        assert doc2_snap.exists
        doc2_data = doc2_snap.to_dict()
        assert doc2_data["event_id"] == eid2
        assert doc2_data["event_type"] == EVENT_TYPE_ARTIFACT_RENDERED
        assert doc2_data["aggregate_version"] == 2
