"""
Unit tests for EventIngestionWorker.
"""

from __future__ import annotations

import uuid
from typing import Any

from creditlock.events.consumer import EventIngestionWorker
from creditlock.events.models import (
    EVENT_TYPE_ARTIFACT_RENDERED,
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    ArtifactRenderedPayload,
    CreditLockEvent,
    CreditRollSubmittedPayload,
)
from creditlock.events.projection import ProjectionError, Projector

PROD1 = "prod-consumer-001"
PROD2 = "prod-consumer-002"
AT = "2026-08-01T00:00:00Z"


class FakeMessage:
    def __init__(
        self,
        value: bytes | str | None,
        topic: str = "creditlock.production.events",
        key: bytes | None = None,
    ):
        self._value = value
        self.topic_name = topic
        self.key_bytes = key
        self.committed = False
        self._error = None

    def error(self) -> Any:
        return self._error

    def value(self) -> bytes | str | None:
        return self._value

    def topic(self) -> str:
        return self.topic_name

    def commit(self) -> None:
        self.committed = True


class FakeKafkaConsumer:
    def __init__(self, messages: list[FakeMessage]):
        self.messages = list(messages)
        self.committed_messages: list[FakeMessage] = []
        self.closed = False

    def poll(self, timeout: float = 0.1) -> FakeMessage | None:
        if self.messages:
            return self.messages.pop(0)
        return None

    def commit(self, message: FakeMessage, asynchronous: bool = False) -> None:
        message.committed = True
        self.committed_messages.append(message)

    def close(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(
        self, fail_on_event_id: str | None = None, exception_on_event_id: str | None = None
    ):
        self.applied_events: list[CreditLockEvent] = []
        self.fail_on_event_id = fail_on_event_id
        self.exception_on_event_id = exception_on_event_id
        self._projector = Projector()

    def apply(self, event: CreditLockEvent) -> bool:
        if self.fail_on_event_id and event.event_id == self.fail_on_event_id:
            raise ProjectionError("Forced ProjectionError", event_type=event.event_type)
        if self.exception_on_event_id and event.event_id == self.exception_on_event_id:
            raise RuntimeError("Database connection lost")

        res = self._projector.apply(event)
        self.applied_events.append(event)
        return res


def _make_event(
    prod_id: str = PROD1, version: int = 1, event_id: str | None = None
) -> CreditLockEvent:
    eid = event_id or str(uuid.uuid4())
    return CreditLockEvent(
        event_id=eid,
        event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        production_id=prod_id,
        aggregate_version=version,
        actor_id="actor-test",
        occurred_at=AT,
        payload=CreditRollSubmittedPayload(
            production_id=prod_id,
            manifest_id=f"manifest-{prod_id}",
            manifest_hash="mhash-test",
            submitted_by="actor-test",
            at=AT,
        ),
    )


class TestEventIngestionWorker:
    def test_successful_apply_then_offset_commit(self) -> None:
        evt = _make_event(PROD1, version=1)
        msg = FakeMessage(evt.model_dump_json().encode())
        consumer = FakeKafkaConsumer([msg])
        store = FakeStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert committed == 1
        assert msg.committed is True
        assert len(store.applied_events) == 1
        assert store.applied_events[0].event_id == evt.event_id

    def test_duplicate_stale_event_then_safe_offset_commit(self) -> None:
        evt = _make_event(PROD1, version=1)
        # Deliver same event twice
        msg1 = FakeMessage(evt.model_dump_json().encode())
        msg2 = FakeMessage(evt.model_dump_json().encode())
        consumer = FakeKafkaConsumer([msg1, msg2])
        store = FakeStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        c1 = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)
        c2 = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert c1 == 1
        assert c2 == 1
        assert msg1.committed is True
        assert msg2.committed is True

    def test_malformed_event_not_committed(self) -> None:
        msg = FakeMessage(b"INVALID_JSON_PAYLOAD")
        consumer = FakeKafkaConsumer([msg])
        store = FakeStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert committed == 0
        assert msg.committed is False
        assert len(store.applied_events) == 0

    def test_projection_error_not_committed(self) -> None:
        # artifact.rendered without credit_roll.submitted raises ProjectionError
        render_evt = CreditLockEvent(
            event_id="eid-orphan",
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD1,
            aggregate_version=1,
            actor_id="actor-test",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD1,
                manifest_id="m-missing",
                artifact_index_digest="digest-123",
                render_profile_version="v1",
                at=AT,
            ),
        )
        msg = FakeMessage(render_evt.model_dump_json().encode())
        consumer = FakeKafkaConsumer([msg])
        store = FakeStore(fail_on_event_id="eid-orphan")

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert committed == 0
        assert msg.committed is False

    def test_firestore_exception_not_committed(self) -> None:
        evt = _make_event(PROD1, version=1, event_id="eid-db-fail")
        msg = FakeMessage(evt.model_dump_json().encode())
        consumer = FakeKafkaConsumer([msg])
        store = FakeStore(exception_on_event_id="eid-db-fail")

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert committed == 0
        assert msg.committed is False

    def test_graceful_shutdown(self) -> None:
        consumer = FakeKafkaConsumer([])
        store = FakeStore()
        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)

        worker.stop()
        assert worker._running is False
        assert consumer.closed is True

    def test_multiple_productions_processed_from_shared_topic(self) -> None:
        evt1 = _make_event(PROD1, version=1)
        evt2 = _make_event(PROD2, version=1)
        msg1 = FakeMessage(evt1.model_dump_json().encode())
        msg2 = FakeMessage(evt2.model_dump_json().encode())
        consumer = FakeKafkaConsumer([msg1, msg2])
        store = FakeStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=2, poll_timeout_seconds=0.5)

        assert committed == 2
        assert msg1.committed is True
        assert msg2.committed is True
        assert len(store.applied_events) == 2
        assert {e.production_id for e in store.applied_events} == {PROD1, PROD2}

    def test_offset_commit_failure_handled(self) -> None:
        class FailingCommitConsumer(FakeKafkaConsumer):
            def commit(self, message: FakeMessage, asynchronous: bool = False) -> None:
                raise RuntimeError("Broker connection lost during commit")

        evt = _make_event(PROD1, version=1)
        msg = FakeMessage(evt.model_dump_json().encode())
        consumer = FailingCommitConsumer([msg])
        store = FakeStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        committed = worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert committed == 0
        assert msg.committed is False

    def test_operation_order_store_apply_before_kafka_commit(self) -> None:
        call_order: list[str] = []

        class OrderingConsumer(FakeKafkaConsumer):
            def commit(self, message: FakeMessage, asynchronous: bool = False) -> None:
                call_order.append("kafka_commit")
                super().commit(message, asynchronous)

        class OrderingStore(FakeStore):
            def apply(self, event: CreditLockEvent) -> bool:
                call_order.append("store_apply")
                return super().apply(event)

        evt = _make_event(PROD1, version=1)
        msg = FakeMessage(evt.model_dump_json().encode())
        consumer = OrderingConsumer([msg])
        store = OrderingStore()

        worker = EventIngestionWorker(transport=None, store=store, consumer=consumer)
        worker.run_once(max_messages=1, poll_timeout_seconds=0.5)

        assert call_order == ["store_apply", "kafka_commit"]
