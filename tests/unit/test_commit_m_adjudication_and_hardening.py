from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from creditlock.agents.steward import StewardAgent, is_admissible_steward_issue
from creditlock.domain.models import (
    CreditManifest,
    CreditSurface,
    Issue,
    IssueCode,
    Obligation,
    ObligationStatus,
    SourceSpan,
)
from creditlock.eval.v2_models import ComponentEvalV2Result
from creditlock.eval.v2_runner import (
    validate_fixtures_only,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_commit_l_evidence_sha256_unchanged():
    """Verify Commit L evidence file is preserved byte-for-byte."""
    p = PROJECT_ROOT / "docs" / "evidence" / "live-v2-frozen-challenge-v2.json"
    assert p.exists(), "Commit L evidence file missing"
    content = p.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    assert digest == "d1939a8a98a286bd179a8a5b6bce2be9a5d24d25ecfe3b3a344ecfd189b3b8be"


def test_adjudication_sidecar_contents():
    """Verify adjudication sidecar lists defects, affected cases, and immutable statements."""
    p = PROJECT_ROOT / "docs" / "evidence" / "live-v2-frozen-challenge-v2-adjudication.json"
    assert p.exists(), "Adjudication sidecar missing"
    data = json.loads(p.read_text(encoding="utf-8"))

    assert data["original_evidence_sha256"] == "d1939a8a98a286bd179a8a5b6bce2be9a5d24d25ecfe3b3a344ecfd189b3b8be"
    assert data["original_quality_gate_status"] == "FAILED_QUALITY_GATE"
    assert data["adjudication_status"] == "INVALID_FIXTURE_ANNOTATIONS"
    assert "immutable" in data["immutable_evidence_statement"].lower()

    defects = {d["defect_id"]: d for d in data["defects"]}
    assert "EXTRACTOR_GOLD_SPAN_ANNOTATION_MISMATCH" in defects
    assert "STEWARD_ISSUE_CODE_ADMISSIBILITY_MISMATCH" in defects
    assert "FIXTURE_SET_DIGEST_AMBIGUITY" in defects

    ext_defect = defects["EXTRACTOR_GOLD_SPAN_ANNOTATION_MISMATCH"]
    assert len(ext_defect["affected_case_ids"]) == 8
    assert ext_defect["invalid_spans_count"] == 13

    stw_defect = defects["STEWARD_ISSUE_CODE_ADMISSIBILITY_MISMATCH"]
    assert len(stw_defect["affected_case_ids"]) == 4
    assert stw_defect["invalid_positive_cases_count"] == 4


def test_steward_inadmissible_issues_short_circuit_without_provider():
    """Verify inadmissible issues short-circuit without calling or constructing Gemini provider."""
    obl = Obligation(
        production_id="prod1",
        obligation_id="obl1",
        credited_party_id="party1",
        role_label="Director",
        required_display_text="Director: Alice Smith",
        credit_surface=CreditSurface.MAIN_TITLES,
        status=ObligationStatus.ACTIVE,
        source_document_id="doc1",
        source_document_version=1,
        source_span=SourceSpan(quote="Director: Alice Smith", start_char=0, end_char=21),
        source_hash="0" * 64,
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="prod-v2",
    )
    manifest = CreditManifest(production_id="prod1", manifest_id="m1", delivery_version_id="deliv1", entries=[])

    inadmissible_issues = [
        Issue(
            issue_id="i1",
            code=IssueCode.MISSING_CREDIT,
            obligation_id="obl1",
            manifest_refs=[],
            detail="Missing credit in manifest",
        ),
        Issue(
            issue_id="i2",
            code=IssueCode.ARTIFACT_SIZE_MISMATCH,
            obligation_id="obl1",
            manifest_refs=["elem1"],
            detail="Size mismatch on element",
        ),
        Issue(
            issue_id="i3",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id="obl1",
            manifest_refs=["elem1"],
            detail="role-only mismatch",
        ),
    ]

    # Create agent with None provider - if it tries to use provider it will raise or fail
    agent = StewardAgent(provider=None)

    for issue in inadmissible_issues:
        admissible, _reason = is_admissible_steward_issue(issue, obl)
        assert not admissible
        # Must return deterministic proposal without requiring provider/Gemini
        proposal = agent.explain_finding_and_propose_patch(issue, obl, manifest)
        assert proposal.patch_proposal is None
        assert proposal.validated is False
        assert proposal.provenance is None


def test_validate_fixtures_only_rejects_inadmissible_missing_credit(tmp_path):
    """RED test 1: validate_fixtures_only rejects PROPOSE_PATCH for MISSING_CREDIT using is_admissible_steward_issue."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    for d in ("extractor", "resolver", "steward"):
        (fixtures_dir / d).mkdir()

    bad_stw_fixture = {
        "case_id": "bad_stw_missing_credit",
        "split": "DEV",
        "scenario_category": "SUBSTITUTE_TEXT",
        "input_issue": {
            "issue_id": "i1",
            "code": "MISSING_CREDIT",
            "obligation_id": "o1",
            "manifest_refs": ["e1"],
            "detail": "Missing credit for obligation",
        },
        "input_obligation": {
            "production_id": "p1",
            "obligation_id": "o1",
            "credited_party_id": "cp1",
            "role_label": "Director",
            "required_display_text": "Director Alice Smith",
            "credit_surface": "MAIN_TITLES",
            "status": "ACTIVE",
            "source_document_id": "doc1",
            "source_document_version": 1,
            "source_span": {"quote": "Director Alice Smith", "start_char": 0, "end_char": 20},
            "source_hash": "0" * 64,
            "agent_reported_confidence": 1.0,
            "extraction_model_id": "gemini-3.6-flash",
            "prompt_version": "prod-v2",
        },
        "input_manifest": {
            "production_id": "p1",
            "manifest_id": "m1",
            "delivery_version_id": "deliv1",
            "entries": [],
        },
        "expected_action": "PROPOSE_PATCH",
        "expected_patch_operation": "SUBSTITUTE_TEXT",
        "expected_patch_value": "Director Alice Smith",
    }
    (fixtures_dir / "steward" / "bad.json").write_text(
        json.dumps(bad_stw_fixture), encoding="utf-8"
    )

    errors = validate_fixtures_only(fixtures_dir)
    assert any("inadmissible for issue code" in e and "MISSING_CREDIT" in e for e in errors)


def test_validate_fixtures_only_accepts_canonical_display_name_mismatch(tmp_path):
    """RED test 2: Genuine checker-contract display_name mismatch passes admissibility validation."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    for d in ("extractor", "resolver", "steward"):
        (fixtures_dir / d).mkdir()

    valid_stw_fixture = {
        "case_id": "valid_stw_display_mismatch",
        "split": "DEV",
        "scenario_category": "SUBSTITUTE_TEXT",
        "input_issue": {
            "issue_id": "i1",
            "code": "ARTIFACT_TEXT_MISMATCH",
            "obligation_id": "o1",
            "manifest_refs": ["e1"],
            "detail": "display_name mismatch: required_display_text 'Director Alice Smith' (NFC: 'Director Alice Smith') does not match manifest display_name 'Director Bob Jones'",
        },
        "input_obligation": {
            "production_id": "p1",
            "obligation_id": "o1",
            "credited_party_id": "cp1",
            "role_label": "Director",
            "required_display_text": "Director Alice Smith",
            "credit_surface": "MAIN_TITLES",
            "status": "ACTIVE",
            "source_document_id": "doc1",
            "source_document_version": 1,
            "source_span": {"quote": "Director Alice Smith", "start_char": 0, "end_char": 20},
            "source_hash": "0" * 64,
            "agent_reported_confidence": 1.0,
            "extraction_model_id": "gemini-3.6-flash",
            "prompt_version": "prod-v2",
        },
        "input_manifest": {
            "production_id": "p1",
            "manifest_id": "m1",
            "delivery_version_id": "deliv1",
            "entries": [
                {
                    "rendered_element_id": "e1",
                    "contributor_id": "cp1",
                    "display_name": "Director Bob Jones",
                    "role": "Director",
                    "credit_surface": "MAIN_TITLES",
                    "ordinal_position": 0,
                    "production_id": "p1",
                    "delivery_version_id": "deliv1",
                }
            ],
        },
        "expected_action": "PROPOSE_PATCH",
        "expected_patch_operation": "SUBSTITUTE_TEXT",
        "expected_patch_value": "Director Alice Smith",
    }
    (fixtures_dir / "steward" / "valid.json").write_text(
        json.dumps(valid_stw_fixture), encoding="utf-8"
    )

    errors = validate_fixtures_only(fixtures_dir, target_split="DEV")
    assert errors == []


def test_coverage_validation_path_independent(tmp_path):
    """RED test 3: Coverage validation is path-independent (does not check pathname endswith)."""
    import shutil

    custom_dir = tmp_path / "custom_fixtures_folder"
    shutil.copytree(Path("./fixtures/agent_eval_v2"), custom_dir)

    # Remove one required Frozen V2 extractor case
    fv2_file = custom_dir / "extractor" / "fv2_ext_000.json"
    if fv2_file.exists():
        fv2_file.unlink()

    errors = validate_fixtures_only(custom_dir, target_split="FROZEN_CHALLENGE_V2")
    assert any("Missing FROZEN_CHALLENGE_V2 scenarios" in e or "Expected 10 unique semantic signatures" in e for e in errors)


def test_target_split_isolation_dev_vs_fv2():
    """RED test 4: target_split distinguishes splits (DEV target vs FROZEN_CHALLENGE_V2 target)."""
    official_dir = Path("./fixtures/agent_eval_v2")

    # Validating FROZEN_CHALLENGE_V2 MUST report the 4 inadmissible MISSING_CREDIT positive annotations
    fv2_errors = validate_fixtures_only(official_dir, target_split="FROZEN_CHALLENGE_V2")
    assert len(fv2_errors) == 4
    missing_credit_errors = [e for e in fv2_errors if "MISSING_CREDIT" in e and "inadmissible for issue code" in e]
    assert len(missing_credit_errors) == 4
    assert any("fv2_stw_000" in e for e in missing_credit_errors)
    assert any("fv2_stw_001" in e for e in missing_credit_errors)
    assert any("fv2_stw_002" in e for e in missing_credit_errors)
    assert any("fv2_stw_003" in e for e in missing_credit_errors)


def test_validate_fixtures_only_rejects_unknown_target_split():
    """M.4 RED test 1: validate_fixtures_only rejects unknown target_split."""
    official_dir = Path("./fixtures/agent_eval_v2")
    errors = validate_fixtures_only(official_dir, target_split="TYPO_SPLIT")
    assert len(errors) == 1
    assert "Invalid target_split: 'TYPO_SPLIT'" in errors[0]


def test_run_component_eval_v2_unknown_target_split_fails_closed(monkeypatch):
    """M.4 RED test 2: run_component_eval_v2 with unknown target returns typed failed result without constructing providers."""
    from creditlock.eval.v2_runner import run_component_eval_v2

    def mock_provider_init(*args, **kwargs):
        raise RuntimeError("ModelProvider was constructed on invalid target_split!")

    monkeypatch.setattr("creditlock.agents.provider.GoogleModelProvider.__init__", mock_provider_init)

    official_dir = Path("./fixtures/agent_eval_v2")
    result = run_component_eval_v2(official_dir, target_split="TYPO_SPLIT")
    assert result.completeness_status == "INVALID_FIXTURE_ANNOTATIONS"
    assert result.quality_gate_status == "FAILED_QUALITY_GATE"
    assert result.summary_status == "INVALID_FIXTURE_ANNOTATIONS"
    assert len(result.per_case_results) == 1
    val_errs = result.per_case_results[0].get("validation_errors", [])
    assert any("Invalid target_split: 'TYPO_SPLIT'" in e for e in val_errs)


def test_full_corpus_validation_detects_missing_category_path_independent(tmp_path):
    """M.4 RED test 3: target_split=None enforces Frozen V2 coverage on full corpus path-independently."""
    import shutil

    custom_dir = tmp_path / "custom_fixtures_folder"
    shutil.copytree(Path("./fixtures/agent_eval_v2"), custom_dir)

    # Remove one required Frozen V2 extractor case
    fv2_file = custom_dir / "extractor" / "fv2_ext_000.json"
    if fv2_file.exists():
        fv2_file.unlink()

    errors = validate_fixtures_only(custom_dir, target_split=None)
    assert any("Missing FROZEN_CHALLENGE_V2 scenarios for extractor" in e or "Expected 10 unique semantic signatures for extractor" in e for e in errors)


def test_full_corpus_validation_detects_absent_agent_fixtures(tmp_path):
    """M.4 RED test 4: target_split=None or FROZEN_CHALLENGE_V2 detects when all fixtures for one agent are absent."""
    import shutil

    custom_dir = tmp_path / "custom_fixtures_folder_2"
    shutil.copytree(Path("./fixtures/agent_eval_v2"), custom_dir)

    # Remove all Frozen V2 steward cases
    for fpath in (custom_dir / "steward").glob("fv2_stw_*.json"):
        fpath.unlink()

    errors = validate_fixtures_only(custom_dir, target_split=None)
    assert any("Missing FROZEN_CHALLENGE_V2 scenarios for steward" in e for e in errors)
    assert any("Expected 7 unique semantic signatures for steward, got 0" in e for e in errors)


def test_digest_provenance_fields():
    """Verify evaluated_fixture_set_digest and full_fixture_corpus_digest are tracked."""
    res = ComponentEvalV2Result(
        evaluated_fixture_set_digest="a" * 64,
        full_fixture_corpus_digest="b" * 64,
        fixture_set_digest="a" * 64,
    )
    assert res.evaluated_fixture_set_digest == "a" * 64
    assert res.full_fixture_corpus_digest == "b" * 64
    assert res.fixture_set_digest == "a" * 64


def test_red_extractor_name_only_gold_annotation_defect():
    """RED test establishing V2 defect: name-only gold annotation fails clause-quote contract."""
    full_clause_quote = "Directed by Alice Smith"
    name_only_gold_quote = "Alice Smith"

    # Production contract requires full clause quote
    pred_span = {"quote": full_clause_quote, "start_char": 0, "end_char": 23}
    gold_span = {"quote": name_only_gold_quote, "start_char": 12, "end_char": 23}

    # Proves exact match fails when gold contains only individual name
    assert pred_span != gold_span


def test_no_live_environment_flag_set():
    """Verify no live environment flag is active during tests."""
    assert os.getenv("CREDITLOCK_RUN_LIVE_GEMINI") != "1"


def test_steward_init_monkeypatched_settings_and_provider_does_not_construct(monkeypatch):
    """RED test: StewardAgent(provider=None) must not touch get_settings or GoogleModelProvider on init or inadmissible issues."""
    def _fail_get_settings():
        raise RuntimeError("get_settings must not be called on init or inadmissible issues")

    def _fail_provider_init(*args, **kwargs):
        raise RuntimeError("GoogleModelProvider must not be constructed on init or inadmissible issues")

    monkeypatch.setattr("creditlock.agents.steward.get_settings", _fail_get_settings)
    monkeypatch.setattr("creditlock.agents.steward.GoogleModelProvider", _fail_provider_init)

    # 1. Instantiation must be genuinely lazy (no exceptions raised)
    agent = StewardAgent(provider=None)

    obl = Obligation(
        production_id="prod1",
        obligation_id="obl1",
        credited_party_id="party1",
        role_label="Name Designer",
        required_display_text="Name Designer: Alice Smith",
        credit_surface=CreditSurface.MAIN_TITLES,
        status=ObligationStatus.ACTIVE,
        source_document_id="doc1",
        source_document_version=1,
        source_span=SourceSpan(quote="Name Designer: Alice Smith", start_char=0, end_char=26),
        source_hash="0" * 64,
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="prod-v2",
    )
    manifest = CreditManifest(production_id="prod1", manifest_id="m1", delivery_version_id="deliv1", entries=[])

    inadmissible_issues = [
        Issue(issue_id="i1", code=IssueCode.MISSING_CREDIT, obligation_id="obl1", manifest_refs=[], detail="Missing credit"),
        Issue(issue_id="i2", code=IssueCode.ARTIFACT_SIZE_MISMATCH, obligation_id="obl1", manifest_refs=["e1"], detail="Size mismatch"),
        Issue(issue_id="i3", code=IssueCode.ARTIFACT_TEXT_MISMATCH, obligation_id="obl1", manifest_refs=["e1"], detail="role mismatch: expected Director, got Name Designer"),
        Issue(issue_id="i4", code=IssueCode.ARTIFACT_TEXT_MISMATCH, obligation_id="obl1", manifest_refs=["e1"], detail="role mismatch: expected Editor, got Text Editor"),
    ]

    # Every inadmissible issue must abstain without touching settings or provider
    for issue in inadmissible_issues:
        prop = agent.explain_finding_and_propose_patch(issue, obl, manifest)
        assert prop.patch_proposal is None
        assert prop.validated is False

    # Inactive obligation must abstain without touching settings or provider
    inactive_obl = obl.model_copy(update={"status": ObligationStatus.CANDIDATE})
    adm_issue = Issue(issue_id="i5", code=IssueCode.ARTIFACT_TEXT_MISMATCH, obligation_id="obl1", manifest_refs=["e1"], detail="display_name mismatch: expected Alice, got Bob")
    prop_inactive = agent.explain_finding_and_propose_patch(adm_issue, inactive_obl, manifest)
    assert prop_inactive.patch_proposal is None

    # Missing obligation (None) must abstain without touching settings or provider
    prop_none = agent.explain_finding_and_propose_patch(adm_issue, None, manifest)
    assert prop_none.patch_proposal is None


def test_steward_lazy_provider_constructed_exactly_once_on_admissible_issue(monkeypatch):
    """RED test: Default provider is lazily constructed exactly once across multiple admissible calls."""
    construct_count = 0

    class DummyProvider:
        def __init__(self, *args, **kwargs):
            nonlocal construct_count
            construct_count += 1

        def is_available(self):
            return True

        def generate_structured(self, *args, **kwargs):
            from creditlock.agents.models import (
                CallProvenance,
                ModelCallAttempt,
                StructuredGeneration,
            )
            from creditlock.agents.steward import StewardLLMOutputSchema
            return StructuredGeneration(
                output=StewardLLMOutputSchema(
                    explanation="Fix display_name",
                    proposed_operation="SUBSTITUTE_TEXT",
                    proposed_value="Director: Alice Smith",
                    derivable=True,
                ),
                provenance=CallProvenance(
                    agent_role="steward",
                    primary_model="gemini-3.5-flash-lite",
                    configured_fallback_model="gemini-3.6-flash",
                    actual_model_used="gemini-3.5-flash-lite",
                    fallback_occurred=False,
                    platform="google_genai",
                    auth_mode="API_KEY",
                    started_at_utc="2026-08-07T12:00:00Z",
                    total_latency_ms=10.0,
                    attempts=[
                        ModelCallAttempt(
                            model_id="gemini-3.5-flash-lite",
                            outcome="SUCCESS",
                            latency_ms=10.0,
                            sanitized_error_code=None,
                        )
                    ],
                ),
            )

    monkeypatch.setattr("creditlock.agents.steward.GoogleModelProvider", DummyProvider)

    agent = StewardAgent(provider=None)
    assert construct_count == 0

    obl = Obligation(
        production_id="prod1",
        obligation_id="obl1",
        credited_party_id="party1",
        role_label="Director",
        required_display_text="Director: Alice Smith",
        credit_surface=CreditSurface.MAIN_TITLES,
        status=ObligationStatus.ACTIVE,
        source_document_id="doc1",
        source_document_version=1,
        source_span=SourceSpan(quote="Director: Alice Smith", start_char=0, end_char=21),
        source_hash="0" * 64,
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="prod-v2",
    )
    from creditlock.domain.models import ManifestEntry
    manifest = CreditManifest(
        production_id="prod1",
        manifest_id="m1",
        delivery_version_id="deliv1",
        entries=[
            ManifestEntry(
                rendered_element_id="e1",
                contributor_id="party1",
                display_name="Bob",
                role="Director",
                credit_surface=CreditSurface.MAIN_TITLES,
                ordinal_position=0,
                production_id="prod1",
                delivery_version_id="deliv1",
            )
        ],
    )
    issue = Issue(issue_id="i1", code=IssueCode.ARTIFACT_TEXT_MISMATCH, obligation_id="obl1", manifest_refs=["e1"], detail="display_name mismatch: expected Alice Smith, got Bob")

    # First admissible call triggers provider construction
    prop1 = agent.explain_finding_and_propose_patch(issue, obl, manifest)
    assert prop1.validated is True
    assert construct_count == 1

    # Second admissible call reuses existing constructed provider
    prop2 = agent.explain_finding_and_propose_patch(issue, obl, manifest)
    assert prop2.validated is True
    assert construct_count == 1


def test_steward_exact_display_name_prefix_discrimination_fail_closed():
    """RED test: Enforce exact fail-closed prefix discrimination for ARTIFACT_TEXT_MISMATCH."""
    obl = Obligation(
        production_id="prod1",
        obligation_id="obl1",
        credited_party_id="party1",
        role_label="Name Designer",
        required_display_text="Name Designer: Alice Smith",
        credit_surface=CreditSurface.MAIN_TITLES,
        status=ObligationStatus.ACTIVE,
        source_document_id="doc1",
        source_document_version=1,
        source_span=SourceSpan(quote="Name Designer: Alice Smith", start_char=0, end_char=26),
        source_hash="0" * 64,
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="prod-v2",
    )

    inadmissible_details = [
        "role mismatch: expected Editor, got Text Editor",
        "role text mismatch: expected Editor, got Producer",
        "role_label mismatch: expected Director, got Producer",
        "unclassified manifest text anomaly",
        "manual text mismatch requiring review",
        "display name mismatch requiring review",
        "note: display_name mismatch: expected Alice, got Bob",
        "",
        "arbitrary manually supplied ARTIFACT_TEXT_MISMATCH",
    ]

    for detail in inadmissible_details:
        issue = Issue(
            issue_id="i1",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id="obl1",
            manifest_refs=["e1"],
            detail=detail,
        )
        admissible, reason = is_admissible_steward_issue(issue, obl)
        assert admissible is False, f"Expected detail '{detail}' to be inadmissible, but got admissible. Reason: {reason}"

    admissible_details = [
        "display_name mismatch: required_display_text 'Alice' (NFC: 'Alice') does not match manifest display_name 'Bob'",
        "   display_name mismatch: expected Alice, got Bob   ",
        "DISPLAY_NAME MISMATCH: expected Alice, got Bob",
    ]

    for detail in admissible_details:
        issue = Issue(
            issue_id="i2",
            code=IssueCode.ARTIFACT_TEXT_MISMATCH,
            obligation_id="obl1",
            manifest_refs=["e1"],
            detail=detail,
        )
        admissible, _ = is_admissible_steward_issue(issue, obl)
        assert admissible is True, f"Expected detail '{detail}' to be admissible, but got inadmissible."


def test_checker_evaluate_findings_steward_admissibility_integration():
    """Integration test: Verify evaluate_findings output contracts align with Steward admissibility."""
    from creditlock.domain.checker import CheckerInput, evaluate_findings
    from creditlock.domain.models import (
        LayoutAssertion,
        LayoutEvidence,
        ManifestEntry,
        VisualObservation,
        VisualObservations,
    )

    obl_display = Obligation(
        production_id="p1",
        obligation_id="o1",
        credited_party_id="cp1",
        role_label="Director",
        required_display_text="Director: Alice Smith",
        credit_surface=CreditSurface.MAIN_TITLES,
        status=ObligationStatus.ACTIVE,
        source_document_id="doc1",
        source_document_version=1,
        source_span=SourceSpan(quote="Director: Alice Smith", start_char=0, end_char=21),
        source_hash="0" * 64,
        agent_reported_confidence=1.0,
        extraction_model_id="gemini-3.6-flash",
        prompt_version="prod-v2",
    )

    manifest_display_mismatch = CreditManifest(
        production_id="p1",
        manifest_id="m1",
        delivery_version_id="d1",
        entries=[
            ManifestEntry(
                rendered_element_id="e1",
                contributor_id="cp1",
                display_name="Director: Bob Jones",
                role="Director",
                credit_surface=CreditSurface.MAIN_TITLES,
                ordinal_position=0,
                production_id="p1",
                delivery_version_id="d1",
            )
        ],
    )

    layout_disp = LayoutEvidence(
        manifest_id="m1",
        render_profile_version="v1",
        assertions=[
            LayoutAssertion(
                rendered_element_id="e1",
                visible_text="Director: Bob Jones",
                computed_font_size_px=24.0,
                bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 50.0},
                frame_index=0,
            )
        ],
    )
    obs_disp = VisualObservations(
        manifest_id="m1",
        model_id="test",
        observations=[VisualObservation(rendered_element_id="e1", observation="ok", flagged=False)],
    )

    # 1. Real display_name mismatch
    inp_disp = CheckerInput(
        obligations=[obl_display],
        manifest=manifest_display_mismatch,
        layout_evidence=layout_disp,
        visual_observations=obs_disp,
    )
    open_issues_disp = evaluate_findings(inp_disp)
    disp_issues = [i for i in open_issues_disp if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
    assert len(disp_issues) == 1
    disp_issue = disp_issues[0]
    assert disp_issue.detail.startswith("display_name mismatch:")
    adm_disp, _ = is_admissible_steward_issue(disp_issue, obl_display)
    assert adm_disp is True

    # 2. Real role mismatch
    manifest_role_mismatch = CreditManifest(
        production_id="p1",
        manifest_id="m2",
        delivery_version_id="d1",
        entries=[
            ManifestEntry(
                rendered_element_id="e1",
                contributor_id="cp1",
                display_name="Director: Alice Smith",
                role="Producer",
                credit_surface=CreditSurface.MAIN_TITLES,
                ordinal_position=0,
                production_id="p1",
                delivery_version_id="d1",
            )
        ],
    )
    layout_role = LayoutEvidence(
        manifest_id="m2",
        render_profile_version="v1",
        assertions=[
            LayoutAssertion(
                rendered_element_id="e1",
                visible_text="Director: Alice Smith",
                computed_font_size_px=24.0,
                bounding_box={"x": 0.0, "y": 0.0, "w": 100.0, "h": 50.0},
                frame_index=0,
            )
        ],
    )
    obs_role = VisualObservations(
        manifest_id="m2",
        model_id="test",
        observations=[VisualObservation(rendered_element_id="e1", observation="ok", flagged=False)],
    )

    inp_role = CheckerInput(
        obligations=[obl_display],
        manifest=manifest_role_mismatch,
        layout_evidence=layout_role,
        visual_observations=obs_role,
    )
    open_issues_role = evaluate_findings(inp_role)
    role_issues = [i for i in open_issues_role if i.code == IssueCode.ARTIFACT_TEXT_MISMATCH]
    assert len(role_issues) == 1
    role_issue = role_issues[0]
    assert role_issue.detail.startswith("role mismatch:")
    adm_role, _ = is_admissible_steward_issue(role_issue, obl_display)
    assert adm_role is False


def test_generic_split_fixture_digest_isolation(tmp_path):
    """RED test: compute_split_fixture_digest isolates DEV, RECORDED_REGRESSION, and FROZEN_CHALLENGE_V2 digests."""
    from creditlock.agents.provider import FakeModelProvider
    from creditlock.eval.v2_runner import (
        compute_frozen_challenge_v2_digest,
        compute_split_fixture_digest,
        run_component_eval_v2,
    )

    fixtures_dir = tmp_path / "fixtures"
    (fixtures_dir / "extractor").mkdir(parents=True)
    (fixtures_dir / "resolver").mkdir(parents=True)
    (fixtures_dir / "steward").mkdir(parents=True)

    dev_fix = {
        "case_id": "ext_dev_001",
        "split": "DEV",
        "scenario_category": "single-person positive",
        "synthetic_document_text": "Directed by Alice Smith",
        "expected_obligations": [
            {
                "credited_party_id": "p1",
                "role_label": "Director",
                "required_display_text": "Director Alice Smith",
                "source_span": {"quote": "Directed by Alice Smith", "start_char": 0, "end_char": 23},
            }
        ],
        "exact_gold_source_spans": [{"quote": "Directed by Alice Smith", "start_char": 0, "end_char": 23}],
    }
    reg_fix = {
        "case_id": "ext_reg_001",
        "split": "RECORDED_REGRESSION",
        "scenario_category": "single-person positive",
        "synthetic_document_text": "Produced by Bob Jones",
        "expected_obligations": [
            {
                "credited_party_id": "p2",
                "role_label": "Producer",
                "required_display_text": "Producer Bob Jones",
                "source_span": {"quote": "Produced by Bob Jones", "start_char": 0, "end_char": 21},
            }
        ],
        "exact_gold_source_spans": [{"quote": "Produced by Bob Jones", "start_char": 0, "end_char": 21}],
    }
    fv2_fix = {
        "case_id": "fv2_ext_000",
        "split": "FROZEN_CHALLENGE_V2",
        "scenario_category": "single-person positive",
        "synthetic_document_text": "Written by Charlie Brown",
        "expected_obligations": [
            {
                "credited_party_id": "p3",
                "role_label": "Writer",
                "required_display_text": "Writer Charlie Brown",
                "source_span": {"quote": "Written by Charlie Brown", "start_char": 0, "end_char": 24},
            }
        ],
        "exact_gold_source_spans": [{"quote": "Written by Charlie Brown", "start_char": 0, "end_char": 24}],
    }

    dev_path = fixtures_dir / "extractor" / "ext_dev_001.json"
    reg_path = fixtures_dir / "extractor" / "ext_reg_001.json"
    fv2_path = fixtures_dir / "extractor" / "fv2_ext_000.json"

    dev_path.write_text(json.dumps(dev_fix), encoding="utf-8")
    reg_path.write_text(json.dumps(reg_fix), encoding="utf-8")
    fv2_path.write_text(json.dumps(fv2_fix), encoding="utf-8")

    digest_dev_1 = compute_split_fixture_digest(fixtures_dir, split="DEV")
    digest_reg_1 = compute_split_fixture_digest(fixtures_dir, split="RECORDED_REGRESSION")
    digest_fv2_1 = compute_split_fixture_digest(fixtures_dir, split="FROZEN_CHALLENGE_V2")
    digest_full_1 = compute_split_fixture_digest(fixtures_dir, split=None)

    # Prove compute_frozen_challenge_v2_digest equals compute_split_fixture_digest(dir, "FROZEN_CHALLENGE_V2")
    assert compute_frozen_challenge_v2_digest(fixtures_dir) == digest_fv2_1

    # 1. Modify DEV file
    dev_fix["synthetic_document_text"] = "Directed by Alice Smith (MODIFIED)"
    dev_path.write_text(json.dumps(dev_fix), encoding="utf-8")

    digest_dev_2 = compute_split_fixture_digest(fixtures_dir, split="DEV")
    digest_reg_2 = compute_split_fixture_digest(fixtures_dir, split="RECORDED_REGRESSION")
    digest_fv2_2 = compute_split_fixture_digest(fixtures_dir, split="FROZEN_CHALLENGE_V2")
    digest_full_2 = compute_split_fixture_digest(fixtures_dir, split=None)

    assert digest_dev_2 != digest_dev_1
    assert digest_full_2 != digest_full_1
    assert digest_reg_2 == digest_reg_1
    assert digest_fv2_2 == digest_fv2_1

    # 2. Modify RECORDED_REGRESSION file
    reg_fix["synthetic_document_text"] = "Produced by Bob Jones (MODIFIED)"
    reg_path.write_text(json.dumps(reg_fix), encoding="utf-8")

    digest_dev_3 = compute_split_fixture_digest(fixtures_dir, split="DEV")
    digest_reg_3 = compute_split_fixture_digest(fixtures_dir, split="RECORDED_REGRESSION")
    digest_fv2_3 = compute_split_fixture_digest(fixtures_dir, split="FROZEN_CHALLENGE_V2")
    digest_full_3 = compute_split_fixture_digest(fixtures_dir, split=None)

    assert digest_reg_3 != digest_reg_2
    assert digest_full_3 != digest_full_2
    assert digest_dev_3 == digest_dev_2
    assert digest_fv2_3 == digest_fv2_2

    # 3. Modify FROZEN_CHALLENGE_V2 file
    fv2_fix["synthetic_document_text"] = "Written by Charlie Brown (MODIFIED)"
    fv2_path.write_text(json.dumps(fv2_fix), encoding="utf-8")

    digest_dev_4 = compute_split_fixture_digest(fixtures_dir, split="DEV")
    digest_reg_4 = compute_split_fixture_digest(fixtures_dir, split="RECORDED_REGRESSION")
    digest_fv2_4 = compute_split_fixture_digest(fixtures_dir, split="FROZEN_CHALLENGE_V2")
    digest_full_4 = compute_split_fixture_digest(fixtures_dir, split=None)

    assert digest_fv2_4 != digest_fv2_3
    assert digest_full_4 != digest_full_3
    assert digest_dev_4 == digest_dev_3
    assert digest_reg_4 == digest_reg_3

    # Test run_component_eval_v2 digest tracking
    fake_ext = FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}})
    fake_res = FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "mock"}})
    fake_stw = FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "mock"}})

    # target_split="DEV": fixture_set_digest equals evaluated_fixture_set_digest
    res_dev = run_component_eval_v2(fixtures_dir, fake_ext, fake_res, fake_stw, target_split="DEV")
    assert res_dev.evaluated_fixture_set_digest == digest_dev_4
    assert res_dev.full_fixture_corpus_digest == digest_full_4
    assert res_dev.fixture_set_digest == res_dev.evaluated_fixture_set_digest

    # target_split="RECORDED_REGRESSION": evaluated_fixture_set_digest equals digest_reg_4
    res_reg = run_component_eval_v2(fixtures_dir, fake_ext, fake_res, fake_stw, target_split="RECORDED_REGRESSION")
    assert res_reg.evaluated_fixture_set_digest == digest_reg_4
    assert res_reg.full_fixture_corpus_digest == digest_full_4


