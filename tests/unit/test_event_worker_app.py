"""
Offline unit tests for Confluent event worker application.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from creditlock.events.worker_app import app
from creditlock.settings import get_settings


def test_12_worker_startup_creates_one_worker_thread_and_shutdown_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)
    monkeypatch.setattr(get_settings(), "google_cloud_project", "test-gcp-proj")
    monkeypatch.setattr(get_settings(), "confluent_bootstrap_servers", "test-server:9092")
    monkeypatch.setattr(get_settings(), "confluent_api_key", "key")
    monkeypatch.setattr(get_settings(), "confluent_api_secret", "secret")
    monkeypatch.setattr(get_settings(), "confluent_topic", "creditlock.production.events")

    stop_event = threading.Event()
    mock_worker = MagicMock()
    mock_worker.start.side_effect = lambda: stop_event.wait(timeout=5.0)
    mock_worker.stop.side_effect = stop_event.set

    mock_transport = MagicMock()
    mock_store = MagicMock()

    with (
        patch("creditlock.events.worker_app.ConfluentTransport.from_settings", return_value=mock_transport),
        patch("creditlock.events.worker_app.FirestoreProjectionStore.from_settings", return_value=mock_store),
        patch("creditlock.events.worker_app.EventIngestionWorker", return_value=mock_worker),
        TestClient(app) as client,
    ):
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["worker_alive"] is True
        assert data["consumer_group"] == "creditlock-firestore-projection-v1"
        assert data["topic"] == "creditlock.production.events"

    # After client context exits (shutdown)
    assert mock_worker.stop.called is True


def test_13_worker_health_output_contains_no_credential_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", True)
    monkeypatch.setattr(get_settings(), "google_cloud_project", "test-gcp-proj")
    monkeypatch.setattr(get_settings(), "confluent_bootstrap_servers", "test-bootstrap-server-sensitive:9092")
    monkeypatch.setattr(get_settings(), "confluent_api_key", "sensitive-key-1234")
    monkeypatch.setattr(get_settings(), "confluent_api_secret", "sensitive-secret-5678")
    monkeypatch.setattr(get_settings(), "confluent_topic", "creditlock.production.events")

    mock_worker = MagicMock()

    with (
        patch("creditlock.events.worker_app.ConfluentTransport.from_settings"),
        patch("creditlock.events.worker_app.FirestoreProjectionStore.from_settings"),
        patch("creditlock.events.worker_app.EventIngestionWorker", return_value=mock_worker),
        TestClient(app) as client,
    ):
        res = client.get("/health")
        body_text = res.text
        assert "sensitive-key-1234" not in body_text
        assert "sensitive-secret-5678" not in body_text
        assert "test-bootstrap-server-sensitive" not in body_text


def test_worker_fails_closed_when_runtime_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "confluent_runtime_enabled", False)

    with pytest.raises(RuntimeError) as exc_info, TestClient(app):
        pass

    assert "CONFLUENT_RUNTIME_ENABLED" in str(exc_info.value)
