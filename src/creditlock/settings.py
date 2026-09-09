"""
Settings for CreditLock — loaded from environment variables.
"""

from __future__ import annotations

import math
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    google_cloud_project: str = ""
    gemini_enterprise_location: str = "global"
    gemini_extractor_model: str = "gemini-3.6-flash"
    gemini_extractor_fallback_model: str = "gemini-3.5-flash-lite"
    gemini_resolver_model: str = "gemini-3.1-pro-preview"
    gemini_resolver_fallback_model: str = "gemini-3.6-flash"
    gemini_steward_model: str = "gemini-3.5-flash-lite"
    gemini_steward_fallback_model: str = "gemini-3.6-flash"
    firestore_database: str = "(default)"
    evidence_bucket: str = ""
    confluent_bootstrap_servers: str = ""
    confluent_api_key: str = ""
    confluent_api_secret: str = ""
    confluent_schema_registry_url: str = ""
    confluent_schema_registry_api_key: str = ""
    confluent_schema_registry_api_secret: str = ""
    confluent_topic: str = "creditlock.production.events"
    confluent_runtime_enabled: bool = False
    confluent_sync_timeout_seconds: float = 12.0
    confluent_sync_poll_interval_seconds: float = 0.25
    confluent_mcp_server_url: str = ""
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480
    render_profile_version: str = "v1"
    gate_policy_version: str = "v1"
    store_backend: str = ""
    allow_in_memory_demo: bool = False
    enable_judge_demo: bool = False
    require_real_chrome: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_gemini_models(self) -> Settings:
        allowlist = {"gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"}

        models = [
            self.gemini_extractor_model,
            self.gemini_extractor_fallback_model,
            self.gemini_resolver_model,
            self.gemini_resolver_fallback_model,
            self.gemini_steward_model,
            self.gemini_steward_fallback_model,
        ]

        for model in models:
            if model not in allowlist:
                raise ValueError(f"Model '{model}' is not in the allowed list: {allowlist}")

        if self.gemini_extractor_model == self.gemini_extractor_fallback_model:
            raise ValueError("Primary and fallback models must differ for Extractor")
        if self.gemini_resolver_model == self.gemini_resolver_fallback_model:
            raise ValueError("Primary and fallback models must differ for Resolver")
        if self.gemini_steward_model == self.gemini_steward_fallback_model:
            raise ValueError("Primary and fallback models must differ for Steward")

        return self

    @field_validator("jwt_secret", mode="after")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v) < 32 or v == "dev-secret-change-in-production-xx":
            raise ValueError(
                "JWT_SECRET is missing, weak (< 32 characters), or set to known default. Production startup failed closed."
            )
        return v

    @model_validator(mode="after")
    def validate_store_backend(self) -> Settings:
        cleaned = self.store_backend.strip().lower()
        if not cleaned:
            raise ValueError(
                "STORE_BACKEND is missing or unconfigured. Production startup failed closed. "
                "Specify STORE_BACKEND='memory_demo' with ALLOW_IN_MEMORY_DEMO=true for local development/testing, "
                "or STORE_BACKEND='firestore' with GOOGLE_CLOUD_PROJECT."
            )
        if cleaned in ("memory_demo", "memory"):
            if not self.allow_in_memory_demo:
                raise ValueError(
                    "In-memory production store selected ('memory_demo') but ALLOW_IN_MEMORY_DEMO is False. "
                    "Ephemeral storage must be explicitly enabled via ALLOW_IN_MEMORY_DEMO=true."
                )
            self.store_backend = "memory_demo"
            return self
        if cleaned == "firestore":
            if not self.google_cloud_project:
                raise ValueError(
                    "STORE_BACKEND='firestore' requires a non-empty GOOGLE_CLOUD_PROJECT setting."
                )
            self.store_backend = "firestore"
            return self
        raise ValueError(
            f"Unrecognized STORE_BACKEND '{self.store_backend}'. Supported values: 'memory_demo', 'firestore'."
        )

    @model_validator(mode="after")
    def validate_confluent_runtime_settings(self) -> Settings:
        if not (
            math.isfinite(self.confluent_sync_timeout_seconds)
            and self.confluent_sync_timeout_seconds > 0
        ):
            raise ValueError("confluent_sync_timeout_seconds must be a finite positive number")
        if self.confluent_sync_timeout_seconds > 30.0:
            raise ValueError("confluent_sync_timeout_seconds must not exceed 30.0 seconds")
        if not (
            math.isfinite(self.confluent_sync_poll_interval_seconds)
            and self.confluent_sync_poll_interval_seconds > 0
        ):
            raise ValueError("confluent_sync_poll_interval_seconds must be a finite positive number")

        if self.confluent_runtime_enabled:
            missing: list[str] = []
            if not self.google_cloud_project:
                missing.append("GOOGLE_CLOUD_PROJECT")
            if not self.confluent_bootstrap_servers:
                missing.append("CONFLUENT_BOOTSTRAP_SERVERS")
            if not self.confluent_api_key:
                missing.append("CONFLUENT_API_KEY")
            if not self.confluent_api_secret:
                missing.append("CONFLUENT_API_SECRET")
            if not self.confluent_topic:
                missing.append("CONFLUENT_TOPIC")
            if missing:
                raise ValueError(
                    f"CONFLUENT_RUNTIME_ENABLED is True but required settings are missing: {', '.join(missing)}"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
