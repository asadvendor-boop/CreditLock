"""
Focused offline unit tests for scripts/verify_judge_journey.py.

These tests verify the verifier's invariant logic without making any real
network requests. They demonstrate that:
  - A wrong HTTP status fails the verifier.
  - An ungrounded or incorrectly indexed Gemini quote fails.
  - A model outside the allowlist fails.
  - Missing release digest or non-GCS package URI fails.
  - Replay divergence fails.
  - Generated output never contains bearer tokens or JWT values.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make scripts/ importable
scripts_dir = Path(__file__).parent.parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from verify_judge_journey import (
    _SECRET_PATTERN,
    ALLOWED_MODELS,
    JourneyVerifier,
    StepResult,
    _sanitize,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal response stub returned by _make_request mock."""

    def __init__(self, status_code: int, body: dict[str, Any] | bytes = b"") -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        if isinstance(self._body, bytes):
            import json
            return json.loads(self._body)
        return self._body  # type: ignore[return-value]

    @property
    def content(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        import json
        return json.dumps(self._body).encode()


def _make_verifier(base_url: str = "https://fake.example.com") -> JourneyVerifier:
    return JourneyVerifier(base_url=base_url)


def _seed_verifier(
    v: JourneyVerifier,
    *,
    production_id: str = "prod_demo_abc123",
    reviewer_token: str = "reviewer_tok",
    approver_token: str = "approver_tok",
    ambiguous_issue_id: str = "issue_ambig_001",
    proposal_id: str = "prop-abc123456789",
    release_digest: str = "a" * 64,
    delivery_uri: str = "gs://bucket/deliveries/x/y.zip",
    gemini_model: str = "gemini-3.6-flash",
) -> None:
    v._production_id = production_id
    v._reviewer_token = reviewer_token
    v._approver_token = approver_token
    v._ambiguous_issue_id = ambiguous_issue_id
    v._proposal_id = proposal_id
    v._release_digest = release_digest
    v._delivery_package_uri = delivery_uri
    v._gemini_model_used = gemini_model


# ── StepResult ────────────────────────────────────────────────────────────────

class TestStepResult:
    def test_pass_label(self) -> None:
        r = StepResult(1, "thing", True, "HTTP 200")
        assert r.label() == "PASS"

    def test_fail_label(self) -> None:
        r = StepResult(2, "thing", False, "HTTP 500")
        assert r.label() == "FAIL"

    def test_str_includes_step_and_observed(self) -> None:
        r = StepResult(3, "some check", True, "HTTP 200", "extra detail")
        rendered = str(r)
        assert "Step 03" in rendered
        assert "PASS" in rendered
        assert "HTTP 200" in rendered
        assert "extra detail" in rendered


# ── Sanitization ──────────────────────────────────────────────────────────────

class TestSanitize:
    def test_jwt_is_redacted(self) -> None:
        # A realistic JWT-like string
        jwt_val = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyXzEyMyIsInJvbGUiOiJSRVZJRVdFUiJ9.signature123"
        out = _sanitize(f"Authorization: Bearer {jwt_val}")
        assert jwt_val not in out
        assert "REDACTED" in out

    def test_bearer_token_is_redacted(self) -> None:
        token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.somepayload.sig"
        out = _sanitize(token)
        assert "eyJ" not in out
        assert "REDACTED" in out

    def test_normal_text_passes_through(self) -> None:
        normal = "HTTP 200 OK — release_digest=abcdef123456"
        assert _sanitize(normal) == normal

    def test_transcript_never_contains_jwt(self) -> None:
        import contextlib
        import io
        v = _make_verifier()
        v._reviewer_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ4In0.sig"
        v._approver_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ5In0.sig"
        _seed_verifier(v)
        # Record a fake pass result and capture transcript
        v.results.append(StepResult(1, "test", True, "HTTP 200"))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            v._print_transcript(partial=False)
        transcript = captured.getvalue()
        assert "eyJ" not in transcript
        assert v._reviewer_token not in transcript
        assert v._approver_token not in transcript


# ── Allowed model check ───────────────────────────────────────────────────────

class TestModelAllowlist:
    def test_allowed_models_constant(self) -> None:
        assert "gemini-3.6-flash" in ALLOWED_MODELS
        assert "gemini-3.5-flash-lite" in ALLOWED_MODELS
        assert "gemini-3.1-pro-preview" in ALLOWED_MODELS

    def test_disallowed_model_fails_verifier(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, gemini_model="gpt-4-turbo")
        with pytest.raises(SystemExit) as exc_info:
            v.step_07_model_in_allowlist()
        assert exc_info.value.code == 1

    def test_allowed_model_passes_verifier(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, gemini_model="gemini-3.6-flash")
        v.step_07_model_in_allowlist()  # must not raise
        assert any(r.step == 7 and r.passed for r in v.results)

    def test_empty_model_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, gemini_model="")
        with pytest.raises(SystemExit) as exc_info:
            v.step_07_model_in_allowlist()
        assert exc_info.value.code == 1


# ── HTTP status invariants ────────────────────────────────────────────────────

