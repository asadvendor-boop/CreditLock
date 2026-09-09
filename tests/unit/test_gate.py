"""
Failing tests for the deterministic gate fold.
Run before implementing gate.py to confirm red state.
"""

from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import GateState, Issue, IssueCode


def _issue(code: IssueCode, issue_id: str = "i1") -> Issue:
    return Issue(
        issue_id=issue_id,
        code=code,
        detail="test",
    )


class TestFoldGate:
    def test_no_issues_ready_to_export(self):
        assert fold_gate([], ProjectionStatus()) == GateState.READY_TO_EXPORT

    def test_missing_credit_gives_blocked(self):
        issues = [_issue(IssueCode.MISSING_CREDIT)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_text_mismatch_gives_blocked(self):
        issues = [_issue(IssueCode.ARTIFACT_TEXT_MISMATCH)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_grouping_mismatch_gives_blocked(self):
        issues = [_issue(IssueCode.ARTIFACT_GROUPING_MISMATCH)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_position_mismatch_gives_blocked(self):
        issues = [_issue(IssueCode.ARTIFACT_POSITION_MISMATCH)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_size_mismatch_gives_blocked(self):
        issues = [_issue(IssueCode.ARTIFACT_SIZE_MISMATCH)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_integrity_failure_gives_blocked(self):
        issues = [_issue(IssueCode.ARTIFACT_INTEGRITY_FAILURE)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_ambiguous_identity_gives_needs_human(self):
        issues = [_issue(IssueCode.AMBIGUOUS_IDENTITY)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_HUMAN

    def test_conflicting_obligation_gives_needs_human(self):
        issues = [_issue(IssueCode.CONFLICTING_OBLIGATION)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_HUMAN

    def test_visual_uncertain_gives_needs_human(self):
        issues = [_issue(IssueCode.VISUAL_OBSERVATION_UNCERTAIN)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_HUMAN

    def test_unsupported_assertion_gives_needs_human(self):
        issues = [_issue(IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_HUMAN

    def test_evidence_unavailable_gives_degraded(self):
        issues = [_issue(IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.DEGRADED

    def test_unconfirmed_obligation_gives_needs_confirmation(self):
        issues = [_issue(IssueCode.UNCONFIRMED_OBLIGATION)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_CONFIRMATION

    def test_stale_manifest_gives_stale(self):
        issues = [_issue(IssueCode.STALE_MANIFEST)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

    def test_artifact_pending_gives_stale(self):
        issues = [_issue(IssueCode.ARTIFACT_PENDING)]
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

    # ── Precedence: DEGRADED beats everything ────────────────────────────────

    def test_degraded_beats_blocked(self):
        issues = [
            _issue(IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE, "i1"),
            _issue(IssueCode.MISSING_CREDIT, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.DEGRADED

    def test_degraded_beats_needs_human(self):
        issues = [
            _issue(IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE, "i1"),
            _issue(IssueCode.AMBIGUOUS_IDENTITY, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.DEGRADED

    def test_blocked_beats_needs_human(self):
        issues = [
            _issue(IssueCode.MISSING_CREDIT, "i1"),
            _issue(IssueCode.AMBIGUOUS_IDENTITY, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_blocked_beats_needs_confirmation(self):
        issues = [
            _issue(IssueCode.MISSING_CREDIT, "i1"),
            _issue(IssueCode.UNCONFIRMED_OBLIGATION, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.BLOCKED

    def test_needs_human_beats_needs_confirmation(self):
        issues = [
            _issue(IssueCode.AMBIGUOUS_IDENTITY, "i1"),
            _issue(IssueCode.UNCONFIRMED_OBLIGATION, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_HUMAN

    def test_needs_confirmation_beats_stale(self):
        issues = [
            _issue(IssueCode.UNCONFIRMED_OBLIGATION, "i1"),
            _issue(IssueCode.ARTIFACT_PENDING, "i2"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.NEEDS_CONFIRMATION

    def test_full_precedence_order(self):
        """All six states represented; DEGRADED must win."""
        issues = [
            _issue(IssueCode.ARTIFACT_PENDING, "i1"),
            _issue(IssueCode.UNCONFIRMED_OBLIGATION, "i2"),
            _issue(IssueCode.AMBIGUOUS_IDENTITY, "i3"),
            _issue(IssueCode.MISSING_CREDIT, "i4"),
            _issue(IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE, "i5"),
        ]
        assert fold_gate(issues, ProjectionStatus()) == GateState.DEGRADED

    def test_all_issues_cleared_gives_ready(self):
        assert fold_gate([], ProjectionStatus()) == GateState.READY_TO_EXPORT

    # ── ProjectionStatus stale flags ─────────────────────────────────────────

    def test_artifact_pending_projection_gives_stale(self):
        status = ProjectionStatus(artifact_pending=True)
        assert fold_gate([], status) == GateState.STALE

    def test_stale_manifest_projection_gives_stale(self):
        status = ProjectionStatus(stale_manifest=True)
        assert fold_gate([], status) == GateState.STALE
