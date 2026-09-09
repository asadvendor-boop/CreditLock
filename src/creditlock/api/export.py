"""
Export API endpoint.

POST /productions/{production_id}/export

Phase 1 — Authentication: missing/invalid token → 401
Phase 2 — Authorization: role != RELEASE_APPROVER → 403 + audit log
Phase 3 — Gate evaluation: full recompute every call → 409 or 200

No cached tokens. No reusable PASS tokens.
Every call recomputes the full gate state from current state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from creditlock.api.auth import get_current_actor
from creditlock.domain.checker import CheckerInput, evaluate_findings
from creditlock.domain.gate import ProjectionStatus, fold_gate
from creditlock.domain.models import (
    Authorization,
    AuthorizationAction,
    CreditManifest,
    GateState,
    Issue,
    IssueCode,
    LayoutEvidence,
    Obligation,
    VisualObservations,
)
from creditlock.domain.store import ProductionStore

router = APIRouter()


class UninitializedProductionStore(ProductionStore):
    """Uninitialized fail-closed production store placeholder."""

    def _fail(self) -> NoReturn:
        raise RuntimeError("Production store is uninitialized. Application lifespan or test setup must initialize a ProductionStore.")

    def get(self, production_id: str) -> dict[str, Any] | None:
        self._fail()

    def register(
        self,
        production_id: str,
        obligations: list[Obligation],
        manifest: CreditManifest,
        layout_evidence: LayoutEvidence | None = None,
        visual_observations: VisualObservations | None = None,
        contributor_registry: list[dict[str, Any]] | None = None,
        authorizations: list[Authorization] | None = None,
        projection: Any | None = None,
        frames: list[Any] | None = None,
        artifact_index: Any | None = None,
        render_profile_version: str | None = None,
    ) -> None:
        self._fail()

    def clear(self) -> None:
        self._fail()

    def save_proposal(self, proposal: Any) -> None:
        self._fail()

    def get_proposals(self, production_id: str) -> list[Any]:
        self._fail()

    def confirm_proposal(
        self,
        production_id: str,
        proposal_id: str,
        approver_id: str,
        current_hashes: tuple[str, str, str, str],
    ) -> Authorization:
        self._fail()

    def get_authorizations(self, production_id: str) -> list[Authorization]:
        self._fail()


# ── Store & Audit Log abstractions ───────────────────────────────────────────
_production_store: ProductionStore = UninitializedProductionStore()
_audit_log: list[dict[str, Any]] = []


def get_production_store() -> ProductionStore:
    return _production_store


def set_production_store(store: ProductionStore) -> None:
    global _production_store
    _production_store = store


def _get_production(production_id: str) -> dict[str, Any]:
    prod = _production_store.get(production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail=f"Production '{production_id}' not found.")
    return prod


def register_production(
    production_id: str,
    obligations: list[Obligation],
    manifest: CreditManifest,
    layout_evidence: LayoutEvidence | None = None,
    visual_observations: VisualObservations | None = None,
    contributor_registry: list[dict[str, Any]] | None = None,
    authorizations: list[Authorization] | None = None,
    projection: ProjectionStatus | None = None,
    frames: list[Any] | None = None,
    artifact_index: Any | None = None,
    render_profile_version: str | None = None,
) -> None:
    """Register or update a production's state. Used in tests and seed scripts."""
    _production_store.register(
        production_id=production_id,
        obligations=obligations,
        manifest=manifest,
        layout_evidence=layout_evidence,
        visual_observations=visual_observations,
        contributor_registry=contributor_registry,
        authorizations=authorizations,
        projection=projection,
        frames=frames,
        artifact_index=artifact_index,
        render_profile_version=render_profile_version,
    )


def clear_productions() -> None:
    """Reset state between tests."""
    _production_store.clear()
    _audit_log.clear()


# ── Response models ───────────────────────────────────────────────────────────


class IssueResponse(BaseModel):
    issue_id: str
    code: str
    obligation_id: str | None
    manifest_refs: list[str]
    detail: str
    source_document_id: str | None
    source_clause: str | None  # from source_span.quote


