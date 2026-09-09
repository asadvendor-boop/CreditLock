"""
FastAPI agent-route endpoints.

Exposes REST endpoints for Extractor, Precedence Resolver, and Steward agents.
Fails closed with typed, non-500 HTTP responses on provider failures.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import (
    DocumentReference,
    ExtractionResult,
    PrecedenceRecommendation,
    StewardProposal,
)
from creditlock.agents.provider import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelInvocationUnavailableError,
    ModelOutputValidationError,
    ModelPermissionError,
    ModelSafetyBlockedError,
    ModelUnsupportedError,
)
from creditlock.agents.resolver import PrecedenceResolverAgent
from creditlock.agents.steward import StewardAgent
from creditlock.domain.models import CreditManifest, Issue, Obligation

agent_router = APIRouter(prefix="/agents", tags=["agents"])


class ExtractionRequest(BaseModel):
    document_uri: str
    document_hash: str
    text_content: str
    production_id: str


class ResolutionRequest(BaseModel):
    candidate_obligations: list[Obligation]


class StewardRequest(BaseModel):
    issue: Issue
    obligation: Obligation | None = None
    manifest: CreditManifest


def _handle_provider_error(ex: Exception) -> None:
    if isinstance(ex, ModelAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTHENTICATION_ERROR", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelPermissionError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "PERMISSION_ERROR", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelInvocationUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "MODEL_UNAVAILABLE", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelOutputValidationError):
        raise HTTPException(
            status_code=422,
            detail={"error_code": "VALIDATION_ERROR", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelSafetyBlockedError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "SAFETY_BLOCKED", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelConfigurationError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "CONFIGURATION_ERROR", "message": str(ex)},
        ) from ex
    if isinstance(ex, ModelUnsupportedError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error_code": "UNSUPPORTED_ERROR", "message": str(ex)},
        ) from ex


@agent_router.post("/extract", response_model=ExtractionResult)
async def extract_obligations(req: ExtractionRequest) -> ExtractionResult:
    try:
        agent = ExtractorAgent()
        doc_ref = DocumentReference(uri=req.document_uri, sha256_hash=req.document_hash)
        return agent.extract_from_document(doc_ref, req.text_content, req.production_id)
    except Exception as ex:
        _handle_provider_error(ex)
        raise


@agent_router.post("/resolve", response_model=list[PrecedenceRecommendation])
async def resolve_precedence(req: ResolutionRequest) -> list[PrecedenceRecommendation]:
    try:
        agent = PrecedenceResolverAgent()
        return agent.recommend_precedence(req.candidate_obligations)
    except Exception as ex:
        _handle_provider_error(ex)
        raise


@agent_router.post("/steward", response_model=StewardProposal)
async def steward_proposal(req: StewardRequest) -> StewardProposal:
    try:
        agent = StewardAgent()
        return agent.explain_finding_and_propose_patch(req.issue, req.obligation, req.manifest)
    except Exception as ex:
        _handle_provider_error(ex)
        raise
