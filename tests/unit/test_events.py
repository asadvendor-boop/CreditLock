"""
Tests for the event envelope, in-memory transport, and projection layer.

Coverage:
- Every event type applied once → correct projection shape.
- Every event type delivered twice → identical projection (idempotency).
- Out-of-order aggregate_version → older event is ignored.
- fold_gate and evaluate_findings are reused from the existing engine (not
  reimplemented here — their integration is verified via gate state assertions).
- credit_roll.submitted immediately sets gate STALE / ARTIFACT_PENDING and clears
  BOTH digests.
- memo.amended invalidates existing clearance (both digests).
- artifact.rendered is Stage 1 only: records digest, does NOT remove
  ARTIFACT_PENDING, does NOT produce READY_TO_EXPORT.
- resolution.recorded is immutable evidence only: appends to authorization_log,
  does NOT remove any open issue, does NOT change gate.
- patch.applied clears BOTH digests, opens ARTIFACT_PENDING; patch_hash is NOT
  assigned to current_manifest_hash.
- Unknown issue codes force DEGRADED gate (fail-closed).
- Unknown event types raise ProjectionError (fail-closed).
"""

from __future__ import annotations

import copy
import uuid

import pytest

from creditlock.domain.gate import GateState
from creditlock.domain.models import AuthorizationAction, IssueCode
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
from creditlock.events.projection import ProjectionError, Projector
from creditlock.events.transport import InMemoryTransport

# ── Fixture helpers ───────────────────────────────────────────────────────────

PROD = "prod-test-001"
AT = "2025-01-01T00:00:00Z"


def _evt(
    event_type: str, payload, version: int = 1, event_id: str | None = None
) -> CreditLockEvent:
    return CreditLockEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        production_id=PROD,
        aggregate_version=version,
        actor_id="actor-1",
        occurred_at=AT,
        payload=payload,
    )


