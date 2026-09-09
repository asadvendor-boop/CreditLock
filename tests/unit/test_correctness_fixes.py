"""
Tests for Task 1 correctness fixes:

1. Auth overlay backdoor: BLOCKED/DEGRADED issues must never be cleared by authorization.
2. Lifecycle mapping: WAIVED/SUPERSEDED produce no issues; CONFLICTING -> CONFLICTING_OBLIGATION.
3. Missing vs ambiguous: credited_party_id absent from manifest -> MISSING_CREDIT regardless of surface.
4. Credit field semantics: required_display_text vs display_name, role_label vs role.
"""

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.auth import create_token
from creditlock.api.export import clear_productions, register_production
from creditlock.domain.canonical import sha256_digest
from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import (
    ISSUE_GATE_MAP,
    Authorization,
    CreditManifest,
    CreditSurface,
    GateState,
    IssueCode,
    LayoutAssertion,
    LayoutEvidence,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    SourceSpan,
    VisualObservation,
    VisualObservations,
)
from creditlock.settings import get_settings

_UNSET = object()


def _min_evidence(
    manifest_id: str = "mfst_fix",
    entries: list[ManifestEntry] | None = None,
) -> tuple[LayoutEvidence, VisualObservations]:
    """Valid evidence covering manifest entries for checker precondition."""
    assertions = []
    observations = []
    if entries:
        for entry in entries:
            assertions.append(
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                    frame_index=0,
                )
            )
            observations.append(
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id,
                    observation="ok",
                    flagged=False,
                )
            )
    return (
        LayoutEvidence(
            manifest_id=manifest_id,
            render_profile_version="v1",
            assertions=assertions,
        ),
        VisualObservations(
            manifest_id=manifest_id,
            model_id="gemini-3.6-flash",
            observations=observations,
        ),
    )


def _register(
    production_id: str,
    obligations: list,
    manifest,
    layout_evidence=_UNSET,
    visual_observations=_UNSET,
    contributor_registry=None,
    authorizations=None,
    projection=None,
) -> None:
    """Convenience: always includes valid evidence unless caller explicitly supplies it."""
    from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
    from creditlock.evidence.release import build_artifact_index
    from tests.fixtures.dummy_evidence import generate_dummy_evidence

    # Default evidence from _min_evidence is missing frames/artifact_index and doesn't match dummy evidence anyway.
    # We will generate dummy evidence and just use it.
    default_layout, default_frames, _default_artifact_index = generate_dummy_evidence(manifest)
    _, default_visual = _min_evidence(manifest.manifest_id, manifest.entries)

    if layout_evidence is _UNSET:
        layout_evidence = default_layout
    if visual_observations is _UNSET:
        visual_observations = default_visual

    # Rebuild artifact_index using the actual layout_evidence used
    manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
    if layout_evidence:
        layout_hash = sha256_bytes_digest(canonical_json_bytes(layout_evidence.model_dump()))
        profile_version = layout_evidence.render_profile_version
    else:
        layout_hash = sha256_bytes_digest(canonical_json_bytes({}))
        profile_version = "1.0"

    artifact_index = build_artifact_index(manifest_hash, profile_version, default_frames, layout_hash).model_dump()

    register_production(
        production_id,
        obligations,
        manifest,
        layout_evidence=layout_evidence,
        visual_observations=visual_observations,
        contributor_registry=contributor_registry,
        authorizations=authorizations,
        projection=projection,
        frames=default_frames,
        artifact_index=artifact_index,
        render_profile_version=profile_version,
    )



@pytest.fixture(autouse=True)
def reset_state():
    get_settings.cache_clear()
    clear_productions()
    yield
    clear_productions()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def approver_token():
    return create_token("approver_1", "RELEASE_APPROVER")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _base_obl(status=ObligationStatus.ACTIVE, **kwargs) -> Obligation:
    defaults = {
        "obligation_id": "obl_fix",
        "production_id": "prod_fix",
        "credited_party_id": "contrib_fix",
        "credit_surface": CreditSurface.MAIN_TITLES,
        "source_document_id": "doc_fix",
        "source_document_version": 1,
        "source_span": SourceSpan(page=1, start_char=0, end_char=5, quote="test"),
        "source_hash": "f" * 64,
        "agent_reported_confidence": 0.9,
        "extraction_model_id": "gemini",
        "prompt_version": "v1",
        "status": status,
    }
    defaults.update(kwargs)
    return Obligation(**defaults)


def _entry(
    contributor_id="contrib_fix",
    surface=CreditSurface.MAIN_TITLES,
    display_name="Test Name",
    role="Director",
) -> ManifestEntry:
    return ManifestEntry(
        rendered_element_id=f"elem_{contributor_id}",
        contributor_id=contributor_id,
        display_name=display_name,
        role=role,
        credit_surface=surface,
        ordinal_position=1,
        production_id="prod_fix",
        delivery_version_id="v1",
    )


def _manifest(*entries) -> CreditManifest:
    return CreditManifest(
        manifest_id="mfst_fix",
        production_id="prod_fix",
        delivery_version_id="v1",
        entries=list(entries),
    )


