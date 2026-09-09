"""
Offline unit tests for ConfluentTransport.

No real Kafka credentials are required.  All Kafka I/O is replaced with
injected fake producer/consumer objects.

Coverage:
- production_id is used as the Kafka message key (UTF-8 bytes)
- serialization / deserialization preserves the full CreditLockEvent
- topic, partition, and offset are captured in DeliveryMetadata
- malformed events fail closed (ValidationError, skipped gracefully)
- InMemoryTransport remains unchanged and all existing contracts hold
- no API key or secret appears in logs, exceptions, or repr
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from creditlock.events.models import (
    EVENT_TYPE_MEMO_UPLOADED,
    CreditLockEvent,
    MemoUploadedPayload,
)
from creditlock.events.transport import (
    ConfluentTransport,
    InMemoryTransport,
)

# ── Shared fixture helpers ────────────────────────────────────────────────────

_PROD_ID = "prod-confluent-unit-001"
_AT = "2025-06-01T00:00:00Z"
_API_KEY = "UNIT_TEST_API_KEY"
_API_SECRET = "UNIT_TEST_API_SECRET"
_BOOTSTRAP = "pkc-unit.us-central1.gcp.confluent.cloud:9092"
_TOPIC = "creditlock.production.events"


def _make_event(
    production_id: str = _PROD_ID,
    event_id: str | None = None,
) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=EVENT_TYPE_MEMO_UPLOADED,
        production_id=production_id,
        aggregate_version=1,
        actor_id="actor-unit",
        occurred_at=_AT,
        payload=MemoUploadedPayload(
            production_id=production_id,
            doc_id="doc-unit-001",
            doc_hash="sha256-aabbcc",
            doc_type="CREDIT_MEMO",
            uploaded_by="actor-unit",
            at=_AT,
        ),
    )


# ── Fake producer / consumer factories ───────────────────────────────────────


class _FakeMessage:
    """Minimal confluent_kafka.Message stub for delivery callbacks and consume."""

    def __init__(
        self,
        topic: str = _TOPIC,
        partition: int = 0,
        offset: int = 42,
        key: bytes | None = None,
        value: bytes | None = None,
        error: object = None,
    ) -> None:
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._key = key
        self._value = value
        self._error = error

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def key(self) -> bytes | None:
        return self._key

    def value(self) -> bytes | None:
        return self._value

    def error(self) -> object:
        return self._error


class _FakeProducer:
    """Fake producer that captures produce() arguments and fires on_delivery."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.produced: list[dict[str, Any]] = []

    def produce(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        on_delivery: Any,
    ) -> None:
        self.produced.append(
            {"topic": topic, "key": key, "value": value, "on_delivery": on_delivery}
        )
        # Simulate successful delivery
        msg = _FakeMessage(topic=topic, partition=0, offset=42, key=key, value=value)
        on_delivery(None, msg)

    def flush(self) -> None:
        pass


class _FakeConsumer:
    """
    Fake consumer that returns a fixed queue of messages then None indefinitely.
    """

    def __init__(self, config: dict[str, Any], messages: list[_FakeMessage]) -> None:
        self.config = config
        self._messages = list(messages)
        self._idx = 0
        self.committed: list[_FakeMessage] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        pass

    def poll(self, timeout: float = 1.0) -> _FakeMessage | None:
        if self._idx < len(self._messages):
            msg = self._messages[self._idx]
            self._idx += 1
            return msg
        return None

    def commit(self, message: _FakeMessage, asynchronous: bool = False) -> None:
        self.committed.append(message)

    def close(self) -> None:
        self.closed = True


def _make_transport(
    messages: list[_FakeMessage] | None = None,
    topic: str = _TOPIC,
) -> tuple[ConfluentTransport, _FakeProducer, _FakeConsumer]:
    """Return a ConfluentTransport wired to fake producer/consumer."""
    msgs = messages or []
    fake_consumer = _FakeConsumer({}, msgs)

    def producer_factory(config: dict[str, Any]) -> _FakeProducer:
        return _FakeProducer(config)

    def consumer_factory(config: dict[str, Any]) -> _FakeConsumer:
        fake_consumer.config = config
        return fake_consumer

    transport = ConfluentTransport(
        bootstrap_servers=_BOOTSTRAP,
        api_key=_API_KEY,
        api_secret=_API_SECRET,
        topic=topic,
        consumer_group="unit-test-group",
        poll_timeout_seconds=0.1,  # fast for tests
        _producer_factory=producer_factory,
        _consumer_factory=consumer_factory,
    )
    fake_producer = _FakeProducer({})
    return transport, fake_producer, fake_consumer


# ── Test: production_id used as Kafka key ────────────────────────────────────


class TestProductionIdAsKey:
    def test_production_id_encoded_as_key(self) -> None:
        """production_id must be the UTF-8 Kafka key."""
        captured: list[dict[str, Any]] = []

        def producer_factory(config: dict[str, Any]) -> Any:
            p = _FakeProducer(config)
            captured.append({"producer": p})
            return p

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, [])

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        event = _make_event(production_id="prod-key-test")
        transport.publish(event)

        producer = captured[0]["producer"]
        assert len(producer.produced) == 1
        assert producer.produced[0]["key"] == b"prod-key-test"


