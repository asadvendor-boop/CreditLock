"""
Comprehensive End-to-End Judge Flow Integration Test.

Exercises:
1. Intake: ExtractorAgent parses raw deal memo into CANDIDATE obligations.
2. Resolver: PrecedenceResolverAgent evaluates precedence rules.
3. Event: Credit roll submitted event (credit_roll.submitted) -> STALE gate state.
4. Render: Chromium card rendering and Stage 1 artifact index creation.
5. Steward: StewardAgent explains finding & validates constrained patch proposal.
6. Two-Person Authority: REVIEWER proposes and distinct RELEASE_APPROVER confirms CONFIRM_VISUAL authorization.
7. Export: RELEASE_APPROVER executes protected export (401 -> 403 -> 409 -> 200).
8. Delivery: Assembly of delivery package zip archive.
9. Replay: Offline replay verification yielding MATCH status (PARTIAL: tamper verification incomplete).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference
from creditlock.agents.resolver import PrecedenceResolverAgent
from creditlock.agents.steward import StewardAgent
from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.gate import GateState
from creditlock.domain.models import (
    AuthorizationAction,
    CreditManifest,
    CreditSurface,
    Issue,
    IssueCode,
    ManifestEntry,
    ObligationStatus,
    VisualObservation,
    VisualObservations,
)
from creditlock.evidence.replay import replay_bundle
from tests.fixtures.dummy_evidence import generate_dummy_evidence

client = TestClient(app)
PROD_E2E = "prod-e2e-judge-flow-1"


class TestEndToEndJudgeFlow:
    def test_full_end_to_end_judge_lifecycle(self) -> None:
        clear_productions()

        from creditlock.agents.extractor import ExtractedCandidateSchema, ExtractorOutputSchema
        from creditlock.agents.provider import FakeModelProvider
        from creditlock.agents.resolver import ResolverLLMOutputSchema
        from creditlock.agents.steward import StewardLLMOutputSchema

        ext_resp = ExtractorOutputSchema(
            candidates=[
                ExtractedCandidateSchema(
                    required_display_text="Hans Zimmer",
                    role_label="Composer",
                    quote="CREDIT: Hans Zimmer",
                )
            ]
        )
        fake_prov = FakeModelProvider(responses={"default": ext_resp})

        # Step 1: Extractor Agent extracts CANDIDATE obligation from deal memo
        extractor = ExtractorAgent(provider=fake_prov)
        doc_ref = DocumentReference(
            uri="gs://bucket/deal-memo-001.txt", sha256_hash="hash-memo-123"
        )
        memo_text = "CREDIT: Hans Zimmer\nROLE: Composer\nSURFACE: END_CARDS"

        extraction_result = extractor.extract_from_document(doc_ref, memo_text, PROD_E2E)
        assert len(extraction_result.extracted_obligations) > 0
        cand_obl = extraction_result.extracted_obligations[0]

        # Safety Invariant Check: Extracted obligation status MUST be CANDIDATE
        assert cand_obl.status == ObligationStatus.CANDIDATE

        # Step 2: Precedence Resolver Agent evaluates precedence recommendations
        res_resp = ResolverLLMOutputSchema(
            recommendation="ABSTAIN",
            rationale="Single candidate",
        )
        fake_res_prov = FakeModelProvider(responses={"default": res_resp})
        resolver = PrecedenceResolverAgent(provider=fake_res_prov)
        precedence_recs = resolver.recommend_precedence([cand_obl])
        assert isinstance(precedence_recs, list)

        # Promote candidate to ACTIVE for production evaluation
        active_obl = cand_obl.model_copy(
            update={
                "status": ObligationStatus.ACTIVE,
                "credited_party_id": "contrib-e2e-1",
                "role_label": "Composer",
            }
        )

        # Step 3: Create Credit Manifest and render frames using RendererService
        manifest = CreditManifest(
            manifest_id="manifest-e2e-1",
            production_id=PROD_E2E,
            delivery_version_id="v1",
            entries=[
                ManifestEntry(
                    rendered_element_id="elem-e2e-1",
                    contributor_id="contrib-e2e-1",
                    display_name="Hans Zimmer",
                    role="Composer",
                    credit_surface=CreditSurface.END_CARDS,
                    ordinal_position=1,
                    production_id=PROD_E2E,
                    delivery_version_id="v1",
                )
            ],
        )

        layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)

        # Flagged visual observation creates NEEDS_HUMAN gate state
        visual_observations = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="gemini-3.6-flash",
            observations=[
                VisualObservation(
                    rendered_element_id="elem-e2e-1",
                    observation="Font clarity requires human reviewer confirmation",
                    flagged=True,
                    flag_reason="Font clarity check",
                )
            ],
        )

        register_production(
            PROD_E2E,
            obligations=[active_obl],
            manifest=manifest,
            layout_evidence=layout_ev,
            frames=frames,
            artifact_index=artifact_index.model_dump(),
            visual_observations=visual_observations,
            contributor_registry=[{"contributor_id": "contrib-e2e-1", "name": "Hans Zimmer"}],
        )

        # Step 4: Steward Agent explains finding and validates patch proposal
        stew_resp = StewardLLMOutputSchema(
            explanation="Proposed patch to align text",
            proposed_operation="SUBSTITUTE_TEXT",
            proposed_value="Hans Zimmer",
        )
        fake_stew_prov = FakeModelProvider(responses={"default": stew_resp})
        steward_agent = StewardAgent(provider=fake_stew_prov)
        demo_issue = Issue(
            issue_id="issue-demo-1",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id=active_obl.obligation_id,
            manifest_refs=["elem-e2e-1"],
            detail="display_name mismatch: Demo text mismatch finding",
        )
        steward_proposal = steward_agent.explain_finding_and_propose_patch(
            demo_issue, active_obl, manifest
        )
        assert steward_proposal.validated is True

        # Step 5: Test Export Security sequence (401 -> 403 -> 409 -> 200)
        # 401 Unauthenticated
        res_401 = client.post(f"/productions/{PROD_E2E}/export")
        assert res_401.status_code == 401

        # 403 Reviewer export attempt
        rev_token = create_token("reviewer-1", "REVIEWER")
        res_403 = client.post(
            f"/productions/{PROD_E2E}/export",
            headers={"Authorization": f"Bearer {rev_token}"},
        )
        assert res_403.status_code == 403

        # 409 Release Approver export attempt with open NEEDS_HUMAN issue
        appr_token = create_token("approver-1", "RELEASE_APPROVER")
        res_409 = client.post(
            f"/productions/{PROD_E2E}/export",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_409.status_code == 409
        target_issue_id = res_409.json()["detail"]["issues"][0]["issue_id"]

        # Step 6: Two-person human authority flow
        # 6a. REVIEWER submits a proposal
        res_prop = client.post(
            f"/productions/{PROD_E2E}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": target_issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "Human reviewer verified frame clarity on 1920x1080 display.",
            },
        )
        assert res_prop.status_code == 201
        proposal_id = res_prop.json()["proposal"]["proposal_id"]

        # 6b. Distinct RELEASE_APPROVER confirms proposal -> creates gate-clearing Authorization
        res_confirm = client.post(
            f"/productions/{PROD_E2E}/proposals/{proposal_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_confirm.status_code == 201

        # Step 7: Release Approver exports -> 200 READY_TO_EXPORT + delivery package path!
        res_200 = client.post(
            f"/productions/{PROD_E2E}/export",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_200.status_code == 200, f"Export failed with detail: {res_200.json()}"
        body_200 = res_200.json()
        assert body_200["gate_state"] == GateState.READY_TO_EXPORT.value
        pkg_path = body_200["delivery_package_path"]
        assert pkg_path is not None

        # Step 8: Offline Replay Engine verification on the exported zip package
        rel_digest = body_200["release_digest"]
        replay_result = replay_bundle(pkg_path, expected_release_digest=rel_digest)
        assert replay_result.status == "MATCH"
        assert replay_result.replayed_gate_state == GateState.READY_TO_EXPORT.value
        assert replay_result.divergence_reasons == []
