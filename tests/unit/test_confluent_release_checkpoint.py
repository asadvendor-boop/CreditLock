"""
Offline unit tests for Confluent release checkpoint and export integration.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from creditlock.api.app import app
from creditlock.domain.models import (
    Authorization,
    AuthorizationAction,
)
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
from creditlock.events.release_checkpoint import (
    ReleaseCheckpointSyncResult,
    ReleaseCheckpointUnavailable,
    build_release_checkpoint_event,
    synchronize_release_checkpoint,
)
from creditlock.events.transport import InMemoryTransport
from creditlock.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def mock_fast_renderer():
    """Mock Chrome execution to fall back to instant synthetic PNGs for fast unit tests."""
    with patch("creditlock.renderer.render._render_frame_with_chrome", return_value=None):
        yield


class FakeSnapshot:
    def __init__(self, path: str, data: dict[str, Any] | None) -> None:
        self.path = path
        self.exists = data is not None
        self._data = copy.deepcopy(data)

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, path: str, store_data: dict[str, dict[str, Any]]) -> None:
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
    def __init__(self, path: str, store_data: dict[str, dict[str, Any]]) -> None:
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
    def __init__(self, store_data: dict[str, dict[str, Any]]) -> None:
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
    def __init__(self, initial_data: dict[str, dict[str, Any]] | None = None) -> None:
        self._store_data: dict[str, dict[str, Any]] = copy.deepcopy(initial_data or {})

    def collection(self, name: str) -> FakeColRef:
        return FakeColRef(name, self._store_data)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self._store_data)


def fake_transaction_runner[T](client: Any, txn_func: Callable[[Any], T]) -> T:
    txn = client.transaction()
    res = txn_func(txn)
    txn.commit()
    return res


def _sample_authorization(
    production_id: str = "prod-test-1",
    auth_id: str = "auth-abc-123",
) -> Authorization:
    return Authorization(
        authorization_id=auth_id,
        production_id=production_id,
        proposal_id=f"prop-xyz-{auth_id}",
        issue_id=f"issue-identity-{auth_id}",
        proposer_id="reviewer-1",
        actor_id="approver-1",
        role="RELEASE_APPROVER",
        action=AuthorizationAction.CONFIRM_IDENTITY,
        reason="Verified against primary contract exhibits.",
        selected_contributor_id="contrib_david_park",
        manifest_hash="mhash-1111",
        obligation_registry_version_hash="orvh-2222",
        artifact_index_digest="aid-3333",
        visual_observations_hash="voh-4444",
        authorized_at="2026-08-01T12:00:00Z",
    )


def _seed_credit_roll(store: FirestoreProjectionStore, production_id: str, version: int = 1) -> None:
    evt = CreditLockEvent(
        event_id=f"seed-roll-{production_id}-{version}",
        event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        production_id=production_id,
        aggregate_version=version,
        actor_id="seed-actor",
        occurred_at="2026-08-01T10:00:00Z",
        payload=CreditRollSubmittedPayload(
            production_id=production_id,
            manifest_id="manifest-1",
            manifest_hash="mhash-1111",
            submitted_by="seed-actor",
            at="2026-08-01T10:00:00Z",
        ),
    )
    store.apply(evt)


def _seed_artifact_rendered(store: FirestoreProjectionStore, production_id: str, version: int = 2) -> None:
    evt = CreditLockEvent(
        event_id=f"seed-artifact-{production_id}-{version}",
        event_type=EVENT_TYPE_ARTIFACT_RENDERED,
        production_id=production_id,
        aggregate_version=version,
        actor_id="seed-actor",
        occurred_at="2026-08-01T11:00:00Z",
        payload=ArtifactRenderedPayload(
            production_id=production_id,
            manifest_id="manifest-1",
            artifact_index_digest="aid-3333",
            render_profile_version="v1",
            at="2026-08-01T11:00:00Z",
        ),
    )
    store.apply(evt)


def test_1_deterministic_event_id_stable_across_retries() -> None:
    auth = _sample_authorization()
    evt1 = build_release_checkpoint_event(auth)
    evt2 = build_release_checkpoint_event(auth)

    assert evt1.event_id == evt2.event_id
    assert uuid.UUID(evt1.event_id).version == 5


def test_2_event_payload_derives_from_actual_authorization_without_invented_values() -> None:
    auth = _sample_authorization()
    evt = build_release_checkpoint_event(auth, aggregate_version=7)

    assert evt.event_type == EVENT_TYPE_RESOLUTION_RECORDED
    assert evt.production_id == auth.production_id
    assert evt.actor_id == auth.actor_id
    assert evt.occurred_at == auth.authorized_at
    assert evt.correlation_id == auth.proposal_id
    assert evt.aggregate_version == 7
    assert evt.schema_version == "1.0"

    payload = evt.payload
    assert isinstance(payload, ResolutionRecordedPayload)
    assert payload.production_id == auth.production_id
    assert payload.issue_id == auth.issue_id
    assert payload.resolution_type == auth.action
    assert payload.actor_id == auth.actor_id
    assert payload.role == auth.role
    assert payload.reason == auth.reason
    assert payload.at == auth.authorized_at
    assert payload.bound_hashes == {
        "manifest_hash": auth.manifest_hash,
        "obligation_registry_version_hash": auth.obligation_registry_version_hash,
        "artifact_index_digest": auth.artifact_index_digest,
        "visual_observations_hash": auth.visual_observations_hash,
    }


def test_3_already_processed_event_returns_success_without_publishing() -> None:
    auth = _sample_authorization()
    evt = build_release_checkpoint_event(auth)

    initial_data = {
        f"productions/{auth.production_id}/processed_events/{evt.event_id}": {
            "event_id": evt.event_id,
            "event_type": evt.event_type,
            "aggregate_version": 1,
        }
    }
    client = FakeFirestoreClient(initial_data)
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    transport = MagicMock(spec=InMemoryTransport)
    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    res = synchronize_release_checkpoint(
        authorization=auth,
        transport=transport,
        store=store,
        settings=settings,
    )

    assert transport.publish.call_count == 0
    assert res.status == "SYNCHRONIZED"
    assert res.transport == "CONFLUENT_CLOUD"
    assert res.topic == "creditlock.production.events"
    assert res.event_type == "resolution.recorded"
    assert res.event_id == evt.event_id
    assert res.projection_backend == "FIRESTORE"


def test_4_successful_publish_plus_firestore_acknowledgement_drives_actual_apply() -> None:
    """Requirement 8: Drives the checkpoint through actual FirestoreProjectionStore.apply()."""
    prod_id = "prod-real-apply-1"
    auth = _sample_authorization(production_id=prod_id)

    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    # Seed credit roll v1 so projector can accept resolution.recorded
    _seed_credit_roll(store, prod_id, version=1)

    transport = InMemoryTransport()

    # Worker applies the real event using store.apply(e)
    def simulating_worker_publish(e: Any) -> None:
        applied = store.apply(e)
        assert applied is True

    transport.publish = MagicMock(side_effect=simulating_worker_publish)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=2.0,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    res = synchronize_release_checkpoint(
        authorization=auth,
        transport=transport,
        store=store,
        settings=settings,
    )

    assert transport.publish.call_count == 1
    assert res.status == "SYNCHRONIZED"
    assert res.projection_backend == "FIRESTORE"
    assert store.has_processed_event(prod_id, res.event_id) is True

    # Confirm authorization document written to Firestore by store.apply()
    auth_path = f"productions/{prod_id}/authorizations/{res.event_id}"
    assert auth_path in client._store_data


def test_5_timeout_raises_dedicated_safe_exception() -> None:
    auth = _sample_authorization()
    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)
    transport = MagicMock(spec=InMemoryTransport)

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=0.05,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    with pytest.raises(ReleaseCheckpointUnavailable) as exc_info:
        synchronize_release_checkpoint(
            authorization=auth,
            transport=transport,
            store=store,
            settings=settings,
        )

    assert "timed out" in str(exc_info.value).lower() or "unavailable" in str(exc_info.value).lower()
    # Confirm no credentials appear in exception message
    assert "key" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_c1_r_production_above_v1_assigned_newer_version_applied_and_acked() -> None:
    """
    Repair test: Checkpoint for a production already at aggregate version >=1 is assigned
    a newer version, is applied via store.apply(), and receives its exact processed-event ack.
    """
    prod_id = "prod-above-v1"
    auth = _sample_authorization(production_id=prod_id, auth_id="auth-v3")

    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    # Seed v1 (credit_roll) and v2 (artifact_rendered)
    _seed_credit_roll(store, prod_id, version=1)
    _seed_artifact_rendered(store, prod_id, version=2)

    # Assert last_aggregate_version in store is 2
    prod_data = client._store_data[f"productions/{prod_id}"]
    assert prod_data["last_aggregate_version"] == 2

    transport = InMemoryTransport()
    published_events: list[CreditLockEvent] = []

    def worker_apply(e: CreditLockEvent) -> None:
        published_events.append(e)
        applied = store.apply(e)
        assert applied is True, f"store.apply failed on event version {e.aggregate_version}"

    transport.publish = MagicMock(side_effect=worker_apply)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=2.0,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    res = synchronize_release_checkpoint(
        authorization=auth,
        transport=transport,
        store=store,
        settings=settings,
    )

    assert len(published_events) == 1
    checkpoint_event = published_events[0]
    # Must be assigned version 3 (strictly > 2)
    assert checkpoint_event.aggregate_version == 3
    assert res.status == "SYNCHRONIZED"
    assert store.has_processed_event(prod_id, checkpoint_event.event_id) is True

    # Verify store has advanced to version 3
    updated_prod = client._store_data[f"productions/{prod_id}"]
    assert updated_prod["last_aggregate_version"] == 3


def test_c1_r_two_distinct_authorizations_receive_unique_increasing_versions_and_both_synchronize() -> None:
    """
    Repair test: Two distinct authorizations for one production receive unique, increasing
    versions and both synchronize.
    """
    prod_id = "prod-multi-auth"
    auth1 = _sample_authorization(production_id=prod_id, auth_id="auth-seq-1")
    auth2 = _sample_authorization(production_id=prod_id, auth_id="auth-seq-2")

    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    _seed_credit_roll(store, prod_id, version=1)

    transport = InMemoryTransport()
    published_events: list[CreditLockEvent] = []

    def worker_apply(e: CreditLockEvent) -> None:
        published_events.append(e)
        applied = store.apply(e)
        assert applied is True, f"store.apply failed on event with version {e.aggregate_version}"

    transport.publish = MagicMock(side_effect=worker_apply)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=2.0,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    # Sync Auth 1
    res1 = synchronize_release_checkpoint(
        authorization=auth1,
        transport=transport,
        store=store,
        settings=settings,
    )
    assert res1.status == "SYNCHRONIZED"

    # Sync Auth 2
    res2 = synchronize_release_checkpoint(
        authorization=auth2,
        transport=transport,
        store=store,
        settings=settings,
    )
    assert res2.status == "SYNCHRONIZED"

    assert len(published_events) == 2
    evt1, evt2 = published_events
    assert evt1.event_id != evt2.event_id
    assert evt1.aggregate_version == 2
    assert evt2.aggregate_version == 3
    assert evt2.aggregate_version > evt1.aggregate_version

    assert store.has_processed_event(prod_id, evt1.event_id) is True
    assert store.has_processed_event(prod_id, evt2.event_id) is True


def test_c1_r_retrying_same_authorization_retains_event_id_and_version_without_republishing() -> None:
    """
    Repair test: Retrying the same authorization retains the same event ID and assigned
    version, and does not republish after acknowledgement.
    """
    prod_id = "prod-retry-auth"
    auth = _sample_authorization(production_id=prod_id, auth_id="auth-stable-1")

    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    _seed_credit_roll(store, prod_id, version=1)

    transport = InMemoryTransport()
    publish_count = 0

    def worker_apply(e: CreditLockEvent) -> None:
        nonlocal publish_count
        publish_count += 1
        store.apply(e)

    transport.publish = MagicMock(side_effect=worker_apply)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=2.0,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    # First attempt
    res1 = synchronize_release_checkpoint(
        authorization=auth,
        transport=transport,
        store=store,
        settings=settings,
    )
    assert res1.status == "SYNCHRONIZED"
    assert publish_count == 1

    # Retry attempt
    res2 = synchronize_release_checkpoint(
        authorization=auth,
        transport=transport,
        store=store,
        settings=settings,
    )
    assert res2.status == "SYNCHRONIZED"
    assert res2.event_id == res1.event_id
    # No extra publish on retry
    assert publish_count == 1

    # Direct allocation query returns same version
    v_allocated = store.allocate_aggregate_version(prod_id, res1.event_id)
    assert v_allocated == 2


def test_c1_r_concurrent_distinct_allocations_cannot_receive_same_version() -> None:
    """
    Repair test: Concurrent distinct allocations cannot receive the same version.
    """
    prod_id = "prod-concurrent-alloc"
    client = FakeFirestoreClient({})

    lock = threading.Lock()

    def thread_safe_txn_runner[T](cl: Any, txn_func: Callable[[Any], T]) -> T:
        with lock:
            txn = cl.transaction()
            res = txn_func(txn)
            txn.commit()
            return res

    store = FirestoreProjectionStore(client=client, transaction_runner=thread_safe_txn_runner)
    _seed_credit_roll(store, prod_id, version=1)

    allocated_versions: list[int] = []
    threads: list[threading.Thread] = []

    def allocate_job(e_id: str) -> None:
        v = store.allocate_aggregate_version(prod_id, e_id)
        with lock:
            allocated_versions.append(v)

    for i in range(10):
        t = threading.Thread(target=allocate_job, args=(f"event-concurrent-{i}",))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(allocated_versions) == 10
    # All allocated versions must be unique
    assert len(set(allocated_versions)) == 10
    # All allocated versions must be >= 2
    assert min(allocated_versions) >= 2
    assert max(allocated_versions) == 11


def test_6_runtime_disabled_export_never_constructs_confluent_or_firestore_event_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", False)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Verified against exhibits.",
        },
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )

    with (
        patch("creditlock.events.transport.ConfluentTransport.from_settings") as mock_conf,
        patch("creditlock.events.firestore_store.FirestoreProjectionStore.from_settings") as mock_fs,
    ):
        exp_res = client.post(
            f"/productions/{pid}/export",
            headers={"Authorization": f"Bearer {app_tok}"},
        )
        assert exp_res.status_code == 200
        assert mock_conf.called is False
        assert mock_fs.called is False

        exp_data = exp_res.json()
        assert exp_data.get("event_sync") is None


def test_7_runtime_enabled_synchronization_failure_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Verified against exhibits.",
        },
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )

    with patch(
        "creditlock.events.release_checkpoint.synchronize_release_checkpoint",
        side_effect=ReleaseCheckpointUnavailable("Release event synchronization is temporarily unavailable."),
    ):
        exp_res = client.post(
            f"/productions/{pid}/export",
            headers={"Authorization": f"Bearer {app_tok}"},
        )
        assert exp_res.status_code == 503
        assert exp_res.json() == {
            "code": "CONFLUENT_SYNC_UNAVAILABLE",
            "retryable": True,
            "message": "Release event synchronization is temporarily unavailable.",
        }


def test_8_package_assembly_replay_gcs_not_called_after_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Verified against exhibits.",
        },
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )

    with (
        patch(
            "creditlock.events.release_checkpoint.synchronize_release_checkpoint",
            side_effect=ReleaseCheckpointUnavailable("Failed to sync"),
        ),
        patch("creditlock.evidence.delivery.assemble_delivery_package") as mock_assemble,
        patch("creditlock.evidence.replay.replay_bundle") as mock_replay,
        patch("creditlock.evidence.storage.GCSStore.put") as mock_gcs,
    ):
        exp_res = client.post(
            f"/productions/{pid}/export",
            headers={"Authorization": f"Bearer {app_tok}"},
        )
        assert exp_res.status_code == 503
        assert mock_assemble.called is False
        assert mock_replay.called is False
        assert mock_gcs.called is False


def test_9_successful_sync_occurs_before_package_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Verified against exhibits.",
        },
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )

    call_order: list[str] = []

    def mock_sync(auth: Any) -> ReleaseCheckpointSyncResult:
        call_order.append("sync")
        return ReleaseCheckpointSyncResult(
            status="SYNCHRONIZED",
            transport="CONFLUENT_CLOUD",
            topic="creditlock.production.events",
            event_type="resolution.recorded",
            event_id="eid-ordered-123",
            projection_backend="FIRESTORE",
        )

    from creditlock.evidence.delivery import assemble_delivery_package as real_assemble

    def wrap_assemble(*args: Any, **kwargs: Any) -> Any:
        call_order.append("assemble")
        return real_assemble(*args, **kwargs)

    with (
        patch("creditlock.events.release_checkpoint.synchronize_release_checkpoint", side_effect=mock_sync),
        patch("creditlock.evidence.delivery.assemble_delivery_package", side_effect=wrap_assemble),
    ):
        exp_res = client.post(
            f"/productions/{pid}/export",
            headers={"Authorization": f"Bearer {app_tok}"},
        )
        assert exp_res.status_code == 200
        assert call_order == ["sync", "assemble"]


def test_10_export_response_contains_expected_safe_event_sync_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Verified against exhibits.",
        },
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )

    with patch(
        "creditlock.events.release_checkpoint.synchronize_release_checkpoint",
        return_value=ReleaseCheckpointSyncResult(
            status="SYNCHRONIZED",
            transport="CONFLUENT_CLOUD",
            topic="creditlock.production.events",
            event_type="resolution.recorded",
            event_id="eid-safe-999",
            projection_backend="FIRESTORE",
        ),
    ):
        exp_res = client.post(
            f"/productions/{pid}/export",
            headers={"Authorization": f"Bearer {app_tok}"},
        )
        assert exp_res.status_code == 200
        data = exp_res.json()
        assert data["gate_state"] == "READY_TO_EXPORT"
        assert data["event_sync"] == {
            "status": "SYNCHRONIZED",
            "transport": "CONFLUENT_CLOUD",
            "topic": "creditlock.production.events",
            "event_type": "resolution.recorded",
            "event_id": "eid-safe-999",
            "projection_backend": "FIRESTORE",
        }


def test_11_has_processed_event_uses_exact_document_path_and_performs_no_writes() -> None:
    initial_data = {
        "productions/prod-test-path/processed_events/evt-123": {
            "event_id": "evt-123",
            "event_type": "resolution.recorded",
        }
    }
    client = FakeFirestoreClient(dict(initial_data))
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)

    assert store.has_processed_event("prod-test-path", "evt-123") is True
    assert store.has_processed_event("prod-test-path", "evt-nonexistent") is False
    assert client._store_data == initial_data


def test_14_conditional_settings_validation_fails_closed_without_revealing_secrets() -> None:
    # 1. confluent_runtime_enabled=True requires all 5 settings
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            confluent_runtime_enabled=True,
            google_cloud_project="",
            confluent_bootstrap_servers="",
            confluent_api_key="",
            confluent_api_secret="",
            confluent_topic="",
            jwt_secret="01234567890123456789012345678901",
            store_backend="memory_demo",
            allow_in_memory_demo=True,
        )
    err_str = str(exc_info.value)
    assert "GOOGLE_CLOUD_PROJECT" in err_str
    assert "CONFLUENT_BOOTSTRAP_SERVERS" in err_str
    assert "CONFLUENT_API_KEY" in err_str
    assert "CONFLUENT_API_SECRET" in err_str
    assert "CONFLUENT_TOPIC" in err_str

    # 2. Invalid timeout > 30s
    with pytest.raises(ValidationError) as exc_info2:
        Settings(
            confluent_sync_timeout_seconds=31.0,
            jwt_secret="01234567890123456789012345678901",
            store_backend="memory_demo",
            allow_in_memory_demo=True,
        )
    assert "timeout" in str(exc_info2.value).lower()

    # 3. Invalid negative / infinite timeout or poll interval
    with pytest.raises(ValidationError):
        Settings(
            confluent_sync_timeout_seconds=-1.0,
            jwt_secret="01234567890123456789012345678901",
            store_backend="memory_demo",
            allow_in_memory_demo=True,
        )

    with pytest.raises(ValidationError):
        Settings(
            confluent_sync_poll_interval_seconds=0.0,
            jwt_secret="01234567890123456789012345678901",
            store_backend="memory_demo",
            allow_in_memory_demo=True,
        )


def test_c1_r2_concurrent_checkpoints_ordered_publication_no_stale_drop() -> None:
    """
    C1-R2: Two concurrent synchronize_release_checkpoint calls for distinct
    authorizations of the same production.
    When v2 pauses after allocation, v3 must not overtake v2 and cause v2 to be dropped as stale.
    Both calls must obtain exact processed-event acknowledgements and neither is dropped as stale.
    """
    import time

    prod_id = "prod-c1-r2-order"
    auth1 = _sample_authorization(production_id=prod_id, auth_id="auth-ord-1")
    auth2 = _sample_authorization(production_id=prod_id, auth_id="auth-ord-2")

    client = FakeFirestoreClient({})
    lock = threading.RLock()

    def thread_safe_txn_runner[T](cl: Any, txn_func: Callable[[Any], T]) -> T:
        with lock:
            txn = cl.transaction()
            res = txn_func(txn)
            txn.commit()
            return res

    store = FirestoreProjectionStore(client=client, transaction_runner=thread_safe_txn_runner)
    _seed_credit_roll(store, prod_id, version=1)

    transport = InMemoryTransport()
    v2_allocated_and_paused = threading.Event()
    allow_v2_publish = threading.Event()
    published_order: list[int] = []

    def tracking_publish(e: CreditLockEvent) -> None:
        if e.aggregate_version == 2:
            v2_allocated_and_paused.set()
            if not allow_v2_publish.wait(timeout=2.0):
                raise TimeoutError("allow_v2_publish wait timed out in test")
        with lock:
            published_order.append(e.aggregate_version)
            store.apply(e)

    transport.publish = MagicMock(side_effect=tracking_publish)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=0.4,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    res1: list[ReleaseCheckpointSyncResult] = []
    res2: list[ReleaseCheckpointSyncResult] = []
    errors: list[Exception] = []

    def run_auth1() -> None:
        try:
            r = synchronize_release_checkpoint(
                authorization=auth1,
                transport=transport,
                store=store,
                settings=settings,
            )
            res1.append(r)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def run_auth2() -> None:
        try:
            r = synchronize_release_checkpoint(
                authorization=auth2,
                transport=transport,
                store=store,
                settings=settings,
            )
            res2.append(r)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run_auth1)
    t2 = threading.Thread(target=run_auth2)

    t1.start()
    assert v2_allocated_and_paused.wait(timeout=2.0) is True

    t2.start()
    time.sleep(0.05)

    allow_v2_publish.set()

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert errors == [], f"Unexpected errors during synchronization: {errors}"
    assert len(res1) == 1
    assert len(res2) == 1
    assert res1[0].status == "SYNCHRONIZED"
    assert res2[0].status == "SYNCHRONIZED"
    assert published_order == [2, 3]

    assert store.has_processed_event(prod_id, res1[0].event_id) is True
    assert store.has_processed_event(prod_id, res2[0].event_id) is True


def test_c1_r2_later_allocated_checkpoint_does_not_overtake_earlier_unacknowledged() -> None:
    """
    Requirement 6 & 8: A later allocated checkpoint must not overtake an earlier
    unacknowledged checkpoint for the same production.
    If earlier checkpoint v2 is unacknowledged, later request v3 may wait within
    confluent_sync_timeout_seconds and return safe 503, but must not publish ahead.
    """
    prod_id = "prod-c1-r2-no-overtake"
    auth_earlier = _sample_authorization(production_id=prod_id, auth_id="auth-earlier-1")
    auth_later = _sample_authorization(production_id=prod_id, auth_id="auth-later-2")

    client = FakeFirestoreClient({})
    store = FirestoreProjectionStore(client=client, transaction_runner=fake_transaction_runner)
    _seed_credit_roll(store, prod_id, version=1)

    # Earlier checkpoint allocates v2, but is unacknowledged
    v_earlier = store.allocate_aggregate_version(prod_id, f"evt-{auth_earlier.authorization_id}")
    assert v_earlier == 2

    transport = InMemoryTransport()
    published_events: list[CreditLockEvent] = []

    def mock_publish(e: CreditLockEvent) -> None:
        published_events.append(e)
        store.apply(e)

    transport.publish = MagicMock(side_effect=mock_publish)  # type: ignore[method-assign]

    settings = Settings(
        google_cloud_project="test-proj",
        confluent_bootstrap_servers="test-server:9092",
        confluent_api_key="key",
        confluent_api_secret="secret",
        confluent_topic="creditlock.production.events",
        confluent_runtime_enabled=True,
        confluent_sync_timeout_seconds=0.1,
        confluent_sync_poll_interval_seconds=0.01,
        jwt_secret="01234567890123456789012345678901",
        store_backend="memory_demo",
        allow_in_memory_demo=True,
    )

    with pytest.raises(ReleaseCheckpointUnavailable) as exc_info:
        synchronize_release_checkpoint(
            authorization=auth_later,
            transport=transport,
            store=store,
            settings=settings,
        )

    assert "timed out" in str(exc_info.value).lower()
    # Must NOT publish ahead!
    assert len(published_events) == 0
    assert transport.publish.call_count == 0
