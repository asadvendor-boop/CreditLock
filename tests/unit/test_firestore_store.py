"""
Offline unit tests for FirestoreProjectionStore using an injected fake Firestore client.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from google.cloud import firestore

from creditlock.domain.gate import GateState
from creditlock.domain.models import AuthorizationAction, IssueCode
from creditlock.events.firestore_store import FirestoreProjectionStore
from creditlock.events.models import (
    EVENT_TYPE_ARTIFACT_RENDERED,
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    EVENT_TYPE_RESOLUTION_RECORDED,
    ArtifactRenderedPayload,
    CreditLockEvent,
    CreditRollSubmittedPayload,
    ResolutionRecordedPayload,
)
from creditlock.events.projection import ProjectionError

PROD = "prod-fs-unit"
AT = "2026-07-31T00:00:00Z"


class FakeSnapshot:
    def __init__(self, path: str, data: dict[str, Any] | None):
        self.path = path
        self.exists = data is not None
        self._data = copy.deepcopy(data)

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, path: str, store_data: dict[str, dict[str, Any]]):
        self.path = path
        self._store_data = store_data

    def collection(self, name: str) -> FakeColRef:
        return FakeColRef(f"{self.path}/{name}", self._store_data)

    def get(self, transaction: Any = None) -> FakeSnapshot:
        if transaction is not None and hasattr(transaction, "record_read"):
            transaction.record_read(self.path)
        return FakeSnapshot(self.path, self._store_data.get(self.path))

    def set(self, data: dict[str, Any]) -> None:
        self._store_data[self.path] = copy.deepcopy(data)

    def update(self, data: dict[str, Any]) -> None:
        current = copy.deepcopy(self._store_data.get(self.path, {}))
        current.update(copy.deepcopy(data))
        self._store_data[self.path] = current

    def delete(self) -> None:
        self._store_data.pop(self.path, None)


class FakeColRef:
    def __init__(self, path: str, store_data: dict[str, dict[str, Any]]):
        self.path = path
        self._store_data = store_data

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(f"{self.path}/{doc_id}", self._store_data)

    def stream(self) -> list[FakeSnapshot]:
        prefix = f"{self.path}/"
        results = []
        for p, d in self._store_data.items():
            if p.startswith(prefix) and "/" not in p[len(prefix) :]:
                results.append(FakeSnapshot(p, d))
        return results

    def get(self) -> list[FakeSnapshot]:
        return self.stream()


class FakeTransaction:
    def __init__(self, store_data: dict[str, dict[str, Any]]):
        self._store_data = store_data
        self.pending_writes: dict[str, dict[str, Any]] = {}
        self.reads: list[str] = []
        self._has_written = False
        self._read_only = False
        self.id = "fake-txn-id"

    def record_read(self, path: str) -> None:
        if self._has_written:
            raise RuntimeError("Firestore transactions require all reads before writes.")
        self.reads.append(path)

    def get(self, ref_or_query: Any) -> Any:
        if self._has_written:
            raise RuntimeError("Firestore transactions require all reads before writes.")
        snapshots = [ref_or_query.get(transaction=self)]
        return iter(snapshots)

    def set(self, doc_ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._has_written = True
        self.pending_writes[doc_ref.path] = copy.deepcopy(data)

    def update(self, doc_ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._has_written = True
        current = copy.deepcopy(
            self.pending_writes.get(doc_ref.path, self._store_data.get(doc_ref.path, {}))
        )
        current.update(copy.deepcopy(data))
        self.pending_writes[doc_ref.path] = current

    def delete(self, doc_ref: FakeDocRef) -> None:
        self._has_written = True
        self.pending_writes[doc_ref.path] = {}

    def commit(self) -> None:
        self._store_data.update(self.pending_writes)


class FakeFirestoreClient:
    def __init__(self) -> None:
        self._store_data: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> FakeColRef:
        return FakeColRef(name, self._store_data)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self._store_data)


def fake_transaction_runner[T](client: Any, txn_func: Callable[[Any], T]) -> T:
    txn = client.transaction()
    res = txn_func(txn)
    txn.commit()
    return res


def _credit_roll_submitted(version: int = 1, event_id: str | None = None) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        production_id=PROD,
        aggregate_version=version,
        actor_id="actor-fs",
        occurred_at=AT,
        payload=CreditRollSubmittedPayload(
            production_id=PROD,
            manifest_id="manifest-fs-1",
            manifest_hash="mhash-fs-1",
            submitted_by="actor-fs",
            at=AT,
        ),
    )


def _artifact_rendered(version: int = 2, event_id: str | None = None) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=EVENT_TYPE_ARTIFACT_RENDERED,
        production_id=PROD,
        aggregate_version=version,
        actor_id="actor-fs",
        occurred_at=AT,
        payload=ArtifactRenderedPayload(
            production_id=PROD,
            manifest_id="manifest-fs-1",
            artifact_index_digest="digest-fs-123",
            render_profile_version="v1",
            at=AT,
        ),
    )


def _resolution_recorded(
    issue_id: str = "issue-1", version: int = 2, event_id: str | None = None, at: str = AT
) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=EVENT_TYPE_RESOLUTION_RECORDED,
        production_id=PROD,
        aggregate_version=version,
        actor_id="approver-1",
        occurred_at=at,
        payload=ResolutionRecordedPayload(
            production_id=PROD,
            issue_id=issue_id,
            resolution_type=AuthorizationAction.CONFIRM_IDENTITY,
            actor_id="approver-1",
            role="RELEASE_APPROVER",
            bound_hashes={
                "manifest_hash": "mhash-fs-1",
                "obligation_registry_version_hash": "orvh-1",
            },
            reason="Verified contract terms",
            at=at,
        ),
    )


class TestFirestoreProjectionStore:
    def test_valid_credit_roll_submitted_persists_stale_artifact_pending(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        evt = _credit_roll_submitted(version=1)
        assert store.apply(evt) is True

        state = store.get(PROD)
        assert state is not None
        assert state["gate"]["state"] == GateState.STALE.value
        assert state["manifests"]["manifest_lifecycle"] == "PENDING_RENDER"
        ap_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap_issues) == 1

    def test_artifact_rendered_v2_restores_and_advances(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))
        assert store.apply(_artifact_rendered(version=2)) is True

        state = store.get(PROD)
        assert state is not None
        assert state["gate"]["artifact_index_digest"] == "digest-fs-123"
        assert state["manifests"]["manifest_lifecycle"] == "RENDER_COMPLETE"

    def test_duplicate_event_id_returns_false_and_performs_zero_writes(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        evt = _credit_roll_submitted(version=1)
        assert store.apply(evt) is True

        raw_store_snap = copy.deepcopy(client._store_data)

        # Apply same event object (same event_id)
        assert store.apply(evt) is False
        assert client._store_data == raw_store_snap

    def test_stale_aggregate_version_returns_false_and_performs_zero_writes(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=2))
        raw_store_snap = copy.deepcopy(client._store_data)

        # Deliver version 1 (stale aggregate_version)
        stale_evt = _credit_roll_submitted(version=1)
        assert store.apply(stale_evt) is False
        assert client._store_data == raw_store_snap

    def test_projection_error_performs_zero_writes(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        # Apply artifact.rendered without credit_roll.submitted -> ProjectionError
        orphan_evt = _artifact_rendered(version=1)
        with pytest.raises(ProjectionError):
            store.apply(orphan_evt)

        assert client._store_data == {}

    def test_processed_event_path_valid_and_uses_event_id(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        eid = "eid-unique-999"
        evt = _credit_roll_submitted(version=1, event_id=eid)
        store.apply(evt)

        evt_path = f"productions/{PROD}/processed_events/{eid}"
        assert evt_path in client._store_data

        evt_doc = client._store_data[evt_path]
        assert evt_doc["event_id"] == eid
        assert evt_doc["event_type"] == EVENT_TYPE_CREDIT_ROLL_SUBMITTED
        assert evt_doc["aggregate_version"] == 1
        assert evt_doc["schema_version"] == "1.0"
        assert evt_doc["occurred_at"] == AT

    def test_resolution_recorded_writes_exactly_one_authorization_document(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))

        auth_eid = "auth-eid-777"
        res_evt = _resolution_recorded(issue_id="issue-1", version=2, event_id=auth_eid)
        assert store.apply(res_evt) is True

        auth_path = f"productions/{PROD}/authorizations/{auth_eid}"
        assert auth_path in client._store_data

        auth_doc = client._store_data[auth_path]
        assert auth_doc["event_id"] == auth_eid
        assert auth_doc["actor_id"] == "approver-1"
        assert auth_doc["role"] == "RELEASE_APPROVER"
        assert auth_doc["action"] == "CONFIRM_IDENTITY"
        assert auth_doc["issue_id"] == "issue-1"

    def test_exact_reason_and_bound_hashes_survive_persistence(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))
        auth_eid = "auth-eid-888"
        res_evt = _resolution_recorded(issue_id="issue-2", version=2, event_id=auth_eid)
        store.apply(res_evt)

        auth_doc = client._store_data[f"productions/{PROD}/authorizations/{auth_eid}"]
        assert auth_doc["reason"] == "Verified contract terms"
        assert auth_doc["bound_hashes"] == {
            "manifest_hash": "mhash-fs-1",
            "obligation_registry_version_hash": "orvh-1",
        }

    def test_production_document_contains_no_authorization_log_array(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))
        store.apply(_resolution_recorded(version=2))

        prod_doc = client._store_data[f"productions/{PROD}"]
        assert "authorization_log" not in prod_doc["projection"]

    def test_get_returns_defensive_copy(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))

        s1 = store.get(PROD)
        assert s1 is not None
        s1["gate"]["state"] = "MUTATED_BY_CALLER"

        s2 = store.get(PROD)
        assert s2 is not None
        assert s2["gate"]["state"] == GateState.STALE.value

    def test_list_authorizations_returns_deterministic_ordering(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        store.apply(_credit_roll_submitted(version=1))
        store.apply(
            _resolution_recorded(
                issue_id="i2", version=2, event_id="eid-b", at="2026-07-31T12:00:00Z"
            )
        )
        store.apply(
            _resolution_recorded(
                issue_id="i1", version=3, event_id="eid-a", at="2026-07-31T10:00:00Z"
            )
        )

        auths = store.list_authorizations(PROD)
        assert len(auths) == 2
        assert auths[0]["event_id"] == "eid-a"  # 10:00 comes before 12:00
        assert auths[1]["event_id"] == "eid-b"

    def test_production_payload_receives_server_timestamp_sentinel(self) -> None:
        client = FakeFirestoreClient()
        store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

        evt = _credit_roll_submitted(version=1)
        store.apply(evt)

        prod_doc = client._store_data[f"productions/{PROD}"]
        assert prod_doc["updated_at"] is firestore.SERVER_TIMESTAMP

    def test_transaction_contention_retry_runner_runs_callback_twice(self) -> None:
        client = FakeFirestoreClient()
        runs_count = 0

        def contention_retry_runner(cl: Any, txn_func: Callable[[Any], Any]) -> Any:
            nonlocal runs_count
            # Attempt 1: simulate contention abort (read snapshots, queue writes, but discard attempt 1 writes)
            txn1 = cl.transaction()
            runs_count += 1
            txn_func(txn1)
            # Discard attempt 1 (no commit)

            # Attempt 2: retry on fresh transaction
            txn2 = cl.transaction()
            runs_count += 1
            res = txn_func(txn2)
            txn2.commit()
            return res

        store = FirestoreProjectionStore(client=client, transaction_runner=contention_retry_runner)

        res_evt = _resolution_recorded(issue_id="issue-retry", version=1, event_id="eid-retry")
        assert store.apply(res_evt) is True
        assert runs_count == 2

        # Prove final store contains exactly 1 production doc, 1 processed_events doc, 1 authorization doc
        assert f"productions/{PROD}" in client._store_data
        assert f"productions/{PROD}/processed_events/eid-retry" in client._store_data
        assert f"productions/{PROD}/authorizations/eid-retry" in client._store_data
        assert len(client._store_data) == 3

    def test_transaction_get_fails_if_used_directly_on_fake(self) -> None:
        """
        Verify that fake Transaction.get returns an iterator, so calling .exists directly
        on a Transaction.get return value raises AttributeError. This guarantees tests catch bad SDK usage.
        """
        client = FakeFirestoreClient()
        txn = client.transaction()
        ref = client.collection("productions").document(PROD)
        result = txn.get(ref)
        with pytest.raises(AttributeError):
            _ = result.exists

    def test_read_after_set_raises(self) -> None:
        client = FakeFirestoreClient()
        txn = client.transaction()
        ref = client.collection("productions").document(PROD)
        txn.set(ref, {"data": 1})
        with pytest.raises(
            RuntimeError, match="Firestore transactions require all reads before writes"
        ):
            ref.get(transaction=txn)

    def test_read_after_update_raises(self) -> None:
        client = FakeFirestoreClient()
        txn = client.transaction()
        ref = client.collection("productions").document(PROD)
        txn.update(ref, {"data": 2})
        with pytest.raises(
            RuntimeError, match="Firestore transactions require all reads before writes"
        ):
            ref.get(transaction=txn)

    def test_read_after_delete_raises(self) -> None:
        client = FakeFirestoreClient()
        txn = client.transaction()
        ref = client.collection("productions").document(PROD)
        txn.delete(ref)
        with pytest.raises(
            RuntimeError, match="Firestore transactions require all reads before writes"
        ):
            ref.get(transaction=txn)
