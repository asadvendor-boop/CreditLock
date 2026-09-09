"""
Transactional Firestore projection store.

Persists deterministic Projector state to Cloud Firestore with atomic idempotency,
production-wide version checking, valid subcollection paths, and dedicated authorization records.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

from google.cloud import firestore

from creditlock.events.models import (
    EVENT_TYPE_RESOLUTION_RECORDED,
    CreditLockEvent,
    ResolutionRecordedPayload,
)
from creditlock.events.projection import ProjectionCheckpoint, Projector

logger = logging.getLogger(__name__)


def _default_transaction_runner[T](client: Any, txn_func: Callable[[Any], T]) -> T:
    """Production transaction runner using google.cloud.firestore.transactional."""
    transaction = client.transaction()
    res: T = firestore.transactional(txn_func)(transaction)
    return res


class FirestoreProjectionStore:
    """
    Firestore-backed projection store.

    Paths:
    - productions/{production_id}
    - productions/{production_id}/processed_events/{event_id}
    - productions/{production_id}/authorizations/{event_id}
    """

    def __init__(
        self,
        client: Any = None,
        transaction_runner: Callable[[Any, Callable[[Any], Any]], Any] | None = None,
    ) -> None:
        self._client = client
        self._transaction_runner = transaction_runner or _default_transaction_runner

    @classmethod
    def from_settings(
        cls,
        database: str | None = None,
        transaction_runner: Callable[[Any, Callable[[Any], Any]], Any] | None = None,
    ) -> FirestoreProjectionStore:
        """Construct store from application settings using get_settings()."""
        from google.cloud import firestore

        from creditlock.settings import get_settings

        s = get_settings()
        db_name = database or s.firestore_database
        client = firestore.Client(
            project=s.google_cloud_project or None,
            database=db_name,
        )
        return cls(client=client, transaction_runner=transaction_runner)

    @property
    def client(self) -> Any:
        """Lazy access to the Firestore client."""
        if self._client is None:
            from google.cloud import firestore

            from creditlock.settings import get_settings

            s = get_settings()
            self._client = firestore.Client(
                project=s.google_cloud_project or None,
                database=s.firestore_database,
            )
        return self._client

    def apply(self, event: CreditLockEvent) -> bool:
        """
        Atomically project event to Firestore within a transaction.

        - Idempotent: duplicate event_id returns False with zero writes.
        - Versioned: aggregate_version <= stored last_aggregate_version returns False with zero writes.
        - Atomic: production document, processed-event, and authorization records are updated together.
        """
        client = self.client

        def _txn_apply(txn: Any) -> bool:
            prod_id = event.production_id
            prod_ref = client.collection("productions").document(prod_id)
            evt_ref = prod_ref.collection("processed_events").document(event.event_id)

            # Reads must occur BEFORE any writes, calling public doc_ref.get(transaction=txn)
            evt_snap = evt_ref.get(transaction=txn)
            prod_snap = prod_ref.get(transaction=txn)

            # 1. Idempotency check: duplicate event_id -> False
            if evt_snap.exists:
                return False

            # 2. Version check: aggregate_version <= last_version -> False
            prod_data: dict[str, Any] | None = prod_snap.to_dict() if prod_snap.exists else None
            last_version = prod_data.get("last_aggregate_version", 0) if prod_data else 0
            last_allocated = prod_data.get("allocated_aggregate_version", 0) if prod_data else 0

            if event.aggregate_version <= last_version:
                return False

            # 3. Hydrate temporary Projector from stored checkpoint
            projector = Projector()
            if prod_data is not None and "projection" in prod_data:
                proj_dict = copy.deepcopy(prod_data["projection"])
                proj_dict["authorization_log"] = []  # Empty authorization_log for hydration
                checkpoint = ProjectionCheckpoint(
                    projection=proj_dict,
                    last_aggregate_version=last_version,
                )
                projector.restore(prod_id, checkpoint)

            # 4. Apply event using Projector reducer
            applied = projector.apply(event)
            if not applied:
                return False

            # 5. Extract updated checkpoint
            new_checkpoint = projector.checkpoint(prod_id)
            if new_checkpoint is None:
                return False

            # 6. Prepare production document (omit authorization_log array)
            new_proj = copy.deepcopy(new_checkpoint.projection)
            new_proj.pop("authorization_log", None)

            prod_payload = {
                "projection": new_proj,
                "last_aggregate_version": new_checkpoint.last_aggregate_version,
                "allocated_aggregate_version": max(
                    last_allocated, new_checkpoint.last_aggregate_version
                ),
                "updated_at": firestore.SERVER_TIMESTAMP,
            }

            # 7. Prepare processed_event document
            evt_payload = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "aggregate_version": event.aggregate_version,
                "schema_version": event.schema_version,
                "occurred_at": event.occurred_at,
            }

            # 8. Write production and processed-event documents using txn.set
            txn.set(prod_ref, prod_payload)
            txn.set(evt_ref, evt_payload)

            # 9. For resolution.recorded only, write authorization document
            if event.event_type == EVENT_TYPE_RESOLUTION_RECORDED:
                assert isinstance(event.payload, ResolutionRecordedPayload)
                auth_ref = prod_ref.collection("authorizations").document(event.event_id)
                res_action = (
                    event.payload.resolution_type.value
                    if hasattr(event.payload.resolution_type, "value")
                    else str(event.payload.resolution_type)
                )
                auth_payload = {
                    "event_id": event.event_id,
                    "actor_id": event.payload.actor_id,
                    "role": event.payload.role,
                    "action": res_action,
                    "issue_id": event.payload.issue_id,
                    "reason": event.payload.reason,
                    "bound_hashes": copy.deepcopy(event.payload.bound_hashes),
                    "occurred_at": event.occurred_at,
                    "at": event.payload.at,
                }
                txn.set(auth_ref, auth_payload)

            return True

        res: bool = self._transaction_runner(client, _txn_apply)
        return res

    def get(self, production_id: str) -> dict[str, Any] | None:
        """Return defensive copy of projected state for production_id, or None if missing."""
        prod_ref = self.client.collection("productions").document(production_id)
        doc_snap = prod_ref.get()
        if not doc_snap.exists:
            return None
        data = doc_snap.to_dict()
        if data is None or "projection" not in data:
            return None
        proj: dict[str, Any] = copy.deepcopy(data["projection"])
        if "authorization_log" not in proj:
            proj["authorization_log"] = []
        return proj

    def list_authorizations(self, production_id: str) -> list[dict[str, Any]]:
        """Return authorization records for production_id in deterministic order."""
        auths_ref = (
            self.client.collection("productions")
            .document(production_id)
            .collection("authorizations")
        )
        docs = auths_ref.stream() if hasattr(auths_ref, "stream") else auths_ref.get()
        results: list[dict[str, Any]] = []
        for doc in docs:
            d = doc.to_dict() if hasattr(doc, "to_dict") else doc
            if d:
                results.append(copy.deepcopy(d))
        results.sort(key=lambda item: (item.get("at", ""), item.get("event_id", "")))
        return results

    def has_processed_event(self, production_id: str, event_id: str) -> bool:
        """
        Check whether an event has been processed and projected into Firestore.

        Inspects only productions/{production_id}/processed_events/{event_id}.
        Read-only: performs no writes and no collection scans.
        """
        evt_ref = (
            self.client.collection("productions")
            .document(production_id)
            .collection("processed_events")
            .document(event_id)
        )
        snap = evt_ref.get()
        return bool(snap.exists)

    def get_last_aggregate_version(self, production_id: str) -> int:
        """
        Return the last projected aggregate version for production_id, or 0 if missing.

        Read-only: inspects productions/{production_id} without scanning subcollections.
        """
        prod_ref = self.client.collection("productions").document(production_id)
        doc_snap = prod_ref.get()
        if not doc_snap.exists:
            return 0
        data = doc_snap.to_dict()
        if not data:
            return 0
        return int(data.get("last_aggregate_version", 0))

    def allocate_aggregate_version(self, production_id: str, event_id: str) -> int:
        """
        Durable, concurrency-safe, per-production aggregate-version allocation.

        - Keyed by deterministic checkpoint event_id.
        - Returns previously allocated version on retries for the same event_id.
        - Allocates max(last_aggregate_version, allocated_aggregate_version) + 1.
        - Does NOT mark the event processed; only the worker creates processed-event ack.
        """
        client = self.client

        def _txn_allocate(txn: Any) -> int:
            prod_ref = client.collection("productions").document(production_id)
            alloc_ref = prod_ref.collection("allocated_events").document(event_id)
            proc_ref = prod_ref.collection("processed_events").document(event_id)

            # All reads must occur before writes in Firestore transaction
            alloc_snap = alloc_ref.get(transaction=txn)
            proc_snap = proc_ref.get(transaction=txn)
            prod_snap = prod_ref.get(transaction=txn)

            if proc_snap.exists:
                proc_data = proc_snap.to_dict()
                if proc_data and "aggregate_version" in proc_data:
                    return int(proc_data["aggregate_version"])

            if alloc_snap.exists:
                alloc_data = alloc_snap.to_dict()
                if alloc_data and "aggregate_version" in alloc_data:
                    return int(alloc_data["aggregate_version"])

            prod_data = prod_snap.to_dict() if prod_snap.exists else {}
            last_applied = int(prod_data.get("last_aggregate_version", 0)) if prod_data else 0
            last_allocated = int(prod_data.get("allocated_aggregate_version", 0)) if prod_data else 0

            next_version = max(last_applied, last_allocated) + 1

            alloc_payload = {
                "event_id": event_id,
                "aggregate_version": next_version,
                "allocated_at": firestore.SERVER_TIMESTAMP,
            }
            txn.set(alloc_ref, alloc_payload)

            new_prod_data = copy.deepcopy(prod_data) if prod_data else {}
            new_prod_data["allocated_aggregate_version"] = next_version
            new_prod_data["updated_at"] = firestore.SERVER_TIMESTAMP
            txn.set(prod_ref, new_prod_data)

            return next_version

        res: int = self._transaction_runner(client, _txn_allocate)
        return res