# ── Test: serialization roundtrip ────────────────────────────────────────────


class TestSerializationRoundtrip:
    def test_published_json_deserializes_to_same_event(self) -> None:
        """Serialized JSON must round-trip to an identical CreditLockEvent."""
        captured: list[bytes] = []

        def producer_factory(config: dict[str, Any]) -> Any:
            class _Cap(_FakeProducer):
                def produce(self, topic: str, key: bytes, value: bytes, on_delivery: Any) -> None:
                    captured.append(value)
                    super().produce(topic, key, value, on_delivery)

            return _Cap(config)

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, [])

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        original = _make_event(event_id="fixed-id-roundtrip")
        transport.publish(original)

        assert len(captured) == 1
        recovered = CreditLockEvent.model_validate_json(captured[0])
        assert recovered.event_id == original.event_id
        assert recovered.event_type == original.event_type
        assert recovered.production_id == original.production_id
        assert recovered.aggregate_version == original.aggregate_version
        assert recovered.payload == original.payload


# ── Test: delivery metadata captured ─────────────────────────────────────────


class TestDeliveryMetadata:
    def test_last_delivery_populated_after_publish(self) -> None:
        """topic, partition, and offset must be captured after publish."""

        def producer_factory(config: dict[str, Any]) -> Any:
            return _FakeProducer(config)  # returns partition=0, offset=42

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, [])

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        assert transport.last_delivery is None
        transport.publish(_make_event())
        meta = transport.last_delivery
        assert meta is not None
        assert meta.topic == _TOPIC
        assert meta.partition == 0
        assert meta.offset == 42


# ── Test: malformed events fail closed ───────────────────────────────────────


class TestMalformedEventFailClosed:
    def test_malformed_message_is_skipped(self) -> None:
        """A message with invalid JSON must not appear in consume() results."""
        bad_msg = _FakeMessage(
            key=_PROD_ID.encode(),
            value=b'{"not": "a valid CreditLockEvent"}',
        )
        valid_event = _make_event()
        good_msg = _FakeMessage(
            key=_PROD_ID.encode(),
            value=valid_event.model_dump_json().encode(),
        )
        messages = [bad_msg, good_msg]

        def producer_factory(config: dict[str, Any]) -> Any:
            return _FakeProducer(config)

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, messages)

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        results = transport.consume(_PROD_ID)
        # Only the valid event should appear; the malformed one is silently dropped
        assert len(results) == 1
        assert results[0].event_id == valid_event.event_id


# ── Test: InMemoryTransport unchanged ────────────────────────────────────────


class TestInMemoryTransportUnchanged:
    def test_basic_publish_consume(self) -> None:
        t = InMemoryTransport()
        evt = _make_event()
        t.publish(evt)
        result = t.consume(_PROD_ID)
        assert len(result) == 1
        assert result[0].event_id == evt.event_id

    def test_inject_out_of_order(self) -> None:
        t = InMemoryTransport()
        e1 = _make_event(event_id="e1")
        e2 = _make_event(event_id="e2")
        t.publish(e1)
        t.inject_out_of_order(e2, before_index=0)
        result = t.consume(_PROD_ID)
        assert result[0].event_id == "e2"
        assert result[1].event_id == "e1"

    def test_clear_all(self) -> None:
        t = InMemoryTransport()
        t.publish(_make_event())
        t.clear()
        assert t.consume(_PROD_ID) == []


# ── Test: credentials never appear in logs or exceptions ─────────────────────


class TestCredentialSafety:
    def test_delivery_error_does_not_leak_credentials(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A delivery failure must not expose api_key or api_secret."""

        def producer_factory(config: dict[str, Any]) -> Any:
            class _ErrorProducer(_FakeProducer):
                def produce(self, topic: str, key: bytes, value: bytes, on_delivery: Any) -> None:
                    # Simulate a failure delivery callback
                    on_delivery("simulated error", None)

            return _ErrorProducer(config)

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, [])

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as exc_info:
            transport.publish(_make_event())

        err_text = str(exc_info.value)
        assert _API_KEY not in err_text
        assert _API_SECRET not in err_text
        # Logs must also be clean
        all_log = caplog.text
        assert _API_KEY not in all_log
        assert _API_SECRET not in all_log

    def test_repr_does_not_leak_credentials(self) -> None:
        """repr / str of ConfluentTransport must not contain secrets."""

        def producer_factory(config: dict[str, Any]) -> Any:
            return _FakeProducer(config)

        def consumer_factory(config: dict[str, Any]) -> Any:
            return _FakeConsumer(config, [])

        transport = ConfluentTransport(
            bootstrap_servers=_BOOTSTRAP,
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            topic=_TOPIC,
            poll_timeout_seconds=0.1,
            _producer_factory=producer_factory,
            _consumer_factory=consumer_factory,
        )

        text = repr(transport) + str(transport)
        assert _API_KEY not in text
        assert _API_SECRET not in text
