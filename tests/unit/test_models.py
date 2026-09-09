"""
Failing tests for domain models — run before implementing to confirm red state.
"""

import pytest
from pydantic import ValidationError

from creditlock.domain.models import (
    GATE_PRECEDENCE,
    ISSUE_GATE_MAP,
    AbsolutePosition,
    AliasRecord,
    Authorization,
    CardPositionKind,
    ContributorRecord,
    CreditManifest,
    CreditSurface,
    GateState,
    IssueCode,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    RelativePosition,
    RelativeRelation,
    SizeComparisonBasis,
    SourceSpan,
)

# ── SourceSpan ────────────────────────────────────────────────────────────────


class TestSourceSpan:
    def test_valid(self):
        s = SourceSpan(page=1, start_char=0, end_char=10, quote="hello world")
        assert s.start_char == 0
        assert s.end_char == 10

    def test_page_none_allowed(self):
        s = SourceSpan(page=None, start_char=0, end_char=5, quote="hi")
        assert s.page is None

    def test_end_before_start_raises(self):
        with pytest.raises(ValidationError):
            SourceSpan(page=1, start_char=10, end_char=5, quote="bad")

    def test_equal_start_end_allowed(self):
        s = SourceSpan(page=1, start_char=5, end_char=5, quote="")
        assert s.start_char == s.end_char

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            SourceSpan(page=1, start_char=0, end_char=1, quote="x", unknown="y")

    def test_page_zero_raises(self):
        with pytest.raises(ValidationError):
            SourceSpan(page=0, start_char=0, end_char=1, quote="x")


# ── CardPosition ──────────────────────────────────────────────────────────────


class TestCardPosition:
    def test_absolute_valid(self):
        p = AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1)
        assert p.ordinal == 1

    def test_absolute_zero_ordinal_raises(self):
        with pytest.raises(ValidationError):
            AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=0)

    def test_relative_valid(self):
        p = RelativePosition(
            kind=CardPositionKind.RELATIVE,
            relation=RelativeRelation.BEFORE,
            reference_rendered_element_id="elem_1",
        )
        assert p.relation == RelativeRelation.BEFORE

    def test_absolute_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1, extra="x")


# ── Obligation ────────────────────────────────────────────────────────────────


def _base_obligation(**kwargs) -> dict:
    base = {
        "obligation_id": "obl_001",
        "production_id": "prod_001",
        "credit_surface": CreditSurface.MAIN_TITLES,
        "source_document_id": "doc_001",
        "source_document_version": 1,
        "source_span": {"page": 1, "start_char": 0, "end_char": 10, "quote": "test"},
        "source_hash": "a" * 64,
        "agent_reported_confidence": 0.95,
        "extraction_model_id": "gemini-3.6-flash",
        "prompt_version": "v1",
        "status": ObligationStatus.CANDIDATE,
    }
    base.update(kwargs)
    return base


class TestObligation:
    def test_minimal_valid(self):
        o = Obligation(**_base_obligation())
        assert o.status == ObligationStatus.CANDIDATE
        assert o.shared_with == []
        assert o.territories == []

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            Obligation(**_base_obligation(unknown_field="bad"))

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            Obligation(**_base_obligation(agent_reported_confidence=1.5))

    def test_negative_confidence_raises(self):
        with pytest.raises(ValidationError):
            Obligation(**_base_obligation(agent_reported_confidence=-0.1))

    def test_size_without_reference_raises(self):
        with pytest.raises(ValidationError):
            Obligation(**_base_obligation(minimum_relative_size=0.5))

    def test_size_reference_without_size_raises(self):
        with pytest.raises(ValidationError):
            Obligation(
                **_base_obligation(
                    size_reference={"rendered_element_id": "title"},
                    size_comparison_basis=SizeComparisonBasis.COMPUTED_FONT_SIZE_PX,
                )
            )

    def test_size_fields_all_present_valid(self):
        o = Obligation(
            **_base_obligation(
                minimum_relative_size=0.5,
                size_reference={"rendered_element_id": "title"},
                size_comparison_basis=SizeComparisonBasis.COMPUTED_FONT_SIZE_PX,
            )
        )
        assert o.minimum_relative_size == 0.5

    def test_duration_zero_allowed(self):
        o = Obligation(**_base_obligation(minimum_visible_duration_ms=0))
        assert o.minimum_visible_duration_ms == 0

    def test_duration_negative_raises(self):
        with pytest.raises(ValidationError):
            Obligation(**_base_obligation(minimum_visible_duration_ms=-1))

    def test_all_lifecycle_states(self):
        for state in ObligationStatus:
            o = Obligation(**_base_obligation(status=state))
            assert o.status == state

    def test_card_position_absolute(self):
        o = Obligation(**_base_obligation(card_position={"kind": "ABSOLUTE", "ordinal": 3}))
        assert isinstance(o.card_position, AbsolutePosition)
        assert o.card_position.ordinal == 3

    def test_card_position_relative(self):
        o = Obligation(
            **_base_obligation(
                card_position={
                    "kind": "RELATIVE",
                    "relation": "BEFORE",
                    "reference_rendered_element_id": "elem_title",
                }
            )
        )
        assert isinstance(o.card_position, RelativePosition)


# ── ManifestEntry and CreditManifest ─────────────────────────────────────────


def _base_entry(**kwargs) -> dict:
    base = {
        "rendered_element_id": "elem_001",
        "contributor_id": "contrib_001",
        "display_name": "Jane Smith",
        "role": "Director",
        "credit_surface": CreditSurface.MAIN_TITLES,
        "ordinal_position": 1,
        "production_id": "prod_001",
        "delivery_version_id": "v1",
    }
    base.update(kwargs)
    return base


