"""
Transport interface and in-memory fake for the creditlock event bus.

ConfluentTransport requires the ``confluent-kafka`` package and live
Confluent Cloud credentials (see Settings).  All other code continues to
depend only on the EventTransport ABC and InMemoryTransport.

Known interface limitation
──────────────────────────
``EventTransport.consume(production_id)`` is defined as a pull-all operation
that returns every event for a production.  For Kafka this is an approximation:
``ConfluentTransport.consume()`` polls for up to ``poll_timeout_seconds`` and
returns only messages whose Kafka key matches ``production_id``.  It does NOT
seek to the beginning of the partition, so it returns messages that arrive
*after* the consumer group was created.  For bounded integration tests (publish
then consume in the same session) this is sufficient.  Long-term replay
requires a separate offset-management strategy outside this interface.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from creditlock.events.models import CreditLockEvent

logger = logging.getLogger(__name__)


class EventTransport(ABC):
    """Abstract publish/consume interface."""

    @abstractmethod
    def publish(self, event: CreditLockEvent) -> None:
        """Publish an event.  Must be idempotent on the consumer side."""

    @abstractmethod
    def consume(self, production_id: str) -> list[CreditLockEvent]:
        """
        Return events for a production in delivery order.

        The returned list may contain duplicates (at-least-once delivery).
        Consumers must implement idempotency via event_id.
        """


class InMemoryTransport(EventTransport):
    """
    In-memory fake transport for tests.

    Guarantees:
    - Per-production ordering: events for the same production_id are returned
      in the order they were published (unless inject_out_of_order is used).
    - Duplicate delivery: publish the same event object twice to simulate
      at-least-once redelivery.
    - Out-of-order injection: use inject_out_of_order to prepend an event at
      an arbitrary position, simulating network reordering between partitions.
    """

    def __init__(self) -> None:
        # production_id -> ordered list of events (may contain duplicates)
        self._store: dict[str, list[CreditLockEvent]] = collections.defaultdict(list)

    def publish(self, event: CreditLockEvent) -> None:
        self._store[event.production_id].append(event)

    def consume(self, production_id: str) -> list[CreditLockEvent]:
        return list(self._store[production_id])

    def inject_out_of_order(self, event: CreditLockEvent, *, before_index: int) -> None:
        """
        Insert *event* into the queue at *before_index*, shifting later events
        right.  Use before_index=0 to prepend.  Raises IndexError if
        before_index is out of range for the production's queue.
        """
        queue = self._store[event.production_id]
        if before_index > len(queue):
            raise IndexError(
                f"before_index {before_index} out of range for queue of length {len(queue)}"
            )
        queue.insert(before_index, event)

    def clear(self, production_id: str | None = None) -> None:
        """Clear one production's queue, or all queues if production_id is None."""
        if production_id is None:
            self._store.clear()
        else:
            self._store.pop(production_id, None)


# ── Confluent Cloud transport ─────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class DeliveryMetadata:
    """Metadata returned by a successful Kafka produce call."""

    topic: str
    partition: int
    offset: int


