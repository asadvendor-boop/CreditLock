"""
Adversarial test suite enforcing Phase 1 human authority model and JWT configuration security.
"""

from __future__ import annotations

import jwt
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.gate import GateState
from creditlock.domain.models import (
    AbsolutePosition,
    AuthorizationAction,
    CardPositionKind,
    CardType,
    CreditManifest,
    CreditSurface,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    SourceSpan,
    VisualObservation,
    VisualObservations,
)

client = TestClient(app)
PROD_ID = "prod-red-authority-1"


def _seed_blocked_production() -> tuple[str, str]:
    clear_productions()
    obl = Obligation(
        obligation_id="obl-red-1",
        production_id=PROD_ID,
        credited_party_id="contrib-1",
        required_display_text="Hans Zimmer",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=10, quote="Hans Zimmer"),
        source_hash="hash-1",
        agent_reported_confidence=0.0,
        extraction_model_id="heuristic-v0",
        prompt_version="1.0",
        status=ObligationStatus.ACTIVE,
    )
    manifest = CreditManifest(
        manifest_id="manifest-red-1",
        production_id=PROD_ID,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
                display_name="Hans Zimmer",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=PROD_ID,
                delivery_version_id="v1",
            )
        ],
    )
    from tests.fixtures.dummy_evidence import generate_dummy_evidence

    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)

    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="heuristic-v0",
        observations=[
            VisualObservation(
                rendered_element_id="elem-1",
                observation="Font clarity check",
                flagged=True,
                flag_reason="Font clarity check",
            )
        ],
    )
    register_production(
        PROD_ID,
        obligations=[obl],
        manifest=manifest,
        layout_evidence=layout_ev,
        frames=frames,
        artifact_index=artifact_index.model_dump(),
        visual_observations=visuals,
        contributor_registry=[{"contributor_id": "contrib-1", "name": "Hans Zimmer"}],
    )

    # Get issue_id by triggering 409 export as RELEASE_APPROVER
    appr_token = create_token("approver-1", "RELEASE_APPROVER")
    res_409 = client.post(
        f"/productions/{PROD_ID}/export",
        headers={"Authorization": f"Bearer {appr_token}"},
    )
    assert res_409.status_code == 409
    issue_id = res_409.json()["detail"]["issues"][0]["issue_id"]
    return issue_id, appr_token


class TestHumanAuthorityDefectsRed:
    def test_old_default_secret_token_rejected(self) -> None:
        _seed_blocked_production()
        old_secret = "dev-secret-change-in-production-xx"
        forged_token = jwt.encode(
            {"sub": "forged-approver", "role": "RELEASE_APPROVER"}, old_secret, algorithm="HS256"
        )

        # SECURE REQUIREMENT: Token signed with old repository default secret MUST be rejected (401)
        res = client.post(
            f"/productions/{PROD_ID}/export",
            headers={"Authorization": f"Bearer {forged_token}"},
        )
        assert res.status_code == 401, f"Expected 401 for old default secret, got {res.status_code}"

    def test_direct_authorization_endpoint_retired(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("reviewer-1", "REVIEWER")

        # SECURE REQUIREMENT: Direct authorization creation is retired (410 Gone or 405 Method Not Allowed)
        res = client.post(
            f"/productions/{PROD_ID}/authorizations",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "Verified on 1920x1080 display",
            },
        )
        assert res.status_code in (405, 410), (
            f"Expected 405/410 for direct authorization creation, got {res.status_code}"
        )

    def test_reviewer_proposal_alone_leaves_gate_blocked(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("reviewer-1", "REVIEWER")

        # REVIEWER submits a proposal
        res_prop = client.post(
            f"/productions/{PROD_ID}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "Verified font legibility on frame #1",
            },
        )
        assert res_prop.status_code == 201
        prop = res_prop.json()["proposal"]
        assert prop["status"] == "PENDING"

        # SECURE REQUIREMENT: RELEASE_APPROVER attempting export with ONLY a pending proposal MUST get 409 Conflict
        appr_token = create_token("approver-1", "RELEASE_APPROVER")
        res_export = client.post(
            f"/productions/{PROD_ID}/export",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_export.status_code == 409
        assert res_export.json()["detail"]["gate_state"] == GateState.NEEDS_HUMAN.value

    def test_distinct_release_approver_can_confirm_proposal(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr_token = create_token("approver-1", "RELEASE_APPROVER")

        # 1. REVIEWER submits proposal
        res_prop = client.post(
            f"/productions/{PROD_ID}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "Verified font legibility on frame #1",
            },
        )
        assert res_prop.status_code == 201
        proposal_id = res_prop.json()["proposal"]["proposal_id"]

        # 2. Distinct RELEASE_APPROVER confirms proposal
        res_confirm = client.post(
            f"/productions/{PROD_ID}/proposals/{proposal_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_confirm.status_code == 201
        assert res_confirm.json()["authorization"]["role"] == "RELEASE_APPROVER"
        assert res_confirm.json()["authorization"]["actor_id"] == "approver-1"

        # 3. Export succeeds (200 OK)
        res_export = client.post(
            f"/productions/{PROD_ID}/export",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_export.status_code == 200
        assert res_export.json()["gate_state"] == GateState.READY_TO_EXPORT.value

    def test_same_person_confirmation_fails(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("actor-same", "REVIEWER")
        appr_token = create_token("actor-same", "RELEASE_APPROVER")

        # Create proposal as REVIEWER
        res_prop = client.post(
            f"/productions/{PROD_ID}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "Verified font legibility on frame #1",
            },
        )
        assert res_prop.status_code == 201
        proposal_id = res_prop.json()["proposal"]["proposal_id"]

        # Same actor attempts to confirm own proposal -> MUST fail (403)
        res_confirm = client.post(
            f"/productions/{PROD_ID}/proposals/{proposal_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_confirm.status_code == 403

    def test_unsupported_waiver_and_precedence_actions_fail_closed(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("reviewer-1", "REVIEWER")

        # WAIVE_OBLIGATION must fail closed
        res_waive = client.post(
            f"/productions/{PROD_ID}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.WAIVE_OBLIGATION.value,
                "reason": "Attempting waiver without scoped workflow",
            },
        )
        assert res_waive.status_code in (400, 409, 422)

    def test_whitespace_only_reason_fails(self) -> None:
        issue_id, _ = _seed_blocked_production()
        rev_token = create_token("reviewer-1", "REVIEWER")

        res = client.post(
            f"/productions/{PROD_ID}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": AuthorizationAction.CONFIRM_VISUAL.value,
                "reason": "   \n\t   ",
            },
        )
        assert res.status_code in (400, 422)