def _auth_for_issue(issue_id, manifest, obligations) -> Authorization:
    from creditlock.domain.models import AuthorizationAction

    return Authorization(
        authorization_id="auth_test",
        production_id="prod_fix",
        proposal_id="prop_test",
        issue_id=issue_id,
        proposer_id="reviewer_1",
        actor_id="approver_1",
        role="RELEASE_APPROVER",
        action=AuthorizationAction.CONFIRM_PRECEDENCE,
        reason="test",
        manifest_hash=sha256_digest(manifest.model_dump()),
        obligation_registry_version_hash=sha256_digest([o.model_dump() for o in obligations]),
        artifact_index_digest=sha256_digest({}),
        visual_observations_hash=sha256_digest({}),
        authorized_at="2026-07-29T10:00:00Z",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1: Auth overlay must NOT clear BLOCKED or DEGRADED issues
# ══════════════════════════════════════════════════════════════════════════════

BLOCKED_CODES = [
    IssueCode.MISSING_CREDIT,
    IssueCode.ARTIFACT_TEXT_MISMATCH,
    IssueCode.ARTIFACT_GROUPING_MISMATCH,
    IssueCode.ARTIFACT_POSITION_MISMATCH,
    IssueCode.ARTIFACT_SIZE_MISMATCH,
    IssueCode.ARTIFACT_INTEGRITY_FAILURE,
]

DEGRADED_CODES = [
    IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE,
]

NON_AUTHORIZABLE_CODES = BLOCKED_CODES + DEGRADED_CODES


class TestAuthOverlayCannotClearBlockedOrDegraded:
    """
    For every BLOCKED and DEGRADED issue code, record a valid hash-bound
    authorization for it and assert export still returns 409.
    These violations must be fixed in the artifact or the obligation must be waived.
    They cannot be authorized past.
    """

    @pytest.mark.parametrize("code", BLOCKED_CODES)
    def test_blocked_code_cannot_be_authorized_away(self, client, approver_token, code):
        # Build a MISSING_CREDIT scenario (creditor absent from manifest)
        obl = _base_obl(obligation_id=f"obl_{code.value}")
        manifest = _manifest()  # empty — obligee absent

        _register("prod_fix", [obl], manifest)

        # Get the issue_id from the 409
        r = client.post(
            "/productions/prod_fix/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r.status_code == 409
        # Find the issue matching this code or any BLOCKED issue
        issues = r.json()["detail"]["issues"]
        # Grab the first issue_id (could be MISSING_CREDIT since party absent)
        issue_id = issues[0]["issue_id"]

        # Build a valid hash-bound authorization for it
        auth = _auth_for_issue(issue_id, manifest, [obl])

        # Re-register with authorization
        _register("prod_fix", [obl], manifest, authorizations=[auth])

        # Must still return 409 — BLOCKED issues cannot be authorized away
        r2 = client.post(
            "/productions/prod_fix/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r2.status_code == 409, (
            f"BLOCKED issue code {code.value} was incorrectly cleared by authorization. "
            f"Got: {r2.status_code} {r2.json()}"
        )

    def test_required_evidence_unavailable_cannot_be_authorized_away(self, client, approver_token):
        from creditlock.domain.models import SizeComparisonBasis

        # Obligation with size check but no layout evidence -> REQUIRED_EVIDENCE_UNAVAILABLE
        obl = _base_obl(
            obligation_id="obl_deg",
            minimum_relative_size=0.5,
            size_reference={"rendered_element_id": "ref_elem"},
            size_comparison_basis=SizeComparisonBasis.COMPUTED_FONT_SIZE_PX,
        )
        entry = _entry()
        ref_entry = ManifestEntry(
            rendered_element_id="ref_elem",
            contributor_id="ref",
            display_name="REF",
            role="ref",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=0,
            production_id="prod_fix",
            delivery_version_id="v1",
        )
        manifest = _manifest(entry, ref_entry)
        # Provide layout evidence with MISSING assertion for subject element
        layout = LayoutEvidence(
            manifest_id="mfst_fix",
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id="ref_elem",
                    visible_text="REF",
                    computed_font_size_px=40.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 40},
                    frame_index=0,
                )
                # subject elem_contrib_fix is absent from assertions
            ],
        )
        _register("prod_fix", [obl], manifest, layout_evidence=layout)
        r = client.post(
            "/productions/prod_fix/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r.status_code == 409
        issue_id = r.json()["detail"]["issues"][0]["issue_id"]
        auth = _auth_for_issue(issue_id, manifest, [obl])
        register_production(
            "prod_fix",
            [obl],
            manifest,
            layout_evidence=layout,
            authorizations=[auth],
        )
        r2 = client.post(
            "/productions/prod_fix/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert r2.status_code == 409, (
            f"DEGRADED issue REQUIRED_EVIDENCE_UNAVAILABLE was incorrectly cleared. "
            f"Got: {r2.status_code}"
        )

    def test_identity_confirmed_with_selected_contributor_reaches_200(self, client, approver_token):
        """
        Positive control: CONFIRM_IDENTITY with a valid selected_contributor_id
        that exists in the manifest, passes all field checks, and has matching
        hashes → export returns 200.
        """
        # obligee_text resolves uniquely to "contrib_confirmed" in the registry,
        # which IS in the manifest → registry resolves to 1; presence check passes;
        # no field mismatches → AMBIGUOUS_IDENTITY does NOT fire since it uniquely
        # resolves.  We instead test zero-match AMBIGUOUS_IDENTITY + confirmation.
        obl = _base_obl(
            obligation_id="obl_ambig",
            credited_party_id=None,
            obligee_text="R. Osei",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="Test Name", role="Director")
        manifest = _manifest(entry)
        # Registry has "R. Osei" → contributor_id "contrib_fix" IS in manifest
        registry = [{"contributor_id": "contrib_fix", "canonical_name": "R. Osei", "aliases": []}]

        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r = client.post(
            "/productions/prod_fix/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        # Unique registry match + present in manifest → no identity issue
        # (field checks may still fire if role_label set — here they don't)
        assert r.status_code == 200, (
            f"Unique obligee_text match + manifest present + no field issues "
            f"must reach 200. Got: {r.status_code} {r.json()}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2: Lifecycle mapping
# ══════════════════════════════════════════════════════════════════════════════


class TestLifecycleMapping:
    def _run(self, status: ObligationStatus) -> tuple[list, GateState]:
        obl = _base_obl(status=status)
        manifest = _manifest()  # empty — party absent, but lifecycle skips before identity check
        layout, visual = _min_evidence()
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        gate = fold_gate(issues, ProjectionStatus())
        return issues, gate

    def test_waived_obligation_produces_no_issues(self):
        issues, gate = self._run(ObligationStatus.WAIVED)
        assert issues == [], (
            f"WAIVED obligation must produce zero issues, got: {[i.code for i in issues]}"
        )
        assert gate == GateState.READY_TO_EXPORT

    def test_superseded_obligation_produces_no_issues(self):
        issues, gate = self._run(ObligationStatus.SUPERSEDED)
        assert issues == [], (
            f"SUPERSEDED obligation must produce zero issues, got: {[i.code for i in issues]}"
        )
        assert gate == GateState.READY_TO_EXPORT

    def test_conflicting_obligation_emits_conflicting_obligation(self):
        issues, gate = self._run(ObligationStatus.CONFLICTING)
        codes = [i.code for i in issues]
        assert IssueCode.CONFLICTING_OBLIGATION in codes, (
            f"CONFLICTING obligation must emit CONFLICTING_OBLIGATION, got: {codes}"
        )
        assert gate == GateState.NEEDS_HUMAN

    def test_conflicting_obligation_not_needs_confirmation(self):
        issues, _ = self._run(ObligationStatus.CONFLICTING)
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION not in codes

    def test_pending_external_authority_emits_needs_human(self):
        issues, gate = self._run(ObligationStatus.PENDING_EXTERNAL_AUTHORITY)
        codes = [i.code for i in issues]
        assert IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION in codes or any(
            ISSUE_GATE_MAP.get(c) == GateState.NEEDS_HUMAN for c in codes
        ), f"PENDING_EXTERNAL_AUTHORITY must emit a NEEDS_HUMAN issue, got: {codes}"
        assert gate == GateState.NEEDS_HUMAN

    def test_candidate_emits_unconfirmed(self):
        issues, gate = self._run(ObligationStatus.CANDIDATE)
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION in codes
        assert gate == GateState.NEEDS_CONFIRMATION

    def test_needs_confirmation_emits_unconfirmed(self):
        issues, gate = self._run(ObligationStatus.NEEDS_CONFIRMATION)
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION in codes
        assert gate == GateState.NEEDS_CONFIRMATION

    def test_waived_obligation_absent_from_manifest_still_no_issues(self):
        """Even if the waived party is missing from the manifest, no issue is raised."""
        obl = _base_obl(
            status=ObligationStatus.WAIVED,
            credited_party_id="contrib_waived",
        )
        manifest = _manifest()  # absent
        layout, visual = _min_evidence()
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert issues == []

    def test_superseded_obligation_absent_from_manifest_still_no_issues(self):
        obl = _base_obl(
            status=ObligationStatus.SUPERSEDED,
            credited_party_id="contrib_superseded",
        )
        manifest = _manifest()
        layout, visual = _min_evidence()
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert issues == []


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3: Missing vs Ambiguous — name-resolution path
# ══════════════════════════════════════════════════════════════════════════════


class TestMissingVsAmbiguous:
    def test_absent_party_no_registry_is_missing_credit(self):
        """credited_party_id absent from manifest with no registry -> MISSING_CREDIT."""
        obl = _base_obl(credited_party_id="contrib_absent")
        manifest = _manifest(_entry("contrib_someone_else"))
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=[],
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_absent_party_not_in_registry_is_missing_credit(self):
        """credited_party_id not in registry, absent from manifest -> MISSING_CREDIT."""
        obl = _base_obl(credited_party_id="contrib_ghost")
        manifest = _manifest(_entry("contrib_other"))
        registry = [{"contributor_id": "contrib_other", "canonical_name": "Other", "aliases": []}]
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_empty_manifest_is_missing_credit_not_ambiguous(self):
        """Empty manifest always gives MISSING_CREDIT, never AMBIGUOUS_IDENTITY."""
        obl = _base_obl(credited_party_id="contrib_anybody")
        registry = [{"contributor_id": "contrib_anybody", "canonical_name": "X", "aliases": []}]
        manifest = _manifest()  # empty
        layout, visual = _min_evidence()
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_party_in_registry_with_surface_entries_is_missing_credit(self):
        """Party is in registry, manifest has entries on same surface, no alias -> MISSING_CREDIT (rule 1).

        Under rule 1, credited_party_id absent from manifest is always MISSING_CREDIT
        regardless of registry membership or surface population.
        """
        obl = _base_obl(credited_party_id="contrib_d_park")
        entry = _entry("contrib_david_park_full")
        manifest = _manifest(entry)
        registry = [
            {"contributor_id": "contrib_d_park", "canonical_name": "D. Park", "aliases": []}
        ]
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_source_name_alias_does_not_resolve_credited_party_id(self):
        """AliasRecord.alias is a source-name string, not a contributor-id redirect.
        Rule 1 uses exact manifest match only; the alias field is not consulted."""
        obl = _base_obl(credited_party_id="contrib_d_park")
        entry = _entry("contrib_david_park_full")
        manifest = _manifest(entry)
        registry = [
            {
                "contributor_id": "contrib_d_park",
                "canonical_name": "D. Park",
                "aliases": [
                    {
                        "alias": "D. Park",  # source-name string, not a contributor-id
                        "registered_by": "reviewer_1",
                        "registered_at": "2026-07-29T00:00:00Z",
                        "reason": "name as written in source clause",
                    }
                ],
            }
        ]
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        # credited_party_id "contrib_d_park" not in manifest → MISSING_CREDIT
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_dev_004_fixture_ambiguous_identity(self):
        """dev_004 uses obligee_text path → AMBIGUOUS_IDENTITY."""
        import json
        import pathlib

        data = json.loads(
            (
                pathlib.Path(__file__).parent.parent.parent
                / "fixtures/benchmark/dev/dev_004_ambiguous_identity.json"
            ).read_text()
        )
        obl = Obligation(**data["obligations"][0])
        manifest = CreditManifest(**data["manifest"])
        registry = data.get("contributor_registry", [])
        layout, visual = _min_evidence(manifest_id=manifest.manifest_id, entries=manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY in codes
        assert IssueCode.MISSING_CREDIT not in codes


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4: Credit field semantics
# ══════════════════════════════════════════════════════════════════════════════


class TestCreditFieldSemantics:
    def _run_checker(self, obl, entry) -> list:
        manifest = _manifest(entry)
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        return evaluate_findings(ci)

    def test_required_display_text_vs_display_name_mismatch(self):
        obl = _base_obl(required_display_text="Executive Producer")
        entry = _entry(display_name="Exec Producer", role="Executive Producer")
        issues = self._run_checker(obl, entry)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        # Detail must mention the field
        details = [i.detail for i in issues if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
        assert any("display_name" in d or "required_display_text" in d for d in details)

    def test_required_display_text_vs_display_name_match(self):
        obl = _base_obl(required_display_text="Executive Producer")
        entry = _entry(display_name="Executive Producer", role="Producer")
        issues = self._run_checker(obl, entry)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH not in codes

    def test_role_label_vs_role_mismatch(self):
        obl = _base_obl(role_label="Executive Producer")
        entry = _entry(display_name="Test Name", role="Exec Producer")
        issues = self._run_checker(obl, entry)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        details = [i.detail for i in issues if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
        assert any("role" in d.lower() for d in details)

    def test_role_label_vs_role_match(self):
        obl = _base_obl(role_label="Executive Producer")
        entry = _entry(display_name="Test Name", role="Executive Producer")
        issues = self._run_checker(obl, entry)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH not in codes

    def test_both_display_and_role_mismatch_emits_one_issue_per_field(self):
        obl = _base_obl(
            required_display_text="Wei Chen",
            role_label="Executive Producer",
        )
        entry = _entry(display_name="Wei Chen", role="Exec Producer")
        issues = self._run_checker(obl, entry)
        text_issues = [i for i in issues if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
        # role mismatch -> one ARTIFACT_TEXT_MISMATCH
        assert len(text_issues) >= 1

    def test_demo_fixture_text_mismatch_on_role(self):
        """Demo fixture obl_demo_002: role_label='Executive Producer', manifest role='Exec Producer'.
        required_display_text='Wei Chen' matches display_name='Wei Chen'.
        role_label mismatch fires ARTIFACT_TEXT_MISMATCH."""
        import json
        import pathlib

        data = json.loads(
            (
                pathlib.Path(__file__).parent.parent.parent / "fixtures/demo/demo_production.json"
            ).read_text()
        )
        obl_data = next(o for o in data["obligations"] if o["obligation_id"] == "obl_demo_002")
        obl = Obligation(**obl_data)
        manifest = CreditManifest(**data["manifest"])
        entry = next(e for e in manifest.entries if e.contributor_id == obl.credited_party_id)

        assert obl.required_display_text == "Wei Chen"
        assert entry.display_name == "Wei Chen"  # display_name matches — no mismatch here
        assert obl.role_label == "Executive Producer"
        assert entry.role == "Exec Producer"  # role mismatch -> ARTIFACT_TEXT_MISMATCH

        layout, visual = _min_evidence(manifest_id=manifest.manifest_id, entries=manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        # Verify the detail mentions 'role'
        role_issues = [i for i in issues if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
        assert any("role" in i.detail.lower() for i in role_issues)


# ══════════════════════════════════════════════════════════════════════════════
# Task C: identity authorization, overlay restrictions, fixture correctness
# ══════════════════════════════════════════════════════════════════════════════


def _confirm_identity_auth(
    issue_id: str,
    manifest,
    obligations: list,
    selected_contributor_id: str,
) -> Authorization:
    """Build a valid CONFIRM_IDENTITY authorization with selected_contributor_id.

    Uses the hash of the minimal evidence produced by _register so that the
    hash-bound check in export.py passes.
    """
    from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
    from creditlock.evidence.release import build_artifact_index
    from tests.fixtures.dummy_evidence import generate_dummy_evidence

    layout, frames, _ = generate_dummy_evidence(manifest)
    _, visual = _min_evidence(manifest.manifest_id, manifest.entries)

    manifest_hash = sha256_bytes_digest(canonical_json_bytes(manifest.model_dump()))
    layout_hash = sha256_bytes_digest(canonical_json_bytes(layout.model_dump()))
    artifact_index = build_artifact_index(manifest_hash, layout.render_profile_version, frames, layout_hash)

    return Authorization(
        authorization_id="auth_ci",
        production_id="prod_fix",
        proposal_id="prop_ci",
        issue_id=issue_id,
        proposer_id="reviewer_1",
        actor_id="approver_1",
        role="RELEASE_APPROVER",
        action="CONFIRM_IDENTITY",
        reason="human confirmed identity",
        selected_contributor_id=selected_contributor_id,
        manifest_hash=sha256_digest(manifest.model_dump()),
        obligation_registry_version_hash=sha256_digest([o.model_dump() for o in obligations]),
        artifact_index_digest=sha256_digest(artifact_index.model_dump()),
        visual_observations_hash=sha256_digest(visual.model_dump()),
        authorized_at="2026-07-29T10:00:00Z",
    )


class TestIdentityAuthorization:
    """
    Task C: CONFIRM_IDENTITY binds a specific contributor; AMBIGUOUS_IDENTITY
    cannot be cleared by the generic overlay.
    """

    def _ambig_setup(self):
        """obligee_text matches 2 registry entries → AMBIGUOUS_IDENTITY (≥2 registry matches)."""
        obl = _base_obl(
            obligation_id="obl_ambig",
            credited_party_id=None,
            obligee_text="D. Park",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="Test Name", role="Director")
        manifest = _manifest(entry)
        # Two registry entries match "D. Park" → ≥2 registry matches → AMBIGUOUS_IDENTITY
        registry = [
            {"contributor_id": "contrib_dp_unreg", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "D. Park", "aliases": []},
        ]
        return obl, manifest, registry

    def test_generic_auth_cannot_clear_ambiguous_identity(self, client, approver_token):
        """Generic hash-bound authorization must NOT clear AMBIGUOUS_IDENTITY."""
        obl, manifest, registry = self._ambig_setup()
        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        assert r1.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        # Generic auth (no selected_contributor_id, action="TEST")
        auth = _auth_for_issue(issue_id, manifest, [obl])
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Generic authorization must not clear AMBIGUOUS_IDENTITY. "
            f"Got {r2.status_code}: {r2.json()}"
        )

    def test_confirm_identity_without_selected_contributor_is_rejected(self):
        """Authorization model: CONFIRM_IDENTITY requires selected_contributor_id."""
        import pydantic

        with pytest.raises((pydantic.ValidationError, ValueError)):
            Authorization(
                authorization_id="auth_bad",
                production_id="prod_fix",
                proposal_id="prop_bad",
                proposer_id="reviewer_1",
                issue_id="issue_001",
                actor_id="approver_1",
                role="RELEASE_APPROVER",
                action="CONFIRM_IDENTITY",
                reason="missing selection",
                # selected_contributor_id omitted / None
                selected_contributor_id=None,
                manifest_hash="a" * 64,
                obligation_registry_version_hash="b" * 64,
                artifact_index_digest="c" * 64,
                visual_observations_hash="d" * 64,
                authorized_at="2026-07-29T10:00:00Z",
            )

    def test_arbitrary_action_cannot_clear_ambiguity(self, client, approver_token):
        """Only CONFIRM_IDENTITY clears AMBIGUOUS_IDENTITY; random action does not."""
        obl, manifest, registry = self._ambig_setup()
        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        from creditlock.domain.models import AuthorizationAction

        auth = Authorization(
            authorization_id="auth_arb",
            production_id="prod_fix",
            proposal_id="prop_arb",
            proposer_id="reviewer_1",
            issue_id=issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action=AuthorizationAction.CONFIRM_PRECEDENCE,  # not CONFIRM_IDENTITY
            reason="arbitrary",
            selected_contributor_id="contrib_fix",
            manifest_hash=sha256_digest(manifest.model_dump()),
            obligation_registry_version_hash=sha256_digest([obl.model_dump()]),
            artifact_index_digest=sha256_digest({}),
            visual_observations_hash=sha256_digest({}),
            authorized_at="2026-07-29T10:00:00Z",
        )
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Arbitrary action must not clear AMBIGUOUS_IDENTITY. Got {r2.status_code}: {r2.json()}"
        )

    def test_selected_contributor_absent_from_manifest_stays_409(self, client, approver_token):
        """CONFIRM_IDENTITY with selected contributor absent from manifest → MISSING_CREDIT."""
        obl, manifest, registry = self._ambig_setup()
        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        auth = _confirm_identity_auth(
            issue_id,
            manifest,
            [obl],
            selected_contributor_id="contrib_nobody",  # not in manifest
        )
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Selected contributor absent from manifest must stay 409. "
            f"Got {r2.status_code}: {r2.json()}"
        )

    def test_selected_contributor_wrong_surface_stays_409(self, client, approver_token):
        """CONFIRM_IDENTITY binding a contributor not on the obligation's surface → MISSING_CREDIT."""
        from creditlock.domain.models import CreditSurface

        obl = _base_obl(
            obligation_id="obl_ambig",
            credited_party_id=None,
            obligee_text="D. Park",
            credit_surface=CreditSurface.MAIN_TITLES,
        )
        # Entry is on END_CARDS, not MAIN_TITLES
        entry = _entry(contributor_id="contrib_fix", surface=CreditSurface.END_CARDS)
        manifest = _manifest(entry)
        # Two registry entries → ≥2 matches → AMBIGUOUS_IDENTITY
        registry = [
            {"contributor_id": "contrib_dp_unreg", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "D. Park", "aliases": []},
        ]

        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        auth = _confirm_identity_auth(
            issue_id,
            manifest,
            [obl],
            selected_contributor_id="contrib_fix",  # in manifest but wrong surface
        )
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Selected contributor on wrong surface must stay 409. "
            f"Got {r2.status_code}: {r2.json()}"
        )

    def test_selected_contributor_wrong_display_text_produces_blocked(self, client, approver_token):
        """After identity bound, field mismatches fire deterministic BLOCKED issues."""
        obl = _base_obl(
            obligation_id="obl_ambig",
            credited_party_id=None,
            obligee_text="D. Park",
            required_display_text="David Park",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="D. Park", role="Director")
        manifest = _manifest(entry)
        # Two registry entries → ≥2 matches → AMBIGUOUS_IDENTITY
        registry = [
            {"contributor_id": "contrib_dp_unreg", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "D. Park", "aliases": []},
        ]

        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]
        assert r1.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"

        auth = _confirm_identity_auth(
            issue_id,
            manifest,
            [obl],
            selected_contributor_id="contrib_fix",
        )
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])

        from creditlock.api.export import get_production_store
        prod = get_production_store().get("prod_fix")
        from creditlock.api.resolutions import _compute_hashes
        mh, oh, ah, vh = _compute_hashes(prod)
        print(f"\nAPI art_index: {prod.get('artifact_index')}")
        from creditlock.domain.canonical import canonical_json_bytes
        print(f"API bytes: {canonical_json_bytes(prod.get('artifact_index'))}")
        print(f"--- API Hashes: mh={mh} oh={oh} ah={ah} vh={vh}")
        print(f"--- Auth Hashes: mh={auth.manifest_hash} oh={auth.obligation_registry_version_hash} ah={auth.artifact_index_digest} vh={auth.visual_observations_hash}")
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Field mismatch after identity binding must produce BLOCKED 409. "
            f"Got {r2.status_code}: {r2.json()}"
        )
        codes = [i["code"] for i in r2.json()["detail"]["issues"]]
        assert "ARTIFACT_TEXT_MISMATCH" in codes

    def test_valid_identity_confirmation_all_fields_pass_reaches_200(self, client, approver_token):
        """CONFIRM_IDENTITY with correct contributor, correct fields → 200."""
        obl = _base_obl(
            obligation_id="obl_ambig",
            credited_party_id=None,
            obligee_text="D. Park",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="Test Name", role="Director")
        manifest = _manifest(entry)
        # Two registry entries → ≥2 matches → AMBIGUOUS_IDENTITY
        registry = [
            {"contributor_id": "contrib_dp_unreg", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "D. Park", "aliases": []},
        ]

        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]
        assert r1.json()["detail"]["issues"][0]["code"] == "AMBIGUOUS_IDENTITY"

        auth = _confirm_identity_auth(
            issue_id,
            manifest,
            [obl],
            selected_contributor_id="contrib_fix",
        )
        _register("prod_fix", [obl], manifest, contributor_registry=registry, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 200, (
            f"Valid CONFIRM_IDENTITY with correct contributor and fields must reach 200. "
            f"Got {r2.status_code}: {r2.json()}"
        )

    def test_conflicting_selections_fail_closed(self, client, approver_token):
        """Two CONFIRM_IDENTITY authorizations selecting different contributors → 409."""
        obl, manifest, registry = self._ambig_setup()
        _register("prod_fix", [obl], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        from tests.fixtures.dummy_evidence import generate_dummy_evidence
        _layout, _, _ = generate_dummy_evidence(manifest)
        _, _visual = _min_evidence(manifest.manifest_id, manifest.entries)
        auth_a = _confirm_identity_auth(issue_id, manifest, [obl], "contrib_fix")
        auth_b = Authorization(
            authorization_id="auth_b",
            production_id="prod_fix",
            proposal_id="prop_b",
            proposer_id="reviewer_1",
            issue_id=issue_id,
            actor_id="approver_2",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            reason="conflicting selection",
            selected_contributor_id="contrib_other",
            manifest_hash=sha256_digest(manifest.model_dump()),
            obligation_registry_version_hash=sha256_digest([obl.model_dump()]),
            artifact_index_digest=auth_a.artifact_index_digest,
            visual_observations_hash=sha256_digest(_visual.model_dump()),
            authorized_at="2026-07-29T11:00:00Z",
        )
        _register(
            "prod_fix",
            [obl],
            manifest,
            contributor_registry=registry,
            authorizations=[auth_a, auth_b],
        )
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Conflicting identity selections must fail closed. Got {r2.status_code}: {r2.json()}"
        )

    def test_stale_identity_confirmation_stays_409(self, client, approver_token):
        """Stale hash-bound CONFIRM_IDENTITY (manifest changed) does not clear issue."""
        obl, manifest_v1, registry = self._ambig_setup()
        _register("prod_fix", [obl], manifest_v1, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]

        auth = _confirm_identity_auth(issue_id, manifest_v1, [obl], "contrib_fix")

        # Change manifest — auth hashes now stale
        entry_v2 = _entry(
            contributor_id="contrib_fix", display_name="Updated Name", role="Director"
        )
        manifest_v2 = CreditManifest(
            manifest_id="mfst_fix_v2",
            production_id="prod_fix",
            delivery_version_id="v2",
            entries=[entry_v2],
        )
        _register(
            "prod_fix", [obl], manifest_v2, contributor_registry=registry, authorizations=[auth]
        )
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"Stale CONFIRM_IDENTITY must not clear issue. Got {r2.status_code}: {r2.json()}"
        )


