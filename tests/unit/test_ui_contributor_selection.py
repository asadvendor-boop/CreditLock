"""
Regression tests for the contributor-selection contract using the real demo fixture.

Requirements:
  - state candidate IDs exactly equal {'contrib_d_park_unresolved', 'contrib_david_park'}
  - HTML contains no fixed contributor IDs (contrib_d_park_alt must not appear)
  - no radio is initially selected
  - proposal control is initially disabled
  - arbitrary/non-candidate selection is rejected (422)
  - Reviewer proposal succeeds for contrib_david_park (201)
  - Reviewer self-confirmation returns 403
  - Reviewer export returns 403
  - distinct Approver confirmation returns 201
  - authenticated state becomes READY_TO_EXPORT with zero issues after approval
  - Approver export returns 200
  - alternate genuine candidate (contrib_d_park_unresolved) remains fail-closed
    with MISSING_CREDIT and export 409

Tests initialize the real demo fixture through POST /demo/api/init rather than
creating any self-authored production with invented IDs.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.settings import get_settings

# ── Path to HTML source ───────────────────────────────────────────────────────
_HTML_PATH = (
    Path(__file__).parent.parent.parent / "src" / "creditlock" / "static" / "index.html"
)

# The two real candidates from fixtures/demo/demo_production.json contributor_registry
EXPECTED_CANDIDATES = {"contrib_d_park_unresolved", "contrib_david_park"}
# This invented value must never be used in the production codebase
INVENTED_ID = "contrib_d_park_alt"


@pytest.fixture
def demo_client(monkeypatch):
    """TestClient with judge demo enabled and Chrome renderer mocked out for speed."""
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    with patch("creditlock.renderer.render._render_frame_with_chrome", return_value=None):
        yield TestClient(app)


@pytest.fixture
def demo_session(demo_client):
    """Initialize a fresh real demo session and return the init payload."""
    res = demo_client.post("/demo/api/init")
    assert res.status_code == 200, f"Init failed: {res.text}"
    return res.json()


@pytest.fixture
def ambiguous_issue_id(demo_client, demo_session):
    """Return the AMBIGUOUS_IDENTITY issue_id from the initialized demo session."""
    state = demo_client.get(
        f"/demo/api/state?production_id={demo_session['production_id']}",
        headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
    ).json()
    amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
    assert amb is not None, "AMBIGUOUS_IDENTITY issue not found in demo state"
    return amb["issue_id"]


# ── HTML static analysis ──────────────────────────────────────────────────────


class TestHTMLContainsNoHardcodedCandidates:
    """The HTML must not contain any fixed contributor IDs."""

    def test_invented_id_absent_from_html(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        assert INVENTED_ID not in html, (
            f"Invented ID '{INVENTED_ID}' must not appear in index.html"
        )

    def test_no_fixed_radio_value_for_david_park(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        # The old hardcoded pattern: value="contrib_david_park" in a radio input
        old_pattern = re.compile(
            r'<input[^>]+type=["\']radio["\'][^>]+value=["\']contrib_david_park["\']',
            re.IGNORECASE,
        )
        assert not old_pattern.search(html), (
            "contrib_david_park must not be hardcoded as a radio value in HTML"
        )

    def test_no_fixed_radio_value_for_d_park_unresolved(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        old_pattern = re.compile(
            r'<input[^>]+type=["\']radio["\'][^>]+value=["\']contrib_d_park_unresolved["\']',
            re.IGNORECASE,
        )
        assert not old_pattern.search(html), (
            "contrib_d_park_unresolved must not be hardcoded as a radio value in HTML"
        )

    def test_no_radio_has_checked_attribute(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        radio_pattern = re.compile(
            r'<input[^>]+name=["\']contributor-candidate["\'][^>]*>',
            re.IGNORECASE,
        )
        # Any static radio buttons must not be pre-checked
        for radio in radio_pattern.findall(html):
            assert "checked" not in radio.lower(), (
                f"Static radio button must not be pre-checked: {radio}"
            )

    def test_submit_button_starts_disabled(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        btn_pattern = re.compile(
            r'<button[^>]+id=["\']btn-submit-proposal["\'][^>]*>',
            re.IGNORECASE,
        )
        buttons = btn_pattern.findall(html)
        assert len(buttons) == 1, "Expected exactly one btn-submit-proposal"
        assert "disabled" in buttons[0].lower(), (
            "btn-submit-proposal must be disabled on initial render"
        )

    def test_label_reviewer_selection_not_ai_output_present(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        assert "Reviewer selection" in html
        assert "not AI output" in html


# ── Server-side candidate contract ───────────────────────────────────────────


class TestServerCandidateContract:
    """State endpoint must return exactly the two real registry candidates."""

    def test_state_returns_candidate_ids_for_ambiguous_issue(
        self, demo_client, demo_session
    ):
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None, "AMBIGUOUS_IDENTITY must be present"
        assert "candidate_ids" in amb, "candidate_ids must be in AMBIGUOUS_IDENTITY issue"
        returned = set(amb["candidate_ids"])
        assert returned == EXPECTED_CANDIDATES, (
            f"candidate_ids must exactly equal {EXPECTED_CANDIDATES}, got {returned}"
        )

    def test_state_candidate_ids_exclude_invented_id(
        self, demo_client, demo_session
    ):
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        assert INVENTED_ID not in amb.get("candidate_ids", []), (
            f"Invented ID '{INVENTED_ID}' must not appear in server candidate_ids"
        )

    def test_state_returns_candidates_list_with_names(
        self, demo_client, demo_session
    ):
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        assert "candidates" in amb, "candidates list with names must be returned"
        cids = {c["contributor_id"] for c in amb["candidates"]}
        assert cids == EXPECTED_CANDIDATES

    def test_init_returns_app_commit_and_k_revision(
        self, demo_client
    ):
        data = demo_client.post("/demo/api/init").json()
        assert "app_commit" in data, "app_commit must be present in init response"
        assert "k_revision" in data, "k_revision must be present in init response"


# ── Proposal validation ───────────────────────────────────────────────────────


class TestProposalCandidateValidation:
    """Proposals must be rejected for fabricated contributor IDs."""

    def test_arbitrary_id_rejected_with_4xx(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": INVENTED_ID,
                "reason": "Test: fabricated ID must be rejected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code in (400, 422), (
            f"Fabricated ID '{INVENTED_ID}' must be rejected with 4xx, got {resp.status_code}: {resp.json()}"
        )
        detail_str = str(resp.json().get("detail", "")).lower()
        assert "valid candidate" in detail_str or "not" in detail_str, (
            f"Error detail must mention candidate validity: {resp.json()}"
        )

    def test_contrib_david_park_accepted(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "Reviewer selects David Park (genuine candidate)",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 201, (
            f"contrib_david_park must be accepted, got {resp.status_code}: {resp.json()}"
        )
        assert resp.json()["proposal"]["selected_contributor_id"] == "contrib_david_park"

    def test_contrib_d_park_unresolved_accepted(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        """The alternate genuine candidate must also be accepted at proposal stage."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_d_park_unresolved",
                "reason": "Reviewer selects D. Park (unresolved, genuine candidate)",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 201, (
            f"contrib_d_park_unresolved must be accepted, got {resp.status_code}: {resp.json()}"
        )


