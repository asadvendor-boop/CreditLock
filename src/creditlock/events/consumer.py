"""
Production Confluent event ingestion worker.

Consumes CreditLock events from the event bus (Kafka/Confluent) and projects them
atomically into the projection store (Firestore / Projector).

At-least-once delivery & Idempotency semantics:
- Kafka manual offset commits are enabled (enable.auto.commit=false).
- Offset commit is performed ONLY AFTER store.apply(event) returns True (successfully applied)
  or returns False (safe duplicate event_id or stale aggregate_version).
- Offset commit is NOT performed when:
  - Event envelope or payload fails Pydantic validation (malformed message).
  - ProjectionError is raised by the Projector.
  - An underlying storage/database exception occurs.
- Never logs event payload contents or Kafka credentials.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from creditlock.events.models import CreditLockEvent
from creditlock.events.projection import ProjectionError

logger = logging.getLogger(__name__)


class EventIngestionWorker:
    """
    Event ingestion worker processing events across all productions.
    """

    def __init__(
        self,
        transport: Any,
        store: Any,
        topic: str = "creditlock.production.events",
        consumer_group: str = "creditlock-ingestion",
        consumer: Any = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._topic = topic
        self._consumer_group = consumer_group
        self._running = False
        self._consumer = consumer

    def _get_consumer(self) -> Any:
        if self._consumer is None:
            if hasattr(self._transport, "_consumer_factory"):
                cfg = self._transport._consumer_config()
                cfg["group.id"] = self._consumer_group
                cfg["enable.auto.commit"] = False
                self._consumer = self._transport._consumer_factory(cfg)
                self._consumer.subscribe([self._topic])
            else:
                raise RuntimeError("No consumer available for worker.")
        return self._consumer

    def process_single_message(self, msg: Any) -> bool:
        """
        Process a single Kafka message:
        1. Parse & validate CreditLockEvent
        2. Apply to store
        3. If apply() returns True or False (safe duplicate/stale), commit offset
        4. If validation, ProjectionError, or storage exception occurs, DO NOT commit offset.

        Returns True if offset was committed, False otherwise.
        """
        if msg is None:
            return False
        if hasattr(msg, "error") and msg.error():
            logger.warning("Kafka message error; offset not committed.")
            return False

        raw_val = msg.value() if hasattr(msg, "value") else msg
        if raw_val is None:
            return False

        raw_bytes: bytes = raw_val if isinstance(raw_val, bytes) else str(raw_val).encode()

        # 1. Validation phase
        try:
            event = CreditLockEvent.model_validate_json(raw_bytes)
        except Exception:  # noqa: BLE001
            logger.warning("Skipping malformed message: validation failed.")
            return False

        # 2. Projection apply phase
        try:
            applied = self._store.apply(event)
        except ProjectionError as pe:
            logger.warning(
                "ProjectionError on event %s (type=%s): %s", event.event_id, event.event_type, pe
            )
            return False
        except Exception as ex:  # noqa: BLE001
            logger.error("Storage exception on event %s: %s", event.event_id, ex)
            return False

        # 3. Commit offset phase
        if applied in (True, False):
            try:
                consumer = self._consumer
                if consumer is not None and hasattr(consumer, "commit") and hasattr(msg, "topic"):
                    consumer.commit(message=msg, asynchronous=False)
                elif hasattr(msg, "commit"):
                    msg.commit()
            except Exception as cex:  # noqa: BLE001
                logger.error("Offset commit exception on event %s: %s", event.event_id, cex)
                return False
            logger.info(
                "Ingested event %s (production=%s, result=%s)",
                event.event_id,
                event.production_id,
                applied,
            )
            return True

        return False

    def run_once(self, max_messages: int = 100, poll_timeout_seconds: float = 1.0) -> int:
        consumer = self._get_consumer()
        committed_count = 0
        deadline = time.monotonic() + poll_timeout_seconds

        while committed_count < max_messages and time.monotonic() < deadline:
            msg = consumer.poll(timeout=0.1)
            if msg is None:
                continue
            if self.process_single_message(msg):
                committed_count += 1

        return committed_count

    def start(self) -> None:
        self._running = True
        logger.info("EventIngestionWorker started on topic=%s", self._topic)
        while self._running:
            try:
                self.run_once(max_messages=50, poll_timeout_seconds=1.0)
            except Exception as ex:  # noqa: BLE001
                logger.error("Ingestion worker loop error: %s", ex)
                time.sleep(0.5)

    def stop(self) -> None:
        self._running = False
        if self._consumer is not None and hasattr(self._consumer, "close"):
            try:
                self._consumer.close()
            except Exception as ex:  # noqa: BLE001
                logger.warning("Error closing consumer during shutdown: %s", ex)
        logger.info("EventIngestionWorker stopped.")
