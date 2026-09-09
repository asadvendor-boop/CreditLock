"""
Data models for bounded ADK credit steward agent ensemble.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

from creditlock.domain.models import Obligation, Patch

T = TypeVar("T")


class ModelCallAttempt(BaseModel):
    model_config = {"extra": "forbid"}

    model_id: str
    outcome: Literal["SUCCESS", "ERROR"]
    latency_ms: float
    sanitized_error_code: str | None


class CallProvenance(BaseModel):
    model_config = {"extra": "forbid"}

    agent_role: str
    primary_model: str
    configured_fallback_model: str
    actual_model_used: str | None
    fallback_occurred: bool
    fallback_reason_code: str | None = None
    platform: str
    auth_mode: str
    started_at_utc: str
    total_latency_ms: float
    attempts: list[ModelCallAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_provenance_integrity(self) -> CallProvenance:
        successful_attempts = [a for a in self.attempts if a.outcome == "SUCCESS"]
        if self.actual_model_used is not None:
            if len(successful_attempts) != 1:
                raise ValueError("Success cannot be reported without exactly one successful attempt.")
            if successful_attempts[0].model_id != self.actual_model_used:
                raise ValueError(
                    f"actual_model_used '{self.actual_model_used}' does not match "
                    f"successful attempt model_id '{successful_attempts[0].model_id}'."
                )
            if self.attempts[-1].outcome != "SUCCESS":
                raise ValueError("Last attempt must be SUCCESS if actual_model_used is populated.")
        else:
            if len(successful_attempts) != 0:
                raise ValueError("actual_model_used cannot be None if there is a successful attempt.")

        expected_fallback = (
            len(self.attempts) > 1
            and any(a.outcome == "ERROR" for a in self.attempts[:-1])
            and (self.attempts[-1].outcome == "SUCCESS")
        )
        if self.fallback_occurred != expected_fallback:
            raise ValueError(
                f"fallback_occurred ({self.fallback_occurred}) does not match attempt sequence."
            )

        return self


class StructuredGeneration(BaseModel, Generic[T]):  # noqa: UP046
    model_config = {"extra": "forbid"}

    output: T
    provenance: CallProvenance


class DocumentReference(BaseModel):
    """URI plus SHA-256 handoff for large document state."""

    model_config = {"extra": "forbid"}

    uri: str
    sha256_hash: str
    mime_type: str = Field(default="text/plain")


class ExtractionResult(BaseModel):
    """Output from Extractor agent."""

    model_config = {"extra": "forbid"}

    document_ref: DocumentReference
    extracted_obligations: list[Obligation]
    provenance: CallProvenance
    raw_candidate_count: int = 0
    accepted_count: int = 0
    rejection_diagnostics: list[dict[str, str]] = Field(default_factory=list)


class PrecedenceRecommendation(BaseModel):
    """Output from Precedence Resolver agent."""

    model_config = {"extra": "forbid"}

    recommendation: Literal["RESOLVE", "ABSTAIN", "CONFLICT"]
    candidate_obligation_ids: list[str]
    controlling_obligation_id: str | None = None
    rationale: str
    provenance: CallProvenance | None = None


class StewardProposal(BaseModel):
    """Output from Steward agent."""

    model_config = {"extra": "forbid"}

    issue_id: str
    explanation: str
    patch_proposal: Patch | None = None
    validated: bool = False
    validation_reason: str | None = None
    provenance: CallProvenance | None = None
