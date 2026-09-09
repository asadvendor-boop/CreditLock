"""
Phase 1 & Phase 2 Security, Authentication, Authorization, and Store Startup Tests.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from creditlock.api.app import app
from creditlock.api.export import (
    UninitializedProductionStore,
    get_production_store,
    set_production_store,
)
from creditlock.domain.models import (
    Authorization,
    AuthorizationAction,
    CreditManifest,
    Obligation,
    Proposal,
)
from creditlock.domain.store import FirestoreProductionStore, InMemoryProductionStore
from creditlock.settings import get_settings

client = TestClient(app)
PROD_ID = "prod-phase12-test"


def create_token(sub: str, role: str) -> str:
    secret = os.environ.get(
        "JWT_SECRET", "test-secret-must-be-at-least-32-bytes-long-for-security"
    )
    payload = {"sub": sub, "role": role}
    return jwt.encode(payload, secret, algorithm="HS256")


def _seed() -> str:
    from creditlock.api.export import clear_productions, register_production
    clear_productions()
    store = InMemoryProductionStore()
    set_production_store(store)
    from creditlock.domain.models import CreditSurface, ManifestEntry
    m = CreditManifest(
        manifest_id="m1",
        production_id=PROD_ID,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem1",
                contributor_id="p1",
                display_name="Alice",
                role="Director",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=PROD_ID,
                delivery_version_id="v1",
            )
        ],
    )
    o = Obligation(
        obligation_id="o1",
        production_id=PROD_ID,
        status="ACTIVE",
        credited_party_id="p1",
        required_display_text="Alice",
        role_label="Director",
        credit_surface="END_CARDS",
        card_type="SOLO",
        source_document_id="doc1",
        source_document_version=1,
        source_hash="h1",
        agent_reported_confidence=0.9,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        source_span={"quote": "Alice", "start_char": 0, "end_char": 5},
    )
    from creditlock.domain.models import VisualObservation, VisualObservations
    from tests.integration.test_phase1_corrections_red import generate_dummy_evidence
    layout_ev, frames, artifact_index = generate_dummy_evidence(m)
    vo = VisualObservations(
        manifest_id="m1",
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem1",
                observation="Unclear text",
                flagged=True,
                flag_reason="Needs human review",
            )
        ],
    )
    register_production(
        PROD_ID,
        obligations=[o],
        manifest=m,
        layout_evidence=layout_ev,
        frames=frames,
        artifact_index=artifact_index.model_dump(),
        visual_observations=vo,
    )

    from creditlock.api.export import _get_production
    from creditlock.domain.checker import CheckerInput, evaluate_findings

    prod_state = _get_production(PROD_ID)
    inp = CheckerInput(
        obligations=prod_state["obligations"],
        manifest=prod_state["manifest"],
        layout_evidence=prod_state.get("layout_evidence"),
        visual_observations=prod_state.get("visual_observations"),
        contributor_registry=prod_state.get("contributor_registry"),
    )
    findings = evaluate_findings(inp)
    prod_state["issues"] = findings
    return findings[0].issue_id


# ── Restored Pre-Phase-1A Tests ─────────────────────────────────────────────


def test_in_memory_demo_store_works_when_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "memory_demo")
    monkeypatch.setenv("ALLOW_IN_MEMORY_DEMO", "true")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.store_backend == "memory_demo"


def test_in_memory_demo_store_refused_when_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "memory_demo")
    monkeypatch.setenv("ALLOW_IN_MEMORY_DEMO", "false")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="ALLOW_IN_MEMORY_DEMO is False"):
        get_settings()


def test_missing_store_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="STORE_BACKEND is missing"):
        get_settings()


def test_unrecognized_store_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "redis_cluster")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="Unrecognized STORE_BACKEND"):
        get_settings()


def test_fastapi_lifespan_initializes_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "memory_demo")
    monkeypatch.setenv("ALLOW_IN_MEMORY_DEMO", "true")
    get_settings.cache_clear()
    with TestClient(app) as tc:
        res = tc.get("/health")
        assert res.status_code == 200


def test_export_endpoint_requires_auth_blank_or_missing_subject() -> None:
    res1 = client.post(f"/productions/{PROD_ID}/export")
    assert res1.status_code == 401

    secret = os.environ["JWT_SECRET"]
    token_no_sub = jwt.encode({"role": "RELEASE_APPROVER"}, secret, algorithm="HS256")
    res2 = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {token_no_sub}"}
    )
    assert res2.status_code == 401

    token_blank_sub = jwt.encode({"sub": "   ", "role": "RELEASE_APPROVER"}, secret, algorithm="HS256")
    res3 = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {token_blank_sub}"}
    )
    assert res3.status_code == 401


def test_unknown_role_on_export_returns_403() -> None:
    _seed()
    secret = os.environ["JWT_SECRET"]
    token_bad_role = jwt.encode(
        {"sub": "actor-1", "role": "ADMIN_OVERRIDE"}, secret, algorithm="HS256"
    )
    res = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {token_bad_role}"}
    )
    assert res.status_code == 403


def test_authorization_model_requires_audit_fields() -> None:
    with pytest.raises(ValidationError):
        Authorization(
            authorization_id="auth-test",
            issue_id="issue-test",
            actor_id="approver-1",
            role="RELEASE_APPROVER",
            action=AuthorizationAction.CONFIRM_VISUAL,
            reason="Verified",
            manifest_hash="m",
            obligation_registry_version_hash="o",
            artifact_index_digest="a",
            visual_observations_hash="v",
            authorized_at="2026-08-02T12:00:00Z",
        )


def test_proposal_model_accepts_reviewer_only() -> None:
    with pytest.raises(ValidationError):
        Proposal(
            proposal_id="prop-test",
            production_id=PROD_ID,
            issue_id="issue-test",
            proposer_id="approver-1",
            proposer_role="RELEASE_APPROVER",  # type: ignore[arg-type]
            action=AuthorizationAction.CONFIRM_VISUAL,
            reason="Verified",
            manifest_hash="m",
            obligation_registry_version_hash="o",
            artifact_index_digest="a",
            visual_observations_hash="v",
            created_at="2026-08-02T12:00:00Z",
        )


def test_concurrent_double_confirmation_exactly_one_succeeds() -> None:
    store = InMemoryProductionStore()
    m = CreditManifest(
        manifest_id="m", production_id="prod-conc", delivery_version_id="v1", entries=[]
    )
    store.register("prod-conc", [], m)

    prop = Proposal(
        proposal_id="prop-conc-1",
        production_id="prod-conc",
        issue_id="issue-1",
        proposer_id="rev-1",
        proposer_role="REVIEWER",
        action=AuthorizationAction.CONFIRM_VISUAL,
        reason="Verified",
        manifest_hash="m",
        obligation_registry_version_hash="o",
        artifact_index_digest="a",
        visual_observations_hash="v",
        created_at="2026-08-02T12:00:00Z",
        status="PENDING",
    )
    store.save_proposal(prop)

    hashes = ("m", "o", "a", "v")

    def _worker(approver_id: str) -> tuple[str, Any]:
        try:
            auth = store.confirm_proposal("prod-conc", "prop-conc-1", approver_id, hashes)
            return ("SUCCESS", auth)
        except Exception as e:  # noqa: BLE001
            return ("FAIL", e)

    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_a = executor.submit(_worker, "approver-a")
        fut_b = executor.submit(_worker, "approver-b")
        res_a = fut_a.result()
        res_b = fut_b.result()

    statuses = [res_a[0], res_b[0]]
    assert statuses.count("SUCCESS") == 1
    assert statuses.count("FAIL") == 1

    fail_res = res_a[1] if res_a[0] == "FAIL" else res_b[1]
    assert isinstance(fail_res, ValueError)
    assert "not found or is not pending" in str(fail_res)

    auths = store.get_authorizations("prod-conc")
    assert len(auths) == 1

    conf_logs = [
        log
        for log in store.audit_log
        if log.get("event") == "PROPOSAL_CONFIRMED" and log.get("proposal_id") == "prop-conc-1"
    ]
    assert len(conf_logs) == 1


def test_failed_persistence_leaves_proposal_pending_and_allows_retry() -> None:
    class FailingAuditList(list[dict[str, Any]]):
        def append(self, item: dict[str, Any]) -> None:
            if item.get("event") == "PROPOSAL_CONFIRMED":
                raise RuntimeError("Injected audit log failure during confirmation commit!")
            super().append(item)

    store = InMemoryProductionStore()
    m = CreditManifest(
        manifest_id="m", production_id="prod-fail", delivery_version_id="v1", entries=[]
    )
    store.register("prod-fail", [], m)

    prop = Proposal(
        proposal_id="prop-fail-1",
        production_id="prod-fail",
        issue_id="issue-1",
        proposer_id="rev-1",
        proposer_role="REVIEWER",
        action=AuthorizationAction.CONFIRM_VISUAL,
        reason="Verified",
        manifest_hash="m",
        obligation_registry_version_hash="o",
        artifact_index_digest="a",
        visual_observations_hash="v",
        created_at="2026-08-02T12:00:00Z",
        status="PENDING",
    )
    store.save_proposal(prop)

    bad_log = FailingAuditList(store._audit_log)
    store._audit_log = bad_log

    with pytest.raises(RuntimeError, match="Injected audit log failure"):
        store.confirm_proposal("prod-fail", "prop-fail-1", "appr-1", ("m", "o", "a", "v"))

    recheck_props = store.get_proposals("prod-fail")
    recheck_prop = next(p for p in recheck_props if p.proposal_id == "prop-fail-1")
    assert recheck_prop.status == "PENDING"
    assert len(store.get_authorizations("prod-fail")) == 0

    store._audit_log = list(bad_log)
    retry_auth = store.confirm_proposal(
        "prod-fail", "prop-fail-1", "appr-1", ("m", "o", "a", "v")
    )
    assert retry_auth.actor_id == "appr-1"
    assert (
        next(
            p for p in store.get_proposals("prod-fail") if p.proposal_id == "prop-fail-1"
        ).status
        == "CONFIRMED"
    )
    assert len(store.get_authorizations("prod-fail")) == 1


def test_direct_authorizations_endpoint_returns_410() -> None:
    issue_id = _seed()
    appr_token = create_token("approver-1", "RELEASE_APPROVER")
    res = client.post(
        f"/productions/{PROD_ID}/authorizations",
        headers={"Authorization": f"Bearer {appr_token}"},
        json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Direct"},
    )
    assert res.status_code == 410


def test_401_403_409_200_ordering_intact() -> None:
    issue_id = _seed()

    res_401 = client.post(f"/productions/{PROD_ID}/export")
    assert res_401.status_code == 401

    rev_token = create_token("reviewer-1", "REVIEWER")
    res_403 = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {rev_token}"}
    )
    assert res_403.status_code == 403

    appr_token = create_token("approver-1", "RELEASE_APPROVER")
    res_409 = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {appr_token}"}
    )
    assert res_409.status_code == 409

    res_p = client.post(
        f"/productions/{PROD_ID}/proposals",
        headers={"Authorization": f"Bearer {rev_token}"},
        json={"issue_id": issue_id, "action": "CONFIRM_VISUAL", "reason": "Verified"},
    )
    assert res_p.status_code == 201
    p_id = res_p.json()["proposal"]["proposal_id"]
    res_c = client.post(
        f"/productions/{PROD_ID}/proposals/{p_id}/confirm",
        headers={"Authorization": f"Bearer {appr_token}"},
    )
    assert res_c.status_code == 201

    res_200 = client.post(
        f"/productions/{PROD_ID}/export", headers={"Authorization": f"Bearer {appr_token}"}
    )
    assert res_200.status_code == 200, f"Export failed: {res_200.json()}"


# ── Phase 1A Startup & Fail-Closed Store Tests ───────────────────────────────


def test_firestore_settings_accepted_with_project_rejected_without(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "firestore")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.store_backend == "firestore"
    assert settings.google_cloud_project == "my-gcp-project"

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="requires a non-empty GOOGLE_CLOUD_PROJECT"):
        get_settings()


def test_uninitialized_production_store_fails_closed() -> None:
    uninit_store = UninitializedProductionStore()
    with pytest.raises(RuntimeError, match="Production store is uninitialized"):
        uninit_store.get("prod-1")

    with pytest.raises(RuntimeError, match="Production store is uninitialized"):
        uninit_store.get_proposals("prod-1")


def test_fastapi_lifespan_selects_firestore_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "firestore")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-test-proj")
    get_settings.cache_clear()

    mock_client = MagicMock()
    with patch("google.cloud.firestore.Client", return_value=mock_client), TestClient(app) as client_app:
        response = client_app.get("/health")
        assert response.status_code == 200
        store = get_production_store()
        assert isinstance(store, FirestoreProductionStore)
        assert store.client is mock_client

    # Verify lifespan shutdown resets store to UninitializedProductionStore
    store_after = get_production_store()
    assert isinstance(store_after, UninitializedProductionStore)


def test_failed_firestore_startup_leaves_uninitialized_store_not_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Install InMemoryProductionStore prior to startup attempt
    set_production_store(InMemoryProductionStore())
    assert isinstance(get_production_store(), InMemoryProductionStore)

    # 2. Request STORE_BACKEND=firestore with project set
    monkeypatch.setenv("STORE_BACKEND", "firestore")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-test-proj")
    get_settings.cache_clear()

    # 3. Force firestore.Client construction to fail
    # Note: Do NOT manually install UninitializedProductionStore sentinel in test before TestClient!
    with patch("google.cloud.firestore.Client", side_effect=Exception("Connection refused")), pytest.raises(
        Exception, match="Connection refused|Firestore initialization failed"
    ), TestClient(app):
        pass

    # 4 & 5. Prove startup raised and resulting store is UninitializedProductionStore, NOT memory!
    store_after = get_production_store()
    assert not isinstance(store_after, InMemoryProductionStore)
    assert isinstance(store_after, UninitializedProductionStore)