class TestManifestEntry:
    def test_valid(self):
        e = ManifestEntry(**_base_entry())
        assert e.display_name == "Jane Smith"

    def test_ordinal_zero_allowed(self):
        # ordinal_position=0 is valid for reference elements (title refs, section headers)
        e = ManifestEntry(**_base_entry(ordinal_position=0))
        assert e.ordinal_position == 0

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            ManifestEntry(**_base_entry(unknown="x"))


class TestCreditManifest:
    def test_valid(self):
        m = CreditManifest(
            manifest_id="m_001",
            production_id="prod_001",
            delivery_version_id="v1",
            entries=[ManifestEntry(**_base_entry())],
        )
        assert len(m.entries) == 1

    def test_empty_entries_allowed(self):
        m = CreditManifest(
            manifest_id="m_002",
            production_id="prod_001",
            delivery_version_id="v1",
            entries=[],
        )
        assert m.entries == []


# ── ContributorRecord ─────────────────────────────────────────────────────────


class TestContributorRecord:
    def test_valid(self):
        r = ContributorRecord(
            contributor_id="c_001",
            canonical_name="David Park",
            aliases=[
                AliasRecord(
                    alias="D. Park",
                    registered_by="reviewer_1",
                    registered_at="2026-07-29T00:00:00Z",
                    reason="abbreviation confirmed",
                )
            ],
        )
        assert len(r.aliases) == 1

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            ContributorRecord(contributor_id="c_1", canonical_name="X", extra="y")


# ── Authorization ─────────────────────────────────────────────────────────────


class TestAuthorization:
    def test_valid(self):
        a = Authorization(
            authorization_id="auth_001",
            production_id="prod_001",
            proposal_id="prop_001",
            proposer_id="reviewer_1",
            issue_id="issue_001",
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            selected_contributor_id="contrib_david_park",
            reason="Confirmed D. Park = David Park via production contract",
            manifest_hash="b" * 64,
            obligation_registry_version_hash="c" * 64,
            artifact_index_digest="d" * 64,
            visual_observations_hash="e" * 64,
            authorized_at="2026-07-29T10:00:00Z",
        )
        assert a.role == "RELEASE_APPROVER"
        assert a.selected_contributor_id == "contrib_david_park"

    def test_wrong_role_raises(self):
        with pytest.raises(ValidationError):
            Authorization(
                authorization_id="auth_002",
                production_id="prod_001",
                proposal_id="prop_001",
                proposer_id="reviewer_1",
                issue_id="issue_001",
                actor_id="reviewer_1",
                role="REVIEWER",  # not allowed
                action="CONFIRM_IDENTITY",
                reason="test",
                manifest_hash="b" * 64,
                obligation_registry_version_hash="c" * 64,
                artifact_index_digest="d" * 64,
                visual_observations_hash="e" * 64,
                authorized_at="2026-07-29T10:00:00Z",
            )

    def test_no_release_evidence_field(self):
        """Authorization must NOT have a release_evidence_digest field."""
        fields = Authorization.model_fields
        assert "release_evidence_digest" not in fields


# ── IssueCode → GateState mapping completeness ────────────────────────────────


class TestIssueMappings:
    def test_all_issue_codes_mapped(self):
        for code in IssueCode:
            assert code in ISSUE_GATE_MAP, f"{code} not in ISSUE_GATE_MAP"

    def test_blocking_codes_map_to_blocked(self):
        blocking = [
            IssueCode.MISSING_CREDIT,
            IssueCode.ARTIFACT_TEXT_MISMATCH,
            IssueCode.ARTIFACT_GROUPING_MISMATCH,
            IssueCode.ARTIFACT_POSITION_MISMATCH,
            IssueCode.ARTIFACT_SIZE_MISMATCH,
            IssueCode.ARTIFACT_INTEGRITY_FAILURE,
        ]
        for code in blocking:
            assert ISSUE_GATE_MAP[code] == GateState.BLOCKED

    def test_human_codes_map_to_needs_human(self):
        human = [
            IssueCode.AMBIGUOUS_IDENTITY,
            IssueCode.CONFLICTING_OBLIGATION,
            IssueCode.VISUAL_OBSERVATION_UNCERTAIN,
            IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION,
        ]
        for code in human:
            assert ISSUE_GATE_MAP[code] == GateState.NEEDS_HUMAN

    def test_degraded_code(self):
        assert ISSUE_GATE_MAP[IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE] == GateState.DEGRADED

    def test_confirmation_code(self):
        assert ISSUE_GATE_MAP[IssueCode.UNCONFIRMED_OBLIGATION] == GateState.NEEDS_CONFIRMATION

    def test_stale_codes(self):
        assert ISSUE_GATE_MAP[IssueCode.STALE_MANIFEST] == GateState.STALE
        assert ISSUE_GATE_MAP[IssueCode.ARTIFACT_PENDING] == GateState.STALE


# ── Gate precedence order ─────────────────────────────────────────────────────


class TestGatePrecedence:
    def test_degraded_is_highest(self):
        assert GATE_PRECEDENCE[0] == GateState.DEGRADED

    def test_ready_is_lowest(self):
        assert GATE_PRECEDENCE[-1] == GateState.READY_TO_EXPORT

    def test_full_order(self):
        assert GATE_PRECEDENCE == [
            GateState.DEGRADED,
            GateState.BLOCKED,
            GateState.NEEDS_HUMAN,
            GateState.NEEDS_CONFIRMATION,
            GateState.STALE,
            GateState.READY_TO_EXPORT,
        ]

    def test_all_states_in_precedence(self):
        for state in GateState:
            assert state in GATE_PRECEDENCE
