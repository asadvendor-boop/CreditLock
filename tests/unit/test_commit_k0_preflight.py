import json
import sys
from pathlib import Path

import pytest

from creditlock.eval.v2_runner import (
    compute_frozen_challenge_v2_digest,
    get_agent_prompt_template_hashes,
)
from creditlock.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.run_live_frozen_challenge_v2 import main as cli_main
from scripts.run_live_frozen_challenge_v2 import run_live

EXPECTED_EXTRACTOR_CATEGORIES = [
    "single-person positive",
    "two-person 'and' positive",
    "ampersand positive",
    "three-person list positive",
    "heading-separated binding clause positive",
    "repeated clause positive with exact unambiguous spans",
    "draft/non-binding negative",
    "conditional/future negative",
    "unfamiliar formatting positive",
    "unfamiliar role/names positive",
]

EXPECTED_RESOLVER_CATEGORIES = [
    "explicit supersession",
    "explicit amendment",
    "date/version-only difference with no supersession language",
    "substantive incompatible obligations",
    "genuinely unrelated grouping identities",
    "same party with no controlling proof and no substantive incompatibility",
]

EXPECTED_STEWARD_CATEGORIES = [
    "SUBSTITUTE_TEXT",
    "REORDER",
    "REGROUP",
    "REPOSITION",
    "deterministic abstention 1",
    "deterministic abstention 2",
    "deterministic abstention 3",
]


class SentinelReached(Exception):
    pass


def provider_factory() -> None:
    raise SentinelReached("Provider construction reached!")


@pytest.fixture
def temp_k0_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.run_live_frozen_challenge_v2.check_clean_worktree", lambda *a: True
    )

    def mock_check_commit(sha, root_dir):
        return sha != "invalid_sha_123"

    monkeypatch.setattr(
        "scripts.run_live_frozen_challenge_v2.check_commit_exists", mock_check_commit
    )
    monkeypatch.setattr(
        "scripts.run_live_frozen_challenge_v2.check_agents_unchanged", lambda *a: True
    )

    monkeypatch.setattr(
        "scripts.run_live_frozen_challenge_v2.validate_fixtures_only", lambda *a, **kw: []
    )

    import shutil
    fixtures_dir = tmp_path / "fixtures"
    shutil.copytree(PROJECT_ROOT / "fixtures" / "agent_eval_v2", fixtures_dir)

    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"

    settings = get_settings()
    manifest_data = {
        "agent_code_commit_sha": "HEAD",
        "frozen_prompt_template_hashes": get_agent_prompt_template_hashes(),
        "configured_primary_models": {
            "extractor": settings.gemini_extractor_model,
            "resolver": settings.gemini_resolver_model,
            "steward": settings.gemini_steward_model,
        },
        "configured_fallback_models": {
            "extractor": settings.gemini_extractor_fallback_model,
            "resolver": settings.gemini_resolver_fallback_model,
            "steward": settings.gemini_steward_fallback_model,
        },
        "fixture_counts": {
            "extractor": 10,
            "resolver": 6,
            "steward": 7,
        },
        "semantic_uniqueness_counts": {
            "extractor": 10,
            "resolver": 6,
            "steward": 7,
        },
        "coverage_categories": {
            "extractor": EXPECTED_EXTRACTOR_CATEGORIES,
            "resolver": EXPECTED_RESOLVER_CATEGORIES,
            "steward": EXPECTED_STEWARD_CATEGORIES,
        },
        "fixture_set_digest": compute_frozen_challenge_v2_digest(fixtures_dir),
    }
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    return tmp_path, fixtures_dir, manifest_path, manifest_data, evidence_path