class ConfluentTransport(EventTransport):
    """
    Kafka transport backed by Confluent Cloud (SASL_SSL / PLAIN).

    Parameters
    ----------
    bootstrap_servers:
        Confluent Cloud bootstrap server address (from Settings).
    api_key:
        Confluent Cloud Kafka API key.  Never logged or re-raised in
        exception messages.
    api_secret:
        Confluent Cloud Kafka API secret.  Never logged or re-raised in
        exception messages.
    topic:
        Target Kafka topic (default: ``creditlock.production.events``).
    consumer_group:
        Consumer group ID used by ``consume()``.
    poll_timeout_seconds:
        Maximum total wall-clock time (seconds) ``consume()`` will spend
        polling.  Defaults to 10 s.  Must be finite — never block
        indefinitely.
    delivery_timeout_ms:
        Per-message delivery timeout in milliseconds passed to the
        producer.  Defaults to 30 000 ms.
    _producer_factory / _consumer_factory:
        Injectable callables that return a producer / consumer client.
        Provided for unit-testing without real Kafka credentials.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        api_key: str,
        api_secret: str,
        topic: str = "creditlock.production.events",
        consumer_group: str = "creditlock-default",
        poll_timeout_seconds: float = 10.0,
        delivery_timeout_ms: int = 30_000,
        _producer_factory: Callable[[dict[str, Any]], Any] | None = None,
        _consumer_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        # Credentials are stored but never surfaced in repr, str, logs, or
        # exceptions — see _safe_produce() and _safe_consume().
        self._bootstrap_servers = bootstrap_servers
        self._api_key = api_key
        self._api_secret = api_secret
        self._topic = topic
        self._consumer_group = consumer_group
        self._poll_timeout_seconds = poll_timeout_seconds
        self._delivery_timeout_ms = delivery_timeout_ms

        # Import deferred so that importing transport.py does not require the
        # confluent-kafka package unless ConfluentTransport is instantiated.
        if _producer_factory is None or _consumer_factory is None:
            from confluent_kafka import (  # type: ignore[import-untyped,unused-ignore]
                Consumer,
                Producer,
            )

            self._producer_factory: Callable[[dict[str, Any]], Any] = Producer
            self._consumer_factory: Callable[[dict[str, Any]], Any] = Consumer
        else:
            self._producer_factory = _producer_factory
            self._consumer_factory = _consumer_factory

        self._last_delivery: DeliveryMetadata | None = None

    # ── Public helpers ────────────────────────────────────────────────────────

    @classmethod
    def from_settings(
        cls,
        consumer_group: str = "creditlock-default",
        poll_timeout_seconds: float = 10.0,
    ) -> ConfluentTransport:
        """Construct from application settings (uses ``get_settings()``)."""
        from creditlock.settings import get_settings

        s = get_settings()
        return cls(
            bootstrap_servers=s.confluent_bootstrap_servers,
            api_key=s.confluent_api_key,
            api_secret=s.confluent_api_secret,
            topic=s.confluent_topic,
            consumer_group=consumer_group,
            poll_timeout_seconds=poll_timeout_seconds,
        )

    @property
    def last_delivery(self) -> DeliveryMetadata | None:
        """Metadata from the most recent successful ``publish()`` call."""
        return self._last_delivery

    # ── EventTransport implementation ─────────────────────────────────────────

    def publish(self, event: CreditLockEvent) -> None:
        """
        Serialize *event* to JSON and produce it to Kafka.

        - Message key  : ``event.production_id`` (UTF-8 bytes)
        - Idempotent   : yes (``enable.idempotence=true``)
        - Acks          : all
        - Delivery timeout: ``delivery_timeout_ms`` ms
        - Credentials  : SASL_SSL / PLAIN — never logged or re-raised
        """
        producer: Any = self._producer_factory(self._producer_config())

        delivery_error: list[Exception] = []
        meta: list[DeliveryMetadata] = []

        def _on_delivery(err: Any, msg: Any) -> None:
            if err:
                # Do NOT include err in the stored message — some exception
                # types from confluent_kafka include config details.
                delivery_error.append(RuntimeError("Kafka delivery failed"))
            else:
                # msg has .topic(), .partition(), .offset() methods
                meta.append(
                    DeliveryMetadata(
                        topic=msg.topic(),
                        partition=msg.partition(),
                        offset=msg.offset(),
                    )
                )

        payload_bytes = event.model_dump_json().encode()
        key_bytes = event.production_id.encode()

        producer.produce(
            topic=self._topic,
            key=key_bytes,
            value=payload_bytes,
            on_delivery=_on_delivery,
        )
        producer.flush()

        if delivery_error:
            raise delivery_error[0]
        if meta:
            self._last_delivery = meta[0]
            logger.info(
                "event published topic=%s partition=%d offset=%d event_id=%s",
                meta[0].topic,
                meta[0].partition,
                meta[0].offset,
                event.event_id,
            )

    def consume(self, production_id: str) -> list[CreditLockEvent]:
        """
        Poll Kafka and return events whose key matches *production_id*.

        - Automatic offset commits: disabled (``enable.auto.commit=false``)
        - Offsets are committed manually after successful deserialization.
        - Malformed envelopes (Pydantic validation failures) are logged and
          skipped — fail closed: the bad message does not enter the result.
        - Polling terminates after ``poll_timeout_seconds`` total elapsed time.
        """
        consumer: Any = self._consumer_factory(self._consumer_config())
        consumer.subscribe([self._topic])

        results: list[CreditLockEvent] = []
        deadline = time.monotonic() + self._poll_timeout_seconds
        per_poll = min(1.0, self._poll_timeout_seconds)

        try:
            while time.monotonic() < deadline:
                msg: Any = consumer.poll(timeout=per_poll)
                if msg is None:
                    continue
                if msg.error():
                    logger.warning("Kafka consumer error (skipped)")
                    continue

                key: Any = msg.key()
                if key is None:
                    continue
                msg_key = key.decode() if isinstance(key, bytes) else str(key)
                if msg_key != production_id:
                    continue

                raw: Any = msg.value()
                if raw is None:
                    continue
                raw_bytes: bytes = raw if isinstance(raw, bytes) else raw.encode()

                try:
                    event = CreditLockEvent.model_validate_json(raw_bytes)
                except Exception:  # noqa: BLE001
                    # Malformed envelope — fail closed, do not surface raw bytes
                    logger.warning("Skipping malformed Kafka message: Pydantic validation failed")
                    continue

                results.append(event)
                consumer.commit(message=msg, asynchronous=False)
        finally:
            consumer.close()

        return results

    # ── Private config helpers ────────────────────────────────────────────────

    def _producer_config(self) -> dict[str, object]:
        return {
            "bootstrap.servers": self._bootstrap_servers,
            "security.protocol": "SASL_SSL",
            "sasl.mechanisms": "PLAIN",
            "sasl.username": self._api_key,
            "sasl.password": self._api_secret,
            "enable.idempotence": True,
            "acks": "all",
            "delivery.timeout.ms": self._delivery_timeout_ms,
        }

    def _consumer_config(self) -> dict[str, object]:
        return {
            "bootstrap.servers": self._bootstrap_servers,
            "security.protocol": "SASL_SSL",
            "sasl.mechanisms": "PLAIN",
            "sasl.username": self._api_key,
            "sasl.password": self._api_secret,
            "group.id": self._consumer_group,
            # earliest: a fresh consumer group (unique per test/run) starts from
            # the beginning of retained messages so that events produced before
            # this consumer subscribes are not missed.  For long-term production
            # use, operators should manage offsets explicitly.
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