class TestWrongHttpStatusFails:
    def test_step1_wrong_status_fails(self) -> None:
        v = _make_verifier()
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(500)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_01_public_loads()
        assert exc_info.value.code == 1

    def test_step2_wrong_status_fails(self) -> None:
        # If token-mint returns 200 (exists), verifier must fail
        v = _make_verifier()
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, b"{}")),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_02_token_mint_unavailable()
        assert exc_info.value.code == 1

    def test_step3_wrong_status_fails(self) -> None:
        # Unauthenticated request returns 200 — verifier must fail
        v = _make_verifier()
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, b"{}")),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_03_unauthenticated_refused()
        assert exc_info.value.code == 1

    def test_step4_export_returns_200_instead_of_409_fails(self) -> None:
        # First call (/init) returns OK, second (/export) returns 200 — must fail
        init_body = {
            "status": "initialized",
            "demo_session_id": "sess_abc",
            "production_id": "prod_demo_abc",
            "gate_state": "NEEDS_HUMAN",
            "issue_count": 1,
            "reviewer_token": "rev_tok",
            "approver_token": "app_tok",
            "reviewer_id": "rev1",
            "approver_id": "app1",
            "render_backend": "synthetic",
        }
        responses = [
            _FakeResponse(200, init_body),
            _FakeResponse(200, {"gate_state": "READY_TO_EXPORT"}),  # should be 409
        ]
        v = _make_verifier()
        with (
            patch("verify_judge_journey._make_request", side_effect=responses),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_04_fresh_production_cannot_export()
        assert exc_info.value.code == 1

    def test_step9_reviewer_gets_200_instead_of_403_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, b"{}")),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_09_reviewer_cannot_confirm()
        assert exc_info.value.code == 1

    def test_step10_reviewer_export_returns_200_instead_of_403_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, b"{}")),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_10_reviewer_cannot_export()
        assert exc_info.value.code == 1


# ── Gemini grounding invariants ───────────────────────────────────────────────

class TestGeminiGroundingInvariants:
    def test_zero_extracted_obligations_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        analyze_response = _FakeResponse(
            200,
            {
                "model_used": "gemini-3.6-flash",
                "fallback_occurred": False,
                "extracted_obligations": [],  # empty — must fail
                "rejection_diagnostics": [],
                "deterministic_issue": {},
                "required_human_action": "CONFIRM_IDENTITY",
                "product_distinction_note": "note",
            },
        )
        with (
            patch("verify_judge_journey._make_request", return_value=analyze_response),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_05_gemini_grounded_candidates()
        assert exc_info.value.code == 1

    def test_analyze_http_error_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(502, b"{}")),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_05_gemini_grounded_candidates()
        assert exc_info.value.code == 1

    def test_no_ambiguous_issue_in_state_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        state_response = _FakeResponse(
            200,
            {
                "production_id": "prod_demo_abc123",
                "gate_state": "NEEDS_HUMAN",
                "issue_count": 0,
                "issues": [],  # no AMBIGUOUS_IDENTITY — must fail
            },
        )
        with (
            patch("verify_judge_journey._make_request", return_value=state_response),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_06_quote_span_integrity()
        assert exc_info.value.code == 1


# ── Release digest and GCS URI invariants ────────────────────────────────────

class TestReleaseDigestAndGcsUri:
    def test_missing_release_digest_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, release_digest="", delivery_uri="gs://bucket/x.zip")
        with pytest.raises(SystemExit) as exc_info:
            v.step_13_release_digest_and_gcs_uri()
        assert exc_info.value.code == 1

    def test_short_release_digest_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, release_digest="abc123", delivery_uri="gs://bucket/x.zip")
        with pytest.raises(SystemExit) as exc_info:
            v.step_13_release_digest_and_gcs_uri()
        assert exc_info.value.code == 1

    def test_non_gcs_uri_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, release_digest="a" * 64, delivery_uri="https://example.com/x.zip")
        with pytest.raises(SystemExit) as exc_info:
            v.step_13_release_digest_and_gcs_uri()
        assert exc_info.value.code == 1

    def test_empty_uri_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, release_digest="a" * 64, delivery_uri="")
        with pytest.raises(SystemExit) as exc_info:
            v.step_13_release_digest_and_gcs_uri()
        assert exc_info.value.code == 1

    def test_valid_digest_and_gcs_uri_passes(self) -> None:
        v = _make_verifier()
        _seed_verifier(v, release_digest="b" * 64, delivery_uri="gs://creditlock-bucket/deliveries/x.zip")
        v.step_13_release_digest_and_gcs_uri()  # must not raise
        assert all(r.passed for r in v.results if r.step == 13)


# ── Replay divergence ─────────────────────────────────────────────────────────

