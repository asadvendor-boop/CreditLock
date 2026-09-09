"""
Domain models for CreditLock.

All enums, typed schemas, and lifecycle states are frozen here.
Use extra="forbid" throughout — unknown fields are rejected at parse time.
Confidence is routing metadata only; it can never activate, clear, or waive.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# ── Enums ─────────────────────────────────────────────────────────────────────


class CreditSurface(str, Enum):
    MAIN_TITLES = "MAIN_TITLES"
    END_CARDS = "END_CARDS"
    END_CRAWL = "END_CRAWL"
    BILLING_BLOCK = "BILLING_BLOCK"


class CardType(str, Enum):
    SOLO = "SOLO"
    SHARED = "SHARED"
    CRAWL = "CRAWL"


class CardPositionKind(str, Enum):
    ABSOLUTE = "ABSOLUTE"
    RELATIVE = "RELATIVE"


class RelativeRelation(str, Enum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"


class SizeComparisonBasis(str, Enum):
    COMPUTED_FONT_SIZE_PX = "COMPUTED_FONT_SIZE_PX"


class ObligationStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    CONFLICTING = "CONFLICTING"
    WAIVED = "WAIVED"
    PENDING_EXTERNAL_AUTHORITY = "PENDING_EXTERNAL_AUTHORITY"


class GateState(str, Enum):
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    STALE = "STALE"
    READY_TO_EXPORT = "READY_TO_EXPORT"


class IssueCode(str, Enum):
    # Blocking (gate = BLOCKED)
    MISSING_CREDIT = "MISSING_CREDIT"
    ARTIFACT_TEXT_MISMATCH = "ARTIFACT_TEXT_MISMATCH"
    ARTIFACT_GROUPING_MISMATCH = "ARTIFACT_GROUPING_MISMATCH"
    ARTIFACT_POSITION_MISMATCH = "ARTIFACT_POSITION_MISMATCH"
    ARTIFACT_SIZE_MISMATCH = "ARTIFACT_SIZE_MISMATCH"
    ARTIFACT_INTEGRITY_FAILURE = "ARTIFACT_INTEGRITY_FAILURE"
    # Needs human (gate = NEEDS_HUMAN)
    AMBIGUOUS_IDENTITY = "AMBIGUOUS_IDENTITY"
    CONFLICTING_OBLIGATION = "CONFLICTING_OBLIGATION"
    VISUAL_OBSERVATION_UNCERTAIN = "VISUAL_OBSERVATION_UNCERTAIN"
    UNSUPPORTED_PRESENTATION_ASSERTION = "UNSUPPORTED_PRESENTATION_ASSERTION"
    # Evidence missing (gate = DEGRADED)
    REQUIRED_EVIDENCE_UNAVAILABLE = "REQUIRED_EVIDENCE_UNAVAILABLE"
    # Unconfirmed extraction (gate = NEEDS_CONFIRMATION)
    UNCONFIRMED_OBLIGATION = "UNCONFIRMED_OBLIGATION"
    # Stale (gate = STALE)
    STALE_MANIFEST = "STALE_MANIFEST"
    ARTIFACT_PENDING = "ARTIFACT_PENDING"


# Map each issue code to its gate contribution
ISSUE_GATE_MAP: dict[IssueCode, GateState] = {
    IssueCode.MISSING_CREDIT: GateState.BLOCKED,
    IssueCode.ARTIFACT_TEXT_MISMATCH: GateState.BLOCKED,
    IssueCode.ARTIFACT_GROUPING_MISMATCH: GateState.BLOCKED,
    IssueCode.ARTIFACT_POSITION_MISMATCH: GateState.BLOCKED,
    IssueCode.ARTIFACT_SIZE_MISMATCH: GateState.BLOCKED,
    IssueCode.ARTIFACT_INTEGRITY_FAILURE: GateState.BLOCKED,
    IssueCode.AMBIGUOUS_IDENTITY: GateState.NEEDS_HUMAN,
    IssueCode.CONFLICTING_OBLIGATION: GateState.NEEDS_HUMAN,
    IssueCode.VISUAL_OBSERVATION_UNCERTAIN: GateState.NEEDS_HUMAN,
    IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION: GateState.NEEDS_HUMAN,
    IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE: GateState.DEGRADED,
    IssueCode.UNCONFIRMED_OBLIGATION: GateState.NEEDS_CONFIRMATION,
    IssueCode.STALE_MANIFEST: GateState.STALE,
    IssueCode.ARTIFACT_PENDING: GateState.STALE,
}

# Gate precedence order: index 0 = highest precedence
GATE_PRECEDENCE: list[GateState] = [
    GateState.DEGRADED,
    GateState.BLOCKED,
    GateState.NEEDS_HUMAN,
    GateState.NEEDS_CONFIRMATION,
    GateState.STALE,
    GateState.READY_TO_EXPORT,
]


# ── Card position discriminated union ─────────────────────────────────────────


class AbsolutePosition(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal[CardPositionKind.ABSOLUTE]
    ordinal: int = Field(ge=1)


class RelativePosition(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal[CardPositionKind.RELATIVE]
    relation: RelativeRelation
    reference_rendered_element_id: str


CardPosition = Annotated[
    AbsolutePosition | RelativePosition,
    Field(discriminator="kind"),
]


# ── Source span ───────────────────────────────────────────────────────────────


class SourceSpan(BaseModel):
    model_config = {"extra": "forbid"}
    page: int | None = Field(default=None, ge=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    quote: str

    @model_validator(mode="after")
    def end_after_start(self) -> SourceSpan:
        if self.end_char < self.start_char:
            raise ValueError("end_char must be >= start_char")
        return self


# ── Obligation ────────────────────────────────────────────────────────────────


class Obligation(BaseModel):
    model_config = {"extra": "forbid"}

    obligation_id: str
    production_id: str
    credited_party_id: str | None = None
    required_display_text: str | None = None
    role_label: str | None = None
    credit_surface: CreditSurface
    card_type: CardType | None = None
    card_position: CardPosition | None = None
    shared_with: list[str] = Field(default_factory=list)
    billing_group: str | None = None
    minimum_relative_size: float | None = None
    size_reference: dict[Literal["rendered_element_id"], str] | None = None
    size_comparison_basis: SizeComparisonBasis | None = None
    minimum_visible_duration_ms: int | None = Field(default=None, ge=0)
    territories: list[str] = Field(default_factory=list)
    media_versions: list[str] = Field(default_factory=list)
    obligee_text: str | None = (
        None  # free-text name from source clause, used for name-to-party resolution
    )
    trigger_condition: dict[str, object] | None = None
    governing_authority: str | None = None

    source_document_id: str
    source_document_version: int = Field(ge=1)
    source_span: SourceSpan
    source_hash: str  # SHA-256 of source document bytes
    agent_reported_confidence: float = Field(ge=0.0, le=1.0)
    extraction_model_id: str
    prompt_version: str

    status: ObligationStatus = ObligationStatus.CANDIDATE

    @model_validator(mode="after")
    def size_fields_consistent(self) -> Obligation:
        has_size = self.minimum_relative_size is not None
        has_ref = self.size_reference is not None
        has_basis = self.size_comparison_basis is not None
        if has_size and not (has_ref and has_basis):
            raise ValueError(
                "minimum_relative_size requires size_reference and size_comparison_basis"
            )
        if (has_ref or has_basis) and not has_size:
            raise ValueError(
                "size_reference and size_comparison_basis require minimum_relative_size"
            )
        return self


# ── Manifest entry ────────────────────────────────────────────────────────────


class ManifestEntry(BaseModel):
    model_config = {"extra": "forbid"}

    rendered_element_id: str
    contributor_id: str
    display_name: str
    role: str
    department: str | None = None
    credit_surface: CreditSurface
    group_id: str | None = None
    ordinal_position: int = Field(ge=0)
    shared_with: list[str] = Field(default_factory=list)
    production_id: str
    delivery_version_id: str


class CreditManifest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_id: str
    production_id: str
    delivery_version_id: str
    entries: list[ManifestEntry]


# ── Contributor registry ──────────────────────────────────────────────────────


class AliasRecord(BaseModel):
    model_config = {"extra": "forbid"}

    alias: str  # exact string as it appears in a document
    registered_by: str
    registered_at: str  # ISO-8601
    reason: str


class ContributorRecord(BaseModel):
    model_config = {"extra": "forbid"}

    contributor_id: str
    canonical_name: str
    aliases: list[AliasRecord] = Field(default_factory=list)


# ── Layout assertion input (produced by renderer, consumed by checker) ────────


class LayoutAssertion(BaseModel):
    model_config = {"extra": "forbid"}

    rendered_element_id: str
    visible_text: str
    computed_font_size_px: float
    bounding_box: dict[str, float]  # {"x": float, "y": float, "w": float, "h": float}
    frame_index: int
    group_id: str | None = None


class LayoutEvidence(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_id: str
    render_profile_version: str
    assertions: list[LayoutAssertion]


# ── Visual observations (Gemini output, stored before checker runs) ───────────


class VisualObservation(BaseModel):
    model_config = {"extra": "forbid"}

    rendered_element_id: str
    observation: str  # plain-language description only
    flagged: bool  # True = route to VISUAL_OBSERVATION_UNCERTAIN
    flag_reason: str | None = None


class VisualObservations(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_id: str
    model_id: str
    observations: list[VisualObservation]


# ── Issue ─────────────────────────────────────────────────────────────────────


class Issue(BaseModel):
    model_config = {"extra": "forbid"}

    issue_id: str
    code: IssueCode
    obligation_id: str | None = None
    manifest_refs: list[str] = Field(default_factory=list)  # rendered_element_ids
    detail: str
    source_document_id: str | None = None
    source_document_version: int | None = None
    source_span: SourceSpan | None = None


# ── Authorization action ──────────────────────────────────────────────────────


class AuthorizationAction(str, Enum):
    """Typed human action for an authorization record.

    WAIVE_OBLIGATION and CONFIRM_PRECEDENCE are immutable evidence only —
    they do not directly clear issues or modify the obligation registry.
    Issue clearing and obligation-level waivers require their respective
    downstream workflows.
    """

    CONFIRM_IDENTITY = "CONFIRM_IDENTITY"
    CONFIRM_VISUAL = "CONFIRM_VISUAL"
    WAIVE_OBLIGATION = "WAIVE_OBLIGATION"
    CONFIRM_PRECEDENCE = "CONFIRM_PRECEDENCE"


# ── Resolution Proposal ───────────────────────────────────────────────────────


class Proposal(BaseModel):
    model_config = {"extra": "forbid"}

    proposal_id: str
    production_id: str
    issue_id: str
    proposer_id: str
    proposer_role: Literal["REVIEWER"]
    action: AuthorizationAction
    reason: str
    selected_contributor_id: str | None = None
    manifest_hash: str
    obligation_registry_version_hash: str
    artifact_index_digest: str
    visual_observations_hash: str
    created_at: str
    status: Literal["PENDING", "CONFIRMED", "REJECTED"] = "PENDING"

    @model_validator(mode="after")
    def validate_proposal(self) -> Proposal:
        for field_name in ("proposal_id", "production_id", "issue_id", "proposer_id"):
            val = getattr(self, field_name)
            if not val or not val.strip():
                raise ValueError(f"{field_name} must be a non-empty, non-whitespace string.")
        if not self.reason or not self.reason.strip():
            raise ValueError("reason must be a non-empty, non-whitespace string.")
        return self


# ── Authorization ─────────────────────────────────────────────────────────────


class Authorization(BaseModel):
    model_config = {"extra": "forbid"}

    authorization_id: str
    production_id: str
    proposal_id: str
    issue_id: str
    proposer_id: str
    actor_id: str  # approver identity
    role: Literal["RELEASE_APPROVER"]
    action: AuthorizationAction
    reason: str
    selected_contributor_id: str | None = None
    # Four input hashes — never bound to release_evidence_digest
    manifest_hash: str
    obligation_registry_version_hash: str
    artifact_index_digest: str
    visual_observations_hash: str
    authorized_at: str  # ISO-8601

    @model_validator(mode="after")
    def validate_authorization(self) -> Authorization:
        for field_name in (
            "authorization_id",
            "production_id",
            "proposal_id",
            "issue_id",
            "proposer_id",
            "actor_id",
        ):
            val = getattr(self, field_name)
            if not val or not val.strip():
                raise ValueError(f"{field_name} must be a non-empty, non-whitespace string.")
        if not self.reason or not self.reason.strip():
            raise ValueError("reason must be a non-empty, non-whitespace string.")
        if self.proposer_id == self.actor_id:
            raise ValueError("proposer_id and actor_id must be distinct identities.")
        if self.action == AuthorizationAction.CONFIRM_IDENTITY and (
            not self.selected_contributor_id or not self.selected_contributor_id.strip()
        ):
            raise ValueError(
                "selected_contributor_id must be a non-empty, non-whitespace string "
                "when action is CONFIRM_IDENTITY"
            )
        return self


# ── Patch ─────────────────────────────────────────────────────────────────────


class PatchOperation(str, Enum):
    SUBSTITUTE_TEXT = "SUBSTITUTE_TEXT"
    REORDER = "REORDER"
    REGROUP = "REGROUP"
    REPOSITION = "REPOSITION"


class Patch(BaseModel):
    model_config = {"extra": "forbid"}

    patch_id: str
    production_id: str
    manifest_id: str
    issue_id: str
    obligation_id: str
    operation: PatchOperation
    target_rendered_element_id: str
    payload: dict[str, object]  # operation-specific, validated by patch validator
    proposed_by: str
    proposed_at: str  # ISO-8601
