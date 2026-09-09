"""
Unit tests for Judge MVP demo endpoints and session isolation.
Strictly fast offline tests (< 5s execution, no network/Chrome/Gemini/GCS).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from creditlock.agents.models import CallProvenance
from creditlock.agents.provider import ModelInvocationUnavailableError, ModelProvider
from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.demo import set_demo_override_provider
from creditlock.settings import get_settings


@pytest.fixture(autouse=True)
def mock_fast_renderer():
    """Mock Chrome execution to fall back to instant synthetic PNGs for fast unit tests."""
    with patch("creditlock.renderer.render._render_frame_with_chrome", return_value=None):
        yield


class MockSuccessProvider(ModelProvider):
    def is_available(self) -> bool:
        return True

    def generate_structured(self, prompt: str, response_schema: type, system_instruction: str | None = None):
        from creditlock.agents.extractor import ExtractedCandidateSchema, ExtractorOutputSchema
        from creditlock.agents.models import StructuredGeneration

        quote = "D. Park, Associate Producer, end cards."
        doc_text = (
            "Wei Chen shall receive sole Executive Producer credit, main titles, position 1.\n"
            "R. Osei shall receive Line Producer credit in main titles, position 3.\n"
            "D. Park, Associate Producer, end cards."
        )
        start_char = doc_text.find(quote)
        end_char = start_char + len(quote)

        output = ExtractorOutputSchema(
            candidates=[
                ExtractedCandidateSchema(
                    required_display_text="D. Park",
                    role_label="Associate Producer",
                    credit_surface="END_CARDS",
                    card_type="SOLO",
                    quote=quote,
                    start_char=start_char,
                    end_char=end_char,
                )
            ]
        )
        from creditlock.agents.models import ModelCallAttempt

        prov = CallProvenance(
            agent_role="extractor",
            primary_model="gemini-3.6-flash",
            configured_fallback_model="gemini-3.5-flash-lite",
            actual_model_used="gemini-3.6-flash",
            fallback_occurred=False,
            platform="google_genai",
            auth_mode="api_key",
            started_at_utc="2026-08-09T00:00:00Z",
            total_latency_ms=120.0,
            attempts=[
                ModelCallAttempt(
                    model_id="gemini-3.6-flash",
                    outcome="SUCCESS",
                    latency_ms=120.0,
                    sanitized_error_code=None,
                )
            ],
        )
        return StructuredGeneration(output=output, provenance=prov)


class MockFailingProvider(ModelProvider):
    def is_available(self) -> bool:
        raise ModelInvocationUnavailableError("API key missing or quota exceeded")

    def generate_structured(self, prompt: str, response_schema: type, system_instruction: str | None = None):
        raise ModelInvocationUnavailableError("API key missing or quota exceeded")


def test_1_demo_endpoints_disabled_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", False)
    client = TestClient(app)

    res = client.post("/demo/api/init")
    assert res.status_code == 404
    assert "disabled" in res.json()["detail"].lower()


def test_2_session_isolation_and_reset(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)

    # Init Session A
    res_a = client.post("/demo/api/init")
    assert res_a.status_code == 200
    data_a = res_a.json()

    # Init Session B
    res_b = client.post("/demo/api/init")
    assert res_b.status_code == 200
    data_b = res_b.json()

    assert data_a["demo_session_id"] != data_b["demo_session_id"]
    assert data_a["production_id"] != data_b["production_id"]
    assert data_a["reviewer_id"] != data_b["reviewer_id"]
    assert data_a["approver_id"] != data_b["approver_id"]

    # Reset Session A creates new session C
    res_c = client.post("/demo/api/reset")
    assert res_c.status_code == 200
    data_c = res_c.json()

    assert data_c["demo_session_id"] not in (data_a["demo_session_id"], data_b["demo_session_id"])

    # Querying state of Session B remains intact
    state_b = client.get(
        f"/demo/api/state?production_id={data_b['production_id']}",
        headers={"Authorization": f"Bearer {data_b['reviewer_token']}"},
    )
    assert state_b.status_code == 200
    assert state_b.json()["production_id"] == data_b["production_id"]


def test_3_signed_identity_switching_and_sub_rejection(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)

    init_res = client.post("/demo/api/init")
    data = init_res.json()
    pid = data["production_id"]

    # Token bound to pid_X cannot access pid_Y (with role RELEASE_APPROVER)
    wrong_token = create_token("user_1", "RELEASE_APPROVER", production_id="other_pid")
    export_res = client.post(
        f"/productions/{pid}/export",
        headers={"Authorization": f"Bearer {wrong_token}"},
    )
    assert export_res.status_code == 403
    assert "denied" in export_res.json()["detail"].lower() or "bound" in export_res.json()["detail"].lower()

    # Get active issue_id from state
    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {data['reviewer_token']}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    # Create proposal with Reviewer token
    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {data['reviewer_token']}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Grounded match verification",
        },
    )
    assert prop_res.status_code == 201
    prop_id = prop_res.json()["proposal"]["proposal_id"]

    # Same sub attempting to confirm (even if role is RELEASE_APPROVER) must be rejected
    same_sub_approver_token = create_token(
        data["reviewer_id"],
        "RELEASE_APPROVER",
        demo_session_id=data["demo_session_id"],
        production_id=pid,
    )
    confirm_res = client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {same_sub_approver_token}"},
    )
    assert confirm_res.status_code == 403
    assert "distinct" in confirm_res.json()["detail"].lower()


def test_4_analysis_provenance_and_error_handling(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)

    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]

    # 1. Success case with injected MockSuccessProvider
    set_demo_override_provider(MockSuccessProvider())
    res_ok = client.post(
        "/demo/api/analyze",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={"production_id": pid},
    )
    print("res_ok body:", res_ok.json())
    assert res_ok.status_code == 200
    body_ok = res_ok.json()
    assert body_ok["model_used"] == "gemini-3.6-flash"
    assert body_ok["required_human_action"] == "CONFIRM_IDENTITY"

    # 2. Failure case with injected MockFailingProvider
    init_failing = client.post("/demo/api/init").json()
    pid_fail = init_failing["production_id"]
    rev_tok_fail = init_failing["reviewer_token"]
    set_demo_override_provider(MockFailingProvider())

    res_fail = client.post(
        "/demo/api/analyze",
        headers={"Authorization": f"Bearer {rev_tok_fail}"},
        json={"production_id": pid_fail},
    )
    assert res_fail.status_code == 503
    err_detail = res_fail.json()["detail"]
    assert err_detail["error_code"] == "GEMINI_UNAVAILABLE"

    set_demo_override_provider(None)


def test_5_golden_flow_ready_to_export_and_replay_match(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)

    # Step 1: Init demo
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(
        f"/demo/api/state?production_id={pid}",
        headers={"Authorization": f"Bearer {rev_tok}"},
    ).json()
    issue_id = state["issues"][0]["issue_id"]

    # Step 2: Attempt export before authorization -> 409 CONFLICT
    exp_blocked = client.post(
        f"/productions/{pid}/export",
        headers={"Authorization": f"Bearer {app_tok}"},
    )
    assert exp_blocked.status_code == 409

    # Step 3: Reviewer creates proposal
    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "issue_id": issue_id,
            "action": "CONFIRM_IDENTITY",
            "selected_contributor_id": "contrib_david_park",
            "reason": "Grounded identity verification",
        },
    )
    assert prop_res.status_code == 201
    prop_id = prop_res.json()["proposal"]["proposal_id"]

    # Step 4: Distinct Approver confirms proposal -> 201 CREATED
    conf_res = client.post(
        f"/productions/{pid}/proposals/{prop_id}/confirm",
        headers={"Authorization": f"Bearer {app_tok}"},
    )
    assert conf_res.status_code == 201

    # Step 5: Export succeeds -> 200 OK with replay_status MATCH
    exp_ok = client.post(
        f"/productions/{pid}/export",
        headers={"Authorization": f"Bearer {app_tok}"},
    )
    assert exp_ok.status_code == 200
    exp_data = exp_ok.json()
    assert exp_data["gate_state"] == "READY_TO_EXPORT"
    assert exp_data["replay_status"] == "MATCH"

    # Step 6: Download endpoint returns exported ZIP package
    dl_res = client.get(
        f"/demo/api/export/download?production_id={pid}",
        headers={"Authorization": f"Bearer {app_tok}"},
    )
    if dl_res.status_code != 200:
        print("dl_res failure body:", dl_res.json())
    assert dl_res.status_code == 200
    assert dl_res.headers["content-type"] == "application/zip"
    assert len(dl_res.content) > 0


def test_6_no_simulated_responses_in_frontend():
    with open("src/creditlock/static/index.html", encoding="utf-8") as f:
        html = f.read()

    # --- Export panel must use only real server fields ---
    # No hardcoded MATCH literal in success span
    assert 'Offline Replay Status:</strong> <span style="font-weight: 700; color: var(--status-ready);">MATCH' not in html
    # Template variable for replay status must be present
    assert "data.replay_status" in html
    # Template variable for release_digest must be present
    assert "data.release_digest" in html
    # No manifest_hash fallback for Release Digest
    assert "manifest_hash || data.release_digest" not in html
    assert "data.manifest_hash" not in html
    # Gate state from server, no hardcoded READY_TO_EXPORT
    assert "data.gate_state" in html
    assert "data.status || data.gate_state" not in html
    # Delivery path from server, no fallback override
    assert "data.delivery_package_path" in html
    assert "data.download_url || data.delivery_package_path" not in html

    # --- Success panel must have integrity check for missing fields ---
    assert "missingFields" in html
    assert "Integrity Error" in html

    # --- No simulated/prefilled successful gate response ---
    assert "length || 1" not in html
    assert "Reviewer selection \u2014 not AI output" in html

    # --- Analysis flow ---
    assert "runAnalysis()" in html
    assert "Retry Analysis" in html

    # --- Timeline uses actor_id, not approver_id ---
    assert "a.actor_id" in html
    # The timeline must NOT use a.approver_id template expression (undefined on authorization objects)
    assert "${a.approver_id}" not in html

    # --- initDemo checks HTTP status before parsing success payload ---
    assert "initRes.ok" in html
    # Retry Initialization button exists
    assert "Retry Initialization" in html
    # _showInitError helper exists
    assert "_showInitError" in html
    # Workflow actions disabled during init
    assert "_setWorkflowActionsDisabled" in html


def test_7_red_tokens_endpoint_deleted(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)
    res = client.get("/demo/api/tokens?production_id=prod_demo_1&session_id=sess_1")
    assert res.status_code == 404


def test_8_red_unauthenticated_protected_demo_routes(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]

    # State without token -> 401
    r_state = client.get(f"/demo/api/state?production_id={pid}")
    assert r_state.status_code == 401

    # Analyze without token -> 401
    r_ana = client.post("/demo/api/analyze", json={"production_id": pid})
    assert r_ana.status_code == 401

    # Download without token -> 401
    r_dl = client.get(f"/demo/api/export/download?production_id={pid}")
    assert r_dl.status_code == 401


def test_9_red_session_isolation_and_unbound_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)
    sess_a = client.post("/demo/api/init").json()
    sess_b = client.post("/demo/api/init").json()

    pid_a = sess_a["production_id"]
    pid_b = sess_b["production_id"]
    tok_a = sess_a["reviewer_token"]

    # Session A token against Session B -> 403
    r_cross = client.get(
        f"/demo/api/state?production_id={pid_b}",
        headers={"Authorization": f"Bearer {tok_a}"},
    )
    assert r_cross.status_code == 403

    # Generic unbound token without demo_session_id / production_id claims against prod_demo_* -> 403
    unbound_tok = create_token("generic_user", "REVIEWER")
    r_unbound = client.get(
        f"/demo/api/state?production_id={pid_a}",
        headers={"Authorization": f"Bearer {unbound_tok}"},
    )
    assert r_unbound.status_code == 403


def test_10_red_empty_gemini_extraction_returns_502(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]

    class MockEmptyProvider(ModelProvider):
        def is_available(self) -> bool:
            return True

        def generate_structured(self, prompt: str, response_schema: type, system_instruction: str | None = None):
            from creditlock.agents.extractor import ExtractorOutputSchema
            from creditlock.agents.models import ModelCallAttempt, StructuredGeneration

            output = ExtractorOutputSchema(candidates=[])
            prov = CallProvenance(
                agent_role="extractor",
                primary_model="gemini-3.6-flash",
                configured_fallback_model="gemini-3.5-flash-lite",
                actual_model_used="gemini-3.6-flash",
                fallback_occurred=False,
                platform="google_genai",
                auth_mode="api_key",
                started_at_utc="2026-08-09T00:00:00Z",
                total_latency_ms=120.0,
                attempts=[
                    ModelCallAttempt(
                        model_id="gemini-3.6-flash",
                        outcome="SUCCESS",
                        latency_ms=120.0,
                        sanitized_error_code=None,
                    )
                ],
            )
            return StructuredGeneration(output=output, provenance=prov)

    set_demo_override_provider(MockEmptyProvider())
    res = client.post(
        "/demo/api/analyze",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={"production_id": pid},
    )
    set_demo_override_provider(None)

    assert res.status_code == 502
    detail = res.json()["detail"]
    assert detail["error_code"] == "GEMINI_NO_GROUNDED_CANDIDATES"
    assert "Retry-After" in res.headers


def test_11_red_production_gcs_persistence_without_test_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    monkeypatch.setattr(get_settings(), "evidence_bucket", "my-test-bucket")
    monkeypatch.delenv("CREDITLOCK_RUN_LIVE_GCS", raising=False)

    client = TestClient(app)
    init_data = client.post("/demo/api/init").json()
    pid = init_data["production_id"]
    rev_tok = init_data["reviewer_token"]
    app_tok = init_data["approver_token"]

    state = client.get(f"/demo/api/state?production_id={pid}", headers={"Authorization": f"Bearer {rev_tok}"}).json()
    issue_id = state["issues"][0]["issue_id"]

    prop_res = client.post(
        f"/productions/{pid}/proposals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={"issue_id": issue_id, "action": "CONFIRM_IDENTITY", "selected_contributor_id": "contrib_david_park", "reason": "ok"},
    )
    prop_id = prop_res.json()["proposal"]["proposal_id"]
    client.post(f"/productions/{pid}/proposals/{prop_id}/confirm", headers={"Authorization": f"Bearer {app_tok}"})

    with patch("creditlock.evidence.storage.GCSStore.put", return_value="gs://my-test-bucket/deliveries/pkg.zip") as mock_put:
        exp_res = client.post(f"/productions/{pid}/export", headers={"Authorization": f"Bearer {app_tok}"})
        assert exp_res.status_code == 200
        assert mock_put.called


def test_12_frontend_uses_active_token_always():
    with open("src/creditlock/static/index.html", encoding="utf-8") as f:
        html = f.read()

    # All protected actions must use getActiveToken()
    assert html.count("getActiveToken()") >= 5
    assert "${currentTokens.reviewer_token}" not in html
    assert "${currentTokens.approver_token}" not in html
    # State refresh uses getActiveToken
    assert "getActiveToken()" in html
    # Download uses getActiveToken
    assert "getActiveToken()" in html


def test_13_frontend_renderer_chrome_timeout_increased():
    """Chromium subprocess timeout must be 30 s (cold-start resilience)."""
    with open("src/creditlock/renderer/render.py", encoding="utf-8") as f:
        src = f.read()
    assert "timeout=30" in src
    # --disable-software-rasterizer must be absent (prevents headless software rendering)
    assert "--disable-software-rasterizer" not in src


def test_14_frontend_initialization_gating_and_proposal_status():
    """Verify frontend initialization gating and status-dependent proposal labels."""
    with open("src/creditlock/static/index.html", encoding="utf-8") as f:
        html = f.read()

    # Stable IDs for all protected workflow actions
    protected_ids = [
        "btn-goto-workbench",
        "btn-run-analysis",
        "btn-goto-authorization",
        "btn-switch-actor",
        "btn-submit-proposal",
        "btn-confirm-proposal",
        "btn-trigger-export",
        "btn-reset-demo",
    ]
    for btn_id in protected_ids:
        assert f'id="{btn_id}"' in html, f"Missing stable ID for protected action: {btn_id}"
        assert f"'{btn_id}'" in html, f"Protected action {btn_id} not managed in _setWorkflowActionsDisabled"

    # Retry Initialization button exists and is stable
    assert 'id="btn-retry-init"' in html

    # Initialization failure leaves actions disabled (_setWorkflowActionsDisabled(true) called on start)
    assert "_setWorkflowActionsDisabled(true)" in html
    assert "_setWorkflowActionsDisabled(false)" in html

    # Defensive checks inside action functions prevent requests without currentProductionId
    assert "if (!currentProductionId)" in html
    assert "Initialization Required" in html

    # Proposal labels are status-dependent and truthful
    assert "${statusLabel}: ${latestProp.proposal_id}" in html
    assert "Proposal Confirmed" in html
    assert "Proposal Rejected" in html
    assert "latestProp.status !== 'PENDING'" in html
    # Confirm button disabled unless status is PENDING
    assert "confirmBtn.disabled = (latestProp.status !== 'PENDING');" in html