class TestReplayDivergence:
    def test_replay_divergence_fails(self, tmp_path: Path) -> None:
        import zipfile as zf
        v = _make_verifier()
        _seed_verifier(v, release_digest="c" * 64)
        # Create a real (but minimal) zip file for the test
        fake_zip = tmp_path / "fake.zip"
        with zf.ZipFile(fake_zip, "w") as zobj:
            zobj.writestr("manifest.json", "{}")
        v._downloaded_zip_path = str(fake_zip)

        # Simulate subprocess returning MISMATCH
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = "REPLAY RESULT: MISMATCH\n"
        mock_proc.stderr = ""

        with (
            patch("subprocess.run", return_value=mock_proc),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_15_offline_replay()
        assert exc_info.value.code == 1

    def test_replay_timeout_fails(self, tmp_path: Path) -> None:
        import subprocess
        import zipfile as zf
        v = _make_verifier()
        _seed_verifier(v, release_digest="d" * 64)
        fake_zip = tmp_path / "fake.zip"
        with zf.ZipFile(fake_zip, "w") as zobj:
            zobj.writestr("manifest.json", "{}")
        v._downloaded_zip_path = str(fake_zip)

        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("replay.py", 60)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_15_offline_replay()
        assert exc_info.value.code == 1

    def test_replay_match_passes(self, tmp_path: Path) -> None:
        import zipfile as zf
        v = _make_verifier()
        _seed_verifier(v, release_digest="e" * 64)
        fake_zip = tmp_path / "fake.zip"
        with zf.ZipFile(fake_zip, "w") as zobj:
            zobj.writestr("manifest.json", "{}")
        v._downloaded_zip_path = str(fake_zip)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "REPLAY RESULT: MATCH\n"
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            v.step_15_offline_replay()  # must not raise
        assert any(r.step == 15 and r.passed for r in v.results)


# ── Output secrets check ──────────────────────────────────────────────────────

class TestOutputNeverContainsSecrets:
    def test_secret_pattern_detects_jwt(self) -> None:
        jwt_val = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1MiJ9.sigpart"
        assert _SECRET_PATTERN.search(jwt_val) is not None

    def test_secret_pattern_detects_bearer(self) -> None:
        bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        assert _SECRET_PATTERN.search(bearer) is not None

    def test_transcript_does_not_leak_tokens(self) -> None:
        """Full transcript output must never contain raw token strings."""
        import contextlib
        import io
        v = _make_verifier()
        _seed_verifier(
            v,
            reviewer_token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyZXZpZXdlciJ9.fakesig",
            approver_token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcHByb3ZlciJ9.fakesig",
        )
        v.results.append(StepResult(1, "check", True, "HTTP 200"))
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            v._print_transcript(partial=False)
        transcript = captured.getvalue()
        assert "eyJhbGciOiJIUzI1NiJ9" not in transcript
        assert "fakesig" not in transcript
        assert _SECRET_PATTERN.search(transcript) is None


# ── Event sync verification in Step 12 ────────────────────────────────────────

class TestEventSyncVerification:
    def _valid_export_body(self) -> dict[str, Any]:
        return {
            "gate_state": "READY_TO_EXPORT",
            "release_digest": "a" * 64,
            "delivery_package_path": "gs://bucket/delivery.zip",
            "replay_status": "VALID",
            "authorized_at": "2026-09-08T00:00:00Z",
            "event_sync": {
                "status": "SYNCHRONIZED",
                "transport": "CONFLUENT_CLOUD",
                "topic": "creditlock.production.events",
                "event_type": "resolution.recorded",
                "event_id": "80c7c669-f264-5e88-8ce9-992bf06d296c",
                "projection_backend": "FIRESTORE",
            },
        }

    def test_valid_event_sync_passes(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        with patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)):
            v.step_12_authorized_export_succeeds()  # must not raise
        assert any(r.step == 12 and r.passed and "SYNCHRONIZED" in r.observed for r in v.results)

    def test_missing_event_sync_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        del body["event_sync"]
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_null_event_sync_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"] = None
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_wrong_status_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"]["status"] = "PENDING"
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_wrong_transport_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"]["transport"] = "IN_MEMORY"
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_wrong_event_type_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"]["event_type"] = "production.created"
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_wrong_projection_backend_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"]["projection_backend"] = "POSTGRES"
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_invalid_uuid_event_id_fails(self) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        body["event_sync"]["event_id"] = "not-a-canonical-uuid"
        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

    def test_invalid_event_sync_redacts_topic_and_raw_payload(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        distinctive_topic = "distinctive.confidential.confluent.topic.42"
        body = self._valid_export_body()
        body["event_sync"]["topic"] = distinctive_topic
        body["event_sync"]["status"] = "INVALID_STATUS"
        body["event_sync"]["extra_secret_key"] = "extra_secret_value"

        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

        captured = capsys.readouterr().out
        assert distinctive_topic not in captured
        assert "extra_secret_value" not in captured
        assert "INVALID_STATUS" not in captured
        assert "event_sync invalid fields: status" in captured

    def test_missing_event_sync_reports_safe_diagnostic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        v = _make_verifier()
        _seed_verifier(v)
        body = self._valid_export_body()
        del body["event_sync"]

        with (
            patch("verify_judge_journey._make_request", return_value=_FakeResponse(200, body)),
            pytest.raises(SystemExit) as exc_info,
        ):
            v.step_12_authorized_export_succeeds()
        assert exc_info.value.code == 1

        captured = capsys.readouterr().out
        assert "event_sync missing or not an object" in captured
