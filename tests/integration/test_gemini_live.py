"""
Credential-gated live integration tests for Gemini API via google.genai SDK.

Only runs when CREDITLOCK_RUN_LIVE_GEMINI=1 environment variable is set.
"""

from __future__ import annotations

import os

import pytest

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference
from creditlock.agents.resolver import PrecedenceResolverAgent
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
    SourceSpan,
)

HAS_LIVE_GATE = os.getenv("CREDITLOCK_RUN_LIVE_GEMINI") == "1"


@pytest.mark.skipif(
    not HAS_LIVE_GATE,
    reason="Live Gemini tests require CREDITLOCK_RUN_LIVE_GEMINI=1",
)
class TestGeminiLiveIntegration:
    def test_live_extractor_agent_call(self) -> None:
        agent = ExtractorAgent()
        doc_ref = DocumentReference(uri="gs://test-bucket/memo_live.txt", sha256_hash="abc123live")
        text = "CREDIT MEMORANDUM\nCREDIT: Hans Zimmer\nROLE: Composer\nSURFACE: END_CARDS\nQUOTE: CREDIT: Hans Zimmer"

        result = agent.extract_from_document(doc_ref, text, "prod-live-1")

        assert result.document_ref.uri == "gs://test-bucket/memo_live.txt"
        prov = result.provenance
        print(f"\n--- EXTRACTOR PROVENANCE ---\n{prov.model_dump_json(indent=2)}")

        assert prov.platform == "gemini_enterprise_agent_platform"
        assert prov.auth_mode == "adc"
        assert prov.agent_role == "extractor"
        assert prov.primary_model == "gemini-3.6-flash"
        assert prov.configured_fallback_model == "gemini-3.5-flash-lite"
        assert prov.actual_model_used in ("gemini-3.6-flash", "gemini-3.5-flash-lite")
        assert len(prov.attempts) > 0
        assert isinstance(prov.fallback_occurred, bool)

    def test_live_resolver_agent_call(self) -> None:
        agent = PrecedenceResolverAgent()
        obl1 = Obligation(
            obligation_id="obl-cand-1",
            production_id="prod-live-1",
            status=ObligationStatus.CANDIDATE,
            extraction_model_id="gemini-3.6-flash",
            required_display_text="Hans Zimmer",
            role_label="Composer",
            credit_surface=CreditSurface.END_CARDS,
            card_type=CardType.SOLO,
            source_document_id="deal_memo_v1.txt",
            source_document_version=1,
            source_hash="hash1",
            agent_reported_confidence=0.9,
            prompt_version="v1",
            source_span=SourceSpan(quote="Hans Zimmer", start_char=0, end_char=11),
        )
        obl2 = Obligation(
            obligation_id="obl-cand-2",
            production_id="prod-live-1",
            status=ObligationStatus.CANDIDATE,
            extraction_model_id="gemini-3.6-flash",
            required_display_text="Hans Zimmer",
            role_label="Composer",
            credit_surface=CreditSurface.END_CARDS,
            card_type=CardType.SOLO,
            source_document_id="amendment_v2.txt",
            source_document_version=2,
            source_hash="hash2",
            agent_reported_confidence=0.95,
            prompt_version="v1",
            source_span=SourceSpan(
                quote="This amendment supersedes deal_memo_v1 for Hans Zimmer", start_char=0, end_char=54
            ),
        )

        recs = agent.recommend_precedence([obl1, obl2])
        assert len(recs) == 1
        rec = recs[0]
        assert rec.provenance is not None
        prov = rec.provenance
        print(f"\n--- RESOLVER PROVENANCE ---\n{prov.model_dump_json(indent=2)}")

        assert prov.platform == "gemini_enterprise_agent_platform"
        assert prov.auth_mode == "adc"
        assert prov.agent_role == "resolver"
        assert prov.primary_model == "gemini-3.1-pro-preview"
        assert prov.configured_fallback_model == "gemini-3.6-flash"
        assert prov.actual_model_used in ("gemini-3.1-pro-preview", "gemini-3.6-flash")
        assert len(prov.attempts) > 0
        assert isinstance(prov.fallback_occurred, bool)

    def test_live_steward_agent_call(self) -> None:
        agent = StewardAgent()
        obl = Obligation(
            obligation_id="obl-act-1",
            production_id="prod-live-1",
            status=ObligationStatus.ACTIVE,
            extraction_model_id="gemini-3.6-flash",
            required_display_text="Hans Zimmer",
            role_label="Composer",
            credit_surface=CreditSurface.END_CARDS,
            card_type=CardType.SOLO,
            card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
            source_document_id="memo.txt",
            source_document_version=1,
            source_hash="hash1",
            agent_reported_confidence=0.95,
            prompt_version="v1",
            source_span=SourceSpan(quote="Hans Zimmer", start_char=0, end_char=11),
        )
        entry = ManifestEntry(
            rendered_element_id="elem-1",
            contributor_id="contrib-1",
            display_name="H. Zimmer",
            role="Composer",
            credit_surface=CreditSurface.END_CARDS,
            ordinal_position=1,
            production_id="prod-live-1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="man-live-1",
            production_id="prod-live-1",
            delivery_version_id="v1",
            entries=[entry],
        )
        issue = Issue(
            issue_id="issue-1",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            manifest_refs=["elem-1"],
            obligation_id="obl-act-1",
            detail="Manifest text 'H. Zimmer' does not match controlling obligation 'Hans Zimmer'.",
        )

        proposal = agent.explain_finding_and_propose_patch(issue, obl, manifest)
        assert proposal.provenance is not None
        prov = proposal.provenance
        print(f"\n--- STEWARD PROVENANCE ---\n{prov.model_dump_json(indent=2)}")

        assert prov.platform == "gemini_enterprise_agent_platform"
        assert prov.auth_mode == "adc"
        assert prov.agent_role == "steward"
        assert prov.primary_model == "gemini-3.5-flash-lite"
        assert prov.configured_fallback_model == "gemini-3.6-flash"
        assert prov.actual_model_used in ("gemini-3.5-flash-lite", "gemini-3.6-flash")
        assert len(prov.attempts) > 0
        assert isinstance(prov.fallback_occurred, bool)
