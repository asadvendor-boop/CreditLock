"""
RED Unit Tests for V2 Measurement Integrity Regressions.
"""

from __future__ import annotations

import json
from pathlib import Path

from creditlock.agents.provider import FakeModelProvider
from creditlock.eval.v2_runner import run_component_eval_v2


def _populate_full_fixture_set(tmp_path: Path) -> tuple[Path, Path, Path]:
    ext_dir = tmp_path / "extractor"
    res_dir = tmp_path / "resolver"
    stw_dir = tmp_path / "steward"
    ext_dir.mkdir(exist_ok=True)
    res_dir.mkdir(exist_ok=True)
    stw_dir.mkdir(exist_ok=True)

    # Add 2 dummy ext cases
    (ext_dir / "ext_d1.json").write_text(json.dumps({
        "case_id": "ext_d1", "split": "DEV", "synthetic_document_text": "CREDIT: A",
        "expected_obligations": [], "scored_fields": ["required_display_text"], "exact_gold_source_spans": [], "is_non_binding": True,
    }))
    (ext_dir / "ext_d2.json").write_text(json.dumps({
        "case_id": "ext_d2", "split": "RECORDED_REGRESSION", "synthetic_document_text": "CREDIT: B",
        "expected_obligations": [], "scored_fields": ["required_display_text"], "exact_gold_source_spans": [], "is_non_binding": True,
    }))

    # Add 2 dummy res cases
    (res_dir / "res_d1.json").write_text(json.dumps({
        "case_id": "res_d1", "split": "DEV", "input_candidate_obligations": [],
        "expected_recommendation": "ABSTAIN", "expected_controlling_id": None,
    }))
    (res_dir / "res_d2.json").write_text(json.dumps({
        "case_id": "res_d2", "split": "RECORDED_REGRESSION", "input_candidate_obligations": [],
        "expected_recommendation": "ABSTAIN", "expected_controlling_id": None,
    }))

    # Add 2 dummy stw cases
    stw_dummy = {
        "case_id": "stw_d1", "split": "DEV",
        "input_issue": {"issue_id": "i", "code": "ARTIFACT_TEXT_MISMATCH", "manifest_refs": ["e"], "obligation_id": "o", "detail": "d"},
        "input_obligation": {
            "obligation_id": "o", "production_id": "p", "status": "CANDIDATE", "extraction_model_id": "gemini-3.6-flash", "prompt_version": "v1",
            "required_display_text": "A", "role_label": "Composer", "credit_surface": "END_CARDS", "card_type": "SOLO",
            "source_document_id": "doc", "source_document_version": 1, "source_hash": "h", "agent_reported_confidence": 0.9,
            "source_span": {"quote": "A", "start_char": 0, "end_char": 1},
        },
        "input_manifest": {"manifest_id": "m", "production_id": "p", "delivery_version_id": "v1", "entries": []},
        "expected_action": "DETERMINISTIC_ABSTAIN",
    }
    (stw_dir / "stw_d1.json").write_text(json.dumps(stw_dummy))
    stw_dummy2 = dict(stw_dummy)
    stw_dummy2["case_id"] = "stw_d2"
    stw_dummy2["split"] = "RECORDED_REGRESSION"
    (stw_dir / "stw_d2.json").write_text(json.dumps(stw_dummy2))

    return ext_dir, res_dir, stw_dir


def test_red_valid_but_wrong_source_span_lowers_gold_accuracy_while_validity_passes(tmp_path: Path) -> None:
    ext_dir, _, _ = _populate_full_fixture_set(tmp_path)

    text = "CREDIT MEMORANDUM\nCREDIT: John Williams\nROLE: Composer"
    # 'John Williams' starts at 26, len 13 -> 26..39
    # 'John' starts at 26, len 4 -> 26..30
    ext_case = {
        "case_id": "ext_d1",
        "split": "DEV",
        "synthetic_document_text": text,
        "expected_obligations": [
            {
                "required_display_text": "John Williams",
                "role_label": "Composer",
                "credit_surface": "END_CARDS",
            }
        ],
        "scored_fields": ["required_display_text", "role_label", "credit_surface"],
        "exact_gold_source_spans": [
            {
                "quote": "John Williams",
                "start_char": 26,
                "end_char": 39,
            }
        ],
        "is_non_binding": False,
    }
    (ext_dir / "ext_d1.json").write_text(json.dumps(ext_case))

    # Fake model returns quote 'John' at (26, 30) -> valid text substring text[26:30] == 'John', but NOT exact gold span!
    fake_ext = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "John Williams",
                        "role_label": "Composer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "card_position_ordinal": 1,
                        "quote": "John",
                        "start_char": 26,
                        "end_char": 30,
                        "confidence": 0.9,
                    }
                ]
            }
        },
    )

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=fake_ext,
        resolver_provider=FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "m"}}),
        steward_provider=FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "m", "derivable": False}}),
        target_split="DEV",
    )

    assert res.combined_metrics["extractor"]["source_span_validity_rate"] == 1.0
    assert res.combined_metrics["extractor"]["exact_gold_source_span_accuracy"] == 0.0