class ExportBlockedResponse(BaseModel):
    gate_state: str
    issue_count: int
    issues: list[IssueResponse]


class EventSyncResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: str
    transport: str
    topic: str
    event_type: str
    event_id: str
    projection_backend: str


class ExportSuccessResponse(BaseModel):
    gate_state: str
    delivery_package_path: str | None = None  # Packaging lands with the renderer work
    release_digest: str | None = None
    replay_status: str
    authorized_at: str
    event_sync: EventSyncResponse | None = None


# ── Authorization overlay ─────────────────────────────────────────────────────

# Only VISUAL_OBSERVATION_UNCERTAIN can be cleared via the generic overlay
# (with action CONFIRM_VISUAL).  AMBIGUOUS_IDENTITY is resolved via
# CONFIRM_IDENTITY + identity_bindings.  CONFLICTING_OBLIGATION and
# UNSUPPORTED_PRESENTATION_ASSERTION are never clearable by authorization.
_AUTHORIZABLE_CODES: frozenset[IssueCode] = frozenset(
    [
        IssueCode.VISUAL_OBSERVATION_UNCERTAIN,
    ]
)


def _build_identity_bindings(
    authorizations: list[Authorization],
    manifest_hash: str,
    obligation_registry_version_hash: str,
    artifact_index_digest: str,
    visual_observations_hash: str,
) -> dict[str, str] | None:
    """
    Extract valid CONFIRM_IDENTITY authorizations and build an obligation_id →
    selected_contributor_id mapping.

    Rules:
    - Authorization must have action == "CONFIRM_IDENTITY" and non-null
      selected_contributor_id (enforced by model validator already).
    - All four hashes must match current state.
    - If two valid CONFIRM_IDENTITY records target the same issue_id with
      different selected_contributor_ids → conflict → return None (fail closed).
    """
    # issue_id -> selected_contributor_id from valid hash-matching CONFIRM_IDENTITY auths
    by_issue: dict[str, str] = {}
    conflict = False

    for auth in authorizations:
        if auth.action != AuthorizationAction.CONFIRM_IDENTITY or not auth.selected_contributor_id:
            continue
        hashes_match = (
            auth.manifest_hash == manifest_hash
            and auth.obligation_registry_version_hash == obligation_registry_version_hash
            and auth.artifact_index_digest == artifact_index_digest
            and auth.visual_observations_hash == visual_observations_hash
        )
        if not hashes_match:
            continue
        existing = by_issue.get(auth.issue_id)
        if existing is not None and existing != auth.selected_contributor_id:
            conflict = True
            break
        by_issue[auth.issue_id] = auth.selected_contributor_id

    if conflict:
        return None  # caller treats None as "no valid bindings — fail closed"

    return by_issue if by_issue else None


def _resolve_open_issues(
    issues: list[Issue],
    authorizations: list[Authorization],
    manifest_hash: str,
    obligation_registry_version_hash: str,
    artifact_index_digest: str,
    visual_observations_hash: str,
) -> list[Issue]:
    """
    Remove VISUAL_OBSERVATION_UNCERTAIN issues that have a valid CONFIRM_VISUAL
    hash-bound authorization.

    BLOCKED and DEGRADED issues can never be cleared by authorization.
    AMBIGUOUS_IDENTITY is resolved upstream via identity_bindings in CheckerInput.
    CONFLICTING_OBLIGATION and UNSUPPORTED_PRESENTATION_ASSERTION are not clearable.
    An authorization is valid only if ALL FOUR input hashes match current state.
    """
    authorized_issue_ids: set[str] = set()
    for auth in authorizations:
        if auth.action != AuthorizationAction.CONFIRM_VISUAL:
            continue
        hashes_match = (
            auth.manifest_hash == manifest_hash
            and auth.obligation_registry_version_hash == obligation_registry_version_hash
            and auth.artifact_index_digest == artifact_index_digest
            and auth.visual_observations_hash == visual_observations_hash
        )
        if hashes_match:
            authorized_issue_ids.add(auth.issue_id)

    return [
        issue
        for issue in issues
        if not (issue.issue_id in authorized_issue_ids and issue.code in _AUTHORIZABLE_CODES)
    ]


