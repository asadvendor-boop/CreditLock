"""
Unit tests for ProjectionCheckpoint, Projector.checkpoint, and Projector.restore.
"""

from __future__ import annotations

import copy
import uuid

from creditlock.events.models import (
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    CreditLockEvent,
    CreditRollSubmittedPayload,
)
from creditlock.events.projection import ProjectionCheckpoint, Projector


def _make_event(
    production_id: str = "prod-cp-001",
    version: int = 1,
    event_id: str | None = None,
) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        production_id=production_id,
        aggregate_version=version,
        actor_id="actor-cp",
        occurred_at="2026-07-31T00:00:00Z",
        payload=CreditRollSubmittedPayload(
            production_id=production_id,
            manifest_id="manifest-cp-1",
            manifest_hash="mhash-cp-1",
            submitted_by="actor-cp",
            at="2026-07-31T00:00:00Z",
        ),
    )


class TestProjectionCheckpoint:
    def test_unknown_production_returns_none(self) -> None:
        p = Projector()
        assert p.checkpoint("nonexistent-prod") is None

    def test_checkpoint_restore_round_trip_preserves_state_and_version(self) -> None:
        p1 = Projector()
        evt = _make_event(production_id="prod-100", version=5)
        p1.apply(evt)

        cp = p1.checkpoint("prod-100")
        assert cp is not None
        assert cp.last_aggregate_version == 5
        assert cp.projection["manifests"]["current_manifest_id"] == "manifest-cp-1"

        p2 = Projector()
        p2.restore("prod-100", cp)
        p2_state = p2.get("prod-100")

        assert p2_state is not None
        assert p2_state["manifests"]["current_manifest_id"] == "manifest-cp-1"
        assert p2._versions["prod-100"] == 5

    def test_checkpoint_result_cannot_mutate_projector_state(self) -> None:
        p = Projector()
        p.apply(_make_event(production_id="prod-mut-1", version=1))

        cp = p.checkpoint("prod-mut-1")
        assert cp is not None

        # Mutate the checkpoint projection dict
        cp.projection["manifests"]["current_manifest_id"] = "TAMPERED"

        # Projector state must be unchanged
        state = p.get("prod-mut-1")
        assert state is not None
        assert state["manifests"]["current_manifest_id"] == "manifest-cp-1"

    def test_restoring_from_caller_owned_dict_cannot_later_mutate_projector(self) -> None:
        p = Projector()
        p.apply(_make_event(production_id="prod-mut-2", version=1))

        cp = p.checkpoint("prod-mut-2")
        assert cp is not None

        dict_mut = copy.deepcopy(cp.projection)

        p2 = Projector()
        p2.restore(
            "prod-mut-2", ProjectionCheckpoint(projection=dict_mut, last_aggregate_version=1)
        )

        # Mutate the caller-owned dict
        dict_mut["manifests"]["current_manifest_id"] = "TAMPERED_CALLER"

        # Projector p2 state must remain untampered
        p2_state = p2.get("prod-mut-2")
        assert p2_state is not None
        assert p2_state["manifests"]["current_manifest_id"] == "manifest-cp-1"
