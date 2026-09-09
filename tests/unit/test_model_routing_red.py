"""
RED Tests for Explicit Model Routing, Provenance, and Enterprise Configuration.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import ValidationError

from creditlock.agents.models import StructuredGeneration
from creditlock.agents.provider import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelInvocationUnavailableError,
    ModelOutputValidationError,
    ModelPermissionError,
    ModelSafetyBlockedError,
)
from creditlock.settings import Settings

# We will import the provider classes here once they are implemented.
# For RED tests, we write the assertions first based on the instructions.

def test_red_allowlist_rejects_old_models() -> None:
    # 1. all non-allowlisted models are rejected
    with pytest.raises(ValidationError) as exc:
        Settings(
            gemini_enterprise_location="global",
            gemini_extractor_model="unsupported-model-id",
            gemini_extractor_fallback_model="gemini-3.5-flash-lite",
            gemini_resolver_model="gemini-3.1-pro-preview",
            gemini_resolver_fallback_model="gemini-3.6-flash",
            gemini_steward_model="gemini-3.5-flash-lite",
            gemini_steward_fallback_model="gemini-3.6-flash"
        )
    assert "unsupported-model-id" in str(exc.value)

def test_red_primary_and_fallback_cannot_be_identical() -> None:
    # 2. primary and fallback cannot be identical
    with pytest.raises(ValidationError) as exc:
        Settings(
            gemini_enterprise_location="global",
            gemini_extractor_model="gemini-3.6-flash",
            gemini_extractor_fallback_model="gemini-3.6-flash",  # Same!
            gemini_resolver_model="gemini-3.1-pro-preview",
            gemini_resolver_fallback_model="gemini-3.6-flash",
            gemini_steward_model="gemini-3.5-flash-lite",
            gemini_steward_fallback_model="gemini-3.6-flash"
        )
    assert "Primary and fallback models must differ" in str(exc.value)

def test_red_provider_construction_requires_project_and_global_location() -> None:
    # 4. provider construction requires project and global location
    from creditlock.agents.provider import GoogleModelProvider
    from creditlock.settings import get_settings
    get_settings.cache_clear()
    
    # If project is empty
    os.environ["GOOGLE_CLOUD_PROJECT"] = ""
    get_settings.cache_clear()
    with pytest.raises(ModelConfigurationError) as exc:
        GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    assert "google_cloud_project" in str(exc.value)

    # If location is not global
    os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "us-central1"
    get_settings.cache_clear()
    with pytest.raises(ModelConfigurationError) as exc:
        GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    assert "gemini_enterprise_location" in str(exc.value)

def test_red_client_receives_explicit_enterprise_args(monkeypatch: Any) -> None:
    # 5. client receives enterprise=True, explicit project and explicit global location
    from google import genai

    from creditlock.agents.provider import GoogleModelProvider

    captured_kwargs = {}
    class MockClient:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setattr(genai, "Client", MockClient)
    
    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")

    assert captured_kwargs.get("enterprise") is True
    assert captured_kwargs.get("project") == "pak-uni-scraper"
    assert captured_kwargs.get("location") == "global"

def test_red_primary_success_records_one_attempt(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider
    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    
    # Mock successful call
    def mock_generate(*args, **kwargs):
        class MockResp:
            text = '{"success": true}'
        return MockResp()
    
    provider._client.models.generate_content = mock_generate

    result = provider.generate_structured(prompt="test", response_schema=DummySchema)
    
    assert isinstance(result, StructuredGeneration)
    assert len(result.provenance.attempts) == 1
    assert result.provenance.attempts[0].outcome == "SUCCESS"
    assert result.provenance.fallback_occurred is False
    assert result.provenance.actual_model_used == "gemini-3.6-flash"

def test_red_allowed_primary_unavailable_records_two_ordered_attempts(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider
    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    
    call_count = 0
    def mock_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            from google.genai.errors import APIError
            raise APIError("503 Service Unavailable", 503, {})
        class MockResp:
            text = '{"success": true}'
        return MockResp()
    
    provider._client.models.generate_content = mock_generate

    result = provider.generate_structured(prompt="test", response_schema=DummySchema)
    
    assert len(result.provenance.attempts) == 2
    assert result.provenance.attempts[0].outcome == "ERROR"
    assert result.provenance.attempts[0].model_id == "gemini-3.6-flash"
    assert result.provenance.attempts[1].outcome == "SUCCESS"
    assert result.provenance.attempts[1].model_id == "gemini-3.5-flash-lite"
    assert result.provenance.fallback_occurred is True
    assert result.provenance.actual_model_used == "gemini-3.5-flash-lite"

def test_red_schema_validation_failure_does_not_call_fallback(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.models import ModelCallAttempt
    from creditlock.agents.provider import GoogleModelProvider
    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    
    call_attempts: list[ModelCallAttempt] = []

    def mock_generate(*args: Any, **kwargs: Any) -> Any:
        class MockResp:
            text = ""
        return MockResp()

    provider._client.models.generate_content = mock_generate

    with pytest.raises(ModelOutputValidationError):
        provider._execute_call("gemini-3.6-flash", "test", DummySchema, None, call_attempts)

    assert len(call_attempts) == 1
    assert call_attempts[0].outcome == "ERROR"
    assert call_attempts[0].sanitized_error_code == "VALIDATION_ERROR"

def test_red_dual_availability_failure_fails_closed(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider
    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    
    def mock_generate(*args, **kwargs):
        from google.genai.errors import APIError
        raise APIError("503 Service Unavailable", 503, {})
    
    provider._client.models.generate_content = mock_generate

    with pytest.raises(ModelInvocationUnavailableError):
        provider.generate_structured(prompt="test", response_schema=DummySchema)


def test_red_authentication_error_does_not_fallback(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider

    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    call_count = 0

    def mock_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        from google.genai.errors import APIError
        raise APIError("401 Unauthorized credentials", 401, {})

    provider._client.models.generate_content = mock_generate

    with pytest.raises(ModelAuthenticationError):
        provider.generate_structured(prompt="test", response_schema=DummySchema)

    assert call_count == 1  # Did NOT call fallback!


def test_red_permission_quota_error_does_not_fallback(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider

    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    call_count = 0

    def mock_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded for project")

    provider._client.models.generate_content = mock_generate

    with pytest.raises(ModelPermissionError):
        provider.generate_structured(prompt="test", response_schema=DummySchema)

    assert call_count == 1  # Quota exhaustion MUST NOT trigger fallback!


def test_red_safety_blocked_error_does_not_fallback(monkeypatch: Any) -> None:
    from pydantic import BaseModel

    from creditlock.agents.provider import GoogleModelProvider

    class DummySchema(BaseModel):
        success: bool

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"

    provider = GoogleModelProvider(agent_role="extractor", primary_model_id="gemini-3.6-flash", fallback_model_id="gemini-3.5-flash-lite")
    call_count = 0

    def mock_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        from google.genai.errors import APIError
        raise APIError("Content blocked due to safety settings", 400, {})

    provider._client.models.generate_content = mock_generate

    with pytest.raises(ModelSafetyBlockedError):
        provider.generate_structured(prompt="test", response_schema=DummySchema)

    assert call_count == 1  # Safety block MUST NOT trigger fallback!


def test_red_agent_routing_isolation() -> None:
    from creditlock.agents.extractor import ExtractorAgent
    from creditlock.agents.resolver import PrecedenceResolverAgent
    from creditlock.agents.steward import StewardAgent
    from creditlock.settings import get_settings

    os.environ["GOOGLE_CLOUD_PROJECT"] = "pak-uni-scraper"
    os.environ["GEMINI_ENTERPRISE_LOCATION"] = "global"
    os.environ["JWT_SECRET"] = "secretsecretsecretsecretsecretsecret"
    os.environ["STORE_BACKEND"] = "memory_demo"
    os.environ["ALLOW_IN_MEMORY_DEMO"] = "true"
    get_settings.cache_clear()

    extractor = ExtractorAgent()
    resolver = PrecedenceResolverAgent()
    steward = StewardAgent()

    assert extractor.provider.primary_model_id == "gemini-3.6-flash"
    assert extractor.provider.fallback_model_id == "gemini-3.5-flash-lite"

    assert resolver.provider.primary_model_id == "gemini-3.1-pro-preview"
    assert resolver.provider.fallback_model_id == "gemini-3.6-flash"

    assert steward.provider.primary_model_id == "gemini-3.5-flash-lite"
    assert steward.provider.fallback_model_id == "gemini-3.6-flash"


def test_red_api_error_handling_no_500(monkeypatch: Any) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from creditlock.agents.app import agent_router
    from creditlock.agents.provider import (
        ModelInvocationUnavailableError,
        ModelOutputValidationError,
    )

    app = FastAPI()
    app.include_router(agent_router)
    client = TestClient(app)

    errors_to_test = [
        (ModelAuthenticationError("auth failed"), 401),
        (ModelPermissionError("perm denied"), 403),
        (ModelInvocationUnavailableError("unavailable"), 503),
        (ModelOutputValidationError("invalid schema"), 422),
        (ModelSafetyBlockedError("blocked safety"), 400),
    ]

    for err_instance, expected_status in errors_to_test:
        def mock_extract(*args: Any, target_err: Exception = err_instance, **kwargs: Any) -> None:
            raise target_err

        monkeypatch.setattr("creditlock.agents.extractor.ExtractorAgent.extract_from_document", mock_extract)

        resp = client.post(
            "/agents/extract",
            json={
                "document_uri": "gs://b/d.txt",
                "document_hash": "hash",
                "text_content": "text",
                "production_id": "p1",
            },
        )
        assert resp.status_code == expected_status
        assert resp.status_code != 500


