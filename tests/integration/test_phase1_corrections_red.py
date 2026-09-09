"""
Comprehensive regression test suite for Phase 1 human authority and packaging repairs.
"""

from __future__ import annotations

import os

import jwt
import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.gate import GateState
from creditlock.domain.models import (
    AbsolutePosition,
    Authorization,
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
from creditlock.domain.store import InMemoryProductionStore
from creditlock.settings import Settings, get_settings
from tests.fixtures.dummy_evidence import generate_dummy_evidence

client = TestClient(app)
PROD_A = "prod-reg-a"
PROD_B = "prod-reg-b"


def _seed(prod_id: str) -> str:
    obl = Obligation(
        obligation_id=f"obl-{prod_id}",
        production_id=prod_id,
        credited_party_id=f"contrib-{prod_id}",
        required_display_text="John Williams",
        role_label="Composer",
        credit_surface=CreditSurface.END_CARDS,
        card_type=CardType.SOLO,
        card_position=AbsolutePosition(kind=CardPositionKind.ABSOLUTE, ordinal=1),
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(start_char=0, end_char=10, quote="John Williams"),
        source_hash="hash-1",
        agent_reported_confidence=0.0,
        extraction_model_id="heuristic-v0",
        prompt_version="1.0",
        status=ObligationStatus.ACTIVE,
    )
    manifest = CreditManifest(
        manifest_id=f"manifest-{prod_id}",
        production_id=prod_id,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id=f"elem-{prod_id}",
                contributor_id=f"contrib-{prod_id}",
                display_name="John Williams",
                role="Composer",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=prod_id,
                delivery_version_id="v1",
            )
        ],
    )
    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)
    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="heuristic-v0",
        observations=[
            VisualObservation(
                rendered_element_id=f"elem-{prod_id}",
                observation="Blurry font",
                flagged=True,
                flag_reason="Blurry font",
            )
        ],
    )
    register_production(
        prod_id,
        obligations=[obl],
        manifest=manifest,
        layout_evidence=layout_ev,
        frames=frames,
        artifact_index=artifact_index.model_dump(),
        visual_observations=visuals,
    )
    appr_token = create_token("appr-init", "RELEASE_APPROVER")
    res = client.post(
        f"/productions/{prod_id}/export", headers={"Authorization": f"Bearer {appr_token}"}
    )
    assert res.status_code == 409
    return res.json()["detail"]["issues"][0]["issue_id"]


