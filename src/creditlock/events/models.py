"""
Event envelope and typed payload models for the six creditlock event types.

Event names are frozen verbatim from the implementation plan.
No confluent-kafka dependency. Models are pure Pydantic.

aggregate_version contract
──────────────────────────
aggregate_version is a single monotonically increasing production-wide sequence
number shared across all event types for a given production_id.  It is NOT a
per-event-type sequence number.

  - The producer (or the event-sourcing framework) owns incrementing it.
  - Events for the same production must arrive with strictly increasing versions
    for each distinct logical step.
  - Two events of different types for the same production occupy consecutive
    positions in the same sequence (e.g. credit_roll.submitted v1,
    artifact.rendered v2, patch.applied v3, …).
  - A stale version (≤ last applied version for the production) is silently
    skipped by the projector.
  - A version collision (two events with the same version but different
    event_ids) is treated as stale: the second is dropped.  Producers must
    ensure uniqueness.

EventType → payload mapping (one canonical table)
──────────────────────────────────────────────────
  memo.uploaded          → MemoUploadedPayload
  memo.amended           → MemoAmendedPayload
  credit_roll.submitted  → CreditRollSubmittedPayload
  resolution.recorded    → ResolutionRecordedPayload
  patch.applied          → PatchAppliedPayload
  artifact.rendered      → ArtifactRenderedPayload

This mapping is enforced at CreditLockEvent construction time via a
model_validator.  Mismatches raise a ValidationError at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from creditlock.domain.models import AuthorizationAction

# ── Event type enum ───────────────────────────────────────────────────────────


class EventType(str):
    """String enum of the six valid event type names."""

    MEMO_UPLOADED = "memo.uploaded"
    MEMO_AMENDED = "memo.amended"
    CREDIT_ROLL_SUBMITTED = "credit_roll.submitted"
    RESOLUTION_RECORDED = "resolution.recorded"
    PATCH_APPLIED = "patch.applied"
    ARTIFACT_RENDERED = "artifact.rendered"


# Backward-compatible module-level constants
EVENT_TYPE_MEMO_UPLOADED = EventType.MEMO_UPLOADED
EVENT_TYPE_MEMO_AMENDED = EventType.MEMO_AMENDED
EVENT_TYPE_CREDIT_ROLL_SUBMITTED = EventType.CREDIT_ROLL_SUBMITTED
EVENT_TYPE_RESOLUTION_RECORDED = EventType.RESOLUTION_RECORDED
EVENT_TYPE_PATCH_APPLIED = EventType.PATCH_APPLIED
EVENT_TYPE_ARTIFACT_RENDERED = EventType.ARTIFACT_RENDERED

# All valid event type strings
_ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.MEMO_UPLOADED,
        EventType.MEMO_AMENDED,
        EventType.CREDIT_ROLL_SUBMITTED,
        EventType.RESOLUTION_RECORDED,
        EventType.PATCH_APPLIED,
        EventType.ARTIFACT_RENDERED,
    }
)


# ── Six typed payload models ───────────────────────────────────────────────────


class MemoUploadedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    doc_id: str
    doc_hash: str
    doc_type: str
    uploaded_by: str
    at: str  # ISO-8601


class MemoAmendedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    doc_id: str
    supersedes_doc_id: str
    doc_hash: str
    uploaded_by: str
    at: str  # ISO-8601


class CreditRollSubmittedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    manifest_id: str
    manifest_hash: str
    submitted_by: str
    at: str  # ISO-8601


class ResolutionRecordedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    issue_id: str
    resolution_type: AuthorizationAction
    actor_id: str
    role: str
    bound_hashes: dict[str, str]
    reason: str  # required; must be non-empty after stripping whitespace
    at: str  # ISO-8601

    @model_validator(mode="after")
    def reason_must_not_be_blank(self) -> ResolutionRecordedPayload:
        if not self.reason or not self.reason.strip():
            raise ValueError("reason must be a non-empty, non-whitespace string")
        return self


class PatchAppliedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    manifest_id: str
    patch_id: str
    patch_hash: str
    applied_by: str
    at: str  # ISO-8601


class ArtifactRenderedPayload(BaseModel):
    model_config = {"extra": "forbid"}

    production_id: str
    manifest_id: str
    artifact_index_digest: str
    render_profile_version: str
    at: str  # ISO-8601


# ── Canonical EventType → payload class mapping ───────────────────────────────

_PAYLOAD_CLASS_FOR_EVENT_TYPE: dict[str, type] = {
    EventType.MEMO_UPLOADED: MemoUploadedPayload,
    EventType.MEMO_AMENDED: MemoAmendedPayload,
    EventType.CREDIT_ROLL_SUBMITTED: CreditRollSubmittedPayload,
    EventType.RESOLUTION_RECORDED: ResolutionRecordedPayload,
    EventType.PATCH_APPLIED: PatchAppliedPayload,
    EventType.ARTIFACT_RENDERED: ArtifactRenderedPayload,
}

# ── Discriminated union of all payloads ───────────────────────────────────────

EventPayload = Annotated[
    MemoUploadedPayload
    | MemoAmendedPayload
    | CreditRollSubmittedPayload
    | ResolutionRecordedPayload
    | PatchAppliedPayload
    | ArtifactRenderedPayload,
    Field(discriminator=None),  # not discriminated — callers use event_type
]


# ── Shared event envelope ─────────────────────────────────────────────────────


class CreditLockEvent(BaseModel):
    """
    Transport-free event envelope.

    event_id is the idempotency key — processing the same event_id twice must
    produce identical projection state. Callers must generate a stable UUID per
    logical event and must not regenerate on retry.

    aggregate_version is a single production-wide monotonically increasing
    sequence number.  See models.py module docstring for the full contract.
    """

    model_config = {"extra": "forbid"}

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    schema_version: str = "1.0"
    production_id: str
    aggregate_version: int = Field(ge=1)
    actor_id: str
    occurred_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    correlation_id: str | None = None
    payload: (
        MemoUploadedPayload
        | MemoAmendedPayload
        | CreditRollSubmittedPayload
        | ResolutionRecordedPayload
        | PatchAppliedPayload
        | ArtifactRenderedPayload
    )

    @model_validator(mode="after")
    def validate_event_type_and_payload(self) -> CreditLockEvent:
        """Enforce the canonical EventType → payload-class mapping.

        Rejects unknown event_type values — they are not constructable.
        Rejects any event_type/payload class mismatch (e.g. memo.uploaded +
        ArtifactRenderedPayload).
        """
        et = self.event_type
        if et not in _ALL_EVENT_TYPES:
            raise ValueError(f"Unknown event_type '{et}'. Valid types: {sorted(_ALL_EVENT_TYPES)}")
        expected_cls = _PAYLOAD_CLASS_FOR_EVENT_TYPE[et]
        if not isinstance(self.payload, expected_cls):
            raise TypeError(
                f"event_type '{et}' requires payload of type "
                f"'{expected_cls.__name__}', "
                f"got '{type(self.payload).__name__}'"
            )
        return self