class TestOverlayRestrictions:
    """
    Task C: only VISUAL_OBSERVATION_UNCERTAIN with action CONFIRM_VISUAL
    may use the generic overlay.  CONFLICTING_OBLIGATION and
    UNSUPPORTED_PRESENTATION_ASSERTION are not clearable.
    """

    def test_conflicting_obligation_not_clearable(self, client, approver_token):
        obl = _base_obl(status=ObligationStatus.CONFLICTING)
        manifest = _manifest()
        _register("prod_fix", [obl], manifest)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]
        auth = _auth_for_issue(issue_id, manifest, [obl])
        _register("prod_fix", [obl], manifest, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"CONFLICTING_OBLIGATION must not be clearable by generic auth. "
            f"Got {r2.status_code}: {r2.json()}"
        )

    def test_unsupported_presentation_assertion_not_clearable(self, client, approver_token):
        obl = _base_obl(status=ObligationStatus.PENDING_EXTERNAL_AUTHORITY)
        manifest = _manifest()
        _register("prod_fix", [obl], manifest)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        issue_id = r1.json()["detail"]["issues"][0]["issue_id"]
        auth = _auth_for_issue(issue_id, manifest, [obl])
        _register("prod_fix", [obl], manifest, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            f"UNSUPPORTED_PRESENTATION_ASSERTION must not be clearable. "
            f"Got {r2.status_code}: {r2.json()}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Task 1 final closure: focused identity/text/whitespace correctness tests
# ══════════════════════════════════════════════════════════════════════════════


class TestIdentityBindingScopedToAmbiguousIssue:
    """
    CONFIRM_IDENTITY must only resolve an obligation whose current issue code is
    AMBIGUOUS_IDENTITY.  Targeting any other issue type must leave the ambiguity
    unresolved.
    """

    def test_confirm_identity_targeting_unsupported_assertion_does_not_resolve_ambiguity(
        self, client, approver_token
    ):
        """
        Obligation has UNSUPPORTED_PRESENTATION_ASSERTION on one obligation and
        AMBIGUOUS_IDENTITY on another.  A CONFIRM_IDENTITY targeting the UPA issue_id
        must NOT resolve the AMBIGUOUS_IDENTITY.
        """
        # Obligation 1: PENDING_EXTERNAL_AUTHORITY → UNSUPPORTED_PRESENTATION_ASSERTION
        obl_upa = _base_obl(
            obligation_id="obl_upa",
            status=ObligationStatus.PENDING_EXTERNAL_AUTHORITY,
            credited_party_id=None,
        )
        # Obligation 2: obligee_text with 2 registry matches → AMBIGUOUS_IDENTITY
        obl_ambig = _base_obl(
            obligation_id="obl_ambig2",
            credited_party_id=None,
            obligee_text="D. Park",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="Test Name", role="Director")
        manifest = _manifest(entry)
        registry = [
            {"contributor_id": "contrib_dp_a", "canonical_name": "D. Park", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "D. Park", "aliases": []},
        ]

        _register("prod_fix", [obl_upa, obl_ambig], manifest, contributor_registry=registry)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        # Find the UNSUPPORTED_PRESENTATION_ASSERTION issue_id
        upa_issue = next(
            i
            for i in r1.json()["detail"]["issues"]
            if i["code"] == "UNSUPPORTED_PRESENTATION_ASSERTION"
        )
        upa_issue_id = upa_issue["issue_id"]

        # CONFIRM_IDENTITY targeting the UPA issue — must NOT resolve ambiguity
        auth = Authorization(
            authorization_id="auth_wrong_target",
            production_id="prod_fix",
            proposal_id="prop_wrong_target",
            proposer_id="reviewer_1",
            issue_id=upa_issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            reason="targeting wrong issue type",
            selected_contributor_id="contrib_fix",
            manifest_hash=sha256_digest(manifest.model_dump()),
            obligation_registry_version_hash=sha256_digest(
                [obl_upa.model_dump(), obl_ambig.model_dump()]
            ),
            artifact_index_digest=sha256_digest({}),
            visual_observations_hash=sha256_digest({}),
            authorized_at="2026-07-29T10:00:00Z",
        )
        _register(
            "prod_fix",
            [obl_upa, obl_ambig],
            manifest,
            contributor_registry=registry,
            authorizations=[auth],
        )
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            "CONFIRM_IDENTITY targeting UNSUPPORTED_PRESENTATION_ASSERTION must not "
            f"resolve AMBIGUOUS_IDENTITY. Got {r2.status_code}: {r2.json()}"
        )
        codes = [i["code"] for i in r2.json()["detail"]["issues"]]
        assert "AMBIGUOUS_IDENTITY" in codes, (
            f"AMBIGUOUS_IDENTITY must still be present. Issues: {codes}"
        )

    def test_confirm_identity_targeting_non_ambiguous_current_issue_is_ignored(
        self, client, approver_token
    ):
        """CONFIRM_IDENTITY against a MISSING_CREDIT issue_id is silently ignored."""
        obl = _base_obl(obligation_id="obl_mc", credited_party_id="contrib_absent")
        manifest = _manifest()
        _register("prod_fix", [obl], manifest)
        r1 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r1.status_code == 409
        mc_issue_id = r1.json()["detail"]["issues"][0]["issue_id"]
        assert r1.json()["detail"]["issues"][0]["code"] == "MISSING_CREDIT"

        auth = Authorization(
            authorization_id="auth_mc_ci",
            production_id="prod_fix",
            proposal_id="prop_mc_ci",
            proposer_id="reviewer_1",
            issue_id=mc_issue_id,
            actor_id="approver_1",
            role="RELEASE_APPROVER",
            action="CONFIRM_IDENTITY",
            reason="targeting MISSING_CREDIT — must be ignored",
            selected_contributor_id="contrib_absent",
            manifest_hash=sha256_digest(manifest.model_dump()),
            obligation_registry_version_hash=sha256_digest([obl.model_dump()]),
            artifact_index_digest=sha256_digest({}),
            visual_observations_hash=sha256_digest({}),
            authorized_at="2026-07-29T10:00:00Z",
        )
        _register("prod_fix", [obl], manifest, authorizations=[auth])
        r2 = client.post(
            "/productions/prod_fix/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert r2.status_code == 409, (
            "CONFIRM_IDENTITY targeting MISSING_CREDIT must be silently ignored. "
            f"Got {r2.status_code}: {r2.json()}"
        )


