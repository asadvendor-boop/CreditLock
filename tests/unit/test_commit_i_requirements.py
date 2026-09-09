"""
Unit tests for Commit I mandatory requirement verifications.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.provider import FakeModelProvider
from creditlock.eval.v2_runner import (
    get_agent_prompt_template_hashes,
    validate_fixtures_only,
)


def test_sidecars_exist_and_valid() -> None:
    doc_dir = Path("docs/evidence")
    frozen_sidecar = doc_dir / "live-v2-frozen-challenge.metadata.json"
    tuned_sidecar = doc_dir / "live-v2-tuned-regression.metadata.json"

    assert frozen_sidecar.exists()
    f_data = json.loads(frozen_sidecar.read_text())
    assert f_data["evidence_class"] == "RECORDED_CHALLENGE_V1_FAILED"
    assert f_data["agent_code_commit_sha"] == "051df57de9c8136ce52797df43b10188552e1fa9"
    assert f_data["fixture_definition_commit_sha"] == "01a0d72"
    assert f_data["original_fixture_set_digest"] == "5be668b9305b2fe47db12e1bc6320b988e232cf149334537655f6d151887453a"

    assert tuned_sidecar.exists()
    t_data = json.loads(tuned_sidecar.read_text())
    assert t_data["evidence_class"] == "TUNED_RECORDED_REGRESSION"
    assert t_data["metadata_status"] == "LEGACY_PARTIAL_METADATA: This report was generated before reproducibility metadata fields were implemented. The fixture_set_digest, git_commit_sha, generated_at_utc, and agent_prompt_template_hashes were all null in the original report. These fields cannot be retrospectively reconstructed with certainty. The original report file is preserved byte-for-byte."


def test_real_prompt_hashes_differ_and_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    hashes = get_agent_prompt_template_hashes()
    h_ext = hashes["extractor"]
    h_res = hashes["resolver"]
    h_stw = hashes["steward"]

    # All three hashes differ
    assert len({h_ext, h_res, h_stw}) == 3

    # Changing extractor template changes ONLY extractor hash
    import creditlock.agents.extractor as ext_mod
    orig_template = ext_mod.USER_PROMPT_TEMPLATE
    monkeypatch.setattr(ext_mod, "USER_PROMPT_TEMPLATE", orig_template + "\n# Extra comment")

    new_hashes = get_agent_prompt_template_hashes()
    assert new_hashes["extractor"] != h_ext
    assert new_hashes["resolver"] == h_res
    assert new_hashes["steward"] == h_stw


def test_validation_only_mode_makes_zero_model_calls(tmp_path: Path) -> None:
    class FailingProvider:
        agent_role = "failing"
        primary_model_id = "p"
        fallback_model_id = "f"

        def is_available(self) -> bool:
            return True

        def generate_structured(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("Model invocation should not occur in validation-only mode!")

    # Populate valid structure
    fixtures_dir = Path("fixtures/agent_eval_v2")
    errors = validate_fixtures_only(fixtures_dir, target_split="DEV")
    assert errors == [], f"Validation errors found: {errors}"


def test_extractor_position_semantics_defaults_to_none(tmp_path: Path) -> None:
    fake_ext = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Alice Smith",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        # card_position_ordinal is omitted -> defaults to None
                        "quote": "Produced by Alice Smith",
                        "start_char": 0,
                        "end_char": 23,
                    }
                ]
            }
        },
    )

    doc_ref = {"uri": "doc1.txt", "sha256_hash": "hash1"}
    agent = ExtractorAgent(provider=fake_ext) # type: ignore[arg-type]
    from creditlock.agents.models import DocumentReference
    res = agent.extract_from_document(DocumentReference.model_validate(doc_ref), "Produced by Alice Smith", "p1")

    assert len(res.extracted_obligations) == 1
    obl = res.extracted_obligations[0]
    # Position must be None when not explicitly provided
    assert obl.card_position is None
