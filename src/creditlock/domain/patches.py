"""
Deterministic patch validator.

A patch proposal from the Steward is only valid if:
1. The operation type is one of the allowed types.
2. The payload values are directly derivable from the controlling obligation.
3. The target element exists in the manifest.

No AI. No I/O. Pure validation logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from creditlock.domain.models import (
    CreditManifest,
    Obligation,
    ObligationStatus,
    Patch,
    PatchOperation,
)


@dataclass
class PatchValidation:
    valid: bool
    rejection_reason: str | None = None


def validate_patch(
    patch: Patch, obligation: Obligation, manifest: CreditManifest
) -> PatchValidation:
    """
    Validate a proposed patch against the controlling obligation and manifest.

    Rules:
    - Obligation must be ACTIVE.
    - Target element must exist in the manifest.
    - SUBSTITUTE_TEXT: new_text must equal obligation.required_display_text (NFC).
    - REORDER: new_ordinal must equal the ordinal from obligation.card_position (ABSOLUTE only).
    - REGROUP: new_card_type must equal obligation.card_type.
    - REPOSITION: target_surface must equal obligation.credit_surface.
    - No patch may introduce a value not present in the controlling obligation.
    """
    import unicodedata

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    # Obligation must be ACTIVE
    if obligation.status != ObligationStatus.ACTIVE:
        return PatchValidation(
            valid=False,
            rejection_reason=(
                f"Patch references obligation {obligation.obligation_id} "
                f"which is not ACTIVE (status={obligation.status.value})."
            ),
        )

    # Target element must exist in manifest
    element_ids = {e.rendered_element_id for e in manifest.entries}
    if patch.target_rendered_element_id not in element_ids:
        return PatchValidation(
            valid=False,
            rejection_reason=(
                f"Target element '{patch.target_rendered_element_id}' "
                f"does not exist in manifest '{manifest.manifest_id}'."
            ),
        )

    op = patch.operation
    payload = patch.payload

    if op == PatchOperation.SUBSTITUTE_TEXT:
        # Must have new_text; must be a string; must match obligation.required_display_text (NFC)
        new_text = payload.get("new_text")
        if new_text is None:
            return PatchValidation(
                valid=False, rejection_reason="SUBSTITUTE_TEXT requires payload.new_text."
            )
        if not isinstance(new_text, str):
            return PatchValidation(
                valid=False,
                rejection_reason="SUBSTITUTE_TEXT payload.new_text must be a string.",
            )
        if obligation.required_display_text is None:
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"SUBSTITUTE_TEXT proposed for obligation {obligation.obligation_id} "
                    f"but required_display_text is null. Cannot derive correct value."
                ),
            )
        if nfc(new_text) != nfc(obligation.required_display_text):
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"SUBSTITUTE_TEXT new_text '{new_text}' (NFC) does not match "
                    f"obligation.required_display_text '{obligation.required_display_text}' (NFC). "
                    f"Patch may only set the value specified in the controlling obligation."
                ),
            )

    elif op == PatchOperation.REORDER:
        # Must have new_ordinal; must match obligation.card_position.ordinal (ABSOLUTE only)
        new_ordinal = payload.get("new_ordinal")
        if new_ordinal is None:
            return PatchValidation(
                valid=False, rejection_reason="REORDER requires payload.new_ordinal."
            )
        from creditlock.domain.models import AbsolutePosition

        if not isinstance(obligation.card_position, AbsolutePosition):
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"REORDER proposed for obligation {obligation.obligation_id} "
                    f"but card_position is not ABSOLUTE. Cannot derive correct ordinal."
                ),
            )
        if new_ordinal != obligation.card_position.ordinal:
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"REORDER new_ordinal={new_ordinal} does not match "
                    f"obligation.card_position.ordinal={obligation.card_position.ordinal}."
                ),
            )

    elif op == PatchOperation.REGROUP:
        # Must have new_card_type; must match obligation.card_type
        new_card_type = payload.get("new_card_type")
        if new_card_type is None:
            return PatchValidation(
                valid=False, rejection_reason="REGROUP requires payload.new_card_type."
            )
        if obligation.card_type is None:
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"REGROUP proposed for obligation {obligation.obligation_id} "
                    f"but card_type is null. Cannot derive correct grouping."
                ),
            )
        if new_card_type != obligation.card_type.value:
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"REGROUP new_card_type='{new_card_type}' does not match "
                    f"obligation.card_type='{obligation.card_type.value}'."
                ),
            )

    elif op == PatchOperation.REPOSITION:
        # Must have new_surface; must match obligation.credit_surface
        new_surface = payload.get("new_surface")
        if new_surface is None:
            return PatchValidation(
                valid=False, rejection_reason="REPOSITION requires payload.new_surface."
            )
        if new_surface != obligation.credit_surface.value:
            return PatchValidation(
                valid=False,
                rejection_reason=(
                    f"REPOSITION new_surface='{new_surface}' does not match "
                    f"obligation.credit_surface='{obligation.credit_surface.value}'."
                ),
            )

    else:
        return PatchValidation(
            valid=False,
            rejection_reason=f"Unknown patch operation '{op}'.",
        )

    return PatchValidation(valid=True)
