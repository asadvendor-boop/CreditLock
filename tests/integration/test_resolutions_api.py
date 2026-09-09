"""
Integration tests for human resolutions API and full 401 -> 403 -> 409 -> 200 gate authorization flow.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.models import (
    AbsolutePosition,
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
from tests.fixtures.dummy_evidence import generate_dummy_evidence

client = TestClient(app)

PROD_RES = "prod-resolutions-1"

def _setup_blocked_production() -> tuple[CreditManifest, Obligation]:
    clear_productions()
    manifest = CreditManifest(
        manifest_id="manifest-res-1",
        production_id=PROD_RES,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-res-1",
                contributor_id="contrib-res-1",
                display_name="John Williams",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=PROD_RES,
                delivery_version_id="v1",
            )
        ],
    )
    obligation = Obligation(
        obligation_id="obl-res-1",
        production_id=PROD_RES,
        credited_party_id="contrib-res-1",
        required_display_text="John Williams",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=10, quote="John Williams"),
        source_hash="hash-1",
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        status=ObligationStatus.ACTIVE,
    )
    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)

    # Flagged visual observation produces VISUAL_OBSERVATION_UNCERTAIN (NEEDS_HUMAN)
    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem-res-1",
                observation="Font size slightly blurry",
                flagged=True,
                flag_reason="Blurry font",
            )
        ],
    )

    register_production(
        PROD_RES,
        obligations=[obligation],
        manifest=manifest,
        layout_evidence=layout_ev,
        frames=frames,
        artifact_index=artifact_index.model_dump(),
        visual_observations=visuals,
    )
    return manifest, obligation


class TestResolutionsAPI:
    def test_full_authorization_flow_401_403_409_200(self) -> None:
        _manifest, _obl = _setup_blocked_production()

        # 1. Unauthenticated request -> 401
        res_401 = client.post(f"/productions/{PROD_RES}/export")
        assert res_401.status_code == 401

        # 2. REVIEWER role export attempt -> 403
        reviewer_token = create_token("rev-1", "REVIEWER")
        res_403 = client.post(
            f"/productions/{PROD_RES}/export",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        assert res_403.status_code == 403

        # 3. RELEASE_APPROVER with open issues -> 409
        approver_token = create_token("appr-1", "RELEASE_APPROVER")
        res_409 = client.post(
            f"/productions/{PROD_RES}/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert res_409.status_code == 409
        body_409 = res_409.json()["detail"]
        assert body_409["gate_state"] == "NEEDS_HUMAN"
        issues = body_409["issues"]
        assert len(issues) == 1
        target_issue_id = issues[0]["issue_id"]

        # 4. REVIEWER proposes resolution / confirms visual observation
        res_prop = client.post(
            f"/productions/{PROD_RES}/proposals",
            headers={"Authorization": f"Bearer {reviewer_token}"},
            json={
                "issue_id": target_issue_id,
                "action": "CONFIRM_VISUAL",
                "reason": "Visual inspection verified readable font size.",
            },
        )
        assert res_prop.status_code == 201
        prop_id = res_prop.json()["proposal"]["proposal_id"]

        # 4b. Distinct RELEASE_APPROVER confirms proposal -> creates gate-clearing Authorization
        res_confirm = client.post(
            f"/productions/{PROD_RES}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert res_confirm.status_code == 201

        # 5. RELEASE_APPROVER exports -> 200 READY_TO_EXPORT + package path!
        res_200 = client.post(
            f"/productions/{PROD_RES}/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert res_200.status_code == 200
        body_200 = res_200.json()
        assert body_200["gate_state"] == "READY_TO_EXPORT"
        assert body_200["delivery_package_path"] is not None
        assert body_200["delivery_package_path"].endswith(".zip")
