from __future__ import annotations

import json
import tempfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from creditlock.api.app import app
from creditlock.api.export import _get_production, clear_productions, register_production
from creditlock.api.resolutions import _compute_hashes
from creditlock.domain.canonical import canonical_json_bytes, sha256_bytes_digest
from creditlock.domain.gate import GateState
from creditlock.domain.models import (
    CreditManifest,
    CreditSurface,
    IssueCode,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    VisualObservations,
)
from creditlock.evidence.delivery import assemble_delivery_package
from creditlock.evidence.replay import replay_bundle
from tests.fixtures.dummy_evidence import generate_dummy_evidence

PROD = "prod-trust-1"


@pytest.fixture(autouse=True)
def reset_store():
    clear_productions()
    yield
    clear_productions()


def _seed_production() -> dict:
    manifest = CreditManifest(
        manifest_id="manifest-1",
        production_id=PROD,
        delivery_version_id="v1",
        entries=[
            ManifestEntry(
                rendered_element_id="elem-1",
                contributor_id="contrib-1",
                display_name="Jane Doe",
                role="Director",
                credit_surface=CreditSurface.END_CARDS,
                ordinal_position=1,
                production_id=PROD,
                delivery_version_id="v1",
            )
        ],
    )
    layout_ev, frames, artifact_index = generate_dummy_evidence(manifest)

    # Introduce an obligation that needs visual confirmation
    from creditlock.domain.models import SourceSpan

    obs = Obligation(
        obligation_id="obl-1",
        production_id=PROD,
        credited_party_id="contrib-1",
        required_display_text="Jane Doe",
        role_label="Director",
        credit_surface=CreditSurface.END_CARDS,
        source_document_id="doc-1",
        source_document_version=1,
        source_span=SourceSpan(page=1, start_char=0, end_char=10, quote="Directed by Jane Doe"),
        source_hash="a" * 64,
        agent_reported_confidence=0.99,
        extraction_model_id="test",
        prompt_version="v1",
        status=ObligationStatus.ACTIVE,
    )

    from creditlock.domain.models import VisualObservation

    visuals = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id="gemini-3.6-flash",
        observations=[
            VisualObservation(
                rendered_element_id="elem-1",
                observation="Looks blurry",
                flagged=True,
                flag_reason="Blurry text",
            )
        ],
    )

    register_production(
        production_id=PROD,
        obligations=[obs],
        manifest=manifest,
        layout_evidence=layout_ev,
        visual_observations=visuals,
        contributor_registry=[],
        authorizations=[],
        frames=frames,
        artifact_index=artifact_index.model_dump(),
        render_profile_version="1.0",
    )

    return _get_production(PROD)