def test_red_unsupported_field_increments_invented_count(tmp_path: Path) -> None:
    ext_dir, _, _ = _populate_full_fixture_set(tmp_path)

    ext_case = {
        "case_id": "ext_d1",
        "split": "DEV",
        "synthetic_document_text": "CREDIT: John Williams",
        "expected_obligations": [{"required_display_text": "John Williams", "role_label": "Composer"}],
        "scored_fields": ["required_display_text", "role_label"],
        "supported_unscored_fields": ["credit_surface", "card_type", "card_position_ordinal", "confidence"],
        "exact_gold_source_spans": [{"quote": "CREDIT: John Williams", "start_char": 0, "end_char": 21}],
        "is_non_binding": False,
    }
    (ext_dir / "ext_d1.json").write_text(json.dumps(ext_case))

    # Fake model returns an unlisted/unsupported extra field, e.g., obligee_text="Invented"
    fake_ext = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "obligee_text": "Invented Obligee",
                        "required_display_text": "John Williams",
                        "role_label": "Composer",
                        "quote": "CREDIT: John Williams",
                        "start_char": 0,
                        "end_char": 21,
                    }
                ]
            }
        },
    )

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=fake_ext,
        resolver_provider=FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "m"}}),
        steward_provider=FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "m", "derivable": False}}),
        target_split="DEV",
    )

    assert res.combined_metrics["extractor"]["unsupported_invented_field_count"] == 1


def test_red_missing_component_directory_causes_incomplete_status(tmp_path: Path) -> None:
    ext_dir = tmp_path / "extractor"
    ext_dir.mkdir()

    ext_case = {
        "case_id": "ext_dev_001",
        "split": "DEV",
        "synthetic_document_text": "CREDIT: John Williams",
        "expected_obligations": [],
        "scored_fields": ["required_display_text"],
        "exact_gold_source_spans": [],
        "is_non_binding": True,
    }
    (ext_dir / "ext_dev_001.json").write_text(json.dumps(ext_case))

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}}),
        target_split="DEV",
    )

    assert res.completeness_status == "INCOMPLETE_FIXTURE_SET"
    assert res.quality_gate_status == "FAILED_QUALITY_GATE"


def test_red_zero_model_backed_resolver_denominator_fails_quality_gate(tmp_path: Path) -> None:
    _populate_full_fixture_set(tmp_path)

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}}),
        resolver_provider=FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "m"}}),
        target_split="DEV",
    )

    assert res.combined_metrics["resolver"]["model_backed_rec_denominator"] == 0
    assert res.quality_gate_status == "FAILED_QUALITY_GATE"


def test_red_deterministic_abstain_is_not_counted_as_gemini_call(tmp_path: Path) -> None:
    _, _, stw_dir = _populate_full_fixture_set(tmp_path)

    stw_case = {
        "case_id": "stw_d1",
        "split": "DEV",
        "input_issue": {"issue_id": "i1", "code": "ARTIFACT_TEXT_MISMATCH", "manifest_refs": ["e1"], "obligation_id": "o1", "detail": "d"},
        "input_obligation": {
            "obligation_id": "o1", "production_id": "p1", "status": "CANDIDATE", "extraction_model_id": "gemini-3.6-flash", "prompt_version": "v1",
            "required_display_text": "John", "role_label": "Composer", "credit_surface": "END_CARDS", "card_type": "SOLO",
            "source_document_id": "doc1", "source_document_version": 1, "source_hash": "h1", "agent_reported_confidence": 0.9,
            "source_span": {"quote": "John", "start_char": 0, "end_char": 4},
        },
        "input_manifest": {"manifest_id": "m1", "production_id": "p1", "delivery_version_id": "v1", "entries": []},
        "expected_action": "DETERMINISTIC_ABSTAIN",
    }
    (stw_dir / "stw_d1.json").write_text(json.dumps(stw_case))

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}}),
        resolver_provider=FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "m"}}),
        steward_provider=FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "m", "derivable": False}}),
        target_split="DEV",
    )

    assert res.agent_execution_counts.get("steward", 0) >= 1
    assert res.model_backed_invocation_counts.get("steward", 0) == 0
    prov_entry = next(p for p in res.per_case_sanitized_provenance if p.case_id == "stw_d1")
    assert prov_entry.execution_path == "DETERMINISTIC_SHORT_CIRCUIT"
