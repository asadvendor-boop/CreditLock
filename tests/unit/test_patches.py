"""
Tests for the patch validator.
Run before any changes to confirm green/red state.
"""

from creditlock.domain.models import (
    CardType,
    CreditManifest,
    CreditSurface,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    Patch,
    PatchOperation,
    SourceSpan,
)
from creditlock.domain.patches import validate_patch


def _base_obligation(**kwargs) -> Obligation:
    defaults = {
        "obligation_id": "obl_p1",
        "production_id": "prod_p",
        "credited_party_id": "contrib_p1",
        "credit_surface": CreditSurface.MAIN_TITLES,
        "source_document_id": "doc_p",
        "source_document_version": 1,
        "source_span": SourceSpan(page=1, start_char=0, end_char=10, quote="test"),
        "source_hash": "a" * 64,
        "agent_reported_confidence": 0.95,
        "extraction_model_id": "gemini",
        "prompt_version": "v1",
        "status": ObligationStatus.ACTIVE,
    }
    defaults.update(kwargs)
    return Obligation(**defaults)


def _base_manifest(display_name: str = "Test Name") -> CreditManifest:
    return CreditManifest(
        manifest_id="m_p1",
        production_id="prod_p",
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem_p1",
                contributor_id="contrib_p1",
                display_name=display_name,
                role="Director",
                credit_surface=CreditSurface.MAIN_TITLES,
                ordinal_position=1,
                production_id="prod_p",
                delivery_version_id="v1",
            )
        ],
    )


def _patch(**kwargs) -> Patch:
    defaults = {
        "patch_id": "patch_001",
        "production_id": "prod_p",
        "manifest_id": "m_p1",
        "issue_id": "issue_001",
        "obligation_id": "obl_p1",
        "operation": PatchOperation.SUBSTITUTE_TEXT,
        "target_rendered_element_id": "elem_p1",
        "payload": {"new_text": "Correct Name"},
        "proposed_by": "steward",
        "proposed_at": "2026-07-29T10:00:00Z",
    }
    defaults.update(kwargs)
    return Patch(**defaults)


class TestSubstituteText:
    def test_valid_substitute(self):
        obl = _base_obligation(required_display_text="Correct Name")
        manifest = _base_manifest("Wrong Name")
        patch = _patch(payload={"new_text": "Correct Name"})
        result = validate_patch(patch, obl, manifest)
        assert result.valid

    def test_substitute_nfc_equivalence(self):
        """NFC and NFD forms of the same text must both be valid."""
        obl = _base_obligation(required_display_text="caf\u00e9")  # NFC
        manifest = _base_manifest("Wrong")
        patch = _patch(payload={"new_text": "cafe\u0301"})  # NFD — same text
        result = validate_patch(patch, obl, manifest)
        assert result.valid

    def test_invented_value_rejected(self):
        """new_text that differs from required_display_text must be rejected."""
        obl = _base_obligation(required_display_text="Correct Name")
        manifest = _base_manifest("Wrong Name")
        patch = _patch(payload={"new_text": "Something Invented"})
        result = validate_patch(patch, obl, manifest)
        assert not result.valid
        assert "Correct Name" in result.rejection_reason

    def test_null_required_display_text_rejected(self):
        """Cannot substitute text when obligation has no required_display_text."""
        obl = _base_obligation(required_display_text=None)
        manifest = _base_manifest()
        patch = _patch(payload={"new_text": "Anything"})
        result = validate_patch(patch, obl, manifest)
        assert not result.valid

    def test_missing_new_text_rejected(self):
        obl = _base_obligation(required_display_text="X")
        manifest = _base_manifest()
        patch = _patch(payload={})  # no new_text key
        result = validate_patch(patch, obl, manifest)
        assert not result.valid

    def test_non_string_new_text_rejected(self):
        """Non-string new_text (e.g. integer) must return valid=False with explicit reason."""
        obl = _base_obligation(required_display_text="Correct Name")
        manifest = _base_manifest("Wrong Name")
        patch = _patch(payload={"new_text": 12345})
        result = validate_patch(patch, obl, manifest)
        assert not result.valid
        assert "must be a string" in result.rejection_reason


class TestReorder:
    def test_valid_reorder(self):
        obl = _base_obligation(card_position={"kind": "ABSOLUTE", "ordinal": 2})
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REORDER,
            payload={"new_ordinal": 2},
        )
        result = validate_patch(patch, obl, manifest)
        assert result.valid

    def test_wrong_ordinal_rejected(self):
        obl = _base_obligation(card_position={"kind": "ABSOLUTE", "ordinal": 2})
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REORDER,
            payload={"new_ordinal": 5},  # 5 ≠ 2
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid

    def test_relative_position_cannot_reorder(self):
        obl = _base_obligation(
            card_position={
                "kind": "RELATIVE",
                "relation": "BEFORE",
                "reference_rendered_element_id": "elem_other",
            }
        )
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REORDER,
            payload={"new_ordinal": 1},
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid

    def test_missing_new_ordinal_rejected(self):
        obl = _base_obligation(card_position={"kind": "ABSOLUTE", "ordinal": 1})
        manifest = _base_manifest()
        patch = _patch(operation=PatchOperation.REORDER, payload={})
        result = validate_patch(patch, obl, manifest)
        assert not result.valid


class TestRegroup:
    def test_valid_regroup(self):
        obl = _base_obligation(card_type=CardType.SOLO)
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REGROUP,
            payload={"new_card_type": "SOLO"},
        )
        result = validate_patch(patch, obl, manifest)
        assert result.valid

    def test_wrong_card_type_rejected(self):
        obl = _base_obligation(card_type=CardType.SOLO)
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REGROUP,
            payload={"new_card_type": "SHARED"},
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid

    def test_null_card_type_rejected(self):
        obl = _base_obligation(card_type=None)
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REGROUP,
            payload={"new_card_type": "SOLO"},
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid


class TestReposition:
    def test_valid_reposition(self):
        obl = _base_obligation(credit_surface=CreditSurface.MAIN_TITLES)
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REPOSITION,
            payload={"new_surface": "MAIN_TITLES"},
        )
        result = validate_patch(patch, obl, manifest)
        assert result.valid

    def test_wrong_surface_rejected(self):
        obl = _base_obligation(credit_surface=CreditSurface.MAIN_TITLES)
        manifest = _base_manifest()
        patch = _patch(
            operation=PatchOperation.REPOSITION,
            payload={"new_surface": "END_CARDS"},
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid


class TestGuardRails:
    def test_nonactive_obligation_rejected(self):
        obl = _base_obligation(
            required_display_text="X",
            status=ObligationStatus.CANDIDATE,
        )
        manifest = _base_manifest()
        patch = _patch(payload={"new_text": "X"})
        result = validate_patch(patch, obl, manifest)
        assert not result.valid
        assert "ACTIVE" in result.rejection_reason

    def test_missing_target_element_rejected(self):
        obl = _base_obligation(required_display_text="X")
        manifest = _base_manifest()
        patch = _patch(
            target_rendered_element_id="nonexistent_elem",
            payload={"new_text": "X"},
        )
        result = validate_patch(patch, obl, manifest)
        assert not result.valid
        assert "nonexistent_elem" in result.rejection_reason
