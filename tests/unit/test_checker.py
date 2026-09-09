"""
Failing tests for the deterministic compliance checker.
Run before implementing checker.py to confirm red state.
"""

import json
import pathlib

from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import (
    CreditManifest,
    GateState,
    IssueCode,
    LayoutAssertion,
    LayoutEvidence,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    VisualObservation,
    VisualObservations,
)

FIXTURES_DEV = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "benchmark" / "dev"
FIXTURES_SEALED = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "benchmark" / "sealed"
FIXTURES_DEMO = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "demo"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def _empty_evidence(
    manifest_id: str, entries: list[ManifestEntry] | None = None
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
        LayoutEvidence(manifest_id=manifest_id, render_profile_version="v1", assertions=assertions),
        VisualObservations(manifest_id=manifest_id, model_id="test", observations=observations),
    )


def _run_case(data: dict) -> tuple[list, GateState]:
    obligations = [Obligation(**o) for o in data["obligations"]]
    manifest = CreditManifest(**data["manifest"])
    layout = LayoutEvidence(**data["layout_evidence"]) if data.get("layout_evidence") else None
    visual = (
        VisualObservations(**data["visual_observations"])
        if data.get("visual_observations")
        else None
    )
    if layout is None:
        layout, _ = _empty_evidence(manifest.manifest_id, manifest.entries)
    else:
        existing_lids = {a.rendered_element_id for a in layout.assertions}
        for entry in manifest.entries:
            if entry.rendered_element_id not in existing_lids:
                layout.assertions.append(
                    LayoutAssertion(
                        rendered_element_id=entry.rendered_element_id,
                        visible_text=entry.display_name,
                        computed_font_size_px=16.0,
                        bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 20.0},
                        frame_index=0,
                    )
                )

    if visual is None:
        _, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
    else:
        existing_vids = {o.rendered_element_id for o in visual.observations}
        for entry in manifest.entries:
            if entry.rendered_element_id not in existing_vids:
                visual.observations.append(
                    VisualObservation(
                        rendered_element_id=entry.rendered_element_id,
                        observation="ok",
                        flagged=False,
                    )
                )
    registry = data.get("contributor_registry", [])

    checker_input = CheckerInput(
        obligations=obligations,
        manifest=manifest,
        layout_evidence=layout,
        visual_observations=visual,
        contributor_registry=registry,
    )
    issues = evaluate_findings(checker_input)
    gate = fold_gate(issues, ProjectionStatus())
    return issues, gate


