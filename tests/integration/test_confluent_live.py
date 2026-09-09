"""
Live integration test for ConfluentTransport against Confluent Cloud.

Gated by the environment variable:
    CREDITLOCK_RUN_LIVE_CONFLUENT=1

If the variable is absent or not equal to "1" this module skips every test
cleanly — even when .env is present and credentials are loaded.

The test:
  1. Creates a unique synthetic production_id and event_id.
  2. Publishes one memo.uploaded event to the real Confluent Cloud topic.
  3. Consumes that exact event.
  4. Asserts event_id, production_id, event_type, and payload match.
  5. Prints PASS, event_id, topic, partition, and offset.
  6. Never prints bootstrap server, API key, API secret, or full environment.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from creditlock.events.models import (
    EVENT_TYPE_MEMO_UPLOADED,
    CreditLockEvent,
    MemoUploadedPayload,
)
from creditlock.events.transport import ConfluentTransport

# Skip the entire module unless the gate variable is set.
_GATE = os.environ.get("CREDITLOCK_RUN_LIVE_CONFLUENT", "")
pytestmark = pytest.mark.skipif(
    _GATE != "1",
    reason=("Live Confluent test skipped — set CREDITLOCK_RUN_LIVE_CONFLUENT=1 to run"),
)


def _unique_production_id() -> str:
    return f"live-test-{uuid.uuid4().hex[:12]}"


def _unique_event_id() -> str:
    return str(uuid.uuid4())


def _build_event(production_id: str, event_id: str) -> CreditLockEvent:
    at = datetime.now(UTC).isoformat()
    return CreditLockEvent(
        event_id=event_id,
        event_type=EVENT_TYPE_MEMO_UPLOADED,
        production_id=production_id,
        aggregate_version=1,
        actor_id="live-test-actor",
        occurred_at=at,
        payload=MemoUploadedPayload(
            production_id=production_id,
            doc_id=f"doc-{uuid.uuid4().hex[:8]}",
            doc_hash=f"sha256-{uuid.uuid4().hex}",
            doc_type="CREDIT_MEMO",
            uploaded_by="live-test-actor",
            at=at,
        ),
    )


class TestConfluentLiveRoundTrip:
    def test_publish_and_consume_round_trip(self) -> None:
        """
        Publish one event to the live Confluent Cloud topic and consume it back.

        Asserts:
          - The consumed event matches event_id, production_id, event_type,
            and payload field-for-field.
          - DeliveryMetadata (topic, partition, offset) is captured.
        """
        production_id = _unique_production_id()
        event_id = _unique_event_id()
        consumer_group = f"creditlock-live-test-{uuid.uuid4().hex[:8]}"

        event = _build_event(production_id=production_id, event_id=event_id)

        # Use a unique consumer group so this test always starts from the
        # latest offset at the moment of subscription, capturing only the
        # event published in this run.
        transport = ConfluentTransport.from_settings(
            consumer_group=consumer_group,
            poll_timeout_seconds=30.0,
        )

        transport.publish(event)

        meta = transport.last_delivery
        assert meta is not None, "DeliveryMetadata must be set after publish"

        results = transport.consume(production_id)

        assert len(results) >= 1, (
            f"Expected at least one event for production_id={production_id}, got {len(results)}"
        )

        match = next((e for e in results if e.event_id == event_id), None)
        assert match is not None, f"Published event_id={event_id} not found in consumed events"

        # Full field assertions
        assert match.production_id == event.production_id
        assert match.event_type == event.event_type
        assert match.aggregate_version == event.aggregate_version
        assert match.actor_id == event.actor_id
        assert match.payload == event.payload

        # Safe output — no credentials, no bootstrap server
        print(
            f"\nPASS  event_id={event_id}  "
            f"topic={meta.topic}  "
            f"partition={meta.partition}  "
            f"offset={meta.offset}"
        )
