"""
Narrowly-scoped, session-isolated demo API endpoints protected behind ENABLE_JUDGE_DEMO=true.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference, ExtractionResult
from creditlock.agents.provider import ModelInvocationUnavailableError, ModelProvider
from creditlock.api.auth import (
    check_token_production_access,
    create_token,
    get_current_actor,
    require_role,
)
from creditlock.api.export import get_production_store
from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import (
    CreditManifest,
    IssueCode,
    Obligation,
    VisualObservation,
    VisualObservations,
)
from creditlock.renderer.service import RendererService
from creditlock.settings import get_settings

router = APIRouter(prefix="/demo/api", tags=["demo"])

# Session budget tracking for analyze (in-memory per Cloud Run instance)
_demo_session_attempts: dict[str, int] = {}
_demo_active_analysis: set[str] = set()
_demo_override_provider: ModelProvider | None = None


def set_demo_override_provider(provider: ModelProvider | None) -> None:
    """Allow injecting fake/mock ModelProvider for unit tests."""
    global _demo_override_provider
    _demo_override_provider = provider


def _check_demo_enabled() -> None:
    settings = get_settings()
    if not settings.enable_judge_demo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Judge demo endpoints are disabled. Set ENABLE_JUDGE_DEMO=true to enable.",
        )


class DemoInitResponse(BaseModel):
    model_config = {"extra": "forbid"}
    status: str = "initialized"
    demo_session_id: str
    production_id: str
    gate_state: str
    issue_count: int
    reviewer_token: str
    approver_token: str
    reviewer_id: str
    approver_id: str
    render_backend: str
    app_commit: str = "unknown"
    k_revision: str = "unknown"


class DemoTokensResponse(BaseModel):
    model_config = {"extra": "forbid"}
    reviewer_token: str
    approver_token: str
    reviewer_id: str
    approver_id: str


class AnalyzeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    production_id: str


class DemoAnalysisResponse(BaseModel):
    model_config = {"extra": "forbid"}
    model_used: str
    fallback_occurred: bool
    extracted_obligations: list[dict[str, Any]]
    rejection_diagnostics: list[dict[str, Any]]
    deterministic_issue: dict[str, Any]
    required_human_action: str = "CONFIRM_IDENTITY"
    product_distinction_note: str = (
        "Gemini extracts obligation evidence from source text. "
        "CreditLock deterministically detects ambiguous identity. "
        "Humans authorize identity bindings."
    )


def _load_demo_fixture() -> dict[str, Any]:
    fixture_path = Path("fixtures/demo/demo_production.json")
    if not fixture_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Demo fixture file '{fixture_path}' is missing.",
        )
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@router.post("/init", response_model=DemoInitResponse)
async def initialize_demo() -> DemoInitResponse:
    _check_demo_enabled()
    store = get_production_store()
    settings = get_settings()

    demo_session_id = f"sess_{uuid.uuid4().hex[:12]}"
    production_id = f"prod_demo_{demo_session_id[5:]}"
    reviewer_id = f"reviewer_{demo_session_id[5:]}"
    approver_id = f"approver_{demo_session_id[5:]}"

    fixture = _load_demo_fixture()
    obligations = [Obligation.model_validate(o) for o in fixture["obligations"]]

    # Ensure in-memory manifest entries for demo flow align so AMBIGUOUS_IDENTITY is the authorizing issue
    manifest_dict = dict(fixture["manifest"])
    manifest_dict["production_id"] = production_id
    entries = [dict(e) for e in manifest_dict["entries"]]
    for e in entries:
        e["production_id"] = production_id
        if e.get("contributor_id") == "contrib_chen_wei":
            e["role"] = "Executive Producer"
    if not any(e.get("contributor_id") == "contrib_rosamund_osei" for e in entries):
        entries.append(
            {
                "rendered_element_id": "elem_demo_003",
                "contributor_id": "contrib_rosamund_osei",
                "display_name": "Rosamund Osei",
                "role": "Line Producer",
                "department": None,
                "credit_surface": "MAIN_TITLES",
                "group_id": None,
                "ordinal_position": 3,
                "shared_with": [],
                "production_id": production_id,
                "delivery_version_id": "v1",
            }
        )
    manifest_dict["entries"] = entries
    manifest = CreditManifest.model_validate(manifest_dict)
    contributor_registry = fixture.get("contributor_registry", [])

    # Render artifact frames & layout evidence using RendererService
    renderer = RendererService()
    render_res = renderer.render_and_audit(manifest)

    render_backend = "chromium" if settings.require_real_chrome else "synthetic"

    visual_obs = VisualObservations(
        manifest_id=manifest.manifest_id,
        model_id=settings.gemini_steward_model,
        observations=[
            VisualObservation(
                rendered_element_id=e.rendered_element_id,
                observation=f"Rendered {e.display_name} as {e.role}",
                flagged=False,
            )
            for e in manifest.entries
        ],
    )

    # Register demo production in production store under isolated production_id
    store.register(
        production_id=production_id,
        obligations=obligations,
        manifest=manifest,
        layout_evidence=render_res.layout_evidence,
        visual_observations=visual_obs,
        contributor_registry=contributor_registry,
        authorizations=[],
        projection=ProjectionStatus(artifact_pending=False, stale_manifest=False),
        frames=render_res.frames,
        artifact_index=render_res.artifact_index,
        render_profile_version=render_res.render_profile.profile_version,
    )

    # Evaluate initial gate
    checker_input = CheckerInput(
        obligations=obligations,
        manifest=manifest,
        layout_evidence=render_res.layout_evidence,
        visual_observations=visual_obs,
        contributor_registry=contributor_registry,
    )
    issues = evaluate_findings(checker_input)
    gate = fold_gate(issues, ProjectionStatus(artifact_pending=False, stale_manifest=False))

    reviewer_token = create_token(
        reviewer_id,
        "REVIEWER",
        demo_session_id=demo_session_id,
        production_id=production_id,
    )
    approver_token = create_token(
        approver_id,
        "RELEASE_APPROVER",
        demo_session_id=demo_session_id,
        production_id=production_id,
    )

    import os as _os
    _app_commit = _os.environ.get("APP_COMMIT", "unknown")
    _k_revision = _os.environ.get("K_REVISION", "unknown")

    return DemoInitResponse(
        status="initialized",
        demo_session_id=demo_session_id,
        production_id=production_id,
        gate_state=gate.value,
        issue_count=len(issues),
        reviewer_token=reviewer_token,
        approver_token=approver_token,
        reviewer_id=reviewer_id,
        approver_id=approver_id,
        render_backend=render_backend,
        app_commit=_app_commit,
        k_revision=_k_revision,
    )


@router.post("/reset", response_model=DemoInitResponse)
async def reset_demo() -> DemoInitResponse:
    """Session-isolated reset: creates a fresh demo session."""
    return await initialize_demo()


@router.get("/state")
async def get_demo_state(
    actor: Annotated[dict[str, Any], Depends(get_current_actor)],
    production_id: str = Query(...),
) -> dict[str, Any]:
    _check_demo_enabled()
    check_token_production_access(actor, production_id)
    store = get_production_store()
    prod = store.get(production_id)
    if prod is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Demo production '{production_id}' not found. Please initialize demo session first.",
        )

    from creditlock.api.resolutions import _compute_hashes

    try:
        manifest_hash, obl_hash, art_hash, vis_hash = _compute_hashes(prod)
    except Exception:  # noqa: BLE001
        manifest_hash = obl_hash = art_hash = vis_hash = ""

    stored_auths = store.get_authorizations(production_id) or prod.get("authorizations", [])
    proposals = store.get_proposals(production_id)

    from creditlock.api.export import _build_identity_bindings

    raw_bindings = _build_identity_bindings(
        stored_auths,
        manifest_hash,
        obl_hash,
        art_hash,
        vis_hash,
    )

    identity_bindings: dict[str, str] | None = None
    if raw_bindings:
        preliminary_input = CheckerInput(
            obligations=prod["obligations"],
            manifest=prod["manifest"],
            layout_evidence=prod.get("layout_evidence"),
            visual_observations=prod.get("visual_observations"),
            contributor_registry=prod.get("contributor_registry"),
        )
        preliminary_issues = evaluate_findings(preliminary_input)
        ambiguous_issue_id_to_obl_id: dict[str, str] = {
            i.issue_id: i.obligation_id
            for i in preliminary_issues
            if i.code == IssueCode.AMBIGUOUS_IDENTITY and i.obligation_id is not None
        }
        ob_bindings: dict[str, str] = {}
        conflict = False
        for issue_id, selected_cid in raw_bindings.items():
            obl_id = ambiguous_issue_id_to_obl_id.get(issue_id)
            if obl_id is None:
                continue
            existing = ob_bindings.get(obl_id)
            if existing is not None and existing != selected_cid:
                conflict = True
                break
            ob_bindings[obl_id] = selected_cid
        identity_bindings = ob_bindings if (ob_bindings and not conflict) else None

    checker_input = CheckerInput(
        obligations=prod["obligations"],
        manifest=prod["manifest"],
        layout_evidence=prod.get("layout_evidence"),
        visual_observations=prod.get("visual_observations"),
        contributor_registry=prod.get("contributor_registry"),
        identity_bindings=identity_bindings if identity_bindings else None,
    )
    issues = evaluate_findings(checker_input)
    gate = fold_gate(issues, prod.get("projection") or ProjectionStatus())

    # For each AMBIGUOUS_IDENTITY issue, derive the genuine candidate set from the
    # obligation, contributor registry, and manifest — and enrich with enough
    # metadata for a reviewer to tell the two candidates apart without hardcoded names.
    from creditlock.domain.checker import _resolve_obligee_text

    registry = prod.get("contributor_registry") or []
    obligations_list: list[Any] = prod.get("obligations", [])

    # Build obligation_id -> obligation dict for fast lookup
    obl_by_id: dict[str, dict[str, Any]] = {}
    for o in obligations_list:
        obl_obj = o if isinstance(o, dict) else o.model_dump()
        obl_by_id[obl_obj["obligation_id"]] = obl_obj

    # Build contributor_id -> manifest entry for fast lookup
    # prod["manifest"] may be a CreditManifest Pydantic model or a plain dict.
    _manifest_raw = prod.get("manifest") or prod.get("manifest", {})
    if hasattr(_manifest_raw, "entries"):
        # CreditManifest Pydantic model — iterate .entries (list[ManifestEntry])
        manifest_by_cid: dict[str, dict[str, Any]] = {
            e.contributor_id: {
                "contributor_id": e.contributor_id,
                "display_name": e.display_name,
                "role": e.role,
                "credit_surface": e.credit_surface.value if hasattr(e.credit_surface, "value") else str(e.credit_surface),
            }
            for e in _manifest_raw.entries
        }
    else:
        _manifest_entries: list[dict[str, Any]] = (_manifest_raw or {}).get("entries") or []
        manifest_by_cid = {
            e["contributor_id"]: e for e in _manifest_entries if e.get("contributor_id")
        }

    issues_with_candidates = []
    for iss in issues:
        iss_dict = iss.model_dump()
        if iss.code == IssueCode.AMBIGUOUS_IDENTITY and iss.obligation_id is not None:
            obl_obj = obl_by_id.get(iss.obligation_id, {})
            obligee = obl_obj.get("obligee_text") or ""
            if obligee:
                cids = _resolve_obligee_text(obligee, registry)
                registry_by_id = {r.get("contributor_id"): r for r in registry}
                # obligation-level role/surface are shared across all candidates for this issue
                obl_role = obl_obj.get("role_label") or ""
                obl_surface = obl_obj.get("credit_surface") or ""
                iss_dict["candidate_ids"] = cids
                iss_dict["candidates"] = [
                    {
                        "contributor_id": cid,
                        "canonical_name": registry_by_id.get(cid, {}).get("canonical_name") or cid,
                        # manifest entry fields — present only when contributor_id matches a manifest entry
                        "display_name": manifest_by_cid.get(cid, {}).get("display_name") or None,
                        "role": manifest_by_cid.get(cid, {}).get("role") or obl_role or None,
                        "credit_surface": (
                            manifest_by_cid.get(cid, {}).get("credit_surface") or obl_surface or None
                        ),
                        "present_in_manifest": cid in manifest_by_cid,
                    }
                    for cid in cids
                ]
        issues_with_candidates.append(iss_dict)

    return {
        "production_id": production_id,
        "title": "Apex: Legacy of Speed",
        "gate_state": gate.value,
        "issue_count": len(issues),
        "issues": issues_with_candidates,
        "proposals": [p.model_dump() if hasattr(p, "model_dump") else p for p in proposals],
        "authorizations": [a.model_dump() if hasattr(a, "model_dump") else a for a in stored_auths],
        "hashes": {
            "manifest_hash": manifest_hash,
            "obligation_registry_version_hash": obl_hash,
            "artifact_index_digest": art_hash,
            "visual_observations_hash": vis_hash,
        },
    }


@router.post("/analyze", response_model=DemoAnalysisResponse)
async def analyze_demo_conflict(
    req: AnalyzeRequest,
    actor: Annotated[dict[str, Any], Depends(require_role("REVIEWER"))],
) -> DemoAnalysisResponse:
    _check_demo_enabled()
    check_token_production_access(actor, req.production_id)
    store = get_production_store()
    production_id = req.production_id

    prod = store.get(production_id)
    if prod is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Demo production '{production_id}' not found.",
        )

    # Session budget & concurrency checks
    session_id = production_id
    if session_id in _demo_active_analysis:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error_code": "ANALYSIS_IN_PROGRESS", "message": "An analysis is already active for this session."},
            headers={"Retry-After": "5"},
        )

    attempts = _demo_session_attempts.get(session_id, 0)
    if attempts >= 3:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error_code": "SESSION_BUDGET_EXCEEDED", "message": "Maximum analysis budget (3 attempts) reached for this session."},
            headers={"Retry-After": "60"},
        )

    _demo_session_attempts[session_id] = attempts + 1
    _demo_active_analysis.add(session_id)

    try:
        checker_input = CheckerInput(
            obligations=prod["obligations"],
            manifest=prod["manifest"],
            layout_evidence=prod.get("layout_evidence"),
            visual_observations=prod.get("visual_observations"),
            contributor_registry=prod.get("contributor_registry"),
        )
        issues = evaluate_findings(checker_input)
        ambiguous_issue = next((i for i in issues if i.code == IssueCode.AMBIGUOUS_IDENTITY), None)
        if ambiguous_issue is None and issues:
            ambiguous_issue = issues[0]

        doc_text = (
            "Wei Chen shall receive sole Executive Producer credit, main titles, position 1.\n"
            "R. Osei shall receive Line Producer credit in main titles, position 3.\n"
            "D. Park, Associate Producer, end cards."
        )
        doc_ref = DocumentReference(
            uri="doc_demo_memo_v1",
            sha256_hash=sha256_bytes_digest(doc_text.encode()),
        )

        extractor = ExtractorAgent(provider=_demo_override_provider)
        try:
            extraction_res: ExtractionResult = extractor.extract_from_document(
                doc_ref, doc_text, production_id
            )
        except ModelInvocationUnavailableError as ex:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error_code": "GEMINI_UNAVAILABLE", "message": str(ex)},
            ) from ex
        except Exception as ex:
            err_str = str(ex).lower()
            code = "GEMINI_AUTH_FAILURE" if ("auth" in err_str or "key" in err_str or "credential" in err_str) else "GEMINI_ERROR"
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error_code": code, "message": str(ex)},
            ) from ex

        accepted_candidates = extraction_res.extracted_obligations
        grounded_candidates: list[Obligation] = []
        for candidate in accepted_candidates:
            if candidate.source_span and candidate.source_span.start_char is not None and candidate.source_span.end_char is not None:
                start = candidate.source_span.start_char
                end = candidate.source_span.end_char
                if 0 <= start < end <= len(doc_text) and doc_text[start:end] == candidate.source_span.quote:
                    grounded_candidates.append(candidate)

        ambiguous_keyword = "park"
        relevant_candidates = [
            c for c in grounded_candidates
            if ambiguous_keyword in (c.required_display_text or "").lower()
            or ambiguous_keyword in (c.obligee_text or "").lower()
            or ambiguous_keyword in (c.source_span.quote if c.source_span else "").lower()
        ]

        if not relevant_candidates:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "GEMINI_NO_GROUNDED_CANDIDATES",
                    "message": "Gemini returned zero accepted or relevant candidates for the active obligation.",
                },
                headers={"Retry-After": "5"},
            )

        provenance = extraction_res.provenance
        model_used = provenance.actual_model_used or provenance.primary_model

        target_issue_dict = ambiguous_issue.model_dump() if ambiguous_issue else {
            "issue_id": "issue_demo_ambiguous",
            "code": "AMBIGUOUS_IDENTITY",
            "detail": "D. Park matches multiple registry records",
        }

        return DemoAnalysisResponse(
            model_used=model_used,
            fallback_occurred=provenance.fallback_occurred,
            extracted_obligations=[o.model_dump() for o in relevant_candidates],
            rejection_diagnostics=extraction_res.rejection_diagnostics,
            deterministic_issue=target_issue_dict,
            required_human_action="CONFIRM_IDENTITY",
        )
    finally:
        _demo_active_analysis.discard(session_id)


@router.get("/export/download")
async def download_demo_export(
    actor: Annotated[dict[str, Any], Depends(require_role("RELEASE_APPROVER"))],
    production_id: str = Query(...),
) -> Response:
    _check_demo_enabled()
    check_token_production_access(actor, production_id)
    store = get_production_store()
    settings = get_settings()

    prod = store.get(production_id)
    if prod is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Demo production '{production_id}' not found.",
        )

    rel_digest = prod.get("release_digest")
    if not rel_digest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exported delivery package found for production '{production_id}'. Run export first.",
        )

    pkg_path_str = prod.get("delivery_package_path")
    if pkg_path_str and Path(pkg_path_str).exists():
        local_path = Path(pkg_path_str)
        return FileResponse(
            path=local_path,
            filename=local_path.name,
            media_type="application/zip",
        )

    if settings.evidence_bucket:
        from creditlock.evidence.storage import GCSStore

        gcs_key = f"deliveries/{production_id}/{rel_digest}.zip"
        try:
            gcs_store = GCSStore(settings.evidence_bucket)
            zip_bytes = gcs_store.get(gcs_key)
            if zip_bytes is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Package '{gcs_key}' not found in GCS bucket.",
                )
            return Response(
                content=zip_bytes,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{production_id}_delivery_package.zip"'},
            )
        except Exception as ex:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve package '{gcs_key}' from GCS: {ex}",
            ) from ex

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Delivery package file for production '{production_id}' is missing.",
    )