class TestCheckerDevCases:
    def test_dev_001_clean(self):
        data = _load(FIXTURES_DEV / "dev_001_clean.json")
        issues, gate = _run_case(data)
        assert gate == GateState.READY_TO_EXPORT
        assert issues == []

    def test_dev_002_missing_credit(self):
        data = _load(FIXTURES_DEV / "dev_002_missing_credit.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert gate == GateState.BLOCKED

    def test_dev_003_text_mismatch(self):
        data = _load(FIXTURES_DEV / "dev_003_text_mismatch.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        assert gate == GateState.BLOCKED

    def test_dev_004_ambiguous_identity(self):
        data = _load(FIXTURES_DEV / "dev_004_ambiguous_identity.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY in codes
        assert IssueCode.MISSING_CREDIT not in codes
        assert gate == GateState.NEEDS_HUMAN

    def test_dev_005_grouping_mismatch(self):
        data = _load(FIXTURES_DEV / "dev_005_grouping_mismatch.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_GROUPING_MISMATCH in codes
        assert gate == GateState.BLOCKED

    def test_dev_006_position_mismatch(self):
        data = _load(FIXTURES_DEV / "dev_006_position_mismatch.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_POSITION_MISMATCH in codes
        assert gate == GateState.BLOCKED

    def test_dev_007_size_mismatch(self):
        data = _load(FIXTURES_DEV / "dev_007_size_mismatch.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_SIZE_MISMATCH in codes
        assert gate == GateState.BLOCKED

    def test_dev_008_unconfirmed(self):
        data = _load(FIXTURES_DEV / "dev_008_unconfirmed.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION in codes
        assert gate == GateState.NEEDS_CONFIRMATION

    def test_demo_production_three_issues(self):
        """Demo fixture: MISSING_CREDIT (obl_001), ARTIFACT_TEXT_MISMATCH (obl_002),
        AMBIGUOUS_IDENTITY (obl_003 via obligee_text path, ≥2 registry matches)."""
        data = _load(FIXTURES_DEMO / "demo_production.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        assert IssueCode.AMBIGUOUS_IDENTITY in codes
        assert gate == GateState.BLOCKED


class TestCheckerSizeTolerance:
    """Verify the 0.1 float tolerance rule: subject_px + 0.1 >= min_ratio * ref_px."""

    def _make_size_case(self, subject_px: float, ref_px: float, ratio: float) -> CheckerInput:
        from creditlock.domain.models import (
            CreditManifest,
            CreditSurface,
            LayoutAssertion,
            LayoutEvidence,
            ManifestEntry,
            Obligation,
            SizeComparisonBasis,
            SourceSpan,
        )

        obl = Obligation(
            obligation_id="obl_size",
            production_id="p1",
            credited_party_id="c1",
            credit_surface=CreditSurface.MAIN_TITLES,
            minimum_relative_size=ratio,
            size_reference={"rendered_element_id": "ref_elem"},
            size_comparison_basis=SizeComparisonBasis.COMPUTED_FONT_SIZE_PX,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=5, quote="test"),
            source_hash="a" * 64,
            agent_reported_confidence=0.9,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_c1",
            contributor_id="c1",
            display_name="X",
            role="Y",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="p1",
            delivery_version_id="v1",
        )
        ref_entry = ManifestEntry(
            rendered_element_id="ref_elem",
            contributor_id="ref",
            display_name="REF",
            role="ref",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=0,
            production_id="p1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="m1",
            production_id="p1",
            delivery_version_id="v1",
            entries=[entry, ref_entry],
        )
        layout = LayoutEvidence(
            manifest_id="m1",
            render_profile_version="v1",
            assertions=[
                LayoutAssertion(
                    rendered_element_id="elem_c1",
                    visible_text="X",
                    computed_font_size_px=subject_px,
                    bounding_box={"x": 0, "y": 0, "w": 100, "h": 20},
                    frame_index=0,
                ),
                LayoutAssertion(
                    rendered_element_id="ref_elem",
                    visible_text="REF",
                    computed_font_size_px=ref_px,
                    bounding_box={"x": 0, "y": 0, "w": 200, "h": 40},
                    frame_index=0,
                ),
            ],
        )
        _, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
        return CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )

    def test_size_exactly_at_threshold_passes(self):
        # 20.0 + 0.1 >= 0.5 * 40.0 => 20.1 >= 20.0 => PASS
        ci = self._make_size_case(20.0, 40.0, 0.5)
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_SIZE_MISMATCH not in codes

    def test_size_just_below_threshold_fails(self):
        # 19.8 + 0.1 = 19.9, 19.9 < 20.0 => FAIL
        ci = self._make_size_case(19.8, 40.0, 0.5)
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_SIZE_MISMATCH in codes

    def test_size_tolerance_boundary(self):
        # 19.9 + 0.1 = 20.0 >= 20.0 => PASS (exactly on boundary)
        ci = self._make_size_case(19.9, 40.0, 0.5)
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_SIZE_MISMATCH not in codes


class TestCheckerNfcNormalization:
    """required_display_text comparison uses NFC normalization on both sides."""

    def test_nfc_nfd_display_text_passes(self):
        from creditlock.domain.models import (
            CreditManifest,
            CreditSurface,
            ManifestEntry,
            Obligation,
            SourceSpan,
        )

        # required_display_text is NFC "café"; manifest has NFD "cafe\u0301"
        obl = Obligation(
            obligation_id="obl_nfc",
            production_id="p1",
            credited_party_id="c1",
            required_display_text="caf\u00e9",  # NFC
            credit_surface=CreditSurface.END_CARDS,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=5, quote="café"),
            source_hash="a" * 64,
            agent_reported_confidence=0.9,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_c1",
            contributor_id="c1",
            display_name="cafe\u0301",  # NFD — should normalize to same as NFC
            role="Y",
            credit_surface=CreditSurface.END_CARDS,
            ordinal_position=1,
            production_id="p1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="m1",
            production_id="p1",
            delivery_version_id="v1",
            entries=[entry],
        )
        layout, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH not in codes


class TestCheckerDurationObligation:
    """Duration obligations emit UNSUPPORTED_PRESENTATION_ASSERTION."""

    def test_duration_obligation_emits_unsupported(self):
        from creditlock.domain.models import (
            CreditManifest,
            CreditSurface,
            ManifestEntry,
            Obligation,
            SourceSpan,
        )

        obl = Obligation(
            obligation_id="obl_dur",
            production_id="p1",
            credited_party_id="c1",
            minimum_visible_duration_ms=3000,
            credit_surface=CreditSurface.MAIN_TITLES,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=5, quote="3 second hold"),
            source_hash="a" * 64,
            agent_reported_confidence=0.9,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_c1",
            contributor_id="c1",
            display_name="X",
            role="Y",
            credit_surface=CreditSurface.MAIN_TITLES,
            ordinal_position=1,
            production_id="p1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="m1",
            production_id="p1",
            delivery_version_id="v1",
            entries=[entry],
        )
        layout, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION in codes


class TestCheckerVisualObservations:
    """Flagged visual observations emit VISUAL_OBSERVATION_UNCERTAIN."""

    def test_flagged_visual_observation_emits_issue(self):
        data = _load(FIXTURES_SEALED / "sealed_004_visual_mismatch.json")
        issues, gate = _run_case(data)
        codes = [i.code for i in issues]
        assert IssueCode.VISUAL_OBSERVATION_UNCERTAIN in codes
        assert gate == GateState.NEEDS_HUMAN

    def test_unflagged_visual_observation_no_issue(self):
        from creditlock.domain.models import (
            CreditManifest,
            CreditSurface,
            ManifestEntry,
            Obligation,
            SourceSpan,
            VisualObservation,
            VisualObservations,
        )

        obl = Obligation(
            obligation_id="obl_v",
            production_id="p1",
            credited_party_id="c1",
            required_display_text="X",
            credit_surface=CreditSurface.END_CARDS,
            source_document_id="doc1",
            source_document_version=1,
            source_span=SourceSpan(page=1, start_char=0, end_char=1, quote="X"),
            source_hash="a" * 64,
            agent_reported_confidence=0.9,
            extraction_model_id="gemini",
            prompt_version="v1",
            status=ObligationStatus.ACTIVE,
        )
        entry = ManifestEntry(
            rendered_element_id="elem_c1",
            contributor_id="c1",
            display_name="X",
            role="Y",
            credit_surface=CreditSurface.END_CARDS,
            ordinal_position=1,
            production_id="p1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="m1",
            production_id="p1",
            delivery_version_id="v1",
            entries=[entry],
        )
        visual = VisualObservations(
            manifest_id="m1",
            model_id="gemini",
            observations=[
                VisualObservation(
                    rendered_element_id="elem_c1",
                    observation="Credit clearly visible",
                    flagged=False,
                )
            ],
        )
        layout, _ = _empty_evidence("m1", manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        assert IssueCode.VISUAL_OBSERVATION_UNCERTAIN not in codes


class TestCheckerAliasResolution:
    """
    AliasRecord.alias is a source-name string (as it appears in a contract),
    not a contributor-id redirect.  Rule 1 (credited_party_id) resolves only
    by exact manifest contributor_id match.  The alias field is used by rule-2
    (obligee_text) to match contract text strings.

    A credited_party_id not in the manifest is always MISSING_CREDIT, even
    when the registry has an entry for it with a matching alias string.
    """

    def test_credited_party_id_not_in_manifest_is_missing_credit(self):
        """Rule 1: credited_party_id absent from manifest → MISSING_CREDIT.
        Presence of a source-name alias in the registry changes nothing."""
        from creditlock.domain.models import (
            CreditManifest,
            CreditSurface,
            ManifestEntry,
            Obligation,
            SourceSpan,
        )

        obl = Obligation(
            obligation_id="obl_alias",
            production_id="p1",
            credited_party_id="contrib_unresolved",
            credit_surface=CreditSurface.END_CARDS,
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
            rendered_element_id="elem_c1",
            contributor_id="contrib_david_park",
            display_name="David Park",
            role="Associate Producer",
            credit_surface=CreditSurface.END_CARDS,
            ordinal_position=1,
            production_id="p1",
            delivery_version_id="v1",
        )
        manifest = CreditManifest(
            manifest_id="m1",
            production_id="p1",
            delivery_version_id="v1",
            entries=[entry],
        )
        # Registry has a source-name alias "D. Park" — this does NOT redirect
        # credited_party_id to contrib_david_park; it is a document-text string.
        registry = [
            {
                "contributor_id": "contrib_unresolved",
                "canonical_name": "D. Park",
                "aliases": [
                    {
                        "alias": "D. Park",
                        "registered_by": "reviewer_1",
                        "registered_at": "2026-07-29T10:00:00Z",
                        "reason": "name as written in source clause",
                    }
                ],
            }
        ]
        layout, visual = _empty_evidence("m1", manifest.entries)
        ci = CheckerInput(
            obligations=[obl],
            manifest=manifest,
            layout_evidence=layout,
            visual_observations=visual,
            contributor_registry=registry,
        )
        issues = evaluate_findings(ci)
        codes = [i.code for i in issues]
        # credited_party_id "contrib_unresolved" is not in manifest → MISSING_CREDIT
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes


# ── Task B: identity classifier rules + issue-id discriminator ────────────────


def _obl(
    *,
    obligation_id="obl_t",
    credited_party_id="cid",
    obligee_text=None,
    required_display_text=None,
    role_label=None,
    surface=None,
    **kwargs,
):
    from creditlock.domain.models import CreditSurface, SourceSpan

    surface = surface or CreditSurface.MAIN_TITLES
    defaults = {
        "obligation_id": obligation_id,
        "production_id": "prod_t",
        "credited_party_id": credited_party_id,
        "obligee_text": obligee_text,
        "required_display_text": required_display_text,
        "role_label": role_label,
        "credit_surface": surface,
        "source_document_id": "doc_t",
        "source_document_version": 1,
        "source_span": SourceSpan(page=1, start_char=0, end_char=5, quote="q"),
        "source_hash": "a" * 64,
        "agent_reported_confidence": 0.9,
        "extraction_model_id": "gemini",
        "prompt_version": "v1",
        "status": ObligationStatus.ACTIVE,
    }
    defaults.update(kwargs)
    return Obligation(**defaults)


def _entry_t(contributor_id="cid", display_name="Name", role="Role", surface=None, ordinal=1):
    from creditlock.domain.models import CreditSurface

    surface = surface or CreditSurface.MAIN_TITLES
    return ManifestEntry(
        rendered_element_id=f"elem_{contributor_id}",
        contributor_id=contributor_id,
        display_name=display_name,
        role=role,
        credit_surface=surface,
        ordinal_position=ordinal,
        production_id="prod_t",
        delivery_version_id="v1",
    )


def _manifest_t(*entries):
    return CreditManifest(
        manifest_id="mfst_t",
        production_id="prod_t",
        delivery_version_id="v1",
        entries=list(entries),
    )


def _run_t(obligations, manifest, registry=None):
    layout, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
    ci = CheckerInput(
        obligations=obligations,
        manifest=manifest,
        layout_evidence=layout,
        visual_observations=visual,
        contributor_registry=registry or [],
    )
    issues = evaluate_findings(ci)
    from creditlock.domain.gate import ProjectionStatus, fold_gate

    gate = fold_gate(issues, ProjectionStatus())
    return issues, gate


class TestIdentityRules:
    """
    Task B: three identity classifier rules.

    Rule 1  credited_party_id set → only resolution path is exact match or
            registered alias to a manifest contributor_id.  If that fails,
            MISSING_CREDIT — always.  Registry membership is irrelevant.
            Surface population is irrelevant.  Absence is never ambiguity.

    Rule 2  credited_party_id null, obligee_text set → resolve text against
            registry (NFC canonical_name or alias string exact match).
            0 or ≥2 registry-manifest matches → AMBIGUOUS_IDENTITY.
            Exactly 1 → bind and run all normal field checks.

    Rule 3  credited_party_id null, obligee_text null → UNCONFIRMED_OBLIGATION.
            The old "if credited_party_id is None: continue" silent pass is gone.
    """

    # ── Rule 1 ────────────────────────────────────────────────────────────────

    def test_rule1_absent_no_registry_is_missing_credit(self):
        """Party not in manifest, registry empty → MISSING_CREDIT."""
        obl = _obl(credited_party_id="cid_absent")
        issues, gate = _run_t([obl], _manifest_t())
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert gate == GateState.BLOCKED

    def test_rule1_absent_in_registry_empty_surface_is_missing_credit(self):
        """Party in registry, surface has no entries → MISSING_CREDIT (not ambiguous)."""
        obl = _obl(credited_party_id="cid_absent")
        registry = [
            {"contributor_id": "cid_absent", "canonical_name": "Absent Person", "aliases": []}
        ]
        issues, gate = _run_t([obl], _manifest_t(), registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert gate == GateState.BLOCKED

    def test_rule1_absent_in_registry_populated_surface_is_still_missing_credit(self):
        """
        THE BACKDOOR TEST.
        Party IS in the registry.  Surface HAS other entries.  Party is absent.
        Must classify as MISSING_CREDIT (BLOCKED), not AMBIGUOUS_IDENTITY.
        This is the most common real defect: someone was simply never added to
        the roll.  Under the old buggy logic it classified as AMBIGUOUS_IDENTITY
        and could be authorized away.
        """
        obl = _obl(credited_party_id="cid_line_producer")
        manifest = _manifest_t(
            _entry_t("cid_director"),
            _entry_t("cid_dp"),
        )
        registry = [
            {"contributor_id": "cid_line_producer", "canonical_name": "R. Osei", "aliases": []}
        ]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes, (
            f"Party in registry + populated surface + absent credit must be "
            f"MISSING_CREDIT, got {codes}"
        )
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert gate == GateState.BLOCKED

    def test_rule1_exact_match_in_manifest_resolves(self):
        """credited_party_id that exactly matches a manifest contributor_id → resolves."""
        obl = _obl(credited_party_id="cid_exact")
        entry = _entry_t("cid_exact")
        manifest = _manifest_t(entry)
        issues, _gate = _run_t([obl], manifest)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT not in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_rule1_source_name_alias_does_not_redirect_contributor_id(self):
        """AliasRecord.alias is a source-name string, not a contributor-id redirect.
        A registry entry with alias='cid_full' on contributor 'cid_short' does NOT
        make 'cid_short' resolve to 'cid_full' in the manifest."""
        obl = _obl(credited_party_id="cid_short")
        entry = _entry_t("cid_full")
        manifest = _manifest_t(entry)
        registry = [
            {
                "contributor_id": "cid_short",
                "canonical_name": "D. Park",
                "aliases": [
                    {
                        "alias": "cid_full",
                        "registered_by": "r1",
                        "registered_at": "2026-07-29T00:00:00Z",
                        "reason": "source name string",
                    },
                ],
            }
        ]
        issues, _gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        # cid_short not in manifest → MISSING_CREDIT; alias does not redirect
        assert IssueCode.MISSING_CREDIT in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_rule1_missing_credit_not_clearable_by_authorization(
        self, client=None, approver_token=None
    ):
        """
        MISSING_CREDIT is BLOCKED — a valid hash-bound authorization recorded
        for the issue_id must still return 409 from the export API.
        Tested inline against checker; the API-level version is in
        TestBackdoorApiGuard in test_export_api.py.
        """
        obl = _obl(credited_party_id="cid_line_producer")
        manifest = _manifest_t(_entry_t("cid_director"))
        registry = [
            {"contributor_id": "cid_line_producer", "canonical_name": "R. Osei", "aliases": []}
        ]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes
        assert gate == GateState.BLOCKED
        # Checker-level: ensure the issue code maps to BLOCKED in ISSUE_GATE_MAP
        from creditlock.domain.models import ISSUE_GATE_MAP
        from creditlock.domain.models import GateState as GS

        assert ISSUE_GATE_MAP[IssueCode.MISSING_CREDIT] == GS.BLOCKED

    # ── Rule 2 ────────────────────────────────────────────────────────────────

    def test_rule2_zero_registry_matches_is_ambiguous(self):
        """obligee_text present, nothing in registry matches → AMBIGUOUS_IDENTITY."""
        obl = _obl(credited_party_id=None, obligee_text="D. Park")
        entry = _entry_t("cid_david_park")
        issues, gate = _run_t([obl], _manifest_t(entry), registry=[])
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY in codes
        assert gate == GateState.NEEDS_HUMAN

    def test_rule2_multiple_registry_matches_is_ambiguous(self):
        """obligee_text matches two registry entries → AMBIGUOUS_IDENTITY."""
        obl = _obl(credited_party_id=None, obligee_text="Park")
        manifest = _manifest_t(_entry_t("cid_a"), _entry_t("cid_b"))
        registry = [
            {"contributor_id": "cid_a", "canonical_name": "Park", "aliases": []},
            {"contributor_id": "cid_b", "canonical_name": "Park", "aliases": []},
        ]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY in codes
        assert gate == GateState.NEEDS_HUMAN

    def test_rule2_exact_canonical_match_binds_and_checks_fields(self):
        """obligee_text matches canonical_name exactly → binds, field checks run."""
        obl = _obl(
            credited_party_id=None,
            obligee_text="David Park",
            required_display_text="David Park",
            role_label="Director",
        )
        entry = _entry_t("cid_david_park", display_name="David Park", role="Director")
        manifest = _manifest_t(entry)
        registry = [
            {"contributor_id": "cid_david_park", "canonical_name": "David Park", "aliases": []}
        ]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert IssueCode.MISSING_CREDIT not in codes
        assert gate == GateState.READY_TO_EXPORT

    def test_rule2_alias_string_match_binds(self):
        """obligee_text matches a registry alias string (NFC) → binds, no ambiguity."""
        obl = _obl(
            credited_party_id=None,
            obligee_text="D. Park",
            required_display_text="David Park",
        )
        entry = _entry_t("cid_david_park", display_name="David Park")
        manifest = _manifest_t(entry)
        registry = [
            {
                "contributor_id": "cid_david_park",
                "canonical_name": "David Park",
                "aliases": [
                    {
                        "alias": "D. Park",
                        "registered_by": "r1",
                        "registered_at": "2026-07-29T00:00:00Z",
                        "reason": "short form",
                    },
                ],
            }
        ]
        issues, _gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert IssueCode.MISSING_CREDIT not in codes

    def test_rule2_bound_party_field_mismatch_fires_normally(self):
        """After obligee_text binds a party, field mismatches are still detected."""
        obl = _obl(
            credited_party_id=None,
            obligee_text="David Park",
            required_display_text="WRONG NAME",
        )
        entry = _entry_t("cid_david_park", display_name="David Park")
        manifest = _manifest_t(entry)
        registry = [
            {"contributor_id": "cid_david_park", "canonical_name": "David Park", "aliases": []}
        ]
        issues, _gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.ARTIFACT_TEXT_MISMATCH in codes
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    def test_rule2_nfc_normalization_applied(self):
        """obligee_text NFC-normalized against canonical_name NFC-normalized."""
        # canonical_name stored as NFD; obligee_text as NFC — should still match
        obl = _obl(credited_party_id=None, obligee_text="caf\u00e9")  # NFC é
        entry = _entry_t("cid_cafe")
        manifest = _manifest_t(entry)
        registry = [
            {
                "contributor_id": "cid_cafe",
                "canonical_name": "cafe\u0301",  # NFD é
                "aliases": [],
            }
        ]
        issues, _ = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes

    # ── Rule 3 ────────────────────────────────────────────────────────────────

    def test_rule3_null_party_null_text_active_is_unconfirmed(self):
        """ACTIVE obligation with no party and no text → UNCONFIRMED_OBLIGATION."""
        obl = _obl(credited_party_id=None, obligee_text=None)
        issues, gate = _run_t([obl], _manifest_t())
        codes = [i.code for i in issues]
        assert IssueCode.UNCONFIRMED_OBLIGATION in codes
        assert gate == GateState.NEEDS_CONFIRMATION

    def test_rule3_not_silently_skipped(self):
        """Old silent-pass is deleted: null+null ACTIVE must not produce zero issues."""
        obl = _obl(credited_party_id=None, obligee_text=None)
        issues, _ = _run_t([obl], _manifest_t())
        assert len(issues) > 0, (
            "ACTIVE obligation with null credited_party_id and null obligee_text "
            "must not be silently skipped — expected UNCONFIRMED_OBLIGATION"
        )

    # ── Issue-id discriminator ─────────────────────────────────────────────────

    def test_two_text_mismatches_same_obligation_get_distinct_issue_ids(self):
        """
        display_name mismatch and role mismatch on the same obligation must produce
        two ARTIFACT_TEXT_MISMATCH issues with *different* issue_ids.
        The old _stable_issue_id(obl_id, code) would hash to the same value for both.
        """
        obl = _obl(
            required_display_text="Wrong Name",
            role_label="Wrong Role",
        )
        entry = _entry_t(display_name="Right Name", role="Right Role")
        manifest = _manifest_t(entry)
        issues, _ = _run_t([obl], manifest)
        text_issues = [i for i in issues if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
        assert len(text_issues) == 2, (
            f"Expected 2 ARTIFACT_TEXT_MISMATCH issues (display_name + role), "
            f"got {len(text_issues)}"
        )
        ids = [i.issue_id for i in text_issues]
        assert ids[0] != ids[1], (
            "display_name mismatch and role mismatch on the same obligation must "
            "produce distinct issue_ids — add a discriminator to _stable_issue_id"
        )

    def test_same_violation_same_id_across_evaluations(self):
        """issue_id is stable: same inputs always produce the same id."""
        obl = _obl(required_display_text="Wrong")
        entry = _entry_t(display_name="Right")
        manifest = _manifest_t(entry)
        layout, visual = _empty_evidence(manifest.manifest_id, manifest.entries)
        ci = CheckerInput(
            obligations=[obl], manifest=manifest, layout_evidence=layout, visual_observations=visual
        )
        issues1 = evaluate_findings(ci)
        issues2 = evaluate_findings(ci)
        ids1 = [i.issue_id for i in issues1]
        ids2 = [i.issue_id for i in issues2]
        assert ids1 == ids2

    # ── Task B rule-2 fix: registry-only resolution, two-stage ───────────────

    def test_rule2_unique_registry_match_absent_from_manifest_is_missing_credit(self):
        """
        obligee_text resolves uniquely (1 match) in the registry, but that
        registry party is absent from the manifest → stage-2 presence check
        must fire MISSING_CREDIT, not AMBIGUOUS_IDENTITY.

        Old broken code: manifest-filter in _resolve_obligee_text turned this
        into 0 manifest matches → AMBIGUOUS_IDENTITY (authorizable backdoor).
        """
        obl = _obl(credited_party_id=None, obligee_text="D. Park")
        # Manifest has a different person — D. Park is absent
        manifest = _manifest_t(_entry_t("cid_director"))
        # Registry resolves "D. Park" uniquely to cid_dp
        registry = [{"contributor_id": "cid_dp", "canonical_name": "D. Park", "aliases": []}]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.MISSING_CREDIT in codes, (
            f"obligee_text resolving uniquely to an absent party must be "
            f"MISSING_CREDIT, not AMBIGUOUS_IDENTITY. Got: {codes}"
        )
        assert IssueCode.AMBIGUOUS_IDENTITY not in codes
        assert gate == GateState.BLOCKED

    def test_rule2_two_registry_matches_one_in_manifest_is_ambiguous(self):
        """
        obligee_text matches two registry parties; only one is in the manifest.
        Old broken code: manifest-filter silenced the ambiguity and 'bound'
        the single manifest match.  Correct: AMBIGUOUS_IDENTITY because the
        registry itself is ambiguous — we cannot know which party the contract
        meant.
        """
        obl = _obl(credited_party_id=None, obligee_text="Park")
        # Only cid_a is in the manifest
        manifest = _manifest_t(_entry_t("cid_a"))
        registry = [
            {"contributor_id": "cid_a", "canonical_name": "Park", "aliases": []},
            {"contributor_id": "cid_b", "canonical_name": "Park", "aliases": []},
        ]
        issues, gate = _run_t([obl], manifest, registry=registry)
        codes = [i.code for i in issues]
        assert IssueCode.AMBIGUOUS_IDENTITY in codes, (
            f"Two registry matches must remain AMBIGUOUS_IDENTITY even when only "
            f"one is in the manifest. Got: {codes}"
        )
        assert gate == GateState.NEEDS_HUMAN
