"""
Integration tests for POST /productions/{production_id}/export.

Validates the three-phase contract:
  Phase 1: 401 when no/invalid token
  Phase 2: 403 when wrong role
  Phase 3: 409 when gate is not READY_TO_EXPORT
            200 when gate is READY_TO_EXPORT

All responses must arrive in this order — no phase skipping.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.canonical import sha256_digest
from creditlock.domain.models import (
    Authorization,
    CreditManifest,
    CreditSurface,
    LayoutAssertion,
    LayoutEvidence,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    SourceSpan,
    VisualObservation,
    VisualObservations,
)


def _mock_register_production(*args, **kwargs):
    from tests.fixtures.dummy_evidence import generate_dummy_evidence

    manifest = kwargs.get("manifest")
    if manifest is None and len(args) > 2:
        manifest = args[2]

    if "frames" not in kwargs and manifest is not None:
        _, frames, _ = generate_dummy_evidence(manifest)
        kwargs["frames"] = frames

    if "layout_evidence" not in kwargs and manifest is not None:
        from creditlock.domain.models import LayoutAssertion, LayoutEvidence

        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="test-v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=e.rendered_element_id,
                    visible_text=e.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
                for e in manifest.entries
            ],
        )
        kwargs["layout_evidence"] = layout

    if "artifact_index" not in kwargs and manifest is not None:
        from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
        from creditlock.evidence.release import build_artifact_index

        manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
        layout = kwargs["layout_evidence"]
        layout_hash = sha256_bytes_digest(canonical_json_bytes(layout.model_dump()))
        kwargs["artifact_index"] = build_artifact_index(
            manifest_hash, layout.render_profile_version, kwargs["frames"], layout_hash
        ).model_dump()

    if "visual_observations" not in kwargs and manifest is not None:
        from creditlock.domain.models import VisualObservation, VisualObservations

        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=e.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
                for e in manifest.entries
            ],
        )
        kwargs["visual_observations"] = visual

    if "render_profile_version" not in kwargs and "layout_evidence" in kwargs:
        kwargs["render_profile_version"] = kwargs["layout_evidence"].render_profile_version

    register_production(*args, **kwargs)


FIXTURES_DEMO = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "demo"


@pytest.fixture(autouse=True)
def reset_productions():
    """Clear in-memory state before each test."""
    clear_productions()
    yield
    clear_productions()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def reviewer_token():
    return create_token("reviewer_1", "REVIEWER")


@pytest.fixture
def approver_token():
    return create_token("approver_1", "RELEASE_APPROVER")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _demo_fixtures():
    """Load demo production fixture with minimal render evidence."""
    data = json.loads((FIXTURES_DEMO / "demo_production.json").read_text())
    obligations = [Obligation(**o) for o in data["obligations"]]
    manifest = CreditManifest(**data["manifest"])
    registry = data.get("contributor_registry", [])
    # Minimal evidence satisfying the ARTIFACT_PENDING precondition gate.
    # No assertions or observations so they don't interfere with the
    # MISSING_CREDIT / ARTIFACT_TEXT_MISMATCH / AMBIGUOUS_IDENTITY checks.
    layout = LayoutEvidence(
        manifest_id=manifest.manifest_id,
        render_profile_version="test-v1",
        assertions=[
            LayoutAssertion(
                rendered_element_id=e.rendered_element_id,
                visible_text=e.display_name,
                computed_font_size_px=16.0,
                bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                frame_index=0,
            )
            for e in manifest.entries
        ],
    )
    visual = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="test",
        observations=[
            VisualObservation(
                rendered_element_id=e.rendered_element_id,
                observation="ok",
                flagged=False,
            )
            for e in manifest.entries
        ],
    )
    return obligations, manifest, registry, layout, visual


def _clean_fixtures():
    """One ACTIVE obligation, manifest exactly satisfies it, with full render evidence."""
    obl = Obligation(
        obligation_id="obl_clean",
        production_id="prod_clean",
        credited_party_id="contrib_clean",
        required_display_text="Nadia Fontaine",
        role_label="Director",
        credit_surface=CreditSurface.MAIN_TITLES,
        source_document_id="doc_clean",
        source_document_version=1,
        source_span=SourceSpan(page=1, start_char=0, end_char=20, quote="Nadia Fontaine, Director"),
        source_hash="a" * 64,
        agent_reported_confidence=0.99,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="v1",
        status=ObligationStatus.ACTIVE,
    )
    entry = ManifestEntry(
        rendered_element_id="elem_clean",
        contributor_id="contrib_clean",
        display_name="Nadia Fontaine",
        role="Director",
        credit_surface=CreditSurface.MAIN_TITLES,
        ordinal_position=1,
        production_id="prod_clean",
        delivery_version_id="v1",
    )
    manifest = CreditManifest(
        manifest_id="mfst_clean",
        production_id="prod_clean",
        delivery_version_id="v1",
        entries=[entry],
    )
    layout = LayoutEvidence(
        manifest_id="mfst_clean",
        render_profile_version="chromium-130-v1",
        assertions=[
            LayoutAssertion(
                rendered_element_id="elem_clean",
                visible_text="Nadia Fontaine",
                computed_font_size_px=24.0,
                bounding_box={"x": 0.0, "y": 0.0, "w": 200.0, "h": 30.0},
                frame_index=0,
            )
        ],
    )
    visual = VisualObservations(
        manifest_id="mfst_clean",
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem_clean",
                observation="Credit clearly legible",
                flagged=False,
            )
        ],
    )
    return [obl], manifest, layout, visual


# ── Phase 1: 401 tests ────────────────────────────────────────────────────────


class TestPhase1Authentication:
    def test_no_token_returns_401(self, client):
        response = client.post("/productions/prod_demo/export")
        assert response.status_code == 401, response.text

    def test_invalid_token_returns_401(self, client):
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": "Bearer not.a.valid.token"},
        )
        assert response.status_code == 401, response.text

    def test_malformed_bearer_returns_401(self, client):
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": "NotBearer sometoken"},
        )
        assert response.status_code == 401, response.text

    def test_401_body_has_detail(self, client):
        response = client.post("/productions/prod_demo/export")
        body = response.json()
        assert "detail" in body

    def test_401_before_403_no_role_check_without_auth(self, client):
        """Without a token, we must get 401, not 403."""
        response = client.post("/productions/prod_demo/export")
        assert response.status_code == 401


# ── Phase 2: 403 tests ────────────────────────────────────────────────────────


class TestPhase2Authorization:
    def test_reviewer_role_returns_403(self, client, reviewer_token):
        obligations, manifest, _layout, _visual = _clean_fixtures()
        _mock_register_production("prod_clean", obligations, manifest)
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        assert response.status_code == 403, response.text

    def test_403_body_mentions_role(self, client, reviewer_token):
        obligations, manifest, _layout, _visual = _clean_fixtures()
        _mock_register_production("prod_clean", obligations, manifest)
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        body = response.json()
        assert "detail" in body
        assert "RELEASE_APPROVER" in str(body["detail"])

    def test_403_logged_to_audit_log(self, client, reviewer_token):
        """403 attempts must be logged; they are not issue codes."""
        from creditlock.api.export import _audit_log

        obligations, manifest, _layout, _visual = _clean_fixtures()
        _mock_register_production("prod_clean", obligations, manifest)
        client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        unauthorized_entries = [e for e in _audit_log if e.get("event") == "EXPORT_UNAUTHORIZED"]
        assert len(unauthorized_entries) == 1
        assert unauthorized_entries[0]["actor_id"] == "reviewer_1"

    def test_403_does_not_expose_gate_state(self, client, reviewer_token):
        """A 403 must not reveal gate state to unauthorized actors."""
        obligations, manifest, _layout, _visual = _clean_fixtures()
        _mock_register_production("prod_clean", obligations, manifest)
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        body = response.json()
        assert "gate_state" not in str(body)
        assert "issues" not in str(body)


# ── Phase 3: 409 tests ────────────────────────────────────────────────────────


class TestPhase3GateBlocked:
    def test_blocked_production_returns_409(self, client, approver_token):
        obligations, manifest, registry, layout, visual = _demo_fixtures()
        _mock_register_production(
            "prod_demo",
            obligations,
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 409, response.text

    def test_409_body_has_gate_state(self, client, approver_token):
        obligations, manifest, registry, layout, visual = _demo_fixtures()
        _mock_register_production(
            "prod_demo",
            obligations,
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        detail = response.json()["detail"]
        assert "gate_state" in detail
        assert detail["gate_state"] == "BLOCKED"

    def test_409_body_has_full_issue_list(self, client, approver_token):
        """No issue is swallowed — all open issues must appear in 409.
        Demo fixture has 3 issues: MISSING_CREDIT (obl_001), ARTIFACT_TEXT_MISMATCH
        (obl_002), AMBIGUOUS_IDENTITY (obl_003 via obligee_text path)."""
        obligations, manifest, registry, layout, visual = _demo_fixtures()
        _mock_register_production(
            "prod_demo",
            obligations,
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        detail = response.json()["detail"]
        assert "issues" in detail
        issue_codes = [i["code"] for i in detail["issues"]]
        assert "MISSING_CREDIT" in issue_codes
        assert "ARTIFACT_TEXT_MISMATCH" in issue_codes
        assert "AMBIGUOUS_IDENTITY" in issue_codes

    def test_409_issue_count_matches_issues_length(self, client, approver_token):
        obligations, manifest, registry, layout, visual = _demo_fixtures()
        _mock_register_production(
            "prod_demo",
            obligations,
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        detail = response.json()["detail"]
        assert detail["issue_count"] == len(detail["issues"])

    def test_409_each_issue_has_required_fields(self, client, approver_token):
        obligations, manifest, registry, layout, visual = _demo_fixtures()
        _mock_register_production(
            "prod_demo",
            obligations,
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_demo/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        for issue in response.json()["detail"]["issues"]:
            assert "issue_id" in issue
            assert "code" in issue
            assert "detail" in issue


# ── Phase 3: 200 tests ────────────────────────────────────────────────────────


class TestPhase3GateReady:
    def test_clean_production_returns_200(self, client, approver_token):
        obligations, manifest, layout, visual = _clean_fixtures()
        _mock_register_production(
            "prod_clean", obligations, manifest, layout_evidence=layout, visual_observations=visual
        )
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 200, response.text

    def test_200_body_has_gate_state_ready(self, client, approver_token):
        obligations, manifest, layout, visual = _clean_fixtures()
        _mock_register_production(
            "prod_clean", obligations, manifest, layout_evidence=layout, visual_observations=visual
        )
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        body = response.json()
        assert body["gate_state"] == "READY_TO_EXPORT"

    def test_200_body_has_delivery_path(self, client, approver_token):
        obligations, manifest, layout, visual = _clean_fixtures()
        _mock_register_production(
            "prod_clean", obligations, manifest, layout_evidence=layout, visual_observations=visual
        )
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        body = response.json()
        assert "delivery_package_path" in body

    def test_200_body_has_authorized_at(self, client, approver_token):
        obligations, manifest, layout, visual = _clean_fixtures()
        _mock_register_production(
            "prod_clean", obligations, manifest, layout_evidence=layout, visual_observations=visual
        )
        response = client.post(
            "/productions/prod_clean/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        body = response.json()
        assert "authorized_at" in body


# ── Authorization overlay: stale hash invalidation ───────────────────────────


class TestAuthorizationOverlay:
    def test_valid_authorization_clears_human_issue(self, client, approver_token):
        """
        An AMBIGUOUS_IDENTITY issue (rule 2 / obligee_text path, ≥2 registry matches)
        is resolved via CONFIRM_IDENTITY with a selected_contributor_id.
        After identity binding resolves to a present, compliant contributor,
        gate = READY_TO_EXPORT → 200.
        """
        obl = Obligation(
            obligation_id="obl_ambig",
            production_id="prod_ambig",
            credited_party_id=None,
            obligee_text="D. Park",
            credit_surface=CreditSurface.MAIN_TITLES,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=10, quote="D. Park"),
            source_hash="a" * 64,
            agent_reported_confidence=0.6,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_park",
            contributor_id="contrib_david_park",
            display_name="David Park",
            role="Associate Producer",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="prod_ambig",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="mfst_ambig",
            production_id="prod_ambig",
            delivery_version_id="v1",
            entries=[entry],
        )
        # Two registry entries match "D. Park" → ≥2 registry matches → AMBIGUOUS_IDENTITY.
        registry = [
            {"contributor_id": "contrib_dp_alt", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_david_park", "canonical_name": "D. Park", "aliases": []},
        ]
        # Minimal render evidence — needed by the ARTIFACT_PENDING precondition gate.
        layout = LayoutEvidence(
            manifest_id="mfst_ambig",
            render_profile_version="test-v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=e.rendered_element_id,
                    visible_text=e.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
                for e in manifest.entries
            ],
        )
        visual = VisualObservations(
            manifest_id="mfst_ambig",
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=e.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
                for e in manifest.entries
            ],
        )

        # First: confirm it returns 409 with AMBIGUOUS_IDENTITY
        _mock_register_production(
            "prod_ambig",
            [obl],
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        response = client.post(
            "/productions/prod_ambig/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 409
        issues_in_409 = response.json()["detail"]["issues"]
        assert len(issues_in_409) == 1
        assert issues_in_409[0]["code"] == "AMBIGUOUS_IDENTITY"
        issue_id = issues_in_409[0]["issue_id"]

        # Compute current hashes matching what the API will compute
        manifest_hash = sha256_digest(manifest.model_dump())
        obl_hash = sha256_digest([obl.model_dump()])
        from creditlock.api.export import get_production_store

        prod = get_production_store().get("prod_ambig")
        artifact_hash = sha256_digest(prod["artifact_index"])
        visual_hash = sha256_digest(visual.model_dump())

        # Create CONFIRM_IDENTITY authorization bound to current hashes,
        # selecting contrib_david_park (present in manifest, no field mismatches)
        auth = Authorization(
            authorization_id="auth_001",
            production_id="prod_001",
            proposal_id="prop_001",
            proposer_id="reviewer_1",
            issue_id=issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            selected_contributor_id="contrib_david_park",
            reason="D. Park confirmed = David Park via production contract",
            manifest_hash=manifest_hash,
            obligation_registry_version_hash=obl_hash,
            artifact_index_digest=artifact_hash,
            visual_observations_hash=visual_hash,
            authorized_at="2026-07-29T10:00:00Z",
        )

        # Re-register with the authorization
        _mock_register_production(
            "prod_ambig",
            [obl],
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
            authorizations=[auth],
        )

        # Identity bound → checker resolves contrib_david_park → present + no field issues
        response = client.post(
            "/productions/prod_ambig/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["gate_state"] == "READY_TO_EXPORT"

    def test_stale_authorization_does_not_clear_issue(self, client, approver_token):
        """
        CONFIRM_IDENTITY authorization that was valid against manifest v1 is
        stale after the manifest changes to v2 — its hashes no longer match and
        the AMBIGUOUS_IDENTITY issue reopens.
        """
        obl = Obligation(
            obligation_id="obl_stale",
            production_id="prod_stale",
            credited_party_id=None,
            obligee_text="X. Person",
            credit_surface=CreditSurface.END_CARDS,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=5, quote="X"),
            source_hash="b" * 64,
            agent_reported_confidence=0.5,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry_v1 = ManifestEntry(
            rendered_element_id="elem_stale",
            contributor_id="contrib_someone",
            display_name="Someone Else",
            role="Producer",
            credit_surface=CreditSurface.END_CARDS,
            ordinal_position=1,
            production_id="prod_stale",
            delivery_version_id="v1",
        )
        manifest_v1 = CreditManifest(
            manifest_id="mfst_stale_v1",
            production_id="prod_stale",
            delivery_version_id="v1",
            entries=[entry_v1],
        )
        # Two registry entries → AMBIGUOUS_IDENTITY on first export
        registry = [
            {"contributor_id": "contrib_xa", "canonical_name": "X. Person", "aliases": []},
            {"contributor_id": "contrib_someone", "canonical_name": "X. Person", "aliases": []},
        ]

        # Minimal render evidence
        layout_v1 = LayoutEvidence(
            manifest_id="mfst_stale_v1",
            render_profile_version="test-v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=e.rendered_element_id,
                    visible_text=e.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
                for e in manifest_v1.entries
            ],
        )
        visual_v1 = VisualObservations(
            manifest_id="mfst_stale_v1",
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=e.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
                for e in manifest_v1.entries
            ],
        )

        # Get the issue_id from first 409
        _mock_register_production(
            "prod_stale",
            [obl],
            manifest_v1,
            contributor_registry=registry,
            layout_evidence=layout_v1,
            visual_observations=visual_v1,
        )
        response = client.post(
            "/productions/prod_stale/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"
        issue_id = response.json()["detail"]["issues"][0]["issue_id"]

        # CONFIRM_IDENTITY bound to v1 manifest hash and v1 evidence hashes
        manifest_v1_hash = sha256_digest(manifest_v1.model_dump())
        auth = Authorization(
            authorization_id="auth_stale",
            production_id="prod_v2",
            proposal_id="prop_stale",
            proposer_id="reviewer_1",
            issue_id=issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            selected_contributor_id="contrib_someone",
            reason="confirmed",
            manifest_hash=manifest_v1_hash,  # bound to v1
            obligation_registry_version_hash=sha256_digest([obl.model_dump()]),
            artifact_index_digest=sha256_digest(layout_v1.model_dump()),
            visual_observations_hash=sha256_digest(visual_v1.model_dump()),
            authorized_at="2026-07-29T10:00:00Z",
        )

        # Change the manifest (v2) — auth manifest hash no longer matches
        entry_v2 = entry_v1.model_copy(
            update={"delivery_version_id": "v2", "display_name": "Someone New"}
        )
        manifest_v2 = CreditManifest(
            manifest_id="mfst_stale_v2",
            production_id="prod_stale",
            delivery_version_id="v2",
            entries=[entry_v2],
        )
        layout_v2 = LayoutEvidence(
            manifest_id="mfst_stale_v2",
            render_profile_version="test-v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=e.rendered_element_id,
                    visible_text=e.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
                for e in manifest_v2.entries
            ],
        )
        visual_v2 = VisualObservations(
            manifest_id="mfst_stale_v2",
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=e.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
                for e in manifest_v2.entries
            ],
        )

        _mock_register_production(
            "prod_stale",
            [obl],
            manifest_v2,
            contributor_registry=registry,
            layout_evidence=layout_v2,
            visual_observations=visual_v2,
            authorizations=[auth],  # auth bound to v1 manifest hash — now stale
        )

        # Should still return 409 because authorization is stale
        response = client.post(
            "/productions/prod_stale/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert response.status_code == 409, f"Expected 409 (stale auth), got {response.status_code}"


# ── Backdoor guard: MISSING_CREDIT must survive authorization attempt ──────────


class TestBackdoorApiGuard:
    """
    The specific scenario that was exploited by the old identity classifier:
    party IS in the registry, surface HAS other entries, credit is absent.
    Old code: AMBIGUOUS_IDENTITY (NEEDS_HUMAN, authorizable) → could be cleared.
    New code (rule 1): MISSING_CREDIT (BLOCKED) → stays 409 even with valid auth.
    """

    def test_missing_credit_stays_409_after_valid_authorization(self, client, approver_token):
        """
        Party in registry, surface has other entries, party absent from manifest.
        Step 1: export returns 409 with MISSING_CREDIT.
        Step 2: record a valid hash-bound authorization for that issue_id.
        Step 3: export still returns 409 — BLOCKED issues are not authorizable.
        """
        # Obligation for the line producer (in registry, absent from manifest)
        obl = Obligation(
            obligation_id="obl_line_producer",
            production_id="prod_backdoor",
            credited_party_id="contrib_line_producer",
            credit_surface=CreditSurface.MAIN_TITLES,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(
                page=1, start_char=0, end_char=20, quote="R. Osei, Line Producer"
            ),
            source_hash="a" * 64,
            agent_reported_confidence=0.97,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        # Surface has other entries (realistic populated roll)
        other_entry = ManifestEntry(
            rendered_element_id="elem_director",
            contributor_id="contrib_director",
            display_name="Some Director",
            role="Director",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="prod_backdoor",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="mfst_backdoor",
            production_id="prod_backdoor",
            delivery_version_id="v1",
            entries=[other_entry],
        )
        # Line producer IS in the registry
        registry = [
            {"contributor_id": "contrib_line_producer", "canonical_name": "R. Osei", "aliases": []}
        ]

        # Minimal render evidence for precondition gate
        layout = LayoutEvidence(
            manifest_id="mfst_backdoor",
            render_profile_version="test-v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=e.rendered_element_id,
                    visible_text=e.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
                for e in manifest.entries
            ],
        )
        visual = VisualObservations(
            manifest_id="mfst_backdoor",
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=e.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
                for e in manifest.entries
            ],
        )

        # Step 1: must be 409 with MISSING_CREDIT
        _mock_register_production(
            "prod_backdoor",
            [obl],
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        r1 = client.post(
            "/productions/prod_backdoor/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r1.status_code == 409
        issues = r1.json()["detail"]["issues"]
        codes = [i["code"] for i in issues]
        assert "MISSING_CREDIT" in codes, (
            f"Party in registry + populated surface + absent credit must be "
            f"MISSING_CREDIT, got {codes}"
        )
        assert "AMBIGUOUS_IDENTITY" not in codes
        issue_id = next(i["issue_id"] for i in issues if i["code"] == "MISSING_CREDIT")

        # Step 2: record a valid hash-bound authorization for that issue_id
        auth = Authorization(
            authorization_id="auth_backdoor",
            production_id="prod_backdoor",
            proposal_id="prop_backdoor",
            proposer_id="reviewer_1",
            issue_id=issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            selected_contributor_id="contrib_director",  # attempting to bind someone else
            reason="attempting to authorize away a missing credit",
            manifest_hash=sha256_digest(manifest.model_dump()),
            obligation_registry_version_hash=sha256_digest([obl.model_dump()]),
            artifact_index_digest=sha256_digest(layout.model_dump()),
            visual_observations_hash=sha256_digest(visual.model_dump()),
            authorized_at="2026-07-29T10:00:00Z",
        )
        _mock_register_production(
            "prod_backdoor",
            [obl],
            manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
            authorizations=[auth],
        )

        # Step 3: must still be 409 — BLOCKED is not authorizable
        r2 = client.post(
            "/productions/prod_backdoor/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r2.status_code == 409, (
            f"MISSING_CREDIT (BLOCKED) must not be clearable by authorization. "
            f"Got {r2.status_code}: {r2.json()}"
        )
        codes2 = [i["code"] for i in r2.json()["detail"]["issues"]]
        assert "MISSING_CREDIT" in codes2


class TestMalformedArtifactIndex:
    def test_malformed_artifact_index_returns_409_blocked(self, client, approver_token):
        """An existing but schema-invalid ArtifactIndex returns 409 ARTIFACT_INTEGRITY_FAILURE."""
        obl = Obligation(
            obligation_id="obl_malformed_1",
            production_id="prod_malformed",
            credited_party_id="contrib_1",
            credit_surface=CreditSurface.MAIN_TITLES,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=10, quote="John Doe"),
            source_hash="a" * 64,
            agent_reported_confidence=0.99,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_contrib_1",
            contributor_id="contrib_1",
            display_name="John Doe",
            role="Director",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="prod_malformed",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="mfst_malformed",
            production_id="prod_malformed",
            delivery_version_id="v1",
            entries=[entry],
        )
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text="John Doe",
                    computed_font_size_px=16,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                )
            ],
        )
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                )
            ],
        )

        _mock_register_production(
            "prod_malformed",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        # Override the artifact_index with malformed data directly in the store
        from creditlock.api.export import _production_store

        _production_store._store["prod_malformed"]["artifact_index"] = {
            "render_profile_version": "1.0"
        }  # Malformed (missing manifest_hash, frames, etc)

        resp = client.post(
            "/productions/prod_malformed/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "BLOCKED"
        issues = [i["code"] for i in resp.json()["detail"]["issues"]]
        assert "ARTIFACT_INTEGRITY_FAILURE" in issues

    def test_malformed_frame_entries_returns_409_blocked(self, client, approver_token):
        """Malformed frame-index entries do not escape as HTTP 500."""
        obl = Obligation(
            obligation_id="obl_malformed_2",
            production_id="prod_malformed2",
            credited_party_id="contrib_1",
            credit_surface=CreditSurface.MAIN_TITLES,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=10, quote="John Doe"),
            source_hash="a" * 64,
            agent_reported_confidence=0.99,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_contrib_1",
            contributor_id="contrib_1",
            display_name="John Doe",
            role="Director",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="prod_malformed2",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="mfst_malformed",
            production_id="prod_malformed2",
            delivery_version_id="v1",
            entries=[entry],
        )
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text="John Doe",
                    computed_font_size_px=16,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                )
            ],
        )
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                )
            ],
        )

        _mock_register_production(
            "prod_malformed2",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        # Override the artifact_index with malformed frames directly in the store
        from creditlock.api.export import _production_store

        _production_store._store["prod_malformed2"]["frames"] = [
            {"frame_id": "frame-0", "image_bytes": b"123", "image_hash": "hash"}
        ]  # missing frame_index
        _production_store._store["prod_malformed2"]["artifact_index"] = {
            "manifest_hash": "hash",
            "render_profile_version": "v1",
            "frames": [{"invalid": "data"}],  # Malformed frame!
            "layout_evidence_hash": "hash",
        }

        resp = client.post(
            "/productions/prod_malformed2/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "BLOCKED"
        issues = [i["code"] for i in resp.json()["detail"]["issues"]]
        assert "ARTIFACT_INTEGRITY_FAILURE" in issues