class TestTrustChainRepair:
    def test_trust_chain_exact_digest_equality(self):
        """
        The valid authorization/export/replay test must prove the same exact canonical ArtifactIndex digest appears in:
        1. current production state;
        2. the confirmed Authorization;
        3. packaged artifact_index.json;
        4. release_evidence.json;
        5. replay’s recomputed digest.
        """
        prod = _seed_production()

        # 1. Expected exact digest from supplied artifact index
        supplied_art_index = prod["artifact_index"]
        expected_digest = sha256_bytes_digest(canonical_json_bytes(supplied_art_index))

        # _compute_hashes output
        _m_hash, _o_hash, c_art_digest, _v_hash = _compute_hashes(prod)
        assert c_art_digest == expected_digest

        client = TestClient(app)

        # Determine the issue ID (VISUAL_OBSERVATION_UNCERTAIN)
        from creditlock.domain.checker import CheckerInput, evaluate_findings

        checker_input = CheckerInput(
            obligations=prod["obligations"],
            manifest=prod["manifest"],
            layout_evidence=prod["layout_evidence"],
            visual_observations=prod["visual_observations"],
        )
        issues = evaluate_findings(checker_input)
        target_issue = next(i for i in issues if i.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN)

        from creditlock.api.auth import create_token

        reviewer_token = create_token("reviewer_1", "REVIEWER")
        approver_token = create_token("approver_1", "RELEASE_APPROVER")

        # Propose (REVIEWER)
        res_prop = client.post(
            f"/productions/{PROD}/proposals",
            json={
                "issue_id": target_issue.issue_id,
                "action": "CONFIRM_VISUAL",
                "reason": "Visual checks pass",
            },
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
        assert res_prop.status_code == 201
        prop_id = res_prop.json()["proposal"]["proposal_id"]

        # Confirm (RELEASE_APPROVER)
        res_auth = client.post(
            f"/productions/{PROD}/proposals/{prop_id}/confirm",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert res_auth.status_code == 201

        # 2. Confirmed Authorization
        auth_data = res_auth.json()["authorization"]
        assert auth_data["artifact_index_digest"] == expected_digest

        # Export (as RELEASE_APPROVER)
        res_exp = client.post(
            f"/productions/{PROD}/export",
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        assert res_exp.status_code == 200, res_exp.json()
        exp_data = res_exp.json()
        assert exp_data["gate_state"] == GateState.READY_TO_EXPORT.value

        pkg_path = exp_data["delivery_package_path"]

        # Inspect zip
        with zipfile.ZipFile(pkg_path, "r") as zf:
            # 3. packaged artifact_index.json
            art_bytes = zf.read("artifact_index.json")
            pkg_art_digest = sha256_bytes_digest(art_bytes)
            assert pkg_art_digest == expected_digest

            # 4. ReleaseEvidence.artifact_index_digest
            rel_bytes = zf.read("release_evidence.json")
            rel_ev = json.loads(rel_bytes)
            assert rel_ev["artifact_index_digest"] == expected_digest

        # Replay
        rep_res = replay_bundle(pkg_path, exp_data["release_digest"])
        assert rep_res.status == "MATCH"
        assert rep_res.divergence_reasons == []
        assert rep_res.expected_gate_state == GateState.READY_TO_EXPORT.value
        assert rep_res.replayed_gate_state == GateState.READY_TO_EXPORT.value

        # Explicitly assert exact equality among the 5 required digests
        with open(pkg_path, "rb") as f:
            f.read()

        # Re-extracting from zip for validation
        with zipfile.ZipFile(pkg_path, "r") as zf:
            packaged_index_digest = sha256_bytes_digest(zf.read("artifact_index.json"))
            rel_evidence = json.loads(zf.read("release_evidence.json"))

        assert rep_res.recomputed_artifact_index_digest == expected_digest
        assert packaged_index_digest == expected_digest
        assert rel_evidence["artifact_index_digest"] == expected_digest

    def test_missing_artifact_index_rejected(self):
        prod = _seed_production()
        del prod["artifact_index"]

        # Direct compute_hashes fails
        with pytest.raises(ValueError, match="Missing artifact_index"):
            _compute_hashes(prod)

        # Packaging fails
        with (
            tempfile.TemporaryDirectory() as tmp,
            pytest.raises(ValueError, match="Missing artifact_index"),
        ):
            assemble_delivery_package(PROD, prod, tmp)

        # Export fails
        from creditlock.api.auth import create_token

        approver_token = create_token("approver_1", "RELEASE_APPROVER")
        client = TestClient(app)
        res = client.post(
            f"/productions/{PROD}/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert res.status_code == 409
        data = res.json()
        assert data["detail"]["gate_state"] == "STALE"
        assert data["detail"]["issues"][0]["code"] == "ARTIFACT_PENDING"

    def test_wrong_manifest_hash_rejected(self):
        prod = _seed_production()
        prod["artifact_index"]["manifest_hash"] = "wrong-hash"

        with pytest.raises(ValueError, match="manifest_hash mismatch"):
            _compute_hashes(prod)

        from creditlock.api.auth import create_token

        approver_token = create_token("approver_1", "RELEASE_APPROVER")
        client = TestClient(app)
        res = client.post(
            f"/productions/{PROD}/export", headers={"Authorization": f"Bearer {approver_token}"}
        )
        assert res.status_code == 409
        assert res.json()["detail"]["gate_state"] == "BLOCKED"
        assert res.json()["detail"]["issues"][0]["code"] == "ARTIFACT_INTEGRITY_FAILURE"

    def test_wrong_layout_hash_rejected(self):
        prod = _seed_production()
        prod["artifact_index"]["layout_evidence_hash"] = "wrong-hash"

        with pytest.raises(ValueError, match="layout_evidence_hash mismatch"):
            _compute_hashes(prod)

    def test_wrong_render_profile_version_rejected(self):
        prod = _seed_production()
        prod["artifact_index"]["render_profile_version"] = "2.0"

        with pytest.raises(ValueError, match="render_profile_version mismatch"):
            _compute_hashes(prod)

    def test_reordered_frame_ids_rejected(self):
        prod = _seed_production()
        # Add a second frame to reorder
        manifest = prod["manifest"]
        _layout_ev, frames, _artifact_index = generate_dummy_evidence(manifest)
        # Manually construct a 2-frame dummy
        import copy

        f2 = copy.deepcopy(frames[0])
        f2.frame_id = "frame-1"
        frames.append(f2)
        prod["frames"] = frames
        art2 = copy.deepcopy(prod["artifact_index"]["frames"][0])
        art2["frame_id"] = "frame-1"
        prod["artifact_index"]["frames"].append(art2)

        # Now swap frames in artifact_index but not in frames
        prod["artifact_index"]["frames"] = [
            prod["artifact_index"]["frames"][1],
            prod["artifact_index"]["frames"][0],
        ]

        with pytest.raises(ValueError, match="frame_id mismatch"):
            _compute_hashes(prod)

    def test_changed_png_bytes_rejected(self):
        prod = _seed_production()
        prod["frames"][0].image_bytes = b"corrupted"

        with pytest.raises(ValueError, match="PNG audit"):
            _compute_hashes(prod)

    def test_mismatched_image_hash_rejected(self):
        prod = _seed_production()
        prod["frames"][0].image_hash = "wrong"

        with pytest.raises(ValueError, match="PNG bytes do not match FrameEvidence.image_hash"):
            _compute_hashes(prod)

    def test_mismatched_artifact_index_frame_sha256_rejected(self):
        prod = _seed_production()
        prod["artifact_index"]["frames"][0]["sha256"] = "wrong"

        with pytest.raises(ValueError, match="PNG bytes do not match ArtifactIndex frame hash"):
            _compute_hashes(prod)


def test_runner_boundary_no_fixture_imports() -> None:
    """RED TEST: Ensure production evaluation runner never imports tests.fixtures."""
    import ast
    from pathlib import Path

    runner_path = Path("src/creditlock/eval/deterministic_runner.py")
    tree = ast.parse(runner_path.read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("tests.fixtures"), (
                    f"Found forbidden import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("tests.fixtures"), (
                f"Found forbidden import from: {node.module}"
            )
