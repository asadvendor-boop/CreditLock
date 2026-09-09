"""
Human resolutions and authorizations API endpoints.

Two-person authority model:
- REVIEWER creates a Proposal via POST /productions/{id}/proposals.
- RELEASE_APPROVER confirms a Proposal via POST /productions/{id}/proposals/{proposal_id}/confirm.
- Proposer and Approver identities MUST be distinct.
- Authorizations are created atomically via ProductionStore.confirm_proposal().
- Direct authorization creation via /authorizations is retired (HTTP 410).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from creditlock.api.auth import check_token_production_access, get_current_actor
from creditlock.api.export import _get_production, get_production_store
from creditlock.domain.models import Authorization, AuthorizationAction, Proposal

router = APIRouter()


class ResolutionRequest(BaseModel):
    issue_id: str
    action: AuthorizationAction
    reason: str = Field(min_length=1)
    selected_contributor_id: str | None = None


class ResolutionResponse(BaseModel):
    authorization: Authorization
    recorded_at: str


class ProposalRequest(BaseModel):
    issue_id: str
    action: AuthorizationAction
    reason: str = Field(min_length=1)
    selected_contributor_id: str | None = None


class ProposalResponse(BaseModel):
    proposal: Proposal
    created_at: str


def _compute_hashes(prod: dict[str, Any]) -> tuple[str, str, str, str]:
    from creditlock.evidence.validation import validate_artifact_evidence
    return validate_artifact_evidence(prod)



def _validate_action_and_reason(
    action: AuthorizationAction, reason: str, selected_cid: str | None
) -> str:
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reason must not be empty or whitespace-only.",
        )
    if action in (AuthorizationAction.WAIVE_OBLIGATION, AuthorizationAction.CONFIRM_PRECEDENCE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action '{action.value}' is unsupported until its scoped evidence workflow exists.",
        )
    if action == AuthorizationAction.CONFIRM_IDENTITY and (
        not selected_cid or not selected_cid.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="selected_contributor_id is required for CONFIRM_IDENTITY.",
        )
    return cleaned_reason


def _validate_actor(actor: dict[str, Any], required_role: str | None = None) -> tuple[str, str]:
    sub = actor.get("sub")
    if not sub or not str(sub).strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject 'sub' claim is missing or empty.",
        )
    role = actor.get("role")
    if role not in ("REVIEWER", "RELEASE_APPROVER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Invalid or untrusted role '{role}'. Must be 'REVIEWER' or 'RELEASE_APPROVER'.",
        )
    if required_role and role != required_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{required_role}' required. Actor has role '{role}'.",
        )
    return str(sub).strip(), str(role)


@router.post(
    "/productions/{production_id}/proposals",
    response_model=ProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_proposal(
    production_id: str,
    req: ProposalRequest,
    actor: Annotated[dict[str, Any], Depends(get_current_actor)],
) -> ProposalResponse:
    # Only REVIEWER may create a proposal
    proposer_id, _ = _validate_actor(actor, required_role="REVIEWER")
    check_token_production_access(actor, production_id)

    cleaned_reason = _validate_action_and_reason(
        req.action, req.reason, req.selected_contributor_id
    )

    store = get_production_store()
    prod = _get_production(production_id)

    from creditlock.domain.checker import CheckerInput, evaluate_findings
    from creditlock.domain.models import IssueCode

    checker_input = CheckerInput(
        obligations=prod.get("obligations", []),
        manifest=prod["manifest"],
        layout_evidence=prod.get("layout_evidence"),
        visual_observations=prod.get("visual_observations"),
        contributor_registry=prod.get("contributor_registry"),
    )
    open_issues = evaluate_findings(checker_input)
    target_issue = next((i for i in open_issues if i.issue_id == req.issue_id), None)
    if target_issue is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Issue '{req.issue_id}' not found or is not currently open.",
        )

    if (
        target_issue.code == IssueCode.AMBIGUOUS_IDENTITY
        and req.action != AuthorizationAction.CONFIRM_IDENTITY
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action '{req.action.value}' is invalid for issue code 'AMBIGUOUS_IDENTITY'.",
        )
    if (
        target_issue.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN
        and req.action != AuthorizationAction.CONFIRM_VISUAL
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action '{req.action.value}' is invalid for issue code 'VISUAL_OBSERVATION_UNCERTAIN'.",
        )

    # For CONFIRM_IDENTITY, validate selected_contributor_id against the genuine
    # candidate set derived from the obligation's obligee_text and contributor_registry.
    if req.action == AuthorizationAction.CONFIRM_IDENTITY:
        from creditlock.domain.checker import _resolve_obligee_text

        obl_for_issue = (
            next(
                (
                    o
                    for o in (prod.get("obligations") or [])
                    if (
                        o.get("obligation_id")
                        if isinstance(o, dict)
                        else getattr(o, "obligation_id", None)
                    )
                    == target_issue.obligation_id
                ),
                None,
            )
            if target_issue.obligation_id
            else None
        )
        if obl_for_issue is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Obligation for issue '{req.issue_id}' not found.",
            )

        obl_dict = (
            obl_for_issue if isinstance(obl_for_issue, dict) else obl_for_issue.model_dump()
        )
        obligee = obl_dict.get("obligee_text") or ""
        if not obligee.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Obligation '{target_issue.obligation_id}' has blank obligee_text.",
            )

        registry = prod.get("contributor_registry") or []
        valid_cids = set(_resolve_obligee_text(obligee, registry))
        if not req.selected_contributor_id or req.selected_contributor_id not in valid_cids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"selected_contributor_id '{req.selected_contributor_id}' is not "
                    f"a valid candidate for issue '{req.issue_id}'. "
                    f"Valid candidates: {sorted(valid_cids)}."
                ),
            )

    try:
        m_hash, orv_hash, art_digest, vis_hash = _compute_hashes(prod)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    import uuid
    from datetime import UTC, datetime

    now_iso = datetime.now(UTC).isoformat()

    prop = Proposal(
        proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
        production_id=production_id,
        issue_id=req.issue_id,
        proposer_id=proposer_id,
        proposer_role="REVIEWER",
        action=req.action,
        reason=cleaned_reason,
        selected_contributor_id=req.selected_contributor_id,
        manifest_hash=m_hash,
        obligation_registry_version_hash=orv_hash,
        artifact_index_digest=art_digest,
        visual_observations_hash=vis_hash,
        created_at=now_iso,
        status="PENDING",
    )

    store.save_proposal(prop)
    return ProposalResponse(proposal=prop, created_at=now_iso)


@router.post(
    "/productions/{production_id}/proposals/{proposal_id}/confirm",
    response_model=ResolutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_proposal(
    production_id: str,
    proposal_id: str,
    actor: Annotated[dict[str, Any], Depends(get_current_actor)],
) -> ResolutionResponse:
    # Only RELEASE_APPROVER may confirm a proposal
    approver_id, _ = _validate_actor(actor, required_role="RELEASE_APPROVER")
    check_token_production_access(actor, production_id)

    store = get_production_store()
    prod = _get_production(production_id)

    # Validate issue is currently open
    from creditlock.domain.checker import CheckerInput, evaluate_findings
    from creditlock.domain.models import IssueCode

    checker_input = CheckerInput(
        obligations=prod.get("obligations", []),
        manifest=prod["manifest"],
        layout_evidence=prod.get("layout_evidence"),
        visual_observations=prod.get("visual_observations"),
        contributor_registry=prod.get("contributor_registry"),
    )
    open_issues = evaluate_findings(checker_input)

    props = store.get_proposals(production_id)
    prop = next((p for p in props if p.proposal_id == proposal_id), None)
    if prop is None or prop.status != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal '{proposal_id}' not found or is not pending.",
        )

    target_issue = next((i for i in open_issues if i.issue_id == prop.issue_id), None)
    if target_issue is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target issue '{prop.issue_id}' is no longer open.",
        )

    if (
        target_issue.code == IssueCode.AMBIGUOUS_IDENTITY
        and prop.action != AuthorizationAction.CONFIRM_IDENTITY
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action '{prop.action.value}' is invalid for issue code 'AMBIGUOUS_IDENTITY'.",
        )
    if (
        target_issue.code == IssueCode.VISUAL_OBSERVATION_UNCERTAIN
        and prop.action != AuthorizationAction.CONFIRM_VISUAL
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action '{prop.action.value}' is invalid for issue code 'VISUAL_OBSERVATION_UNCERTAIN'.",
        )

    try:
        current_hashes = _compute_hashes(prod)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    try:
        auth = store.confirm_proposal(production_id, proposal_id, approver_id, current_hashes)
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        detail_str = str(e)
        if "distinct" in detail_str.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail_str)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail_str)

    return ResolutionResponse(authorization=auth, recorded_at=auth.authorized_at)


@router.post(
    "/productions/{production_id}/authorizations",
    status_code=status.HTTP_410_GONE,
)
async def record_authorization(
    production_id: str,
    req: ResolutionRequest,
    actor: Annotated[dict[str, Any], Depends(get_current_actor)],
) -> ResolutionResponse:
    # Direct authorization creation is retired
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Direct authorization creation is retired and unsupported. All authorizations require a proposal "
            "created by a REVIEWER and confirmed by a distinct RELEASE_APPROVER via "
            "POST /productions/{production_id}/proposals/{proposal_id}/confirm."
        ),
    )
