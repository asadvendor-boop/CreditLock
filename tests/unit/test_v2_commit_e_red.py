"""
RED Unit Tests for Commit E: Measurement & Policy Correctness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creditlock.agents.provider import FakeModelProvider
from creditlock.eval.v2_runner import run_component_eval_v2


def _populate_test_fixtures(tmp_path: Path) -> None:
    ext_dir = tmp_path / "extractor"
    res_dir = tmp_path / "resolver"
    stw_dir = tmp_path / "steward"
    ext_dir.mkdir(exist_ok=True)
    res_dir.mkdir(exist_ok=True)
    stw_dir.mkdir(exist_ok=True)

    text1 = "CREDIT MEMORANDUM\nCREDIT: John Williams\nROLE: Composer\nSURFACE: END_CARDS\nQUOTE: CREDIT: John Williams"
    (ext_dir / "ext_dev_001.json").write_text(json.dumps({
        "case_id": "ext_dev_001", "split": "DEV", "synthetic_document_text": text1,
        "expected_obligations": [{"required_display_text": "John Williams", "role_label": "Composer", "credit_surface": "END_CARDS"}],
        "scored_fields": ["required_display_text", "role_label", "credit_surface"],
        "exact_gold_source_spans": [{"quote": "CREDIT: John Williams", "start_char": 18, "end_char": 39}],
        "is_non_binding": False,
    }))
    (ext_dir / "ext_holdout_001.json").write_text(json.dumps({
        "case_id": "ext_holdout_001", "split": "RECORDED_REGRESSION", "synthetic_document_text": "CREDIT: B",
        "expected_obligations": [], "scored_fields": ["required_display_text"], "exact_gold_source_spans": [], "is_non_binding": True,
    }))

    (res_dir / "res_dev_001.json").write_text(json.dumps({
        "case_id": "res_dev_001", "split": "DEV", "input_candidate_obligations": [],
        "expected_recommendation": "ABSTAIN", "expected_controlling_id": None,
    }))
    (res_dir / "res_holdout_001.json").write_text(json.dumps({
        "case_id": "res_holdout_001", "split": "RECORDED_REGRESSION", "input_candidate_obligations": [],
        "expected_recommendation": "CONFLICT", "expected_controlling_id": None,
    }))

    stw_dummy = {
        "case_id": "stw_dev_001", "split": "DEV",
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
    (stw_dir / "stw_dev_001.json").write_text(json.dumps(stw_dummy))
    stw_dummy2 = dict(stw_dummy)
    stw_dummy2["case_id"] = "stw_holdout_001"
    stw_dummy2["split"] = "RECORDED_REGRESSION"
    (stw_dir / "stw_holdout_001.json").write_text(json.dumps(stw_dummy2))


def test_red_invalid_fixture_annotation_rejects_fixture_set(tmp_path: Path) -> None:
    _populate_test_fixtures(tmp_path)

    # Corrupt an annotation: start_char: 0, end_char: 10 vs quote: "CREDIT: John Williams"
    ext_dir = tmp_path / "extractor"
    corrupt_case = json.loads((ext_dir / "ext_dev_001.json").read_text())
    corrupt_case["exact_gold_source_spans"][0]["start_char"] = 0
    corrupt_case["exact_gold_source_spans"][0]["end_char"] = 10
    (ext_dir / "ext_dev_001.json").write_text(json.dumps(corrupt_case))

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}}),
    )

    assert res.completeness_status == "INVALID_FIXTURE_ANNOTATIONS"
    assert res.quality_gate_status == "FAILED_QUALITY_GATE"


def test_red_provenance_execution_status_distinguishes_fake_vs_live(tmp_path: Path) -> None:
    _populate_test_fixtures(tmp_path)

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        extractor_provider=FakeModelProvider(agent_role="extractor", responses={"default": {"candidates": []}}),
        resolver_provider=FakeModelProvider(agent_role="resolver", responses={"default": {"recommendation": "ABSTAIN", "rationale": "m"}}),
        steward_provider=FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "m", "derivable": False}}),
        target_split="DEV",
    )

    assert res.execution_status == "COMPLETED_OFFLINE_FAKE"
    prov_entry = res.per_case_sanitized_provenance[0]
    assert prov_entry.agent_role in ("extractor", "resolver", "steward")
    assert "prompt_version" in prov_entry.model_dump() or (prov_entry.provenance and "prompt_version" in prov_entry.provenance)


def test_red_steward_rejection_reason_reported_on_abstain(tmp_path: Path) -> None:
    _populate_test_fixtures(tmp_path)

    res = run_component_eval_v2(
        fixtures_dir=tmp_path,
        steward_provider=FakeModelProvider(agent_role="steward", responses={"default": {"explanation": "m", "derivable": False}}),
        target_split="DEV",
    )

    stw_case = next(c for c in res.per_case_results if c["component"] == "steward" and c["case_id"] == "stw_dev_001")
    assert stw_case["actual_action"] == "DETERMINISTIC_ABSTAIN"
    assert stw_case["rejection_reason"] is not None
    assert "CANDIDATE" in stw_case["rejection_reason"] or "ACTIVE" in stw_case["rejection_reason"]


def test_red_v2_cli_exits_nonzero_on_failed_quality_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts.run_evaluation import main

    _populate_test_fixtures(tmp_path)
    report_dir = tmp_path / "report"

    monkeypatch.setattr("sys.argv", ["run_evaluation.py", "--v2", "--output-dir", str(report_dir)])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    assert (report_dir / "benchmark_report.json").exists()