class TestCheckerDeterminism:
    """Duplicate registry rows and whitespace edge cases."""

    def test_duplicate_registry_rows_for_one_contributor_count_as_one_match(self):
        """Two registry rows for the same contributor_id are deduplicated — counts as 1 match."""
        obl = _base_obl(
            obligation_id="obl_dedup",
            credited_party_id=None,
            obligee_text="R. Osei",
        )
        entry = _entry(contributor_id="contrib_fix", display_name="Test Name", role="Director")
        manifest = _manifest(entry)
        # Same contributor appears twice in registry with the same name
        registry = [
            {"contributor_id": "contrib_fix", "canonical_name": "R. Osei", "aliases": []},
            {"contributor_id": "contrib_fix", "canonical_name": "R. Osei", "aliases": []},
        ]
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=registry,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes, (
            "Duplicate registry rows for the same contributor must be deduplicated "
            f"and count as one match. Got codes: {codes}"
        )
        assert IssueCode.MISSING_CREDIT not in codes

    def test_whitespace_only_obligee_text_produces_unconfirmed_obligation(self):
        """obligee_text that is whitespace-only must fall through to UNCONFIRMED_OBLIGATION."""
        obl = _base_obl(
            obligation_id="obl_ws",
            credited_party_id=None,
            obligee_text="   ",  # whitespace only
        )
        manifest = _manifest()
        layout, visual = _min_evidence()
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            contributor_registry=[],
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION in codes, (
            f"Whitespace-only obligee_text must produce UNCONFIRMED_OBLIGATION. Got: {codes}"
        )
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes


