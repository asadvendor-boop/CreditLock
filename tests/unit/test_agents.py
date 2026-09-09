"""
Unit and safety invariant tests for the 3 bounded Gemini/ADK agents using FakeModelProvider.
"""

from __future__ import annotations

import pytest

from creditlock.agents.extractor import (
    ExtractedCandidateSchema,
    ExtractorAgent,
    ExtractorOutputSchema,
)
from creditlock.agents.models import DocumentReference
from creditlock.agents.provider import FakeModelProvider, ModelInvocationUnavailableError
from creditlock.agents.resolver import PrecedenceResolverAgent, ResolverLLMOutputSchema
from creditlock.agents.steward import StewardAgent, StewardLLMOutputSchema
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
    SourceSpan,
)


def _make_manifest() -> CreditManifest:
    return CreditManifest(
        manifest_id="manifest-ag-1",
        production_id="prod-ag-1",
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
                display_name="Hans Zimmer",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id="prod-ag-1",
                delivery_version_id="v1",
            )
        ],
    )


def _make_active_obligation() -> Obligation:
    return Obligation(
        obligation_id="obl-active-1",
        production_id="prod-ag-1",
        credited_party_id="contrib-1",
        required_display_text="Hans Zimmer",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=18, quote="CREDIT: Hans Zimmer"),
        source_hash="hash-1",
        agent_reported_confidence=0.98,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        status=ObligationStatus.ACTIVE,
    )


class TestAgentInvariants:
    def test_provider_unavailable_raises_explicit_error(self) -> None:
        fake_prov = FakeModelProvider(available=False)
        agent = ExtractorAgent(provider=fake_prov)
        ref = DocumentReference(uri="gs://bucket/memo.txt", sha256_hash="abc123hash")

        with pytest.raises(ModelInvocationUnavailableError, match="unavailable"):
            agent.extract_from_document(ref, "CREDIT: Hans Zimmer", "prod-1")

    def test_extractor_emits_candidate_status_and_provider_provenance(self) -> None:
        fake_resp = ExtractorOutputSchema(
            candidates=[
                ExtractedCandidateSchema(
                    required_display_text="Hans Zimmer",
                    role_label="Composer",
                    quote="CREDIT: Hans Zimmer",
                    confidence=0.95,
                )
            ]
        )
        fake_prov = FakeModelProvider(primary_model_id="gemini-3.6-flash", responses={"default": fake_resp})
        agent = ExtractorAgent(provider=fake_prov)
        ref = DocumentReference(uri="gs://bucket/memo.txt", sha256_hash="abc123hash")
        text = "CREDIT: Hans Zimmer\nROLE: Composer"

        res = agent.extract_from_document(ref, text, "prod-ag-1")

        assert res.document_ref.uri == "gs://bucket/memo.txt"
        assert len(res.extracted_obligations) == 1
        obl = res.extracted_obligations[0]

        # Safety Invariant 1: ALL extracted obligations MUST have CANDIDATE status
        assert obl.status == ObligationStatus.CANDIDATE
        assert obl.extraction_model_id == "gemini-3.6-flash"
        assert obl.agent_reported_confidence == 0.95
        assert obl.required_display_text == "Hans Zimmer"

    def test_resolver_dates_alone_do_not_prove_supersession(self) -> None:
        fake_resp = ResolverLLMOutputSchema(
            recommendation="ABSTAIN",
            rationale="Dates or timestamps alone do not prove supersession.",
        )
        fake_prov = FakeModelProvider(primary_model_id="gemini-3.6-flash", responses={"default": fake_resp})
        agent = PrecedenceResolverAgent(provider=fake_prov)

        obl1 = _make_active_obligation()
        obl2 = _make_active_obligation()

        recs = agent.recommend_precedence([obl1, obl2])

        assert len(recs) == 1
        assert recs[0].recommendation == "ABSTAIN"
        assert "do not prove supersession" in recs[0].rationale

    def test_resolver_explicit_amendment_version_resolves(self) -> None:
        fake_resp = ResolverLLMOutputSchema(
            recommendation="RESOLVE",
            controlling_obligation_id="obl-active-2",
            rationale="Explicit document version 2 controls.",
        )
        fake_prov = FakeModelProvider(primary_model_id="gemini-3.6-flash", responses={"default": fake_resp})
        agent = PrecedenceResolverAgent(provider=fake_prov)

        obl1 = _make_active_obligation()
        obl2_dict = obl1.model_dump()
        obl2_dict["obligation_id"] = "obl-active-2"
        obl2_dict["source_document_version"] = 2
        obl2_dict["source_span"] = SourceSpan(
            start_char=0, end_char=31, quote="CREDIT: Hans Zimmer Amendment v2"
        )
        obl2 = Obligation.model_validate(obl2_dict)

        recs = agent.recommend_precedence([obl1, obl2])
        assert len(recs) == 1
        assert recs[0].recommendation == "RESOLVE"
        assert recs[0].controlling_obligation_id == "obl-active-2"

    def test_steward_constrained_tool_validation(self) -> None:
        fake_resp = StewardLLMOutputSchema(
            explanation="Proposed patch to align manifest text with controlling obligation.",
            proposed_operation="SUBSTITUTE_TEXT",
            proposed_value="Hans Zimmer",
        )
        fake_prov = FakeModelProvider(primary_model_id="gemini-3.6-flash", responses={"default": fake_resp})
        agent = StewardAgent(provider=fake_prov)

        manifest = _make_manifest()
        obl = _make_active_obligation()

        issue = Issue(
            issue_id="issue-text-1",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id=obl.obligation_id,
            manifest_refs=["elem-1"],
            detail="display_name mismatch: Manifest display_name mismatch",
        )

        prop = agent.explain_finding_and_propose_patch(issue, obl, manifest)

        assert prop.validated is True
        assert prop.patch_proposal is not None
        assert prop.patch_proposal.payload.get("new_text") == "Hans Zimmer"

    def test_steward_abstains_on_non_active_obligation(self) -> None:
        fake_prov = FakeModelProvider(primary_model_id="gemini-3.6-flash")
        agent = StewardAgent(provider=fake_prov)

        manifest = _make_manifest()
        obl_candidate = _make_active_obligation().model_copy(
            update={"status": ObligationStatus.CANDIDATE}
        )

        issue = Issue(
            issue_id="issue-text-2",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id=obl_candidate.obligation_id,
            manifest_refs=["elem-1"],
            detail="Candidate obligation mismatch",
        )

        prop = agent.explain_finding_and_propose_patch(issue, obl_candidate, manifest)

        # Invariant: Steward abstains (no patch proposal) when obligation is not ACTIVE
        assert prop.patch_proposal is None
        assert prop.validated is False
        assert "not active" in prop.explanation.lower()