def _memo_uploaded(version: int = 1, event_id: str | None = None) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_MEMO_UPLOADED,
        MemoUploadedPayload(
            production_id=PROD,
            doc_id="doc-1",
            doc_hash="aabbcc",
            doc_type="DEAL_MEMO",
            uploaded_by="actor-1",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


def _memo_amended(version: int = 1, event_id: str | None = None) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_MEMO_AMENDED,
        MemoAmendedPayload(
            production_id=PROD,
            doc_id="doc-2",
            supersedes_doc_id="doc-1",
            doc_hash="ddeeff",
            uploaded_by="actor-1",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


def _credit_roll_submitted(version: int = 1, event_id: str | None = None) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
        CreditRollSubmittedPayload(
            production_id=PROD,
            manifest_id="manifest-1",
            manifest_hash="mhash-001",
            submitted_by="actor-1",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


def _artifact_rendered(version: int = 2, event_id: str | None = None) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_ARTIFACT_RENDERED,
        ArtifactRenderedPayload(
            production_id=PROD,
            manifest_id="manifest-1",
            artifact_index_digest="digest-abc123",
            render_profile_version="chromium-130-v1",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


def _resolution_recorded(
    issue_id: str, version: int = 2, event_id: str | None = None
) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_RESOLUTION_RECORDED,
        ResolutionRecordedPayload(
            production_id=PROD,
            issue_id=issue_id,
            resolution_type=AuthorizationAction.CONFIRM_IDENTITY,
            actor_id="approver-1",
            role="RELEASE_APPROVER",
            bound_hashes={
                "manifest_hash": "mhash-001",
                "obligation_registry_version_hash": "orvh-001",
                "artifact_index_digest": "digest-abc123",
            },
            reason="Confirmed identity via production contract",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


def _patch_applied(version: int = 2, event_id: str | None = None) -> CreditLockEvent:
    return _evt(
        EVENT_TYPE_PATCH_APPLIED,
        PatchAppliedPayload(
            production_id=PROD,
            manifest_id="manifest-1",
            patch_id="patch-1",
            patch_hash="phash-001",
            applied_by="actor-1",
            at=AT,
        ),
        version=version,
        event_id=event_id,
    )


# ── Fresh projector per test ──────────────────────────────────────────────────


@pytest.fixture()
def projector() -> Projector:
    return Projector()


@pytest.fixture()
def transport() -> InMemoryTransport:
    return InMemoryTransport()


# ── Basic application tests ───────────────────────────────────────────────────


class TestMemoUploaded:
    def test_apply_once_creates_projection(self, projector):
        projector.apply(_memo_uploaded())
        state = projector.get(PROD)
        assert state is not None

    def test_gate_unchanged_after_memo_uploaded(self, projector):
        # memo.uploaded alone does not change the gate state; it stays at the
        # default (STALE from _default_projection).
        projector.apply(_memo_uploaded())
        state = projector.get(PROD)
        # Default projection starts as STALE (no issues but manifest fields not set)
        # After memo.uploaded the gate state is left as-is (STALE)
        assert state["gate"]["state"] == GateState.STALE.value

    def test_idempotent_double_delivery(self, projector):
        eid = str(uuid.uuid4())
        evt = _memo_uploaded(event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        # Gate state and open_issues must be identical; updated_at may differ
        assert snap1["gate"]["state"] == snap2["gate"]["state"]
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]
        assert snap1["manifests"] == snap2["manifests"]
        assert snap1["authorization_log"] == snap2["authorization_log"]


class TestMemoAmended:
    def test_invalidates_clearance(self, projector):
        # Set up a rendered state first
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == "digest-abc123"

        # Now amend — should clear BOTH digests and add STALE_MANIFEST
        projector.apply(_memo_amended(version=3))
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] is None
        assert state["gate"]["release_evidence_digest"] is None
        stale_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.STALE_MANIFEST.value
        ]
        assert len(stale_issues) >= 1

    def test_gate_stale_after_amendment(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        projector.apply(_memo_amended(version=3))
        state = projector.get(PROD)
        assert state["gate"]["state"] == GateState.STALE.value

    def test_idempotent_double_delivery(self, projector):
        eid = str(uuid.uuid4())
        evt = _memo_amended(version=1, event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]
        assert snap1["gate"]["state"] == snap2["gate"]["state"]
        assert snap1["gate"]["artifact_index_digest"] == snap2["gate"]["artifact_index_digest"]

    def test_stale_manifest_not_duplicated_on_double_delivery(self, projector):
        eid = str(uuid.uuid4())
        evt = _memo_amended(version=1, event_id=eid)
        projector.apply(evt)
        projector.apply(evt)
        state = projector.get(PROD)
        stale_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.STALE_MANIFEST.value
        ]
        assert len(stale_issues) == 1


class TestCreditRollSubmitted:
    def test_sets_gate_stale_immediately(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        state = projector.get(PROD)
        assert state["gate"]["state"] == GateState.STALE.value

    def test_opens_artifact_pending_issue(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        state = projector.get(PROD)
        ap_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap_issues) == 1

    def test_records_manifest_hash(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        state = projector.get(PROD)
        assert state["manifests"]["current_manifest_hash"] == "mhash-001"
        assert state["manifests"]["manifest_lifecycle"] == "PENDING_RENDER"

    def test_clears_artifact_index_digest(self, projector):
        # Pre-set a digest, then submit a new roll
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        assert projector.get(PROD)["gate"]["artifact_index_digest"] == "digest-abc123"

        projector.apply(_credit_roll_submitted(version=3))
        assert projector.get(PROD)["gate"]["artifact_index_digest"] is None

    def test_clears_release_evidence_digest(self, projector):
        """credit_roll.submitted must clear release_evidence_digest (Rule 4)."""
        # Manually inject a release_evidence_digest to simulate prior state
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["release_evidence_digest"] = "red-previous"

        projector.apply(_credit_roll_submitted(version=2))
        assert projector.get(PROD)["gate"]["release_evidence_digest"] is None

    def test_idempotent_double_delivery(self, projector):
        eid = str(uuid.uuid4())
        evt = _credit_roll_submitted(version=1, event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        assert snap1["gate"]["state"] == snap2["gate"]["state"]
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]
        assert snap1["manifests"] == snap2["manifests"]

    def test_artifact_pending_not_duplicated_on_double_delivery(self, projector):
        eid = str(uuid.uuid4())
        evt = _credit_roll_submitted(version=1, event_id=eid)
        projector.apply(evt)
        projector.apply(evt)
        ap_issues = [
            i
            for i in projector.get(PROD)["gate"]["open_issues"]
            if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap_issues) == 1


class TestResolutionRecorded:
    def test_appends_to_authorization_log(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        # Inject a synthetic open issue
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "issue-xyz", "type": IssueCode.AMBIGUOUS_IDENTITY.value, "details": "test"}
        )
        projector.apply(_resolution_recorded("issue-xyz", version=2))
        log = projector.get(PROD)["authorization_log"]
        assert len(log) == 1
        assert log[0]["issue_id"] == "issue-xyz"

    def test_does_not_remove_open_issue(self, projector):
        """
        Rule 2: resolution.recorded is immutable evidence only.
        It must NOT remove any open issue from the gate.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "issue-abc", "type": IssueCode.AMBIGUOUS_IDENTITY.value, "details": "test"}
        )
        projector.apply(_resolution_recorded("issue-abc", version=2))
        open_ids = {i["issue_id"] for i in projector.get(PROD)["gate"]["open_issues"]}
        # Issue must still be open after resolution.recorded
        assert "issue-abc" in open_ids

    def test_does_not_change_gate_state(self, projector):
        """
        Rule 2: resolution.recorded must not directly change the gate state.
        """
        projector.apply(_credit_roll_submitted(version=1))
        state_before = projector.get(PROD)["gate"]["state"]
        projector.apply(_resolution_recorded("some-issue", version=2))
        state_after = projector.get(PROD)["gate"]["state"]
        assert state_before == state_after

    def test_resolution_from_any_role_cannot_clear_missing_credit(self, projector):
        """
        Rule 2: a resolution event from any role cannot clear MISSING_CREDIT
        (a BLOCKED-class issue).
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "mc-001", "type": IssueCode.MISSING_CREDIT.value, "details": "missing"}
        )
        projector.apply(_resolution_recorded("mc-001", version=2))
        # MISSING_CREDIT must still be present
        open_types = {i["type"] for i in projector.get(PROD)["gate"]["open_issues"]}
        assert IssueCode.MISSING_CREDIT.value in open_types

    def test_resolution_from_any_role_cannot_clear_blocked_class_issue(self, projector):
        """
        Rule 2: resolution.recorded cannot clear any BLOCKED-class issue.
        """
        projector.apply(_credit_roll_submitted(version=1))
        for code in (
            IssueCode.MISSING_CREDIT,
            IssueCode.ARTIFACT_TEXT_MISMATCH,
            IssueCode.ARTIFACT_INTEGRITY_FAILURE,
        ):
            projector.get(PROD)["gate"]["open_issues"].append(
                {"issue_id": f"{code.value}-001", "type": code.value, "details": "blocked"}
            )
        for i, code in enumerate(
            (
                IssueCode.MISSING_CREDIT,
                IssueCode.ARTIFACT_TEXT_MISMATCH,
                IssueCode.ARTIFACT_INTEGRITY_FAILURE,
            ),
            start=2,
        ):
            projector.apply(_resolution_recorded(f"{code.value}-001", version=i))

        open_types = {i["type"] for i in projector.get(PROD)["gate"]["open_issues"]}
        for code in (
            IssueCode.MISSING_CREDIT,
            IssueCode.ARTIFACT_TEXT_MISMATCH,
            IssueCode.ARTIFACT_INTEGRITY_FAILURE,
        ):
            assert code.value in open_types, f"{code.value} should remain open"

    def test_idempotent_double_delivery(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "issue-dup", "type": IssueCode.AMBIGUOUS_IDENTITY.value, "details": "test"}
        )
        eid = str(uuid.uuid4())
        evt = _resolution_recorded("issue-dup", version=2, event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        assert snap1["authorization_log"] == snap2["authorization_log"]
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]


class TestPatchApplied:
    def test_sets_manifest_to_pending_render(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        projector.apply(_patch_applied(version=3))
        state = projector.get(PROD)
        assert state["manifests"]["manifest_lifecycle"] == "PENDING_RENDER"

    def test_opens_artifact_pending_after_patch(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        projector.apply(_patch_applied(version=3))
        state = projector.get(PROD)
        ap_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap_issues) == 1

    def test_gate_stale_after_patch(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        projector.apply(_patch_applied(version=3))
        assert projector.get(PROD)["gate"]["state"] == GateState.STALE.value

    def test_patch_hash_not_assigned_to_manifest_hash(self, projector):
        """
        Rule 4: patch_hash is evidence about the patch, not the resulting
        manifest hash.  current_manifest_hash must NOT be updated by patch.applied.
        """
        projector.apply(_credit_roll_submitted(version=1))
        manifest_hash_before = projector.get(PROD)["manifests"]["current_manifest_hash"]
        projector.apply(_patch_applied(version=2))
        # Manifest hash must remain the same as set by credit_roll.submitted
        assert projector.get(PROD)["manifests"]["current_manifest_hash"] == manifest_hash_before
        assert projector.get(PROD)["manifests"]["current_manifest_hash"] != "phash-001"

    def test_patch_clears_both_digests(self, projector):
        """Rule 4: patch.applied clears both artifact_index_digest and release_evidence_digest."""
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        # Manually inject a release_evidence_digest
        projector.get(PROD)["gate"]["release_evidence_digest"] = "red-previous"

        projector.apply(_patch_applied(version=3))
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] is None
        assert state["gate"]["release_evidence_digest"] is None

    def test_idempotent_double_delivery(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        eid = str(uuid.uuid4())
        evt = _patch_applied(version=3, event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        assert snap1["gate"]["state"] == snap2["gate"]["state"]
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]
        assert snap1["manifests"] == snap2["manifests"]


class TestArtifactRendered:
    def test_does_not_remove_artifact_pending(self, projector):
        """
        Rule 1: artifact.rendered must NOT remove ARTIFACT_PENDING.
        That issue stays open until the full atomic release-evidence
        completion (Task 3).
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        ap_issues = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap_issues) == 1, "ARTIFACT_PENDING must remain open after artifact.rendered"

    def test_records_artifact_index_digest(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == "digest-abc123"

    def test_records_render_profile_version(self, projector):
        """Rule 1: artifact.rendered records render_profile_version."""
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        assert state["gate"]["render_profile_version"] == "chromium-130-v1"

    def test_manifest_lifecycle_render_complete(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        assert projector.get(PROD)["manifests"]["manifest_lifecycle"] == "RENDER_COMPLETE"

    def test_gate_remains_stale_after_rendered(self, projector):
        """
        Rule 1: after credit_roll.submitted followed only by artifact.rendered,
        the gate must remain STALE (not READY_TO_EXPORT).
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        assert state["gate"]["state"] == GateState.STALE.value

    def test_release_evidence_digest_remains_null_after_rendered(self, projector):
        """
        Rule 1: release_evidence_digest must remain None after artifact.rendered.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)
        assert state["gate"]["release_evidence_digest"] is None

    def test_artifact_rendered_never_produces_ready_to_export(self, projector):
        """
        Rule 1: artifact.rendered alone must never result in READY_TO_EXPORT,
        even if there are no other open issues besides ARTIFACT_PENDING.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        assert projector.get(PROD)["gate"]["state"] != GateState.READY_TO_EXPORT.value

    def test_idempotent_double_delivery(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        eid = str(uuid.uuid4())
        evt = _artifact_rendered(version=2, event_id=eid)
        projector.apply(evt)
        snap1 = copy.deepcopy(projector.get(PROD))
        projector.apply(evt)
        snap2 = projector.get(PROD)
        assert snap1["gate"]["state"] == snap2["gate"]["state"]
        assert snap1["gate"]["open_issues"] == snap2["gate"]["open_issues"]
        assert snap1["gate"]["artifact_index_digest"] == snap2["gate"]["artifact_index_digest"]
        assert snap1["manifests"] == snap2["manifests"]


# ── Fail-closed gate folding tests ────────────────────────────────────────────


class TestGateFoldFailClosed:
    def test_unknown_issue_code_forces_degraded(self, projector):
        """
        Rule 3: an unknown issue code must never be silently skipped.
        The gate must become DEGRADED.
        """
        projector.apply(_credit_roll_submitted(version=1))
        # Inject an unknown issue code directly into the projection
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "unknown-001", "type": "TOTALLY_UNKNOWN_CODE", "details": "test"}
        )
        # Trigger a recompute by applying another event in a different bucket
        projector.apply(_memo_uploaded(version=2))
        # The gate state should be DEGRADED because of the unknown issue
        # (memo_uploaded doesn't call _recompute_gate, so we trigger it directly
        # by applying a memo.amended which does call recompute)
        projector.apply(_memo_amended(version=3))
        state = projector.get(PROD)
        assert state["gate"]["state"] == GateState.DEGRADED.value

    def test_unknown_issue_preserved_in_open_issues(self, projector):
        """
        Rule 3: unknown issues must be preserved in open_issues, not silently
        removed.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "unknown-002", "type": "ANOTHER_UNKNOWN_CODE", "details": "kept"}
        )
        projector.apply(_memo_amended(version=2))
        open_ids = {i["issue_id"] for i in projector.get(PROD)["gate"]["open_issues"]}
        assert "unknown-002" in open_ids

    def test_unknown_issue_cannot_result_in_ready_to_export(self, projector):
        """
        Rule 3: an unknown issue must never allow a path to READY_TO_EXPORT.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.get(PROD)["gate"]["open_issues"].append(
            {"issue_id": "unknown-003", "type": "UNKNOWN_FUTURE_CODE", "details": "future"}
        )
        projector.apply(_memo_amended(version=2))
        state = projector.get(PROD)
        assert state["gate"]["state"] != GateState.READY_TO_EXPORT.value

    def test_unknown_event_type_raises_at_construction(self, projector):
        """
        Unknown event_type values are rejected at CreditLockEvent construction
        (ValidationError), not at projection time.
        """
        import pydantic

        with pytest.raises((pydantic.ValidationError, ValueError)):
            CreditLockEvent(
                event_id=str(uuid.uuid4()),
                event_type="totally.unknown.event",
                production_id=PROD,
                aggregate_version=1,
                actor_id="actor-1",
                occurred_at=AT,
                payload=MemoUploadedPayload(
                    production_id=PROD,
                    doc_id="doc-1",
                    doc_hash="aabbcc",
                    doc_type="DEAL_MEMO",
                    uploaded_by="actor-1",
                    at=AT,
                ),
            )

    def test_unknown_event_construction_leaves_prior_state_unchanged(self, projector):
        """
        Unknown event_type rejected at construction; projection is never called.
        Prior state must be unchanged.
        """
        import pydantic

        projector.apply(_credit_roll_submitted(version=1))
        snap_before = copy.deepcopy(projector.get(PROD))

        with pytest.raises((pydantic.ValidationError, ValueError)):
            CreditLockEvent(
                event_id=str(uuid.uuid4()),
                event_type="unknown.event.type",
                production_id=PROD,
                aggregate_version=99,
                actor_id="actor-1",
                occurred_at=AT,
                payload=MemoUploadedPayload(
                    production_id=PROD,
                    doc_id="doc-x",
                    doc_hash="xx",
                    doc_type="DEAL_MEMO",
                    uploaded_by="actor-1",
                    at=AT,
                ),
            )

        snap_after = projector.get(PROD)
        assert snap_before["gate"]["state"] == snap_after["gate"]["state"]
        assert snap_before["gate"]["open_issues"] == snap_after["gate"]["open_issues"]
        assert snap_before["manifests"] == snap_after["manifests"]


# ── Out-of-order aggregate_version tests ─────────────────────────────────────


class TestOutOfOrderDelivery:
    def test_older_version_ignored(self, projector):
        """
        An event with a lower aggregate_version than what was already applied
        for this production returns False.
        """
        # Apply version 2 first
        projector.apply(_credit_roll_submitted(version=2))
        snap_after_v2 = copy.deepcopy(projector.get(PROD))

        # Now deliver version 1 (stale — should be skipped)
        stale_evt = _credit_roll_submitted(version=1)
        result = projector.apply(stale_evt)
        assert result is False
        snap_after_stale = projector.get(PROD)

        assert snap_after_v2["manifests"] == snap_after_stale["manifests"]
        assert snap_after_v2["gate"]["open_issues"] == snap_after_stale["gate"]["open_issues"]

    def test_equal_version_ignored(self, projector):
        """
        An event with the same aggregate_version as already applied
        but a different event_id returns False.
        """
        projector.apply(_credit_roll_submitted(version=1))
        snap1 = copy.deepcopy(projector.get(PROD))

        # Different event_id, same version = stale
        stale_evt = _credit_roll_submitted(version=1)
        result = projector.apply(stale_evt)
        assert result is False
        assert snap1["manifests"] == projector.get(PROD)["manifests"]

    def test_out_of_order_inject_via_transport(self, transport, projector):
        """
        inject_out_of_order places an event before index 0.
        replay must still ignore the older event when applying in delivery order.
        """
        later_evt = _credit_roll_submitted(version=3)
        transport.publish(later_evt)

        # Inject an older event at the front of the queue
        earlier_evt = _credit_roll_submitted(version=1)
        transport.inject_out_of_order(earlier_evt, before_index=0)

        # Queue is now: [version=1, version=3]
        projector.replay(transport, PROD)
        state = projector.get(PROD)

        # version 1 applied first, version 3 applied second → version 3 wins
        assert state is not None
        # Production-wide version sequence: last applied version is 3.
        assert projector._versions[PROD] == 3

    def test_memo_amended_older_version_ignored(self, projector):
        """
        Out-of-order memo.amended must not re-open a STALE_MANIFEST that was
        not yet present at a higher version.
        """
        projector.apply(_memo_amended(version=5))
        snap = copy.deepcopy(projector.get(PROD))

        stale_amendment = _memo_amended(version=2)
        result = projector.apply(stale_amendment)
        assert result is False
        assert snap["gate"]["open_issues"] == projector.get(PROD)["gate"]["open_issues"]


# ── Replay via transport ──────────────────────────────────────────────────────


class TestReplayViaTransport:
    def test_replay_produces_same_state_as_direct_apply(self, transport):
        """
        Replaying a sequence of events through the transport must produce the
        same gate state as applying them individually.
        """
        events = [
            _credit_roll_submitted(version=1),
            _artifact_rendered(version=2),
        ]
        for e in events:
            transport.publish(e)

        projector_a = Projector()
        for e in events:
            projector_a.apply(e)

        projector_b = Projector()
        projector_b.replay(transport, PROD)

        a_state = projector_a.get(PROD)
        b_state = projector_b.get(PROD)
        assert a_state["gate"]["state"] == b_state["gate"]["state"]
        assert a_state["gate"]["open_issues"] == b_state["gate"]["open_issues"]
        assert a_state["manifests"] == b_state["manifests"]

    def test_replay_with_duplicate_events_is_idempotent(self, transport):
        """
        Publishing the same event object twice simulates at-least-once delivery.
        The projection must be identical to single delivery.
        """
        evt = _credit_roll_submitted(version=1)
        transport.publish(evt)
        transport.publish(evt)  # duplicate

        projector_once = Projector()
        projector_once.apply(evt)

        projector_replay = Projector()
        projector_replay.replay(transport, PROD)

        once_state = projector_once.get(PROD)
        replay_state = projector_replay.get(PROD)
        assert once_state["gate"]["state"] == replay_state["gate"]["state"]
        assert once_state["gate"]["open_issues"] == replay_state["gate"]["open_issues"]


# ── Engine reuse verification ─────────────────────────────────────────────────


class TestEngineReuse:
    """
    Verify that fold_gate from the existing engine is what drives the gate
    state in the projection — not a reimplementation.
    """

    def test_fold_gate_drives_projection_gate_state(self, projector):
        """
        After credit_roll.submitted, the projection gate state must equal
        what fold_gate produces for an ARTIFACT_PENDING issue.
        """
        from creditlock.domain.gate import ProjectionStatus, fold_gate
        from creditlock.domain.models import Issue

        projector.apply(_credit_roll_submitted(version=1))
        state = projector.get(PROD)

        # Replicate what the projector computed
        issues_raw = state["gate"]["open_issues"]
        issues = [
            Issue(issue_id=i["issue_id"], code=IssueCode(i["type"]), detail=i.get("details", ""))
            for i in issues_raw
        ]
        ps = ProjectionStatus(
            artifact_pending=any(i["type"] == IssueCode.ARTIFACT_PENDING.value for i in issues_raw),
        )
        expected_gate = fold_gate(issues, ps)
        assert state["gate"]["state"] == expected_gate.value

    def test_artifact_rendered_alone_does_not_reach_ready_to_export(self, projector):
        """
        Regression: credit_roll.submitted → artifact.rendered alone must NOT
        produce READY_TO_EXPORT.  The gate must remain STALE because
        ARTIFACT_PENDING is still open.
        """
        from creditlock.domain.gate import ProjectionStatus, fold_gate
        from creditlock.domain.models import Issue

        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        state = projector.get(PROD)

        # ARTIFACT_PENDING still present → gate must be STALE
        issues_raw = state["gate"]["open_issues"]
        ap = [i for i in issues_raw if i["type"] == IssueCode.ARTIFACT_PENDING.value]
        assert len(ap) == 1, "ARTIFACT_PENDING must remain open"

        issues = [
            Issue(issue_id=i["issue_id"], code=IssueCode(i["type"]), detail=i.get("details", ""))
            for i in issues_raw
        ]
        ps = ProjectionStatus(
            artifact_pending=bool(ap),
        )
        expected_gate = fold_gate(issues, ps)
        assert expected_gate == GateState.STALE
        assert state["gate"]["state"] == GateState.STALE.value


# ── Projection hardening regression tests ─────────────────────────────────────

PROD2 = "prod-OTHER-999"


def _cross_production_memo_uploaded() -> CreditLockEvent:
    """Envelope says PROD but payload says PROD2."""
    return CreditLockEvent(
        event_id=str(uuid.uuid4()),
        event_type=EVENT_TYPE_MEMO_UPLOADED,
        production_id=PROD,
        aggregate_version=1,
        actor_id="actor-1",
        occurred_at=AT,
        payload=MemoUploadedPayload(
            production_id=PROD2,  # mismatch
            doc_id="doc-x",
            doc_hash="xx",
            doc_type="DEAL_MEMO",
            uploaded_by="actor-1",
            at=AT,
        ),
    )


class TestEnvelopePayloadProductionBinding:
    """Rule: envelope.production_id == payload.production_id; mismatch raises ProjectionError."""

    def _mismatched(self, event_type: str, payload) -> CreditLockEvent:  # type: ignore[no-untyped-def]
        """Build an event where envelope production_id != payload production_id."""
        return CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            production_id=PROD,
            aggregate_version=1,
            actor_id="actor-1",
            occurred_at=AT,
            payload=payload,
        )

    def test_memo_uploaded_cross_production_raises(self, projector):
        evt = self._mismatched(
            EVENT_TYPE_MEMO_UPLOADED,
            MemoUploadedPayload(
                production_id=PROD2,
                doc_id="d",
                doc_hash="h",
                doc_type="T",
                uploaded_by="a",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_memo_amended_cross_production_raises(self, projector):
        evt = self._mismatched(
            EVENT_TYPE_MEMO_AMENDED,
            MemoAmendedPayload(
                production_id=PROD2,
                doc_id="d2",
                supersedes_doc_id="d1",
                doc_hash="h",
                uploaded_by="a",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_credit_roll_submitted_cross_production_raises(self, projector):
        evt = self._mismatched(
            EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            CreditRollSubmittedPayload(
                production_id=PROD2,
                manifest_id="m1",
                manifest_hash="mh",
                submitted_by="a",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_resolution_recorded_cross_production_raises(self, projector):
        evt = self._mismatched(
            EVENT_TYPE_RESOLUTION_RECORDED,
            ResolutionRecordedPayload(
                production_id=PROD2,
                issue_id="i1",
                resolution_type=AuthorizationAction.CONFIRM_IDENTITY,
                actor_id="a",
                role="RELEASE_APPROVER",
                bound_hashes={},
                reason="cross-production test",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_patch_applied_cross_production_raises(self, projector):
        # First establish a valid manifest so we can test the production-id guard
        projector.apply(_credit_roll_submitted(version=1))
        evt = self._mismatched(
            EVENT_TYPE_PATCH_APPLIED,
            PatchAppliedPayload(
                production_id=PROD2,
                manifest_id="manifest-1",
                patch_id="p1",
                patch_hash="ph",
                applied_by="a",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_artifact_rendered_cross_production_raises(self, projector):
        # First establish a valid manifest
        projector.apply(_credit_roll_submitted(version=1))
        evt = self._mismatched(
            EVENT_TYPE_ARTIFACT_RENDERED,
            ArtifactRenderedPayload(
                production_id=PROD2,
                manifest_id="manifest-1",
                artifact_index_digest="d",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(evt)

    def test_cross_production_leaves_no_projection(self, projector):
        """A cross-production error on the FIRST event must not create a projection."""
        evt = _cross_production_memo_uploaded()
        with pytest.raises(ProjectionError):
            projector.apply(evt)
        assert projector.get(PROD) is None

    def test_cross_production_leaves_prior_state_unchanged(self, projector):
        """A cross-production error after prior events must not mutate state."""
        projector.apply(_credit_roll_submitted(version=1))
        snap = copy.deepcopy(projector.get(PROD))

        evt = _cross_production_memo_uploaded()
        with pytest.raises(ProjectionError):
            projector.apply(evt)

        state = projector.get(PROD)
        assert state["gate"]["state"] == snap["gate"]["state"]
        assert state["gate"]["open_issues"] == snap["gate"]["open_issues"]
        assert state["manifests"] == snap["manifests"]


class TestProductionWideVersionSequence:
    """
    Rule: aggregate_version is a single production-wide monotonic sequence.
    submit v1 + render v2 both apply; render at v1 after submit v1 is stale.
    """

    def test_submit_v1_then_render_v2_both_apply(self, projector):
        """Standard flow: submit v1, render v2 — both apply."""
        r1 = projector.apply(_credit_roll_submitted(version=1))
        r2 = projector.apply(_artifact_rendered(version=2))

        assert r1 is True
        assert r2 is True
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == "digest-abc123"

    def test_any_event_at_stale_version_returns_false(self, projector):
        """Any event at a version ≤ last applied production version returns False."""
        projector.apply(_credit_roll_submitted(version=3))
        # memo.uploaded at version 2 is stale
        result = projector.apply(_memo_uploaded(version=2))
        assert result is False

    def test_duplicate_event_id_returns_false(self, projector):
        """Duplicate event_id returns False."""
        eid = str(uuid.uuid4())
        evt = _credit_roll_submitted(version=1, event_id=eid)
        projector.apply(evt)
        result = projector.apply(evt)
        assert result is False

    def test_stale_version_returns_false(self, projector):
        """Stale version returns False (production-wide)."""
        projector.apply(_credit_roll_submitted(version=3))
        result = projector.apply(_credit_roll_submitted(version=1))
        assert result is False

    def test_patch_after_render_uses_higher_version(self, projector):
        """submit v1, render v2, patch v3 all apply in sequence."""
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        result = projector.apply(_patch_applied(version=3))
        assert result is True


class TestManifestRelationshipValidation:
    """Rules: artifact.rendered and patch.applied must reference current_manifest_id."""

    def test_render_without_prior_manifest_raises(self, projector):
        """artifact.rendered with no current manifest raises ProjectionError."""
        with pytest.raises(ProjectionError) as exc_info:
            projector.apply(_artifact_rendered(version=1))
        assert exc_info.value.event_type == EVENT_TYPE_ARTIFACT_RENDERED

    def test_render_without_prior_manifest_creates_no_projection(self, projector):
        """Rejected first event must not create a projection."""
        with pytest.raises(ProjectionError):
            projector.apply(_artifact_rendered(version=1))
        assert projector.get(PROD) is None

    def test_patch_without_prior_manifest_raises(self, projector):
        """patch.applied with no current manifest raises ProjectionError."""
        with pytest.raises(ProjectionError) as exc_info:
            projector.apply(_patch_applied(version=1))
        assert exc_info.value.event_type == EVENT_TYPE_PATCH_APPLIED

    def test_patch_without_prior_manifest_creates_no_projection(self, projector):
        """Rejected first patch must not create a projection."""
        with pytest.raises(ProjectionError):
            projector.apply(_patch_applied(version=1))
        assert projector.get(PROD) is None

    def test_render_for_wrong_manifest_raises(self, projector):
        """artifact.rendered for a different manifest_id raises ProjectionError."""
        projector.apply(_credit_roll_submitted(version=1))

        wrong_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="actor-1",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="WRONG-MANIFEST-ID",
                artifact_index_digest="digest-xyz",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError) as exc_info:
            projector.apply(wrong_render)
        assert exc_info.value.event_type == EVENT_TYPE_ARTIFACT_RENDERED

    def test_render_for_wrong_manifest_leaves_state_unchanged(self, projector):
        """Rejected render must not change artifact_index_digest, lifecycle, or gate."""
        projector.apply(_credit_roll_submitted(version=1))
        snap = copy.deepcopy(projector.get(PROD))

        wrong_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="actor-1",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="OLD-MANIFEST-42",
                artifact_index_digest="digest-stale",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(wrong_render)

        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == snap["gate"]["artifact_index_digest"]
        assert state["manifests"]["manifest_lifecycle"] == snap["manifests"]["manifest_lifecycle"]
        assert state["gate"]["state"] == snap["gate"]["state"]
        assert state["gate"]["open_issues"] == snap["gate"]["open_issues"]

    def test_render_wrong_manifest_not_added_to_seen(self, projector):
        """Rejected render must not be recorded in the seen-event set."""
        projector.apply(_credit_roll_submitted(version=1))
        eid = str(uuid.uuid4())
        wrong_render = CreditLockEvent(
            event_id=eid,
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="actor-1",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="OLD-MANIFEST",
                artifact_index_digest="d",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(wrong_render)
        assert eid not in projector._seen.get(PROD, set())

    def test_render_wrong_manifest_version_not_recorded(self, projector):
        """Rejected render must not update the version counter for artifact.rendered."""
        projector.apply(_credit_roll_submitted(version=1))
        eid = str(uuid.uuid4())
        wrong_render = CreditLockEvent(
            event_id=eid,
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=99,
            actor_id="actor-1",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="OLD-MANIFEST",
                artifact_index_digest="d",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(wrong_render)
        # Production-wide version must not have been bumped to 99
        assert projector._versions.get(PROD, 0) != 99

    def test_late_render_higher_version_still_rejected(self, projector):
        """
        A stale render for an OLDER manifest carrying a HIGHER aggregate_version
        must still be rejected.  Manifest identity beats version number.
        """
        # Submit manifest-1 (v1), render it (v2), then submit manifest-2 (v3)
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))

        submit_v2 = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            production_id=PROD,
            aggregate_version=3,
            actor_id="actor-1",
            occurred_at=AT,
            payload=CreditRollSubmittedPayload(
                production_id=PROD,
                manifest_id="manifest-2",
                manifest_hash="mhash-002",
                submitted_by="actor-1",
                at=AT,
            ),
        )
        projector.apply(submit_v2)

        # Now a late render for manifest-1 arrives with a high version
        late_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=999,
            actor_id="render-worker",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="manifest-1",  # stale manifest
                artifact_index_digest="stale-digest",
                render_profile_version="v1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(late_render)

        # artifact_index_digest must be None (cleared by submit_v2)
        assert projector.get(PROD)["gate"]["artifact_index_digest"] is None

    def test_matching_render_records_digest_remains_stale(self, projector):
        """
        A valid matching render records the artifact_index_digest but
        ARTIFACT_PENDING stays open and the gate remains STALE.
        """
        projector.apply(_credit_roll_submitted(version=1))
        result = projector.apply(_artifact_rendered(version=2))

        assert result is True
        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == "digest-abc123"
        assert state["gate"]["render_profile_version"] == "chromium-130-v1"
        assert state["gate"]["state"] == GateState.STALE.value
        ap = [
            i for i in state["gate"]["open_issues"] if i["type"] == IssueCode.ARTIFACT_PENDING.value
        ]
        assert len(ap) == 1

    def test_patch_for_wrong_manifest_raises(self, projector):
        """patch.applied for a different manifest_id raises ProjectionError."""
        projector.apply(_credit_roll_submitted(version=1))

        wrong_patch = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_PATCH_APPLIED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="actor-1",
            occurred_at=AT,
            payload=PatchAppliedPayload(
                production_id=PROD,
                manifest_id="OLD-MANIFEST-XYZ",
                patch_id="p1",
                patch_hash="ph",
                applied_by="actor-1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError) as exc_info:
            projector.apply(wrong_patch)
        assert exc_info.value.event_type == EVENT_TYPE_PATCH_APPLIED

    def test_patch_for_wrong_manifest_leaves_state_unchanged(self, projector):
        """Rejected patch must not mutate digests, issues, or manifest."""
        projector.apply(_credit_roll_submitted(version=1))
        snap = copy.deepcopy(projector.get(PROD))

        wrong_patch = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_PATCH_APPLIED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="actor-1",
            occurred_at=AT,
            payload=PatchAppliedPayload(
                production_id=PROD,
                manifest_id="WRONG-ID",
                patch_id="p1",
                patch_hash="ph",
                applied_by="actor-1",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(wrong_patch)

        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == snap["gate"]["artifact_index_digest"]
        assert state["gate"]["release_evidence_digest"] == snap["gate"]["release_evidence_digest"]
        assert state["gate"]["open_issues"] == snap["gate"]["open_issues"]
        assert state["manifests"] == snap["manifests"]

    def test_credit_roll_stores_manifest_id(self, projector):
        """credit_roll.submitted stores current_manifest_id for later binding."""
        projector.apply(_credit_roll_submitted(version=1))
        assert projector.get(PROD)["manifests"]["current_manifest_id"] == "manifest-1"


# ── Required new regression tests ─────────────────────────────────────────────


class TestDocumentsSection:
    """Documents section records memo uploads and amendments with lineage."""

    def test_memo_uploaded_records_doc_in_documents(self, projector):
        evt = _memo_uploaded(version=1)
        projector.apply(evt)
        state = projector.get(PROD)
        assert "documents" in state
        docs = state["documents"]
        assert len(docs) == 1
        assert docs[0]["doc_id"] == "doc-1"
        assert docs[0]["doc_hash"] == "aabbcc"
        assert docs[0]["doc_type"] == "DEAL_MEMO"
        assert docs[0]["uploaded_by"] == "actor-1"
        assert docs[0]["supersedes_doc_id"] is None
        assert docs[0]["event_id"] == evt.event_id

    def test_memo_amended_records_amendment_with_lineage(self, projector):
        evt1 = _memo_uploaded(version=1)
        evt2 = _memo_amended(version=2)
        projector.apply(evt1)
        projector.apply(evt2)
        state = projector.get(PROD)
        docs = state["documents"]
        # Should have both the original and the amendment
        doc_ids = {d["doc_id"] for d in docs}
        assert "doc-1" in doc_ids
        assert "doc-2" in doc_ids
        original = next(d for d in docs if d["doc_id"] == "doc-1")
        assert original["event_id"] == evt1.event_id
        amendment = next(d for d in docs if d["doc_id"] == "doc-2")
        assert amendment["doc_type"] == "amendment"
        assert amendment["supersedes_doc_id"] == "doc-1"
        assert amendment["event_id"] == evt2.event_id

    def test_documents_deduplicated_by_doc_id(self, projector):
        """Same event applied twice (idempotent) must not duplicate doc records."""
        eid = str(uuid.uuid4())
        evt = _memo_uploaded(version=1, event_id=eid)
        projector.apply(evt)
        projector.apply(evt)  # duplicate — ignored
        assert len(projector.get(PROD)["documents"]) == 1


class TestAuthorizationLogReason:
    """resolution.recorded must record payload.reason verbatim."""

    def test_authorization_log_contains_exact_reason(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        res = _resolution_recorded("some-issue", version=2)
        projector.apply(res)
        log = projector.get(PROD)["authorization_log"]
        assert len(log) == 1
        assert log[0]["reason"] == "Confirmed identity via production contract"

    def test_reason_not_manufactured_from_resolution_type(self, projector):
        """reason must be the string from payload, not derived from resolution_type."""
        projector.apply(_credit_roll_submitted(version=1))
        res = _resolution_recorded("issue-x", version=2)
        projector.apply(res)
        log = projector.get(PROD)["authorization_log"]
        # The reason must not be the enum value string
        assert log[0]["reason"] != "CONFIRM_IDENTITY"
        assert log[0]["reason"] == "Confirmed identity via production contract"


class TestSecondRenderAfterRenderComplete:
    """A second distinct higher-version render after RENDER_COMPLETE raises."""

    def test_second_render_after_render_complete_raises(self, projector):
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        # First render completed → RENDER_COMPLETE
        assert projector.get(PROD)["manifests"]["manifest_lifecycle"] == "RENDER_COMPLETE"

        second_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=3,
            actor_id="render-worker",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="manifest-1",
                artifact_index_digest="new-digest",
                render_profile_version="v2",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError) as exc_info:
            projector.apply(second_render)
        assert exc_info.value.event_type == EVENT_TYPE_ARTIFACT_RENDERED

    def test_second_render_after_render_complete_makes_no_mutation(self, projector):
        """Second render that raises must not change any state."""
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        snap = copy.deepcopy(projector.get(PROD))

        second_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=3,
            actor_id="render-worker",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="manifest-1",
                artifact_index_digest="new-digest",
                render_profile_version="v2",
                at=AT,
            ),
        )
        with pytest.raises(ProjectionError):
            projector.apply(second_render)

        state = projector.get(PROD)
        assert state["gate"]["artifact_index_digest"] == snap["gate"]["artifact_index_digest"]
        assert state["manifests"]["manifest_lifecycle"] == snap["manifests"]["manifest_lifecycle"]

    def test_duplicate_old_render_after_newer_manifest_returns_false(self, projector):
        """
        A render at a stale version (≤ last applied) after a newer manifest
        returns False — must not raise manifest mismatch.
        Stale check runs BEFORE manifest validation.
        """
        projector.apply(_credit_roll_submitted(version=1))
        projector.apply(_artifact_rendered(version=2))
        # Submit manifest-2 at version 3 — last_version is now 3
        submit2 = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            production_id=PROD,
            aggregate_version=3,
            actor_id="actor-1",
            occurred_at=AT,
            payload=CreditRollSubmittedPayload(
                production_id=PROD,
                manifest_id="manifest-2",
                manifest_hash="mhash-002",
                submitted_by="actor-1",
                at=AT,
            ),
        )
        projector.apply(submit2)
        # Old render for manifest-1 arrives at version 2 (stale — 2 ≤ 3)
        old_render = CreditLockEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_TYPE_ARTIFACT_RENDERED,
            production_id=PROD,
            aggregate_version=2,
            actor_id="render-worker",
            occurred_at=AT,
            payload=ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="manifest-1",
                artifact_index_digest="stale-digest",
                render_profile_version="v1",
                at=AT,
            ),
        )
        # Must return False (stale), not raise ProjectionError
        result = projector.apply(old_render)
        assert result is False


class TestApplyReturnsBoolOnly:
    """apply() must return only True or False; never SkipReason or other types."""

    def test_apply_returns_true_on_success(self, projector):
        result = projector.apply(_memo_uploaded(version=1))
        assert result is True
        assert type(result) is bool

    def test_apply_returns_false_on_duplicate(self, projector):
        eid = str(uuid.uuid4())
        evt = _credit_roll_submitted(version=1, event_id=eid)
        projector.apply(evt)
        result = projector.apply(evt)
        assert result is False
        assert type(result) is bool

    def test_apply_returns_false_on_stale(self, projector):
        projector.apply(_credit_roll_submitted(version=3))
        result = projector.apply(_memo_uploaded(version=1))
        assert result is False
        assert type(result) is bool


class TestAllSixPayloadMismatches:
    """All six event-type/payload mismatches fail at model construction."""

    def _mismatch(self, event_type: str, wrong_payload):
        import pydantic

        with pytest.raises((pydantic.ValidationError, TypeError, ValueError)):
            CreditLockEvent(
                event_id=str(uuid.uuid4()),
                event_type=event_type,
                production_id=PROD,
                aggregate_version=1,
                actor_id="actor-1",
                occurred_at=AT,
                payload=wrong_payload,
            )

    def test_memo_uploaded_with_artifact_payload_fails(self):
        self._mismatch(
            EVENT_TYPE_MEMO_UPLOADED,
            ArtifactRenderedPayload(
                production_id=PROD,
                manifest_id="m",
                artifact_index_digest="d",
                render_profile_version="v1",
                at=AT,
            ),
        )

    def test_memo_amended_with_credit_roll_payload_fails(self):
        self._mismatch(
            EVENT_TYPE_MEMO_AMENDED,
            CreditRollSubmittedPayload(
                production_id=PROD,
                manifest_id="m",
                manifest_hash="h",
                submitted_by="a",
                at=AT,
            ),
        )

    def test_credit_roll_with_patch_payload_fails(self):
        self._mismatch(
            EVENT_TYPE_CREDIT_ROLL_SUBMITTED,
            PatchAppliedPayload(
                production_id=PROD,
                manifest_id="m",
                patch_id="p",
                patch_hash="ph",
                applied_by="a",
                at=AT,
            ),
        )

    def test_resolution_with_memo_uploaded_payload_fails(self):
        self._mismatch(
            EVENT_TYPE_RESOLUTION_RECORDED,
            MemoUploadedPayload(
                production_id=PROD,
                doc_id="d",
                doc_hash="h",
                doc_type="T",
                uploaded_by="a",
                at=AT,
            ),
        )

    def test_patch_applied_with_memo_amended_payload_fails(self):
        self._mismatch(
            EVENT_TYPE_PATCH_APPLIED,
            MemoAmendedPayload(
                production_id=PROD,
                doc_id="d2",
                supersedes_doc_id="d1",
                doc_hash="h",
                uploaded_by="a",
                at=AT,
            ),
        )

    def test_artifact_rendered_with_resolution_payload_fails(self):
        from creditlock.domain.models import AuthorizationAction

        self._mismatch(
            EVENT_TYPE_ARTIFACT_RENDERED,
            ResolutionRecordedPayload(
                production_id=PROD,
                issue_id="i",
                role="R",
                resolution_type=AuthorizationAction.CONFIRM_IDENTITY,
                actor_id="a",
                bound_hashes={},
                reason="r",
                at=AT,
            ),
        )