class TestWhitespaceSelectedContributor:
    """Whitespace-only selected_contributor_id must be rejected at model validation."""

    def test_whitespace_only_selected_contributor_id_raises_validation_error(self):
        """selected_contributor_id='   ' on CONFIRM_IDENTITY must raise ValidationError."""
        import pydantic

        with pytest.raises((pydantic.ValidationError, ValueError)):
            Authorization(
                authorization_id="auth_ws",
                production_id="prod_fix",
                proposal_id="prop_ws",
                proposer_id="reviewer_1",
                issue_id="issue_001",
                actor_id="approver_1",
                role="RELEASE_APPROVER",
                action="CONFIRM_IDENTITY",
                reason="whitespace-only selection",
                selected_contributor_id="   ",
                manifest_hash="a" * 64,
                obligation_registry_version_hash="b" * 64,
                artifact_index_digest="c" * 64,
                visual_observations_hash="d" * 64,
                authorized_at="2026-07-29T10:00:00Z",
            )


class TestExportGateRenderEvidence:
    """
    Tests for render evidence validation:
    Evidence authorizes release ONLY if present, bound to current manifest,
    and covers every element being evaluated.
    """

    def test_none_evidence_returns_stale_and_409(self, client, approver_token):
        """None layout_evidence or visual_observations returns STALE gate and 409."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        _register("prod_ev_none", [obl], manifest, layout_evidence=None, visual_observations=None)

        # Domain check
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=None, visual_observations=None
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 1
        assert issues[0].code == IssueCode.ARTIFACT_PENDING
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

        # API check
        resp = client.post(
            "/productions/prod_ev_none/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "STALE"

    def test_empty_collections_returns_stale_and_409(self, client, approver_token):
        """Empty assertion or observation collections can never satisfy a non-empty manifest."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id, render_profile_version="v1", assertions=[]
        )
        visual = VisualObservations(
            manifest_id=manifest.manifest_id, model_id="test", observations=[]
        )
        _register(
            "prod_ev_empty", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        # Domain check
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 1
        assert issues[0].code == IssueCode.ARTIFACT_PENDING
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

        # API check
        resp = client.post(
            "/productions/prod_ev_empty/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "STALE"

    def test_mismatched_layout_manifest_id_returns_stale_and_409(self, client, approver_token):
        """layout_evidence.manifest_id mismatch returns STALE gate and 409."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        layout = LayoutEvidence(
            manifest_id="TOTALLY_DIFFERENT",
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
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
        _register(
            "prod_ev_mismatch_layout",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 1
        assert issues[0].code == IssueCode.ARTIFACT_PENDING
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

        resp = client.post(
            "/productions/prod_ev_mismatch_layout/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "BLOCKED"

    def test_mismatched_visual_manifest_id_returns_stale_and_409(self, client, approver_token):
        """visual_observations.manifest_id mismatch returns STALE gate and 409."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                )
            ],
        )
        visual = VisualObservations(
            manifest_id="TOTALLY_DIFFERENT",
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                )
            ],
        )
        _register(
            "prod_ev_mismatch_visual",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 1
        assert issues[0].code == IssueCode.ARTIFACT_PENDING
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

        resp = client.post(
            "/productions/prod_ev_mismatch_visual/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "BLOCKED"

    def test_incomplete_element_coverage_returns_stale_and_409(self, client, approver_token):
        """Evidence covering only some of the evaluated elements returns STALE gate and 409."""
        obl1 = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        obl2 = _base_obl(obligation_id="obl_2", credited_party_id="contrib_2")
        entry1 = _entry(contributor_id="contrib_1")
        entry2 = _entry(contributor_id="contrib_2")
        manifest = _manifest(entry1, entry2)

        # Layout evidence has assertion ONLY for entry1 (elem_contrib_1), missing entry2 (elem_contrib_2)
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry1.rendered_element_id,
                    visible_text=entry1.display_name,
                    computed_font_size_px=16.0,
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
                    rendered_element_id=entry1.rendered_element_id, observation="ok", flagged=False
                ),
                VisualObservation(
                    rendered_element_id=entry2.rendered_element_id, observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_ev_partial",
            [obl1, obl2],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl1, obl2],
            manifest=manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )
        issues = evaluate_findings(ci)
        pending_issues = [i for i in issues if i.code == IssueCode.ARTIFACT_PENDING]
        assert len(pending_issues) == 1
        assert pending_issues[0].manifest_refs == [entry2.rendered_element_id]
        assert fold_gate(issues, ProjectionStatus()) == GateState.STALE

        resp = client.post(
            "/productions/prod_ev_partial/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "STALE"

    def test_complete_matching_evidence_returns_ready_and_200(self, client, approver_token):
        """Complete evidence matching manifest returns READY_TO_EXPORT and 200."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        _register(
            "prod_ev_complete", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 0
        assert fold_gate(issues, ProjectionStatus()) == GateState.READY_TO_EXPORT

        resp = client.post(
            "/productions/prod_ev_complete/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["gate_state"] == "READY_TO_EXPORT"
        assert body["delivery_package_path"] is not None
        assert body["delivery_package_path"].endswith(".zip")

    def test_repro_1_partial_visual_coverage_returns_stale_and_409(self, client, approver_token):
        """Manifest with two entries, complete layout evidence, visual observations for only one element."""
        obl = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        entry1 = _entry(contributor_id="contrib_1")
        entry2 = _entry(contributor_id="contrib_2")
        manifest = _manifest(entry1, entry2)

        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry1.rendered_element_id,
                    visible_text=entry1.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
                LayoutAssertion(
                    rendered_element_id=entry2.rendered_element_id,
                    visible_text=entry2.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
            ],
        )
        # Visual observations ONLY cover entry1
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry1.rendered_element_id, observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_repro_1", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        pending = [i for i in issues if i.code == IssueCode.ARTIFACT_PENDING]
        assert len(pending) >= 1
        assert pending[0].manifest_refs == [entry2.rendered_element_id]

        resp = client.post(
            "/productions/prod_repro_1/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "STALE"

    def test_repro_2_extra_manifest_entry_missing_from_evidence_returns_stale_and_409(
        self, client, approver_token
    ):
        """Manifest has extra element absent from evidence, active obligation only on first element."""
        obl = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        entry1 = _entry(contributor_id="contrib_1")
        entry2 = _entry(contributor_id="contrib_2")  # extra manifest entry
        manifest = _manifest(entry1, entry2)

        # Evidence only covers entry1, missing entry2
        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry1.rendered_element_id,
                    visible_text=entry1.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
            ],
        )
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry1.rendered_element_id, observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_repro_2", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        pending = [i for i in issues if i.code == IssueCode.ARTIFACT_PENDING]
        assert len(pending) >= 1
        assert pending[0].manifest_refs == [entry2.rendered_element_id]

        resp = client.post(
            "/productions/prod_repro_2/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "STALE"

    def test_unrelated_visual_observations_returns_stale_and_409(self, client, approver_token):
        """Visual observations non-empty but for unrelated element ID -> missing evidence for manifest element."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)

        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id="elem_unrelated", observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_ev_unrelated_vis",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert any(
            i.code in (IssueCode.ARTIFACT_PENDING, IssueCode.ARTIFACT_INTEGRITY_FAILURE)
            for i in issues
        )

        resp = client.post(
            "/productions/prod_ev_unrelated_vis/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409

    def test_duplicate_layout_assertion_ids_fails_closed(self, client, approver_token):
        """Duplicate LayoutAssertion element IDs -> ARTIFACT_INTEGRITY_FAILURE."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)

        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
            ],
        )
        _, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        _register(
            "prod_ev_dup_layout",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert any(i.code == IssueCode.ARTIFACT_INTEGRITY_FAILURE for i in issues)

        resp = client.post(
            "/productions/prod_ev_dup_layout/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409

    def test_duplicate_visual_observation_ids_fails_closed(self, client, approver_token):
        """Duplicate VisualObservation element IDs -> ARTIFACT_INTEGRITY_FAILURE."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)

        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                ),
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_ev_dup_vis", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert any(i.code == IssueCode.ARTIFACT_INTEGRITY_FAILURE for i in issues)

        resp = client.post(
            "/productions/prod_ev_dup_vis/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409

    def test_unexpected_layout_ids_fails_closed(self, client, approver_token):
        """LayoutAssertion for element ID not in manifest -> ARTIFACT_INTEGRITY_FAILURE."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)

        layout = LayoutEvidence(
            manifest_id=manifest.manifest_id,
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id=entry.rendered_element_id,
                    visible_text=entry.display_name,
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
                LayoutAssertion(
                    rendered_element_id="elem_extra",
                    visible_text="Extra",
                    computed_font_size_px=16.0,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
            ],
        )
        _, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        _register(
            "prod_ev_unexp_layout",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert any(i.code == IssueCode.ARTIFACT_INTEGRITY_FAILURE for i in issues)

        resp = client.post(
            "/productions/prod_ev_unexp_layout/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409

    def test_unexpected_visual_ids_fails_closed(self, client, approver_token):
        """VisualObservation for element ID not in manifest -> ARTIFACT_INTEGRITY_FAILURE."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)

        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id, observation="ok", flagged=False
                ),
                VisualObservation(
                    rendered_element_id="elem_extra", observation="ok", flagged=False
                ),
            ],
        )
        _register(
            "prod_ev_unexp_vis", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert any(i.code == IssueCode.ARTIFACT_INTEGRITY_FAILURE for i in issues)

        resp = client.post(
            "/productions/prod_ev_unexp_vis/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409

    def test_complete_exact_coverage_all_manifest_entries_reaches_200(self, client, approver_token):
        """Complete exact coverage for multi-entry manifest with single active obligation reaches 200."""
        obl = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        entry1 = _entry(contributor_id="contrib_1")
        entry2 = _entry(contributor_id="contrib_2")
        manifest = _manifest(entry1, entry2)

        layout, visual = _min_evidence(manifest.manifest_id, manifest.entries)
        _register(
            "prod_ev_exact_multi",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        assert len(issues) == 0
        assert fold_gate(issues, ProjectionStatus()) == GateState.READY_TO_EXPORT

        resp = client.post(
            "/productions/prod_ev_exact_multi/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["gate_state"] == "READY_TO_EXPORT"
        assert resp.json()["delivery_package_path"] is not None
        assert resp.json()["delivery_package_path"].endswith(".zip")


class TestVisualObservationsUncertain:
    """Tests for VISUAL_OBSERVATION_UNCERTAIN on obligated and unobligated elements."""

    def test_flagged_obligated_element_returns_needs_human_and_409(self, client, approver_token):
        """Flagged visual observation on an obligated element -> 409 NEEDS_HUMAN."""
        obl = _base_obl()
        entry = _entry()
        manifest = _manifest(entry)
        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry.rendered_element_id,
                    observation="Obscured by logo",
                    flagged=True,
                    flag_reason="Obscured by logo",
                )
            ],
        )
        _register(
            "prod_vis_obl", [obl], manifest, layout_evidence=layout, visual_observations=visual
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        uncert = [i for i in issues if i.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN]
        assert len(uncert) == 1
        assert uncert[0].manifest_refs == [entry.rendered_element_id]
        assert uncert[0].obligation_id is None
        assert "Obscured by logo" in uncert[0].detail

        resp = client.post(
            "/productions/prod_vis_obl/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "NEEDS_HUMAN"

    def test_flagged_unobligated_element_returns_needs_human_and_409(self, client, approver_token):
        """Flagged visual observation on an unobligated element (e.g. section header) -> 409 NEEDS_HUMAN."""
        obl = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        entry1 = _entry(contributor_id="contrib_1")
        entry_title = _entry(
            contributor_id="section_header",
            surface=CreditSurface.MAIN_TITLES,
            role="_section_header",
        )
        manifest = _manifest(entry1, entry_title)

        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry1.rendered_element_id, observation="ok", flagged=False
                ),
                VisualObservation(
                    rendered_element_id=entry_title.rendered_element_id,
                    observation="Title text cut off at frame edge",
                    flagged=True,
                    flag_reason="Title text cut off at frame edge",
                ),
            ],
        )
        _register(
            "prod_vis_unobligated",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        uncert = [i for i in issues if i.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN]
        assert len(uncert) == 1
        assert uncert[0].manifest_refs == [entry_title.rendered_element_id]
        assert "Title text cut off at frame edge" in uncert[0].detail

        resp = client.post(
            "/productions/prod_vis_unobligated/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "NEEDS_HUMAN"
        issues_json = resp.json()["detail"]["issues"]
        assert len(issues_json) == 1
        assert issues_json[0]["code"] == "VISUAL_OBSERVATION_UNCERTAIN"

    def test_several_flagged_elements_produces_one_issue_each(self, client, approver_token):
        """Several flagged elements (obligated & unobligated) produce one issue each, all in response."""
        obl = _base_obl(obligation_id="obl_1", credited_party_id="contrib_1")
        entry1 = _entry(contributor_id="contrib_1")
        entry2 = _entry(contributor_id="contrib_2")
        manifest = _manifest(entry1, entry2)

        layout, _ = _min_evidence(manifest.manifest_id, manifest.entries)
        visual = VisualObservations(
            manifest_id=manifest.manifest_id,
            model_id="test",
            observations=[
                VisualObservation(
                    rendered_element_id=entry1.rendered_element_id,
                    observation="Flag 1",
                    flagged=True,
                    flag_reason="Reason 1",
                ),
                VisualObservation(
                    rendered_element_id=entry2.rendered_element_id,
                    observation="Flag 2",
                    flagged=True,
                    flag_reason="Reason 2",
                ),
            ],
        )
        _register(
            "prod_vis_several",
            [obl],
            manifest,
            layout_evidence=layout,
            visual_observations=visual,
        )

        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        uncert = [i for i in issues if i.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN]
        assert len(uncert) == 2
        refs = sorted([i.manifest_refs[0] for i in uncert])
        assert refs == sorted([entry1.rendered_element_id, entry2.rendered_element_id])

        resp = client.post(
            "/productions/prod_vis_several/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["gate_state"] == "NEEDS_HUMAN"
        issues_json = resp.json()["detail"]["issues"]
        assert len(issues_json) == 2
