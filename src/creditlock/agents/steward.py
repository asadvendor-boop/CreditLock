"""
Steward Agent.

Explains typed deterministic findings and proposes constrained patches whose values are
directly derivable from controlling evidence.
Validates proposals through deterministic patch validation (creditlock.domain.patches).
Abstains when source is ambiguous.
Never writes manifests, clears issues, authorizes, changes gate state, or exports.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from creditlock.agents.models import StewardProposal, StructuredGeneration
from creditlock.agents.provider import (
    GoogleModelProvider,
    ModelInvocationUnavailableError,
    ModelProvider,
)
from creditlock.domain.models import (
    AbsolutePosition,
    CreditManifest,
    Issue,
    IssueCode,
    Obligation,
    ObligationStatus,
    Patch,
    PatchOperation,
)
from creditlock.domain.patches import validate_patch
from creditlock.settings import get_settings

# ── Stable prompt template constants for reproducible hashing ─────────────────
PROMPT_VERSION = "steward-v2"

SYSTEM_INSTRUCTION_TEMPLATE = (
    "You are a credit steward assistant. Derive patches strictly from "
    "controlling obligations. "
    "For ARTIFACT_POSITION_MISMATCH, set proposed_operation='REORDER' and "
    "proposed_value to the controlling ordinal string (e.g. '1'). "
    "For ARTIFACT_TEXT_MISMATCH, set proposed_operation='SUBSTITUTE_TEXT' "
    "and proposed_value to controlling required_display_text. "
    "For ARTIFACT_GROUPING_MISMATCH, set proposed_operation='REGROUP' and "
    "proposed_value to the controlling card_type (e.g. 'SOLO'). "
    "If the value cannot be unambiguously derived, set derivable=false."
)

USER_PROMPT_TEMPLATE = (
    "Analyze finding code '{issue_code}' on manifest element "
    "'{manifest_refs}'.\n"
    "Controlling Obligation: text='{display_text}', role='{role_label}', "
    "position='{card_position}', surface='{credit_surface}', "
    "card_type='{card_type}'.\n"
    "Detail: {detail}.\n\n"
    "Instructions for proposed_operation and proposed_value:\n"
    "- For ARTIFACT_TEXT_MISMATCH: proposed_operation MUST be "
    "'SUBSTITUTE_TEXT' and proposed_value MUST be '{display_text}'.\n"
    "- For ARTIFACT_POSITION_MISMATCH: proposed_operation MUST be "
    "'REORDER' and proposed_value MUST be the ordinal number as a "
    "string (e.g. '1').\n"
    "- For ARTIFACT_GROUPING_MISMATCH: proposed_operation MUST be "
    "'REGROUP' and proposed_value MUST be the card_type value "
    "string (e.g. 'SOLO').\n"
    "- For ARTIFACT_SIZE_MISMATCH: if the repair cannot be truthfully "
    "derived from the controlling obligation, set derivable=false.\n"
)


def is_admissible_steward_issue(issue: Issue, obligation: Obligation | None) -> tuple[bool, str]:
    """
    Exact admissibility matrix for Steward deterministic pre-check.
    Returns (admissible, rejection_reason).
    """
    if obligation is None or obligation.status != ObligationStatus.ACTIVE:
        return False, "Obligation not ACTIVE or missing (deterministic no-model decision)."

    code = issue.code
    if code == IssueCode.MISSING_CREDIT:
        return False, "MISSING_CREDIT cannot produce a patch because no ADD operation exists (deterministic no-model decision)."

    if code == IssueCode.ARTIFACT_SIZE_MISMATCH:
        return False, "ARTIFACT_SIZE_MISMATCH cannot produce a patch because no size-changing operation exists (deterministic no-model decision)."

    if code == IssueCode.ARTIFACT_TEXT_MISMATCH:
        detail_lower = (issue.detail or "").strip().lower()
        if detail_lower.startswith("display_name mismatch:"):
            return True, ""
        return False, f"ARTIFACT_TEXT_MISMATCH detail '{issue.detail}' is not an admissible display_name mismatch (deterministic no-model decision)."

    if code == IssueCode.ARTIFACT_POSITION_MISMATCH:
        if isinstance(obligation.card_position, AbsolutePosition):
            return True, ""
        return False, "ARTIFACT_POSITION_MISMATCH requires AbsolutePosition (deterministic no-model decision)."

    if code == IssueCode.ARTIFACT_GROUPING_MISMATCH:
        if obligation.card_type is not None:
            return True, ""
        return False, "ARTIFACT_GROUPING_MISMATCH requires card_type (deterministic no-model decision)."

    return False, f"Issue code '{code.value}' cannot deterministically derive a supported patch operation (deterministic no-model decision)."


class StewardLLMOutputSchema(BaseModel):
    model_config = {"extra": "forbid"}

    explanation: str
    proposed_operation: str | None = None  # "SUBSTITUTE_TEXT", "REORDER", "REGROUP", "REPOSITION"
    proposed_value: str | None = None
    derivable: bool = True


class StewardAgent:
    """Steward agent providing constrained patch proposals and explanations via Gemini LLM."""

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        provider: ModelProvider | None = None,
    ) -> None:
        self._provider = provider

    @property
    def provider(self) -> ModelProvider:
        if self._provider is None:
            settings = get_settings()
            self._provider = GoogleModelProvider(
                agent_role="steward",
                primary_model_id=settings.gemini_steward_model,
                fallback_model_id=settings.gemini_steward_fallback_model,
            )
        return self._provider

    def explain_finding_and_propose_patch(
        self,
        issue: Issue,
        obligation: Obligation | None,
        manifest: CreditManifest,
    ) -> StewardProposal:
        """
        Explain a deterministic finding and propose a constrained patch if derivable.

        Invariants:
        - Perform deterministic admissibility check before invoking Gemini.
        - Fail closed if model provider unavailable when issue is admissible.
        - If obligation is missing, non-ACTIVE, or issue is inadmissible, abstains (no patch proposal).
        - Proposed operation and value from LLM are validated against exact deterministic derivation.
        - Proposed patch is validated through validate_patch.
        - Never mutates gate, manifest, or authorizations.
        """
        admissible, reason = is_admissible_steward_issue(issue, obligation)
        if not admissible:
            return StewardProposal(
                issue_id=issue.issue_id,
                explanation=f"Finding '{issue.code.value}': {issue.detail}. {reason}",
                patch_proposal=None,
                validated=False,
                validation_reason=reason,
                provenance=None,
            )

        if not self.provider.is_available():
            raise ModelInvocationUnavailableError(
                "Gemini model provider is unavailable: missing API key credentials."
            )

        assert obligation is not None

        prompt = USER_PROMPT_TEMPLATE.format(
            issue_code=issue.code.value,
            manifest_refs=issue.manifest_refs,
            display_text=obligation.required_display_text,
            role_label=obligation.role_label,
            card_position=obligation.card_position,
            credit_surface=obligation.credit_surface,
            card_type=obligation.card_type,
            detail=issue.detail,
        )

        generation: StructuredGeneration[StewardLLMOutputSchema] = (
            self.provider.generate_structured(
                prompt=prompt,
                response_schema=StewardLLMOutputSchema,
                system_instruction=SYSTEM_INSTRUCTION_TEMPLATE,
            )
        )
        output = generation.output
        provenance = generation.provenance

        target_element_id = (
            issue.manifest_refs[0]
            if issue.manifest_refs
            else (manifest.entries[0].rendered_element_id if manifest.entries else "")
        )

        # Deterministic expectation derivation
        expected_op: PatchOperation | None = None
        expected_val: str | None = None
        payload: dict[str, object] = {}

        if issue.code == IssueCode.ARTIFACT_TEXT_MISMATCH:
            expected_op = PatchOperation.SUBSTITUTE_TEXT
            expected_val = obligation.required_display_text
            payload = {"new_text": expected_val}
        elif issue.code == IssueCode.ARTIFACT_POSITION_MISMATCH:
            expected_op = PatchOperation.REORDER
            if isinstance(obligation.card_position, AbsolutePosition):
                expected_val = str(obligation.card_position.ordinal)
                payload = {"new_ordinal": obligation.card_position.ordinal}
        elif issue.code == IssueCode.ARTIFACT_GROUPING_MISMATCH:
            expected_op = PatchOperation.REGROUP
            if obligation.card_type is not None:
                expected_val = obligation.card_type.value
                payload = {"new_card_type": expected_val}

        # Compare LLM proposal against deterministic expectation
        llm_op_str = (output.proposed_operation or "").upper()
        llm_val_str = str(output.proposed_value or "")

        mismatch = False
        if not output.derivable or expected_op is None or not expected_val or not payload or not target_element_id or llm_op_str != expected_op.value or llm_val_str != expected_val:
            mismatch = True

        if mismatch:
            return StewardProposal(
                issue_id=issue.issue_id,
                explanation=output.explanation
                or f"Finding '{issue.code.value}': {issue.detail}. Value cannot be unambiguously derived.",
                patch_proposal=None,
                validated=False,
                validation_reason="LLM proposed operation/value does not match controlling obligation derivation.",
                provenance=provenance,
            )

        assert expected_op is not None
        model_id_for_patch = provenance.actual_model_used or provenance.primary_model
        patch = Patch(
            patch_id=f"patch-{uuid.uuid4().hex[:12]}",
            production_id=manifest.production_id,
            manifest_id=manifest.manifest_id,
            issue_id=issue.issue_id,
            obligation_id=obligation.obligation_id,
            operation=expected_op,
            target_rendered_element_id=target_element_id,
            payload=payload,
            proposed_by=f"steward-agent-{model_id_for_patch}",
            proposed_at=datetime.now(UTC).isoformat(),
        )

        validation = validate_patch(patch, obligation, manifest)

        return StewardProposal(
            issue_id=issue.issue_id,
            explanation=output.explanation,
            patch_proposal=patch if validation.valid else None,
            validated=validation.valid,
            validation_reason=validation.rejection_reason,
            provenance=provenance,
        )
