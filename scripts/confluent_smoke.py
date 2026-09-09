#!/usr/bin/env python
"""
Confluent Cloud smoke test — safe live round-trip verification.

Refuses to run unless CREDITLOCK_RUN_LIVE_CONFLUENT=1 is set in the
current environment.

Output:
    PASS  event_id=<uuid>  topic=<topic>  partition=<n>  offset=<n>

Never prints: bootstrap server, API key, API secret, or full environment.

Usage:
    CREDITLOCK_RUN_LIVE_CONFLUENT=1 .venv/bin/python scripts/confluent_smoke.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime

# ── Gate check — must be before any import of settings ────────────────────────
if os.environ.get("CREDITLOCK_RUN_LIVE_CONFLUENT") != "1":
    print(
        "SKIPPED — set CREDITLOCK_RUN_LIVE_CONFLUENT=1 to run the live smoke test.",
        file=sys.stderr,
    )
    sys.exit(0)

# ── Imports after gate ─────────────────────────────────────────────────────────
from creditlock.events.models import (
    EVENT_TYPE_MEMO_UPLOADED,
    CreditLockEvent,
    MemoUploadedPayload,
)
from creditlock.events.transport import ConfluentTransport


def _build_event(production_id: str, event_id: str) -> CreditLockEvent:
    at = datetime.now(UTC).isoformat()
    return CreditLockEvent(
        event_id=event_id,
        event_type=EVENT_TYPE_MEMO_UPLOADED,
        production_id=production_id,
        aggregate_version=1,
        actor_id="smoke-test-actor",
        occurred_at=at,
        payload=MemoUploadedPayload(
            production_id=production_id,
            doc_id=f"doc-{uuid.uuid4().hex[:8]}",
            doc_hash=f"sha256-{uuid.uuid4().hex}",
            doc_type="CREDIT_MEMO",
            uploaded_by="smoke-test-actor",
            at=at,
        ),
    )


def main() -> None:
    production_id = f"smoke-{uuid.uuid4().hex[:12]}"
    event_id = str(uuid.uuid4())
    consumer_group = f"creditlock-smoke-{uuid.uuid4().hex[:8]}"

    event = _build_event(production_id=production_id, event_id=event_id)

    transport = ConfluentTransport.from_settings(
        consumer_group=consumer_group,
        poll_timeout_seconds=30.0,
    )

    # Publish
    transport.publish(event)
    meta = transport.last_delivery
    if meta is None:
        print("FAIL — no delivery metadata after publish", file=sys.stderr)
        sys.exit(1)

    # Consume
    results = transport.consume(production_id)
    match = next((e for e in results if e.event_id == event_id), None)

    if match is None:
        print(
            f"FAIL — event_id={event_id} not found in consumed messages "
            f"(got {len(results)} message(s))",
            file=sys.stderr,
        )
        sys.exit(1)

    # Verify payload
    assert match.production_id == event.production_id
    assert match.event_type == event.event_type
    assert match.payload == event.payload

    # Safe output — credentials and bootstrap server are intentionally omitted
    print(
        f"PASS  event_id={event_id}  "
        f"topic={meta.topic}  "
        f"partition={meta.partition}  "
        f"offset={meta.offset}"
    )


if __name__ == "__main__":
    main()