# ── Export endpoint ───────────────────────────────────────────────────────────


@router.post("/productions/{production_id}/export", response_model=None)
async def export_production(
    production_id: str,
    # Phase 1: authentication (401 if missing/invalid)
    actor: Annotated[dict[str, Any], Depends(get_current_actor)],
) -> ExportSuccessResponse | ExportBlockedResponse | JSONResponse:
    # Phase 2: authorization (403 if wrong role)
    if actor.get("role") != "RELEASE_APPROVER":
        # Write audit log entry for unauthorized attempt
        _audit_log.append(
            {
                "event": "EXPORT_UNAUTHORIZED",
                "production_id": production_id,
                "actor_id": actor.get("sub"),
                "role": actor.get("role"),
                "at": datetime.now(UTC).isoformat(),
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"RELEASE_APPROVER role required. Actor has role '{actor.get('role')}'.",
        )

    # Phase 3: gate evaluation (full recompute every call)
    from creditlock.api.auth import check_token_production_access
    check_token_production_access(actor, production_id)

    prod = _get_production(production_id)



    from creditlock.api.resolutions import _compute_hashes
    from creditlock.evidence.validation import (
        ArtifactEvidenceIntegrityError,
        ArtifactEvidenceMissingError,
    )

    try:
        manifest_hash, obligation_registry_version_hash, artifact_index_digest, visual_observations_hash = _compute_hashes(prod)
    except ArtifactEvidenceMissingError as e:
        msg = str(e)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ExportBlockedResponse(
                gate_state=GateState.STALE.value,
                issue_count=1,
                issues=[
                    IssueResponse(
                        issue_id="sys-artifact-pending",
                        code=IssueCode.ARTIFACT_PENDING.value,
                        obligation_id=None,
                        manifest_refs=[],
                        detail=msg,
                        source_document_id=None,
                        source_clause=None,
                    )
                ],
            ).model_dump(),
        )
    except ArtifactEvidenceIntegrityError as e:
        msg = str(e)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ExportBlockedResponse(
                gate_state=GateState.BLOCKED.value,
                issue_count=1,
                issues=[
                    IssueResponse(
                        issue_id="sys-artifact-integrity",
                        code="ARTIFACT_INTEGRITY_FAILURE",
                        obligation_id=None,
                        manifest_refs=[],
                        detail=msg,
                        source_document_id=None,
                        source_clause=None,
                    )
                ],
            ).model_dump(),
        )

    stored_auths = _production_store.get_authorizations(production_id) or prod.get(
        "authorizations", []
    )

    # Build identity_bindings from valid CONFIRM_IDENTITY authorizations.
    raw_bindings = _build_identity_bindings(
        stored_auths,
        manifest_hash,
        obligation_registry_version_hash,
        artifact_index_digest,
        visual_observations_hash,
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
    all_issues = evaluate_findings(checker_input)

    open_issues = _resolve_open_issues(
        all_issues,
        stored_auths,
        manifest_hash,
        obligation_registry_version_hash,
        artifact_index_digest,
        visual_observations_hash,
    )

    projection: ProjectionStatus = prod.get("projection") or ProjectionStatus()
    gate = fold_gate(open_issues, projection)

    if gate != GateState.READY_TO_EXPORT:
        issue_responses = [
            IssueResponse(
                issue_id=i.issue_id,
                code=i.code.value,
                obligation_id=i.obligation_id,
                manifest_refs=i.manifest_refs,
                detail=i.detail,
                source_document_id=i.source_document_id,
                source_clause=i.source_span.quote if i.source_span else None,
            )
            for i in open_issues
        ]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ExportBlockedResponse(
                gate_state=gate.value,
                issue_count=len(open_issues),
                issues=issue_responses,
            ).model_dump(),
        )

    # Gate is READY_TO_EXPORT — perform release event synchronization if enabled
    from creditlock.settings import get_settings

    settings = get_settings()
    event_sync_result: EventSyncResponse | None = None

    if settings.confluent_runtime_enabled:
        valid_auths = [
            auth
            for auth in stored_auths
            if (
                auth.manifest_hash == manifest_hash
                and auth.obligation_registry_version_hash == obligation_registry_version_hash
                and auth.artifact_index_digest == artifact_index_digest
                and auth.visual_observations_hash == visual_observations_hash
            )
        ]
        identity_auths = [
            a for a in valid_auths if a.action == AuthorizationAction.CONFIRM_IDENTITY
        ]
        target_auth = identity_auths[0] if identity_auths else (valid_auths[0] if valid_auths else None)

        if target_auth is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "code": "CONFLUENT_SYNC_UNAVAILABLE",
                    "retryable": True,
                    "message": "Release event synchronization is temporarily unavailable.",
                },
            )

        from creditlock.events.release_checkpoint import (
            ReleaseCheckpointUnavailable,
            synchronize_release_checkpoint,
        )

        try:
            sync_res = await asyncio.to_thread(
                synchronize_release_checkpoint,
                target_auth,
            )
            event_sync_result = EventSyncResponse(
                status=sync_res.status,
                transport=sync_res.transport,
                topic=sync_res.topic,
                event_type=sync_res.event_type,
                event_id=sync_res.event_id,
                projection_backend=sync_res.projection_backend,
            )
        except ReleaseCheckpointUnavailable:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "code": "CONFLUENT_SYNC_UNAVAILABLE",
                    "retryable": True,
                    "message": "Release event synchronization is temporarily unavailable.",
                },
            )

    # Assemble delivery package from authorized snapshot
    import tempfile

    from creditlock.evidence.delivery import assemble_delivery_package
    from creditlock.evidence.replay import replay_bundle

    snapshot = dict(prod)
    snapshot["identity_bindings"] = identity_bindings or {}
    snapshot["open_issues"] = open_issues
    snapshot["gate"] = gate
    snapshot["authorizations"] = stored_auths



    tmp_dir = tempfile.gettempdir()
    pkg_res = assemble_delivery_package(production_id, snapshot, tmp_dir)

    # Assert that package release evidence and immediate offline replay reproduce READY_TO_EXPORT
    replay_res = replay_bundle(
        pkg_res.package_path, expected_release_digest=pkg_res.release_evidence_digest
    )
    if (
        replay_res.status != "MATCH"
        or replay_res.replayed_gate_state != GateState.READY_TO_EXPORT.value
        or replay_res.expected_gate_state != GateState.READY_TO_EXPORT.value
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Export verification failed: offline replay diverged (status='{replay_res.status}', replayed_gate='{replay_res.replayed_gate_state}', reasons={replay_res.divergence_reasons})",
        )

    # GCS Persistence if bucket is configured
    from creditlock.evidence.storage import GCSStore
    from creditlock.settings import get_settings

    settings = get_settings()
    final_delivery_path = pkg_res.package_path
    if settings.evidence_bucket:
        try:
            gcs_store = GCSStore(settings.evidence_bucket)
            gcs_rel_path = f"deliveries/{production_id}/{pkg_res.release_evidence_digest}.zip"
            zip_data = Path(pkg_res.package_path).read_bytes()
            final_delivery_path = gcs_store.put(gcs_rel_path, zip_data)
        except Exception as ex:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"GCS delivery package persistence failed: {ex}",
            ) from ex

    prod["delivery_package_path"] = final_delivery_path
    prod["release_digest"] = pkg_res.release_evidence_digest

    now_iso = datetime.now(UTC).isoformat()
    _audit_log.append(
        {
            "event": "EXPORT_AUTHORIZED",
            "production_id": production_id,
            "actor_id": actor.get("sub"),
            "gate_state": gate.value,
            "release_digest": pkg_res.release_evidence_digest,
            "at": now_iso,
        }
    )

    return ExportSuccessResponse(
        gate_state=gate.value,
        delivery_package_path=final_delivery_path,
        release_digest=pkg_res.release_evidence_digest,
        replay_status=replay_res.status,
        authorized_at=now_iso,
        event_sync=event_sync_result,
    )
