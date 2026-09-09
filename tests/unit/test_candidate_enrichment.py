"""
Failing-first tests for AMBIGUOUS_IDENTITY candidate enrichment requirements.

Requirements (all must be verified before implementation):
  1. Candidate objects are derived only from the existing obligation, contributor
     registry, and manifest — not from invented data.
  2. Candidate metadata returned by the state endpoint includes:
       contributor_id, canonical_name (registry), display_name (manifest entry or absent),
       role, credit_surface, present_in_manifest.
  3. The two demo candidates are visibly distinguishable: their labels differ
     because one has a manifest entry with a full display_name and the other does not.
  4. Neither candidate is preselected (no 'checked' attribute); the submit button
     starts disabled.
  5. CONFIRM_IDENTITY returns HTTP 422 when:
       a) the target obligation is missing (issue_id not found → 400/422),
       b) obligee_text is blank on the obligation,
       c) candidate derivation returns an empty set,
       d) selected_contributor_id is outside the derived set.
  6. The HTML _renderCandidates function uses DOM creation and textContent/value
     assignment instead of unescaped innerHTML template interpolation.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.export import get_production_store
from creditlock.settings import get_settings

_HTML_PATH = (
    Path(__file__).parent.parent.parent / "src" / "creditlock" / "static" / "index.html"
)

EXPECTED_CANDIDATES = {"contrib_d_park_unresolved", "contrib_david_park"}


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_judge_demo", True)
    with patch("creditlock.renderer.render._render_frame_with_chrome", return_value=None):
        yield TestClient(app)


@pytest.fixture
def demo_session(demo_client):
    res = demo_client.post("/demo/api/init")
    assert res.status_code == 200, f"Init failed: {res.text}"
    return res.json()


@pytest.fixture
def ambiguous_issue_id(demo_client, demo_session):
    state = demo_client.get(
        f"/demo/api/state?production_id={demo_session['production_id']}",
        headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
    ).json()
    amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
    assert amb is not None
    return amb["issue_id"]


# ── Requirement 2: Enriched candidate metadata ────────────────────────────────


class TestCandidateEnrichedMetadata:
    """State endpoint candidates list must include enriched metadata fields."""

    def test_candidates_include_role(self, demo_client, demo_session):
        """Each candidate must carry the obligation's role_label."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        for cand in amb["candidates"]:
            assert "role" in cand, (
                f"Candidate {cand.get('contributor_id')} must include 'role' field"
            )
            assert cand["role"], f"Candidate role must be non-empty for {cand}"

    def test_candidates_include_credit_surface(self, demo_client, demo_session):
        """Each candidate must carry the obligation's credit_surface."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        for cand in amb["candidates"]:
            assert "credit_surface" in cand, (
                f"Candidate {cand.get('contributor_id')} must include 'credit_surface' field"
            )

    def test_candidates_include_present_in_manifest(self, demo_client, demo_session):
        """Each candidate must have a boolean 'present_in_manifest' field."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        for cand in amb["candidates"]:
            assert "present_in_manifest" in cand, (
                f"Candidate {cand.get('contributor_id')} must include 'present_in_manifest'"
            )
            assert isinstance(cand["present_in_manifest"], bool), (
                f"present_in_manifest must be bool, got {type(cand['present_in_manifest'])}"
            )

    def test_candidates_include_display_name_when_in_manifest(self, demo_client, demo_session):
        """contrib_david_park is in the manifest with display_name 'David Park'."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        david = next(
            (c for c in amb["candidates"] if c["contributor_id"] == "contrib_david_park"), None
        )
        assert david is not None, "contrib_david_park must be in candidates"
        assert david.get("present_in_manifest") is True, (
            "contrib_david_park must be marked present_in_manifest=True"
        )
        assert david.get("display_name"), (
            "contrib_david_park must have a non-empty display_name from manifest"
        )
        assert david["display_name"] == "David Park", (
            f"Expected display_name='David Park', got '{david.get('display_name')}'"
        )

    def test_candidates_absent_from_manifest_has_false_flag(self, demo_client, demo_session):
        """contrib_d_park_unresolved is NOT in the manifest; present_in_manifest must be False."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        unresolved = next(
            (c for c in amb["candidates"] if c["contributor_id"] == "contrib_d_park_unresolved"),
            None,
        )
        assert unresolved is not None, "contrib_d_park_unresolved must be in candidates"
        assert unresolved.get("present_in_manifest") is False, (
            "contrib_d_park_unresolved must be marked present_in_manifest=False"
        )

    def test_candidates_derived_from_obligation_and_registry_not_invented(
        self, demo_client, demo_session
    ):
        """Candidates must be exactly what the obligation + registry produce — no extras."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        returned_ids = {c["contributor_id"] for c in amb["candidates"]}
        assert returned_ids == EXPECTED_CANDIDATES, (
            f"Candidates must exactly match registry-derived set {EXPECTED_CANDIDATES}, "
            f"got {returned_ids}"
        )


# ── Requirement 3: Candidates are visibly distinguishable ─────────────────────


class TestCandidatesDistinguishable:
    """Two candidates must produce different judge-readable labels."""

    def test_two_candidates_have_different_labels(self, demo_client, demo_session):
        """The two candidates must not produce identical display labels."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        assert len(amb["candidates"]) == 2, "Expected exactly two candidates"
        c1, c2 = amb["candidates"]

        def _label(c: dict) -> str:
            parts = [c.get("display_name") or c.get("canonical_name") or c["contributor_id"]]
            if c.get("role"):
                parts.append(c["role"])
            if c.get("credit_surface"):
                parts.append(c["credit_surface"])
            if c.get("present_in_manifest"):
                parts.append("present in current manifest")
            else:
                parts.append("no matching current manifest credit entry")
            return " — ".join(parts)

        label1 = _label(c1)
        label2 = _label(c2)
        assert label1 != label2, (
            f"Two candidates must have different labels so a reviewer can distinguish them, "
            f"but both produce: '{label1}'"
        )

    def test_david_park_label_contains_manifest_marker(self, demo_client, demo_session):
        """contrib_david_park's candidate data must enable a 'present in current manifest' label."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        david = next(
            (c for c in amb["candidates"] if c["contributor_id"] == "contrib_david_park"), None
        )
        assert david is not None
        # A meaningful label requires display_name to differ from canonical_name
        assert david.get("display_name") != david.get("canonical_name"), (
            "David Park's display_name ('David Park') should differ from canonical_name "
            "('D. Park') to help the reviewer distinguish them"
        )

    def test_d_park_unresolved_label_has_no_manifest_entry(self, demo_client, demo_session):
        """contrib_d_park_unresolved has no manifest entry; data must reflect that."""
        state = demo_client.get(
            f"/demo/api/state?production_id={demo_session['production_id']}",
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        ).json()
        amb = next((i for i in state["issues"] if i["code"] == "AMBIGUOUS_IDENTITY"), None)
        assert amb is not None
        unresolved = next(
            (c for c in amb["candidates"] if c["contributor_id"] == "contrib_d_park_unresolved"),
            None,
        )
        assert unresolved is not None
        assert not unresolved.get("display_name"), (
            "contrib_d_park_unresolved must not have a display_name (not in manifest)"
        )
        assert unresolved.get("present_in_manifest") is False


# ── Requirement 5: 422 fail-closed scenarios ──────────────────────────────────


class TestConfirmIdentity422Scenarios:
    """CONFIRM_IDENTITY must return 422 (or 400) for specific bad-input cases."""

    def test_missing_issue_id_returns_400_or_422(self, demo_client, demo_session):
        """Non-existent issue_id must be rejected."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": "nonexistent_issue_id_xyz",
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "Test",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code in (400, 422), (
            f"Missing issue_id must return 400 or 422, got {resp.status_code}: {resp.json()}"
        )

    def test_blank_selected_contributor_id_returns_422(self, demo_client, demo_session, ambiguous_issue_id):
        """Blank selected_contributor_id must be rejected with 422."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "   ",
                "reason": "Test",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 422, (
            f"Blank selected_contributor_id must return 422, got {resp.status_code}: {resp.json()}"
        )

    def test_id_outside_derived_set_returns_422(self, demo_client, demo_session, ambiguous_issue_id):
        """A contributor_id outside the derived candidate set must return 422."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_totally_not_a_candidate",
                "reason": "Test: outside derived set",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 422, (
            f"ID outside derived set must return 422, got {resp.status_code}: {resp.json()}"
        )

    def test_null_selected_contributor_id_returns_422(self, demo_client, demo_session, ambiguous_issue_id):
        """Omitting selected_contributor_id for CONFIRM_IDENTITY must return 422."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": None,
                "reason": "Test",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 422, (
            f"Null selected_contributor_id must return 422, got {resp.status_code}: {resp.json()}"
        )

    def test_zero_derived_candidates_rejects_arbitrary_id_with_422(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        """Zero derived candidates + arbitrary selected ID must return 422."""
        store = get_production_store()
        prod = store.get(demo_session["production_id"])
        assert prod is not None
        prod["contributor_registry"] = []

        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "Test: zero derived candidates must fail closed",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 422, (
            f"Zero derived candidates must return 422, got {resp.status_code}: {resp.json()}"
        )

    def test_valid_server_derived_candidate_accepted(
        self, demo_client, demo_session, ambiguous_issue_id
    ):
        """A valid server-derived candidate remains accepted."""
        resp = demo_client.post(
            f"/productions/{demo_session['production_id']}/proposals",
            json={
                "issue_id": ambiguous_issue_id,
                "action": "CONFIRM_IDENTITY",
                "selected_contributor_id": "contrib_david_park",
                "reason": "Test: valid server-derived candidate",
            },
            headers={"Authorization": f"Bearer {demo_session['reviewer_token']}"},
        )
        assert resp.status_code == 201, (
            f"Valid server-derived candidate must return 201, got {resp.status_code}: {resp.json()}"
        )
        assert resp.json()["proposal"]["selected_contributor_id"] == "contrib_david_park"


# ── Requirement 6: No innerHTML interpolation ─────────────────────────────────


def _extract_render_candidates_body() -> str:
    """Extract the complete body of _renderCandidates by brace counting."""
    html = _HTML_PATH.read_text(encoding="utf-8")
    start_pat = re.search(r"function _renderCandidates\s*\([^)]*\)\s*\{", html)
    assert start_pat is not None, "_renderCandidates function must exist in index.html"
    open_brace = html.index("{", start_pat.start())
    depth = 0
    i = open_brace
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return html[open_brace + 1 : i]


class TestNoInnerHTMLInterpolation:
    """The _renderCandidates function must not use template literals that
    splice candidate data into innerHTML unsanitized."""

    def test_render_candidates_does_not_use_innerHTML_template_splice(self):
        """The _renderCandidates function body must not contain innerHTML with ${..} splice."""
        fn_body = _extract_render_candidates_body()
        # Pattern: innerHTML = `...${...}...` or innerHTML += `...${...}...`
        unsafe_pattern = re.compile(
            r"\.innerHTML\s*[+]?=\s*[`\"'].*?\$\{",
            re.DOTALL,
        )
        assert not unsafe_pattern.search(fn_body), (
            "_renderCandidates must not splice candidate data into innerHTML via "
            "template literals — use DOM creation and textContent/value assignment instead"
        )

    def test_render_candidates_uses_dom_creation(self):
        """_renderCandidates must use document.createElement to build candidate elements."""
        fn_body = _extract_render_candidates_body()
        assert "createElement" in fn_body, (
            "_renderCandidates must use document.createElement for candidate elements"
        )

    def test_render_candidates_uses_textcontent_not_innerhtml_for_names(self):
        """Candidate names must be assigned via textContent, not innerHTML."""
        fn_body = _extract_render_candidates_body()
        assert "textContent" in fn_body, (
            "_renderCandidates must use .textContent to set candidate name text"
        )