def test_runner_level_authoritative_expected_obligation_source_span(tmp_path):
    """RED test: run_component_eval_v2 uses expected obligation's source_span as authoritative for exact gold scoring."""
    from creditlock.agents.provider import FakeModelProvider
    from creditlock.eval.v2_runner import run_component_eval_v2

    fixtures_dir = tmp_path / "fixtures"
    (fixtures_dir / "extractor").mkdir(parents=True)
    (fixtures_dir / "resolver").mkdir(parents=True)
    (fixtures_dir / "steward").mkdir(parents=True)

    # Expected obligation carries authoritative full-clause source_span
    # Legacy exact_gold_source_spans carries conflicting name-only span
    fixture_data = {
        "case_id": "ext_span_auth_001",
        "split": "DEV",
        "scenario_category": "single-person positive",
        "synthetic_document_text": "Directed by Alice Smith",
        "expected_obligations": [
            {
                "credited_party_id": "p1",
                "role_label": "Director",
                "required_display_text": "Director Alice Smith",
                "source_span": {"quote": "Directed by Alice Smith", "start_char": 0, "end_char": 23},
            }
        ],
        "exact_gold_source_spans": [
            {"quote": "Alice Smith", "start_char": 12, "end_char": 23}
        ],
    }

    fix_path = fixtures_dir / "extractor" / "ext_auth.json"
    fix_path.write_text(json.dumps(fixture_data), encoding="utf-8")

    # Extractor outputs full-clause span matching expected_obligations[0].source_span
    fake_ext = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "credited_party_id": "p1",
                        "role_label": "Director",
                        "required_display_text": "Director Alice Smith",
                        "quote": "Directed by Alice Smith",
                        "start_char": 0,
                        "end_char": 23,
                    }
                ]
            }
        },
    )
    fake_res = FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "mock"}})
    fake_stw = FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "mock"}})

    result_auth = run_component_eval_v2(fixtures_dir, fake_ext, fake_res, fake_stw, target_split="DEV")
    assert result_auth.dev_metrics["extractor"]["exact_gold_source_span_accuracy"] == 1.0

    # Changing expected_obligations[0].source_span to name-only span causes accuracy == 0.0
    fixture_data["expected_obligations"][0]["source_span"] = {"quote": "Alice Smith", "start_char": 12, "end_char": 23}
    fix_path.write_text(json.dumps(fixture_data), encoding="utf-8")

    result_name_only = run_component_eval_v2(fixtures_dir, fake_ext, fake_res, fake_stw, target_split="DEV")
    assert result_name_only.dev_metrics["extractor"]["exact_gold_source_span_accuracy"] == 0.0
