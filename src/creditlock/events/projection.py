"""
In-memory projection consumer.

Applies creditlock events to an in-memory projection dict that mirrors the
Firestore schema defined in the implementation plan.

Shape of the projection dict for a single production:

    {
        "gate": {
            "state": str,                         # GateState value
            "gate_state_hash": str | None,
            "open_issues": [ { "issue_id", "type", "details" } ],
            "obligation_registry_version_hash": str | None,
            "artifact_index_digest": str | None,
            "render_profile_version": str | None,
            "release_evidence_digest": str | None,
            "updated_at": str,                    # ISO-8601
        },
        "obligations": {
            "registry_version_hash": str | None,
            "obligations": list[dict],            # raw obligation dicts (lifecycle_state etc.)
        },
        "manifests": {
            "current_manifest_id": str | None,
            "current_manifest_hash": str | None,
            "manifest_lifecycle": str | None,     # PENDING_RENDER | RENDER_COMPLETE | SUPERSEDED
        },
        "authorization_log": list[dict],
        "documents": list[dict],                  # memo upload/amendment lineage
    }

Rules:
- aggregate_version is a single production-wide monotonically increasing sequence
  number.  _versions maps production_id -> last applied aggregate_version.
- apply() returns True when applied, False when skipped (duplicate event_id or
  stale aggregate_version).
- Processing order:
    1. validate envelope/payload production_id (no mutation);
    2. check duplicate event_id;
    3. check stale aggregate_version;
    4. manifest/lifecycle validation (no mutation on failure);
    5. initialise tracking and mutate state.
- credit_roll.submitted  → gate STALE / ARTIFACT_PENDING; clears BOTH digests;
                           stores current_manifest_id and current_manifest_hash.
- artifact.rendered      → Stage 1 render anchor only.  Rejected if no current
                           manifest is pending or payload.manifest_id does not
                           match current_manifest_id.  A second higher-version
                           render after RENDER_COMPLETE raises ProjectionError.
                           Records artifact_index_digest and render_profile_version.
                           Does NOT remove ARTIFACT_PENDING.
                           Does NOT produce READY_TO_EXPORT.
- patch.applied          → Rejected if no current manifest or payload.manifest_id
                           does not match current_manifest_id.  Clears BOTH digests;
                           opens ARTIFACT_PENDING; keeps current manifest identity.
- resolution.recorded    → Immutable evidence: appends to authorization_log with
                           payload.reason verbatim.  Does NOT remove any open issue.
                           Does NOT change the gate.
- memo.amended           → Invalidates render clearance (both digests = None).
                           Records lineage in documents section.
- A duplicate event_id or stale aggregate_version returns False.
- Unknown event types raise ProjectionError (fail-closed).
- envelope.production_id must equal payload.production_id; mismatch raises
  ProjectionError before any state is created or mutated.
- Validation failures must not leave behind a newly created default projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import GateState, IssueCode
from creditlock.events.models import (
    EVENT_TYPE_ARTIFACT_RENDERED,
    EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
    EVENT_TYPE_MEMO_AMENDED,
    EVENT_TYPE_MEMO_UPLOADED,
    EVENT_TYPE_PATCH_APPLIED,
    EVENT_TYPE_RESOLUTION_RECORDED,
    ArtifactRenderedPayload,
    CreditLockEvent,
    CreditRollSubmittedPayload,
    MemoAmendedPayload,
    MemoUploadedPayload,
    PatchAppliedPayload,
    ResolutionRecordedPayload,
)
from creditlock.events.transport import EventTransport


@dataclass(frozen=True)
class ProjectionCheckpoint:
    """Typed immutable checkpoint boundary for a single production projection."""

    projection: dict[str, Any]
    last_aggregate_version: int
# ── Typed projection error ─────────────────────────────────────────────────────


class ProjectionError(ValueError):
    """Raised when a projection transition cannot be applied."""

    def __init__(self, message: str, *, event_type: str) -> None:
        super().__init__(message)
        self.event_type = event_type


# ── Projection helpers ────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hash_str(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _default_projection() -> dict[str, Any]:
    return {
        "gate": {
            "state": GateState.STALE.value,
            "gate_state_hash": None,
            "open_issues": [],
            "obligation_registry_version_hash": None,
            "artifact_index_digest": None,
            "render_profile_version": None,
            "release_evidence_digest": None,
            "updated_at": _now_iso(),
        },
        "obligations": {
            "registry_version_hash": None,
            "obligations": [],
        },
        "manifests": {
            "current_manifest_id": None,
            "current_manifest_hash": None,
            "manifest_lifecycle": None,
        },
        "authorization_log": [],
        "documents": [],
    }


def _open_issue(issue_id: str, issue_type: str, details: str) -> dict[str, Any]:
    return {"issue_id": issue_id, "type": issue_type, "details": details}


def _remove_open_issue(projection: dict[str, Any], issue_type: str) -> None:
    projection["gate"]["open_issues"] = [
        i for i in projection["gate"]["open_issues"] if i["type"] != issue_type
    ]


def _recompute_gate(projection: dict[str, Any]) -> None:
    """Re-fold the gate state from current open issues and projection flags.

    Fail-closed rule: any issue whose type is not a recognised IssueCode is
    retained in open_issues AND forces DEGRADED gate state.  Unknown codes are
    never silently skipped.
    """
    from creditlock.domain.models import Issue

    issues_raw = projection["gate"]["open_issues"]
    known_issues: list[Issue] = []
    has_unknown = False

    for raw in issues_raw:
        try:
            code = IssueCode(raw["type"])
            known_issues.append(
                Issue(issue_id=raw["issue_id"], code=code, detail=raw.get("details", ""))
            )
        except (ValueError, KeyError):
            # Unknown issue code — keep raw record, mark gate as DEGRADED
            has_unknown = True

    if has_unknown:
        projection["gate"]["state"] = GateState.DEGRADED.value
    else:
        ps = ProjectionStatus(
            artifact_pending=any(i["type"] == IssueCode.ARTIFACT_PENDING.value for i in issues_raw),
            stale_manifest=any(i["type"] == IssueCode.STALE_MANIFEST.value for i in issues_raw),
        )
        gate_state = fold_gate(known_issues, ps)
        projection["gate"]["state"] = gate_state.value

    # Recompute gate_state_hash: SHA-256 of serialised state + open issues
    hash_input = json.dumps(
        {"state": projection["gate"]["state"], "open_issues": issues_raw},
        sort_keys=True,
    )
    projection["gate"]["gate_state_hash"] = _hash_str(hash_input)
    projection["gate"]["updated_at"] = _now_iso()


# ── Per-event handlers ────────────────────────────────────────────────────────


def _apply_memo_uploaded(
    projection: dict[str, Any],
    payload: MemoUploadedPayload,
    event_id: str | None = None,
) -> None:
    """
    A new document upload does not change obligations or the manifest directly.
    Records the document in the documents section for traceability.
    The gate is left unchanged.
    """
    doc_record: dict[str, Any] = {
        "doc_id": payload.doc_id,
        "doc_hash": payload.doc_hash,
        "doc_type": payload.doc_type,
        "uploaded_by": payload.uploaded_by,
        "at": payload.at,
        "supersedes_doc_id": None,
        "event_id": event_id,
    }
    # Deduplicate by doc_id
    existing_ids = {d["doc_id"] for d in projection["documents"]}
    if payload.doc_id not in existing_ids:
        projection["documents"].append(doc_record)
    projection["gate"]["updated_at"] = _now_iso()


def _apply_memo_amended(
    projection: dict[str, Any],
    payload: MemoAmendedPayload,
    event_id: str | None = None,
) -> None:
    """
    An amendment invalidates any existing render clearance — the manifest may
    need to be updated once extraction completes.
    Records the amendment lineage in the documents section.
    """
    # Record amendment lineage with doc_type="amendment"
    doc_record: dict[str, Any] = {
        "doc_id": payload.doc_id,
        "doc_hash": payload.doc_hash,
        "doc_type": "amendment",
        "uploaded_by": payload.uploaded_by,
        "at": payload.at,
        "supersedes_doc_id": payload.supersedes_doc_id,
        "event_id": event_id,
    }
    # Deduplicate by doc_id
    existing_ids = {d["doc_id"] for d in projection["documents"]}
    if payload.doc_id not in existing_ids:
        projection["documents"].append(doc_record)

    # Invalidate BOTH render clearance fields
    projection["gate"]["artifact_index_digest"] = None
    projection["gate"]["release_evidence_digest"] = None

    # Open a STALE_MANIFEST issue to surface the stale state
    existing = [
        i for i in projection["gate"]["open_issues"] if i["type"] == IssueCode.STALE_MANIFEST.value
    ]
    if not existing:
        projection["gate"]["open_issues"].append(
            _open_issue(
                f"stale-{payload.doc_id}",
                IssueCode.STALE_MANIFEST.value,
                f"Memo amended (doc_id={payload.doc_id} supersedes {payload.supersedes_doc_id}); manifest may require update.",
            )
        )

    _recompute_gate(projection)


def _apply_credit_roll_submitted(
    projection: dict[str, Any],
    payload: CreditRollSubmittedPayload,
) -> None:
    """
    Submitting a new credit roll immediately sets the gate STALE with
    ARTIFACT_PENDING before any render work begins.

    Stores current_manifest_id and current_manifest_hash for render/patch
    validation.  Clears BOTH artifact_index_digest and release_evidence_digest.
    """
    projection["manifests"]["current_manifest_id"] = payload.manifest_id
    projection["manifests"]["current_manifest_hash"] = payload.manifest_hash
    projection["manifests"]["manifest_lifecycle"] = "PENDING_RENDER"

    # Clear BOTH digests — previous render evidence is stale
    projection["gate"]["artifact_index_digest"] = None
    projection["gate"]["release_evidence_digest"] = None

    # Ensure ARTIFACT_PENDING issue is open (deduplicated by type)
    existing = [
        i
        for i in projection["gate"]["open_issues"]
        if i["type"] == IssueCode.ARTIFACT_PENDING.value
    ]
    if not existing:
        projection["gate"]["open_issues"].append(
            _open_issue(
                f"artifact-pending-{payload.manifest_id}",
                IssueCode.ARTIFACT_PENDING.value,
                f"Manifest {payload.manifest_id} submitted; awaiting render.",
            )
        )

    _recompute_gate(projection)


def _apply_resolution_recorded(
    projection: dict[str, Any],
    payload: ResolutionRecordedPayload,
) -> None:
    """
    Immutable evidence only: append the resolution record to authorization_log.

    Uses payload.reason verbatim — never manufactured from resolution_type.
    This handler does NOT remove any open issue and does NOT change the gate
    state.
    """
    log_entry: dict[str, Any] = {
        "actor_id": payload.actor_id,
        "role": payload.role,
        "action": payload.resolution_type,
        "issue_id": payload.issue_id,
        "bound_hashes": payload.bound_hashes,
        "reason": payload.reason,
        "at": payload.at,
    }
    projection["authorization_log"].append(log_entry)
    # Gate is NOT recomputed; issue list is NOT modified.
    projection["gate"]["updated_at"] = _now_iso()


def _apply_patch_applied(
    projection: dict[str, Any],
    payload: PatchAppliedPayload,
) -> None:
    """
    A patch invalidates the current render.  Opens ARTIFACT_PENDING and clears
    BOTH digests.

    patch_hash is evidence about the patch itself, NOT the resulting manifest
    hash.  current_manifest_id and current_manifest_hash are intentionally NOT
    updated here; the subsequent credit_roll.submitted event supplies the new
    manifest hash.

    Pre-condition (enforced by Projector.apply): payload.manifest_id must equal
    current_manifest_id and a current manifest must exist.
    """
    projection["manifests"]["manifest_lifecycle"] = "PENDING_RENDER"

    # Clear BOTH digests — render and release evidence are now stale
    projection["gate"]["artifact_index_digest"] = None
    projection["gate"]["release_evidence_digest"] = None

    # Re-open ARTIFACT_PENDING (patch invalidates previous render)
    _remove_open_issue(projection, IssueCode.ARTIFACT_PENDING.value)
    projection["gate"]["open_issues"].append(
        _open_issue(
            f"artifact-pending-patch-{payload.patch_id}",
            IssueCode.ARTIFACT_PENDING.value,
            f"Patch {payload.patch_id} applied to manifest {payload.manifest_id}; awaiting re-render.",
        )
    )

    _recompute_gate(projection)


def _apply_artifact_rendered(
    projection: dict[str, Any],
    payload: ArtifactRenderedPayload,
) -> None:
    """
    Stage 1 render anchor only.

    Records artifact_index_digest and render_profile_version.

    Pre-condition (enforced by Projector.apply): payload.manifest_id must equal
    current_manifest_id, a current manifest must exist, and lifecycle must be
    PENDING_RENDER.  A second render after RENDER_COMPLETE raises ProjectionError.

    This handler does NOT remove ARTIFACT_PENDING.  It does NOT produce
    READY_TO_EXPORT.
    """
    projection["gate"]["artifact_index_digest"] = payload.artifact_index_digest
    projection["gate"]["render_profile_version"] = payload.render_profile_version
    projection["manifests"]["manifest_lifecycle"] = "RENDER_COMPLETE"

    # ARTIFACT_PENDING is intentionally NOT removed here.
    _recompute_gate(projection)


# ── Known event types ─────────────────────────────────────────────────────────

_KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_TYPE_MEMO_UPLOADED,
        EVENT_TYPE_MEMO_AMENDED,
        EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        EVENT_TYPE_RESOLUTION_RECORDED,
        EVENT_TYPE_PATCH_APPLIED,
        EVENT_TYPE_ARTIFACT_RENDERED,
    }
)


# ── Public projection class ───────────────────────────────────────────────────


class Projector:
    """
    Stateful in-memory projection for one or more productions.

    Usage::

        transport = InMemoryTransport()
        projector = Projector()
        projector.replay(transport, production_id="prod-1")
        state = projector.get("prod-1")
    """

    def __init__(self) -> None:
        # production_id -> projection dict
        self._projections: dict[str, dict[str, Any]] = {}
        # production_id -> set of applied event_ids (idempotency)
        self._seen: dict[str, set[str]] = {}
        # production_id -> last applied aggregate_version (production-wide monotonic)
        self._versions: dict[str, int] = {}

    def get(self, production_id: str) -> dict[str, Any] | None:
        return self._projections.get(production_id)

    def apply(self, event: CreditLockEvent) -> bool:
        """
        Apply a single event to the in-memory projection.

        Returns True if the event was applied.
        Returns False if the event was skipped (duplicate event_id or stale
        aggregate_version).

        Raises ProjectionError for:
        - Unknown event types.
        - envelope.production_id != payload.production_id.
        - artifact.rendered with no pending manifest, mismatched manifest_id, or
          lifecycle already RENDER_COMPLETE (second render after first succeeds).
        - patch.applied with no current manifest or mismatched manifest_id.

        Failures do NOT leave behind a newly created default projection.
        """
        pid = event.production_id

        # ── Step 1: pure validation (before any state creation or mutation) ────

        # 1a. Unknown event type — fail closed
        if event.event_type not in _KNOWN_EVENT_TYPES:
            raise ProjectionError(
                f"Unknown event type '{event.event_type}': cannot project.",
                event_type=event.event_type,
            )

        # 1b. Envelope/payload production_id binding
        payload_pid = getattr(event.payload, "production_id", None)
        if payload_pid != pid:
            raise ProjectionError(
                f"Envelope production_id '{pid}' does not match "
                f"payload production_id '{payload_pid}'.",
                event_type=event.event_type,
            )

        # ── Step 2: duplicate event_id check ────────────────────────────────
        seen_for_pid = self._seen.get(pid, set())
        if event.event_id in seen_for_pid:
            return False

        # ── Step 3: stale aggregate_version check ────────────────────────────
        # Production-wide monotonic sequence: any version ≤ last applied is stale.
        last_version = self._versions.get(pid, 0)
        if event.aggregate_version <= last_version:
            return False

        # ── Step 4: manifest/lifecycle validation (peek only, no mutation) ───
        existing = self._projections.get(pid)

        if event.event_type == EVENT_TYPE_ARTIFACT_RENDERED:
            assert isinstance(event.payload, ArtifactRenderedPayload)
            current_mid = existing["manifests"]["current_manifest_id"] if existing else None
            if current_mid is None:
                raise ProjectionError(
                    f"artifact.rendered rejected: no current manifest is pending for '{pid}'.",
                    event_type=event.event_type,
                )
            if event.payload.manifest_id != current_mid:
                raise ProjectionError(
                    f"artifact.rendered rejected: payload.manifest_id "
                    f"'{event.payload.manifest_id}' does not match "
                    f"current_manifest_id '{current_mid}'.",
                    event_type=event.event_type,
                )
            # Second render after RENDER_COMPLETE raises without mutation
            current_lifecycle = existing["manifests"]["manifest_lifecycle"] if existing else None
            if current_lifecycle == "RENDER_COMPLETE":
                raise ProjectionError(
                    f"artifact.rendered rejected: manifest '{current_mid}' lifecycle is "
                    f"already RENDER_COMPLETE; a second render is not permitted.",
                    event_type=event.event_type,
                )

        elif event.event_type == EVENT_TYPE_PATCH_APPLIED:
            assert isinstance(event.payload, PatchAppliedPayload)
            current_mid = existing["manifests"]["current_manifest_id"] if existing else None
            if current_mid is None:
                raise ProjectionError(
                    f"patch.applied rejected: no current manifest for '{pid}'.",
                    event_type=event.event_type,
                )
            if event.payload.manifest_id != current_mid:
                raise ProjectionError(
                    f"patch.applied rejected: payload.manifest_id "
                    f"'{event.payload.manifest_id}' does not match "
                    f"current_manifest_id '{current_mid}'.",
                    event_type=event.event_type,
                )

        # ── Step 5: initialise per-production tracking (lazy, after validation) ─

        if pid not in self._projections:
            self._projections[pid] = _default_projection()
            self._seen[pid] = set()

        # ── Step 6: dispatch ─────────────────────────────────────────────────
        et = event.event_type
        if et == EVENT_TYPE_MEMO_UPLOADED:
            assert isinstance(event.payload, MemoUploadedPayload)
            _apply_memo_uploaded(self._projections[pid], event.payload, event_id=event.event_id)
        elif et == EVENT_TYPE_MEMO_AMENDED:
            assert isinstance(event.payload, MemoAmendedPayload)
            _apply_memo_amended(self._projections[pid], event.payload, event_id=event.event_id)
        elif et == EVENT_TYPE_CREDIT_ROLL_SUBMITTED:
            assert isinstance(event.payload, CreditRollSubmittedPayload)
            _apply_credit_roll_submitted(self._projections[pid], event.payload)
        elif et == EVENT_TYPE_RESOLUTION_RECORDED:
            assert isinstance(event.payload, ResolutionRecordedPayload)
            _apply_resolution_recorded(self._projections[pid], event.payload)
        elif et == EVENT_TYPE_PATCH_APPLIED:
            assert isinstance(event.payload, PatchAppliedPayload)
            _apply_patch_applied(self._projections[pid], event.payload)
        elif et == EVENT_TYPE_ARTIFACT_RENDERED:
            assert isinstance(event.payload, ArtifactRenderedPayload)
            _apply_artifact_rendered(self._projections[pid], event.payload)
        # No else: unknown types are rejected in Step 1.

        # Record seen event and update production-wide version
        self._seen[pid].add(event.event_id)
        self._versions[pid] = event.aggregate_version
        return True

    def replay(self, transport: EventTransport, production_id: str) -> None:
        """
        Consume all events for *production_id* from the transport and apply
        them to the in-memory projection.
        """
        for event in transport.consume(production_id):
            self.apply(event)

    def checkpoint(self, production_id: str) -> ProjectionCheckpoint | None:
        """Return a deep-copied ProjectionCheckpoint for production_id, or None if unknown."""
        if production_id not in self._projections:
            return None
        return ProjectionCheckpoint(
            projection=copy.deepcopy(self._projections[production_id]),
            last_aggregate_version=self._versions.get(production_id, 0),
        )

    def restore(self, production_id: str, checkpoint: ProjectionCheckpoint) -> None:
        """Restore production_id state from a ProjectionCheckpoint."""
        self._projections[production_id] = copy.deepcopy(checkpoint.projection)
        self._versions[production_id] = checkpoint.last_aggregate_version
        if production_id not in self._seen:
            self._seen[production_id] = set()