class TestPhase1AuthorityRegressions:
    def test_1_old_default_secret_jwt_rejected(self) -> None:
        clear_productions()
        _seed(PROD_A)
        old_secret = "dev-secret-change-in-production-xx"
        forged_token = jwt.encode(
            {"sub": "forged", "role": "RELEASE_APPROVER"}, old_secret, algorithm="HS256"
        )
        res = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {forged_token}"}
        )
        assert res.status_code == 401

    def test_2_missing_jwt_secret_fails_startup(self) -> None:
        old_val = os.environ.get("JWT_SECRET")
        try:
            os.environ["JWT_SECRET"] = ""
            get_settings.cache_clear()
            with pytest.raises(ValueError, match="JWT_SECRET is missing"):
                Settings()
        finally:
            if old_val:
                os.environ["JWT_SECRET"] = old_val
            get_settings.cache_clear()

    def test_3_weak_jwt_secret_fails_startup(self) -> None:
        old_val = os.environ.get("JWT_SECRET")
        try:
            os.environ["JWT_SECRET"] = "short-secret"
            get_settings.cache_clear()
            with pytest.raises(ValueError, match="JWT_SECRET is missing, weak"):
                Settings()
        finally:
            if old_val:
                os.environ["JWT_SECRET"] = old_val
            get_settings.cache_clear()

    def test_4_missing_or_invalid_sub_and_role_claims_fail(self) -> None:
        clear_productions()
        _seed(PROD_A)
        # Token missing sub
        t_no_sub = jwt.encode({"role": "REVIEWER"}, os.environ["JWT_SECRET"], algorithm="HS256")
        res1 = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {t_no_sub}"},
            json={"issue_id": "i", "action": "CONFIRM_VISUAL", "reason": "r"},
        )
        assert res1.status_code in (401, 422)

        # Token invalid role
        t_bad_role = jwt.encode(
            {"sub": "actor-1", "role": "ADMIN_OVERRIDE"},
            os.environ["JWT_SECRET"],
            algorithm="HS256",
        )
        res2 = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {t_bad_role}"},
            json={"issue_id": "i", "action": "CONFIRM_VISUAL", "reason": "r"},
        )
        assert res2.status_code == 403

    def test_5_reviewer_can_propose(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        res = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": "CONFIRM_VISUAL",
                "reason": "Verified on 1080p display",
            },
        )
        assert res.status_code == 201
        prop = res.json()["proposal"]
        assert prop["proposer_role"] == "REVIEWER"
        assert prop["status"] == "PENDING"

    def test_6_release_approver_cannot_propose(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        appr_token = create_token("approver-1", "RELEASE_APPROVER")
        res = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {appr_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Approver proposing"},
        )
        assert res.status_code == 403

    def test_7_reviewer_cannot_confirm(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev1 = create_token("reviewer-1", "REVIEWER")
        rev2 = create_token("reviewer-2", "REVIEWER")
        res_prop = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev1}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Valid reason"},
        )
        prop_id = res_prop.json()["proposal"]["proposal_id"]

        res_conf = client.post(
            f"/productions/{PROD_A}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {rev2}"},
        )
        assert res_conf.status_code == 403

    def test_8_release_approver_cannot_authorize_directly(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        appr_token = create_token("approver-1", "RELEASE_APPROVER")
        res = client.post(
            f"/productions/{PROD_A}/authorizations",
            headers={"Authorization": f"Bearer {appr_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Direct attempt"},
        )
        assert res.status_code == 410, "Direct authorization creation is retired with 410"

    def test_9_proposal_alone_leaves_export_at_409(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr_token = create_token("approver-1", "RELEASE_APPROVER")

        client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Proposal only"},
        )
        res = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {appr_token}"}
        )
        assert res.status_code == 409
        assert res.json()["detail"]["gate_state"] == GateState.NEEDS_HUMAN.value

    def test_10_distinct_approver_confirmation_enables_200(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr_token = create_token("approver-1", "RELEASE_APPROVER")

        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified clarity"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]

        res_c = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_c.status_code == 201

        res_e = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {appr_token}"}
        )
        assert res_e.status_code == 200
        assert res_e.json()["gate_state"] == GateState.READY_TO_EXPORT.value

    def test_11_same_person_confirmation_fails(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        token = create_token("actor-same", "REVIEWER")
        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]

        # Same actor gets RELEASE_APPROVER token
        same_appr = create_token("actor-same", "RELEASE_APPROVER")
        res_c = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {same_appr}"},
        )
        assert res_c.status_code == 403

    def test_12_wrong_production_fails(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr_token = create_token("approver-1", "RELEASE_APPROVER")

        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]

        res_c = client.post(
            f"/productions/nonexistent-prod/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_c.status_code in (404, 409)

    def test_13_wrong_missing_closed_issue_fails(self) -> None:
        clear_productions()
        _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        res = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": "non-existent-issue-999",
                "action": "CONFIRM_VISUAL",
                "reason": "Verified",
            },
        )
        assert res.status_code == 400

    def test_14_wrong_action_fails(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)  # VISUAL_OBSERVATION_UNCERTAIN issue
        rev_token = create_token("reviewer-1", "REVIEWER")
        res = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={
                "issue_id": issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "c1",
                "reason": "Wrong action",
            },
        )
        assert res.status_code == 400

    def test_15_stale_hashes_fail(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr_token = create_token("approver-1", "RELEASE_APPROVER")

        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]

        # Mutate production manifest to change state hash
        from creditlock.api.export import _get_production

        prod = _get_production(PROD_A)
        prod["manifest"].delivery_version_id = "v2-mutated"

        res_c = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_c.status_code == 409

    def test_16_reused_proposal_fails(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)
        rev_token = create_token("reviewer-1", "REVIEWER")
        appr1_token = create_token("approver-1", "RELEASE_APPROVER")
        appr2_token = create_token("approver-2", "RELEASE_APPROVER")

        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]

        # 1st confirm succeeds
        res_c1 = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr1_token}"},
        )
        assert res_c1.status_code == 201

        # 2nd confirm reusing same proposal fails
        res_c2 = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr2_token}"},
        )
        assert res_c2.status_code == 409

    def test_18_production_a_authorization_never_appears_in_production_b(self) -> None:
        clear_productions()
        issue_a = _seed(PROD_A)
        _seed(PROD_B)

        store = InMemoryProductionStore()
        store.register(
            PROD_A,
            [],
            CreditManifest(
                manifest_id="mA", production_id=PROD_A, delivery_version_id="v1", entries=[]
            ),
        )
        store.register(
            PROD_B,
            [],
            CreditManifest(
                manifest_id="mB", production_id=PROD_B, delivery_version_id="v1", entries=[]
            ),
        )

        auth_a = Authorization(
            authorization_id="auth-a-1",
            production_id=PROD_A,
            proposal_id="prop-a-1",
            issue_id=issue_a,
            proposer_id="rev-1",
            actor_id="appr-1",
            role="RELEASE_APPROVER",
            action=AuthorizationAction.CONFIRM_VISUAL,
            reason="Confirmed A",
            manifest_hash="m",
            obligation_registry_version_hash="o",
            artifact_index_digest="a",
            visual_observations_hash="v",
            authorized_at="2026-08-02T12:00:00Z",
        )
        store._store[PROD_A]["authorizations"] = [auth_a]

        auths_b = store.get_authorizations(PROD_B)
        assert len(auths_b) == 0

    def test_20_export_ordering_401_403_409_200_intact(self) -> None:
        clear_productions()
        issue_id = _seed(PROD_A)

        # 401
        res_401 = client.post(f"/productions/{PROD_A}/export")
        assert res_401.status_code == 401

        # 403
        rev_token = create_token("reviewer-1", "REVIEWER")
        res_403 = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {rev_token}"}
        )
        assert res_403.status_code == 403

        # 409
        appr_token = create_token("approver-1", "RELEASE_APPROVER")
        res_409 = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {appr_token}"}
        )
        assert res_409.status_code == 409

        # 200 after reviewer proposal + distinct approver confirm
        res_p = client.post(
            f"/productions/{PROD_A}/proposals",
            headers={"Authorization": f"Bearer {rev_token}"},
            json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
        )
        p_id = res_p.json()["proposal"]["proposal_id"]
        res_c = client.post(
            f"/productions/{PROD_A}/proposals/{p_id}/confirm",
            headers={"Authorization": f"Bearer {appr_token}"},
        )
        assert res_c.status_code == 201

        res_200 = client.post(
            f"/productions/{PROD_A}/export", headers={"Authorization": f"Bearer {appr_token}"}
        )
        assert res_200.status_code == 200
