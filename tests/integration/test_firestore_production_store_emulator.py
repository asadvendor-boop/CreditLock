"""
Real Firestore Emulator Integration Tests for FirestoreProductionStore and FirestoreProjectionStore.

Gated behind CREDITLOCK_RUN_FIRESTORE_EMULATOR=1 and FIRESTORE_EMULATOR_HOST=<host>.
"""

from __future__ import annotations

import contextlib
import os
import socket
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from creditlock.domain.models import (
    Authorization,
    AuthorizationAction,
    CreditManifest,
    Obligation,
    Proposal,
)
from creditlock.domain.store import FirestoreProductionStore
from creditlock.events.firestore_store import FirestoreProjectionStore
from creditlock.events.models import (
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    CreditLockEvent,
    CreditRollSubmittedPayload,
)


@pytest.fixture
def emulator_client() -> Generator[Any, None, None]:
    run_emulator = os.environ.get("CREDITLOCK_RUN_FIRESTORE_EMULATOR") == "1"
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")

    if not (run_emulator and host):
        pytest.skip(
            "Firestore emulator integration test skipped — set CREDITLOCK_RUN_FIRESTORE_EMULATOR=1 and FIRESTORE_EMULATOR_HOST to run."
        )

    host_parts = host.split(":")
    h = host_parts[0]
    p = int(host_parts[1]) if len(host_parts) > 1 else 8080
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect((h, p))
        sock.close()
    except OSError:
        pytest.skip(f"Firestore emulator host {host} is not reachable.")

    from google.cloud import firestore

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-test-project")
    client = firestore.Client(project=project)
    yield client


def test_emulator_firestore_production_store_crud_and_restart(emulator_client: Any) -> None:
    # 1. Generate unique production ID for isolated test run
    prod_id = f"prod-emul-{uuid.uuid4().hex[:8]}"

    try:
        store1 = FirestoreProductionStore(client=emulator_client)

        obl = Obligation(
            obligation_id="obl-e1",
            production_id=prod_id,
            status="CANDIDATE",
            extraction_model_id="gemini-3.6-flash",
            prompt_version="v1",
            credited_party_id="p1",
            required_display_text="Producer Bob",
            role_label="Producer",
            credit_surface="END_CARDS",
            card_type="SOLO",
            source_document_id="doc1",
            source_document_version=1,
            source_hash="h1",
            agent_reported_confidence=0.9,
            source_span={"quote": "Producer Bob", "start_char": 0, "end_char": 12},
        )
        manifest = CreditManifest(
            manifest_id="man-e1",
            production_id=prod_id,
            delivery_version_id="v1",
            entries=[],
        )

        # 2. Register structured state
        store1.register(
            production_id=prod_id,
            obligations=[obl],
            manifest=manifest,
            contributor_registry=[{"id": "p1", "name": "Bob"}],
        )

        # 3. Reconstruct after creating a new store instance
        store2 = FirestoreProductionStore(client=emulator_client)
        state = store2.get(prod_id)
        assert state is not None
        assert state["manifest"].manifest_id == "man-e1"
        assert state["obligations"][0].required_display_text == "Producer Bob"

        # 4. Save proposal & verify persistence across restart
        prop = Proposal(
            proposal_id="prop-emul-1",
            production_id=prod_id,
            issue_id="iss-1",
            proposer_id="user-proposer",
            proposer_role="REVIEWER",
            action=AuthorizationAction.WAIVE_OBLIGATION,
            reason="Emulator proposal test",
            manifest_hash="mhash",
            obligation_registry_version_hash="orvhash",
            artifact_index_digest="artdigest",
            visual_observations_hash="vishash",
            created_at="2026-08-01T00:00:00Z",
        )
        store2.save_proposal(prop)

        store3 = FirestoreProductionStore(client=emulator_client)
        props = store3.get_proposals(prod_id)
        assert len(props) == 1
        assert props[0].proposal_id == "prop-emul-1"

        # 5. Real concurrent confirmation attempt using two worker threads
        hashes = ("mhash", "orvhash", "artdigest", "vishash")

        def _confirm_worker(approver_id: str) -> tuple[str, Any]:
            try:
                auth = store3.confirm_proposal(
                    production_id=prod_id,
                    proposal_id="prop-emul-1",
                    approver_id=approver_id,
                    current_hashes=hashes,
                )
                return ("SUCCESS", auth)
            except Exception as e:  # noqa: BLE001
                return ("FAIL", e)

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = executor.submit(_confirm_worker, "user-approver-a")
            fut_b = executor.submit(_confirm_worker, "user-approver-b")
            res_a = fut_a.result()
            res_b = fut_b.result()

        statuses = [res_a[0], res_b[0]]
        assert statuses.count("SUCCESS") == 1
        assert statuses.count("FAIL") == 1

        success_res = res_a[1] if res_a[0] == "SUCCESS" else res_b[1]
        assert isinstance(success_res, Authorization)

        fail_res = res_a[1] if res_a[0] == "FAIL" else res_b[1]
        assert isinstance(fail_res, ValueError)
        assert str(fail_res) == "Proposal 'prop-emul-1' is not pending (status: CONFIRMED)."

        auths = store3.get_authorizations(prod_id)
        assert len(auths) == 1

        store4 = FirestoreProductionStore(client=emulator_client)
        props_after = store4.get_proposals(prod_id)
        assert len(props_after) == 1
        assert props_after[0].status == "CONFIRMED"

        # 6. Event projection and application state remain isolated
        proj_store = FirestoreProjectionStore(client=emulator_client)
        evt = CreditLockEvent(
            event_id=f"evt-emul-{uuid.uuid4().hex[:8]}",
            event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            production_id=prod_id,
            aggregate_version=1,
            actor_id="actor-1",
            occurred_at="2026-08-01T00:00:00Z",
            payload=CreditRollSubmittedPayload(
                production_id=prod_id,
                manifest_id="man-e1",
                manifest_hash="mhash",
                submitted_by="actor-1",
                at="2026-08-01T00:00:00Z",
            ),
        )
        assert proj_store.apply(evt) is True

        # Application state is isolated and intact in production_states/{prod_id}
        final_state = store3.get(prod_id)
        assert final_state is not None
        assert final_state["manifest"].manifest_id == "man-e1"

    finally:
        # Strict isolated cleanup for only this test production ID
        with contextlib.suppress(Exception):
            doc_state = emulator_client.collection("production_states").document(prod_id)
            for pdoc in doc_state.collection("proposals").stream():
                pdoc.reference.delete()
            for adoc in doc_state.collection("authorizations").stream():
                adoc.reference.delete()
            doc_state.delete()

            doc_prod = emulator_client.collection("productions").document(prod_id)
            for edoc in doc_prod.collection("processed_events").stream():
                edoc.reference.delete()
            for adoc in doc_prod.collection("authorizations").stream():
                adoc.reference.delete()
            doc_prod.delete()