def test_missing_manifest(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, _manifest_data, evidence_path = temp_k0_env
    manifest_path.unlink()

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "Manifest file missing" in capsys.readouterr().err


def test_malformed_manifest_halt(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, _manifest_data, evidence_path = temp_k0_env
    manifest_path.write_text("invalid json", encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "Manifest is malformed or invalid" in capsys.readouterr().err


def test_missing_commit_sha_halt(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, manifest_data, evidence_path = temp_k0_env
    manifest_data["agent_code_commit_sha"] = "invalid_sha_123"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "agent_code_commit_sha invalid_sha_123 does not exist" in capsys.readouterr().err


def test_duplicate_fixtures_halt(temp_k0_env, capsys, monkeypatch):
    _tmp_path, fixtures_dir, manifest_path, _manifest_data, evidence_path = temp_k0_env
    monkeypatch.undo()
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_clean_worktree", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_commit_exists", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_agents_unchanged", lambda *a: True)

    ext1 = json.loads((fixtures_dir / "extractor" / "fv2_ext_000.json").read_text())
    ext2 = ext1.copy()
    ext2["case_id"] = "dup_c1"

    (fixtures_dir / "extractor" / "dup_c1.json").write_text(json.dumps(ext2), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "semantic duplicate detected in extractor" in capsys.readouterr().err


def test_wrong_schemas_halt(temp_k0_env, capsys, monkeypatch):
    _tmp_path, fixtures_dir, manifest_path, _manifest_data, evidence_path = temp_k0_env
    monkeypatch.undo()
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_clean_worktree", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_commit_exists", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_agents_unchanged", lambda *a: True)

    ext1 = json.loads((fixtures_dir / "extractor" / "fv2_ext_000.json").read_text())
    del ext1["scenario_category"]
    (fixtures_dir / "extractor" / "fv2_ext_000.json").write_text(json.dumps(ext1), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "missing scenario_category" in capsys.readouterr().err


def test_changed_prompt_hashes_halt(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, manifest_data, evidence_path = temp_k0_env
    manifest_data["frozen_prompt_template_hashes"]["extractor"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "Prompt hash mismatch for extractor" in capsys.readouterr().err


def test_changed_model_routes_halt(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, manifest_data, evidence_path = temp_k0_env
    manifest_data["configured_primary_models"]["extractor"] = "modified-model-name"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2


def test_wrong_fixture_digests_halt(temp_k0_env, capsys):
    _tmp_path, fixtures_dir, manifest_path, manifest_data, evidence_path = temp_k0_env
    manifest_data["fixture_set_digest"] = "1" * 64
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    res = run_live(manifest_path, fixtures_dir, PROJECT_ROOT, evidence_path, provider_factory)
    assert res == 2
    assert "Fixture digest mismatch" in capsys.readouterr().err


def test_preflight_aborts_before_provider_construction_on_invalidated_frozen_v2_split(monkeypatch, tmp_path, capsys):
    """RED test 5: Frozen V2 preflight aborts before provider construction due to inadmissible MISSING_CREDIT cases."""
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_clean_worktree", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_commit_exists", lambda *a: True)
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.check_agents_unchanged", lambda *a: True)

    # If GoogleModelProvider is constructed, raise SentinelReached
    def mock_provider_init(*args, **kwargs):
        raise SentinelReached("GoogleModelProvider was constructed during preflight!")

    monkeypatch.setattr("creditlock.agents.provider.GoogleModelProvider.__init__", mock_provider_init)

    temp_evidence = tmp_path / "live-v2-frozen-challenge-v2.json"
    monkeypatch.setattr("scripts.run_live_frozen_challenge_v2.PROJECT_ROOT", tmp_path)

    manifest_src = PROJECT_ROOT / "docs" / "evidence" / "frozen-challenge-v2-manifest.json"
    fixtures_src = PROJECT_ROOT / "fixtures" / "agent_eval_v2"
    (tmp_path / "docs" / "evidence").mkdir(parents=True)
    (tmp_path / "docs" / "evidence" / "frozen-challenge-v2-manifest.json").write_text(
        manifest_src.read_text()
    )
    import shutil
    shutil.copytree(fixtures_src, tmp_path / "fixtures" / "agent_eval_v2")

    res = cli_main(["--preflight-only"])
    assert res == 2
    err = capsys.readouterr().err
    assert "Fixture validation failed" in err
    assert "inadmissible for issue code 'MISSING_CREDIT'" in err
    assert not temp_evidence.exists()


def test_execute_live_without_env_var_fails():
    res = cli_main(["--execute-live"])
    assert res == 2