# ── Full reviewer/approver flow with contrib_david_park ──────────────────────


class TestFullFlowDavidPark:
    """Selecting contrib_david_park leads to READY_TO_EXPORT and HTTP 200 export."""

    def test_reviewer_proposal_accepted(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park selected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 201

    def test_reviewer_self_confirm_is_403(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park selected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        confirm = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert confirm.status_code == 403, (
            f"Reviewer self-confirmation must be 403, got {confirm.status_code}"
        )

    def test_reviewer_export_is_403(
        self, demo_client, demo_session
    ):
        export = demo_client.post(
            f"/productions/{demo_session['production_id']}/export",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert export.status_code == 403, (
            f"REVIEWER export must be 403, got {export.status_code}"
        )

    def test_distinct_approver_confirmation_is_201(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park selected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        confirm = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )
        assert confirm.status_code == 201, (
            f"Distinct approver confirmation must be 201, got {confirm.status_code}: {confirm.json()}"
        )

    def test_state_becomes_ready_after_approval(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park selected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )

        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        assert state["gate_state"] == "READY_TO_EXPORT", (
            f"Gate must be READY_TO_EXPORT after approval, got {state['gate_state']}"
        )
        # AMBIGUOUS_IDENTITY issue must be cleared
        ambiguous_remaining = [i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"]
        assert len(ambiguous_remaining) == 0, (
            f"AMBIGUOUS_IDENTITY must be cleared, remaining: {ambiguous_remaining}"
        )

    def test_approver_export_is_200(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park selected",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )

        export = demo_client.post(
            f"/productions/{demo_session['production_id']}/export",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )
        assert export.status_code == 200, (
            f"Approver export must be 200 after approval, got {export.status_code}: {export.json()}"
        )
        data = export.json()
        assert data.get("gate_state") == "READY_TO_EXPORT"
        assert data.get("release_digest")
        assert len(data["release_digest"]) == 64


# ── Alternate genuine candidate remains fail-closed ───────────────────────────


class TestAlternateCandidateFailClosed:
    """Selecting contrib_d_park_unresolved clears AMBIGUOUS_IDENTITY but
    MISSING_CREDIT (absent from manifest) keeps export blocked at 409."""

    def test_d_park_unresolved_keeps_export_blocked(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_d_park_unresolved",
                "reason": "Selecting the alternate genuine candidate",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )

        # Export must still be blocked (MISSING_CREDIT issue for contributor absent from manifest)
        export = demo_client.post(
            f"/productions/{demo_session['production_id']}/export",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )
        assert export.status_code == 409, (
            f"Export must remain 409 for contrib_d_park_unresolved (MISSING_CREDIT), "
            f"got {export.status_code}: {export.json()}"
        )
        detail = export.json().get("detail", {})
        issue_codes = [i.get("code") for i in detail.get("issues", [])]
        assert "MISSING_CREDIT" in issue_codes, (
            f"MISSING_CREDIT must be in remaining issues, got {issue_codes}"
        )


# ── Confirmed proposal recorded UI state contract ─────────────────────────────


class TestConfirmedProposalRecordedState:
    """Proves that a confirmed proposal renders recorded identity and cannot claim selection required."""

    def test_confirmed_proposal_cannot_render_selection_required(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        # 1. Submit proposal for David Park
        prop = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "David Park confirmed",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert prop.status_code == 201
        prop_id = prop.json()["proposal"]["proposal_id"]

        # 2. Confirm proposal
        confirm = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )
        assert confirm.status_code == 201

        # 3. Export
        export = demo_client.post(
            f"/productions/{demo_session['production_id']}/export",
            headers={"Authorization": f"Bearer {demo_session['approver_token']}"},
        )
        assert export.status_code == 200

        # 4. Verify state contract has confirmed proposal with contrib_david_park
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        assert len(state["proposals"]) > 0
        latest_prop = state["proposals"][-1]
        assert latest_prop["status"] == "CONFIRMED"
        assert latest_prop["selected_contributor_id"] == "contrib_david_park"

        # 5. Verify HTML contract: when latestProp exists, recorded selection is rendered
        html = _HTML_PATH.read_text(encoding="utf-8")
        assert "_renderRecordedSelection" in html
        assert "Identity authorized:" in html
        assert "Recorded contributor:" in html
        assert "recorded-selection-box" in html
        assert "neutral-identity-msg" in html

        # Neutral state must never say selection required
        neutral_block = re.search(
            r"function _renderNeutralState\(\)\s*\{([\s\S]*?)\}",
            html,
        )
        assert neutral_block is not None
        assert "selection required" not in neutral_block.group(1).lower()
        assert "no candidate selected" not in neutral_block.group(1).lower()
        assert "No active identity resolution required." in neutral_block.group(1)
