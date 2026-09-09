"""
RED Unit Tests for Commit F: Real Agent Quality Repairs.
"""

from __future__ import annotations

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference
from creditlock.agents.provider import FakeModelProvider
from creditlock.agents.steward import StewardAgent
from creditlock.domain.models import (
    AbsolutePosition,
    CardPositionKind,
    CardType,
    CreditManifest,
    CreditSurface,
    Issue,
    IssueCode,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    PatchOperation,
    SourceSpan,
)


def test_red_extractor_conjunctive_clause_creates_two_obligations() -> None:
    text = "DEAL MEMO\nExecutive Producer Credit: Produced by Jane Doe and John Smith."
    # len("DEAL MEMO\nExecutive Producer Credit: ") == 37
    # len("Produced by Jane Doe and John Smith.") == 36 -> (37, 73)
    doc_ref = DocumentReference(uri="gs://test/conjunctive.txt", sha256_hash="h1")

    fake_ext = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Jane Doe",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Produced by Jane Doe and John Smith.",
                        "start_char": 37,
                        "end_char": 73,
                    },
                    {
                        "required_display_text": "John Smith",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Produced by Jane Doe and John Smith.",
                        "start_char": 37,
                        "end_char": 73,
                    },
                ]
            }
        },
    )

    agent = ExtractorAgent(provider=fake_ext)
    res = agent.extract_from_document(doc_ref, text, "prod-1")

    assert len(res.extracted_obligations) == 2
    names = {o.required_display_text for o in res.extracted_obligations}
    assert names == {"Jane Doe", "John Smith"}
    for o in res.extracted_obligations:
        assert o.source_span is not None
        assert o.source_span.quote == "Produced by Jane Doe and John Smith."


def test_red_steward_position_mismatch_reorder() -> None:
    issue = Issue(
        issue_id="issue-pos-1",
        code=IssueCode.ARTIFACT_POSITION_MISMATCH,
        manifest_refs=["elem-1"],
        obligation_id="obl-pos-1",
        detail="Manifest ordinal 2 does not match controlling ordinal 1.",
    )
    obligation = Obligation(
        obligation_id="obl-pos-1",
        production_id="prod-1",
        status=ObligationStatus.ACTIVE,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        required_display_text="Hans Zimmer",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="memo.txt",
        source_document_version=1,
        source_hash="h1",
        agent_reported_confidence=0.9,
        source_span=SourceSpan(quote="Hans Zimmer", start_char=0, end_char=11),
    )
    manifest = CreditManifest(
        manifest_id="man-pos-1",
        production_id="prod-1",
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="c1",
                display_name="Hans Zimmer",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=2,
                production_id="prod-1",
                delivery_version_id="v1",
            )
        ],
    )

    fake_stw = FakeModelProvider(
        agent_role="steward",
        responses={
            "default": {
                "explanation": "Ordinal 2 in manifest does not match controlling ordinal 1; reorder to 1.",
                "proposed_operation": "REORDER",
                "proposed_value": "1",
                "derivable": True,
            }
        },
    )

    agent = StewardAgent(provider=fake_stw)
    proposal = agent.explain_finding_and_propose_patch(issue, obligation, manifest)

    assert proposal.patch_proposal is not None
    assert proposal.patch_proposal.operation == PatchOperation.REORDER
    assert proposal.patch_proposal.payload.get("new_ordinal") == 1
    assert proposal.validated is True
