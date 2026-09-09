"""
Negative Control Unit Tests for Precedence Resolver Metrics (Commit G).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from creditlock.agents.provider import FakeModelProvider
from creditlock.eval.v2_runner import run_component_eval_v2


def test_swapped_abstain_and_conflict_fails_quality_gate(tmp_path: Path) -> None:
    # Copy real fixture directory to tmp_path
    target_fixtures = tmp_path / "fixtures"
    shutil.copytree(Path("./fixtures/agent_eval_v2"), target_fixtures)

    # Override res_holdout_001_conflict.json or add a conflict case expected recommendation
    res_dir = target_fixtures / "resolver"
    res_fixture = {
        "case_id": "res_holdout_001_conflict",
        "split": "RECORDED_REGRESSION",
        "input_candidate_obligations": [
            {
                "obligation_id": "obl-c1",
                "production_id": "prod-1",
                "status": "CANDIDATE",
                "extraction_model_id": "gemini-3.6-flash",
                "prompt_version": "v1",
                "credited_party_id": "contrib-dir",
                "required_display_text": "Person A",
                "role_label": "Director",
                "credit_surface": "END_CARDS",
                "card_type": "SOLO",
                "card_position": {"kind": "ABSOLUTE", "ordinal": 1},
                "source_document_id": "doc1.txt",
                "source_document_version": 1,
                "source_hash": "h1",
                "agent_reported_confidence": 0.9,
                "source_span": {"quote": "Person A", "start_char": 0, "end_char": 8},
            },
            {
                "obligation_id": "obl-c2",
                "production_id": "prod-1",
                "status": "CANDIDATE",
                "extraction_model_id": "gemini-3.6-flash",
                "prompt_version": "v1",
                "credited_party_id": "contrib-dir",
                "required_display_text": "Person B",
                "role_label": "Director",
                "credit_surface": "END_CARDS",
                "card_type": "SOLO",
                "card_position": {"kind": "ABSOLUTE", "ordinal": 1},
                "source_document_id": "doc2.txt",
                "source_document_version": 1,
                "source_hash": "h2",
                "agent_reported_confidence": 0.9,
                "source_span": {"quote": "Person B", "start_char": 0, "end_char": 8},
            },
        ],
        "expected_recommendation": "CONFLICT",
        "expected_controlling_id": None,
    }
    (res_dir / "res_holdout_001_conflict.json").write_text(
        str(res_fixture).replace("'", '"').replace("None", "null")
    )
    res_fixture_2 = dict(res_fixture)
    res_fixture_2["case_id"] = "res_holdout_002_conflict"
    (res_dir / "res_holdout_002_conflict.json").write_text(
        str(res_fixture_2).replace("'", '"').replace("None", "null")
    )

    # Fake Resolver that INCORRECTLY outputs ABSTAIN for CONFLICT case
    fake_res = FakeModelProvider(
        agent_role="resolver",
        responses={
            "default": {
                "recommendation": "ABSTAIN",
                "controlling_obligation_id": None,
                "rationale": "Swapped abstain for conflict",
            }
        },
    )

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
                        "quote": "CREDIT: John Williams",
                        "start_char": 18,
                        "end_char": 39,
                    }
                ]
            }
        },
    )

    fake_stw = FakeModelProvider(
        agent_role="steward",
        responses={
            "default": {
                "explanation": "Patch proposed",
                "proposed_operation": "SUBSTITUTE_TEXT",
                "proposed_value": "Hans Zimmer",
                "derivable": True,
            }
        },
    )

    result = run_component_eval_v2(
        fixtures_dir=target_fixtures,
        extractor_provider=fake_ext,
        resolver_provider=fake_res,
        steward_provider=fake_stw,
        target_split="RECORDED_REGRESSION",
    )

    comb_res = result.combined_metrics["resolver"]
    assert comb_res["model_backed_conflict_denominator"] == 2
    assert comb_res["model_backed_conflict_accuracy"] == 0.0
    assert result.quality_gate_status == "FAILED_QUALITY_GATE"
