"""
Injectable Google Model Provider boundary for Gemini LLM calls via google.genai SDK.

Supports real Gemini model calls in production / live tests and mock/fake providers
in offline unit tests.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from creditlock.agents.models import CallProvenance, ModelCallAttempt, StructuredGeneration
from creditlock.settings import get_settings

T = TypeVar("T", bound=BaseModel)


class ModelConfigurationError(Exception):
    """Raised when Gemini project, location, or model parameters are invalid."""


class ModelAuthenticationError(Exception):
    """Raised when authentication credentials (ADC / API Key) are invalid or missing."""


class ModelPermissionError(Exception):
    """Raised when permissions are denied or project quota is exhausted."""


class ModelInvocationUnavailableError(Exception):
    """Raised ONLY when Gemini service is genuinely unavailable or overloaded."""


class ModelOutputValidationError(Exception):
    """Raised when model output fails JSON parsing or Pydantic schema validation."""


class ModelSafetyBlockedError(Exception):
    """Raised when prompt or response is blocked by safety filters."""


class ModelUnsupportedError(Exception):
    """Raised for unmapped or bad client requests."""


class ModelProvider(Protocol):
    """Abstract model provider protocol."""

    agent_role: str
    primary_model_id: str
    fallback_model_id: str

    def is_available(self) -> bool: ...

    def generate_structured(
        self,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
    ) -> StructuredGeneration[T]: ...


class GoogleModelProvider:
    """Production Gemini model provider using google.genai SDK."""

    def __init__(
        self,
        agent_role: str,
        primary_model_id: str,
        fallback_model_id: str,
    ) -> None:
        self.agent_role = agent_role
        self.primary_model_id = primary_model_id
        self.fallback_model_id = fallback_model_id

        self.settings = get_settings()
        if not self.settings.google_cloud_project:
            raise ModelConfigurationError("Settings google_cloud_project cannot be empty")
        if self.settings.gemini_enterprise_location != "global":
            raise ModelConfigurationError("Settings gemini_enterprise_location must be 'global'")

        self.platform = "gemini_enterprise_agent_platform"
        self.auth_mode = "adc"
        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        from google import genai
        try:
            self._client = genai.Client(
                enterprise=True,
                project=self.settings.google_cloud_project,
                location=self.settings.gemini_enterprise_location,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "credentials" in err_str or "auth" in err_str or "default" in err_str:
                raise ModelAuthenticationError("Failed to authenticate with Google Cloud ADC.") from e
            raise ModelConfigurationError("Failed to configure Enterprise Gemini client.") from e

    def is_available(self) -> bool:
        return self._client is not None

    def _map_sdk_error(self, e: Exception) -> Exception:
        from google.genai.errors import APIError

        err_str = str(e).lower()

        # Quota exhaustion must NEVER be classified as model unavailability
        if "quota" in err_str or "resource_exhausted" in err_str or "resourceexhausted" in err_str:
            return ModelPermissionError("Project quota exhausted.")

        if isinstance(e, APIError):
            code = getattr(e, "code", None)
            if code == 401 or "authentication" in err_str or "unauthenticated" in err_str or "credentials" in err_str:
                return ModelAuthenticationError("Authentication failed with Gemini API.")
            if code == 403 or "permission" in err_str or "forbidden" in err_str:
                return ModelPermissionError("Permission denied for Gemini API.")
            if "safety" in err_str or "blocked" in err_str:
                return ModelSafetyBlockedError("Response was blocked by safety filters.")
            if code in (503, 502, 504) or "unavailable" in err_str or "overloaded" in err_str or "capacity" in err_str:
                return ModelInvocationUnavailableError("Gemini service temporarily unavailable.")
            if code == 400:
                return ModelUnsupportedError("Invalid request to Gemini API.")

        if "unavailable" in err_str or "503" in err_str or "connection" in err_str:
            return ModelInvocationUnavailableError("Gemini service temporarily unavailable.")

        return ModelUnsupportedError("Unexpected SDK error encountered.")

    def _sanitized_code_for_exception(self, ex: Exception) -> str:
        if isinstance(ex, ModelAuthenticationError):
            return "AUTHENTICATION_ERROR"
        if isinstance(ex, ModelPermissionError):
            return "PERMISSION_ERROR"
        if isinstance(ex, ModelInvocationUnavailableError):
            return "MODEL_UNAVAILABLE"
        if isinstance(ex, ModelOutputValidationError):
            return "VALIDATION_ERROR"
        if isinstance(ex, ModelSafetyBlockedError):
            return "SAFETY_BLOCKED"
        if isinstance(ex, ModelConfigurationError):
            return "CONFIGURATION_ERROR"
        return "UNSUPPORTED_ERROR"

    def _execute_call(
        self,
        model_id: str,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None,
        attempts: list[ModelCallAttempt],
    ) -> T:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            temperature=0.0,
        )
        if system_instruction:
            config.system_instruction = system_instruction

        start_time = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=config,
            )
            elapsed_ms = (time.monotonic() - start_time) * 1000.0

            if not response or not response.text:
                attempts.append(
                    ModelCallAttempt(
                        model_id=model_id,
                        outcome="ERROR",
                        latency_ms=elapsed_ms,
                        sanitized_error_code="VALIDATION_ERROR",
                    )
                )
                raise ModelOutputValidationError("Empty response text from Gemini model.")

            parsed = response_schema.model_validate_json(response.text)
            attempts.append(
                ModelCallAttempt(
                    model_id=model_id,
                    outcome="SUCCESS",
                    latency_ms=elapsed_ms,
                    sanitized_error_code=None,
                )
            )
            return parsed
        except ModelOutputValidationError:
            raise
        except (ValidationError, ValueError) as ve:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            attempts.append(
                ModelCallAttempt(
                    model_id=model_id,
                    outcome="ERROR",
                    latency_ms=elapsed_ms,
                    sanitized_error_code="VALIDATION_ERROR",
                )
            )
            raise ModelOutputValidationError(f"Schema validation failed: {ve}") from ve
        except Exception as ex:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            mapped_ex = self._map_sdk_error(ex)
            err_code = self._sanitized_code_for_exception(mapped_ex)
            attempts.append(
                ModelCallAttempt(
                    model_id=model_id,
                    outcome="ERROR",
                    latency_ms=elapsed_ms,
                    sanitized_error_code=err_code,
                )
            )
            raise mapped_ex from ex

    def generate_structured(
        self,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
    ) -> StructuredGeneration[T]:
        started_at = datetime.now(UTC).isoformat()
        attempts: list[ModelCallAttempt] = []
        fallback_reason_code: str | None = None

        # 1. Primary Model Attempt
        try:
            output = self._execute_call(
                self.primary_model_id, prompt, response_schema, system_instruction, attempts
            )
            prov = CallProvenance(
                agent_role=self.agent_role,
                primary_model=self.primary_model_id,
                configured_fallback_model=self.fallback_model_id,
                actual_model_used=self.primary_model_id,
                fallback_occurred=False,
                fallback_reason_code=None,
                platform=self.platform,
                auth_mode=self.auth_mode,
                started_at_utc=started_at,
                total_latency_ms=sum(a.latency_ms for a in attempts),
                attempts=attempts,
            )
            return StructuredGeneration(output=output, provenance=prov)
        except ModelInvocationUnavailableError:
            fallback_reason_code = "MODEL_UNAVAILABLE"
            # ONLY ModelInvocationUnavailableError allows continuing to fallback!

        # 2. Fallback Model Attempt
        output = self._execute_call(
            self.fallback_model_id, prompt, response_schema, system_instruction, attempts
        )
        prov = CallProvenance(
            agent_role=self.agent_role,
            primary_model=self.primary_model_id,
            configured_fallback_model=self.fallback_model_id,
            actual_model_used=self.fallback_model_id,
            fallback_occurred=True,
            fallback_reason_code=fallback_reason_code,
            platform=self.platform,
            auth_mode=self.auth_mode,
            started_at_utc=started_at,
            total_latency_ms=sum(a.latency_ms for a in attempts),
            attempts=attempts,
        )
        return StructuredGeneration(output=output, provenance=prov)


class FakeModelProvider:
    """In-memory model provider for offline unit tests."""

    def __init__(
        self,
        agent_role: str = "fake-role",
        primary_model_id: str = "gemini-3.6-flash",
        fallback_model_id: str = "gemini-3.5-flash-lite",
        responses: dict[str, Any] | list[Any] | None = None,
        available: bool = True,
        simulate_primary_unavailable: bool = False,
    ) -> None:
        self.agent_role = agent_role
        self.primary_model_id = primary_model_id
        self.fallback_model_id = fallback_model_id
        self.responses = responses
        self.available = available
        self.simulate_primary_unavailable = simulate_primary_unavailable
        self.platform = "fake_platform"
        self.auth_mode = "fake_auth"

    def is_available(self) -> bool:
        return self.available

    def generate_structured(
        self,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
    ) -> StructuredGeneration[T]:
        if not self.available:
            raise ModelInvocationUnavailableError("Fake model provider is set to unavailable.")

        attempts: list[ModelCallAttempt] = []
        started_at = datetime.now(UTC).isoformat()

        if self.simulate_primary_unavailable:
            attempts.append(
                ModelCallAttempt(
                    model_id=self.primary_model_id,
                    outcome="ERROR",
                    latency_ms=5.0,
                    sanitized_error_code="MODEL_UNAVAILABLE",
                )
            )
            model_used = self.fallback_model_id
            fallback_occurred = True
            fallback_reason_code = "MODEL_UNAVAILABLE"
        else:
            model_used = self.primary_model_id
            fallback_occurred = False
            fallback_reason_code = None

        output: T | None = None
        if self.responses is not None:
            if isinstance(self.responses, list) and self.responses:
                item = self.responses.pop(0)
                if isinstance(item, response_schema):
                    output = item
                elif isinstance(item, dict):
                    output = response_schema.model_validate(item)
                elif isinstance(item, str):
                    output = response_schema.model_validate_json(item)
            elif isinstance(self.responses, dict):
                for key, resp in self.responses.items():
                    if key in prompt or key == "default":
                        if isinstance(resp, response_schema):
                            output = resp
                            break
                        if isinstance(resp, dict):
                            output = response_schema.model_validate(resp)
                            break
                        if isinstance(resp, str):
                            output = response_schema.model_validate_json(resp)
                            break

        if output is None:
            attempts.append(
                ModelCallAttempt(
                    model_id=model_used,
                    outcome="ERROR",
                    latency_ms=5.0,
                    sanitized_error_code="VALIDATION_ERROR",
                )
            )
            raise ModelOutputValidationError(f"No mock response configured for prompt: '{prompt[:50]}...'")

        attempts.append(
            ModelCallAttempt(
                model_id=model_used,
                outcome="SUCCESS",
                latency_ms=10.0,
                sanitized_error_code=None,
            )
        )

        prov = CallProvenance(
            agent_role=self.agent_role,
            primary_model=self.primary_model_id,
            configured_fallback_model=self.fallback_model_id,
            actual_model_used=model_used,
            fallback_occurred=fallback_occurred,
            fallback_reason_code=fallback_reason_code,
            platform=self.platform,
            auth_mode=self.auth_mode,
            started_at_utc=started_at,
            total_latency_ms=sum(a.latency_ms for a in attempts),
            attempts=attempts,
        )

        return StructuredGeneration(output=output, provenance=prov)
