"""
Deterministic compliance checker.

evaluate_findings takes a CheckerInput and returns a list of Issues.
No AI, no I/O. All decisions are pure deterministic code.

Lifecycle mapping (only ACTIVE obligations are enforced):
  CANDIDATE, NEEDS_CONFIRMATION -> UNCONFIRMED_OBLIGATION
  CONFLICTING                   -> CONFLICTING_OBLIGATION (NEEDS_HUMAN)
  PENDING_EXTERNAL_AUTHORITY    -> UNSUPPORTED_PRESENTATION_ASSERTION (NEEDS_HUMAN)
  WAIVED, SUPERSEDED            -> skip, produce no issue
  ACTIVE                        -> evaluate all obligation fields

Identity resolution — three rules, no exceptions:

  Rule 1  credited_party_id set.
          Resolve via exact contributor_id match or registered alias to a
          manifest contributor_id.  If resolution fails → MISSING_CREDIT.
          Always.  Registry membership and surface population are irrelevant.
          Absence is never ambiguity.

  Rule 2  credited_party_id null, obligee_text set.
          Resolve text against registry: NFC-exact match against canonical_name
          or any alias string.  Collect matching contributor_ids that also
          appear in the manifest.
          0 or ≥2 manifest matches → AMBIGUOUS_IDENTITY.
          Exactly 1 → bind that contributor_id and run all normal field checks.

  Rule 3  credited_party_id null, obligee_text null.
          → UNCONFIRMED_OBLIGATION.  Never a silent skip.

Credit field semantics:
  required_display_text compared to manifest entry.display_name (NFC both sides).
  role_label compared to manifest entry.role (NFC both sides).
  Either mismatch emits ARTIFACT_TEXT_MISMATCH with the field identified in detail.

Size rule: subject_px + 0.1 >= minimum_relative_size * reference_px.

Duration obligations emit UNSUPPORTED_PRESENTATION_ASSERTION (not auto-enforced).

issue_id stability: deterministic SHA-256 of (obligation_id, IssueCode, discriminator)
so authorizations remain valid across re-evaluations, and two findings with the
same code on one obligation get distinct ids via the discriminator.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

from creditlock.domain.models import (
    AbsolutePosition,
    CreditManifest,
    Issue,
    IssueCode,
    LayoutEvidence,
    ManifestEntry,
    Obligation,
    ObligationStatus,
    SourceSpan,
    VisualObservations,
)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _stable_issue_id(
    obligation_id: str | None,
    code: IssueCode,
    discriminator: str = "",
) -> str:
    """Deterministic issue_id: stable across re-evaluations.

    discriminator is appended so that two findings with the same (obligation_id,
    code) — e.g. display_name mismatch and role mismatch — produce distinct ids.
    """
    raw = f"{obligation_id or ''}:{code.value}:{discriminator}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _new_issue(
    code: IssueCode,
    *,
    obligation_id: str | None = None,
    manifest_refs: list[str] | None = None,
    detail: str,
    discriminator: str = "",
    source_document_id: str | None = None,
    source_document_version: int | None = None,
    source_span: SourceSpan | None = None,
) -> Issue:
    return Issue(
        issue_id=_stable_issue_id(obligation_id, code, discriminator),
        code=code,
        obligation_id=obligation_id,
        manifest_refs=manifest_refs or [],
        detail=detail,
        source_document_id=source_document_id,
        source_document_version=source_document_version,
        source_span=source_span,
    )


@dataclass
class CheckerInput:
    obligations: list[Obligation]
    manifest: CreditManifest
    layout_evidence: LayoutEvidence | None = None
    visual_observations: VisualObservations | None = None
    contributor_registry: list[dict[str, Any]] | None = None
    # obligation_id -> selected_contributor_id from valid CONFIRM_IDENTITY authorizations
    identity_bindings: dict[str, str] | None = None


def _resolve_contributor(
    credited_party_id: str,
    manifest_contributor_ids: set[str],
) -> str | None:
    """Rule 1 resolution: exact manifest contributor_id match only.

    AliasRecord.alias is a source-name string, not a contributor-id redirect.
    Rule 1 never consults aliases — the credited_party_id must be literally
    present in the manifest.
    """
    if credited_party_id in manifest_contributor_ids:
        return credited_party_id
    return None


def _resolve_obligee_text(
    obligee_text: str,
    registry: list[dict[str, Any]] | None,
) -> list[str]:
    """Rule 2 stage-1: resolve free-text name against the registry alone.

    Matches NFC-normalised obligee_text against each registry record's
    canonical_name and every alias string.  Returns all matching contributor_ids
    regardless of manifest presence (0, 1, or more).

    Duplicate registry rows for the same contributor_id count as one match.
    Results are returned in deterministic sorted order.

    Stage-2 (manifest presence check) is handled by the caller.
    """
    if not registry:
        return []
    needle = _nfc(obligee_text)
    seen: set[str] = set()
    for record in registry:
        cid = record.get("contributor_id", "")
        if _nfc(record.get("canonical_name", "")) == needle:
            seen.add(cid)
            continue
        for alias_entry in record.get("aliases", []):
            if _nfc(alias_entry.get("alias", "")) == needle:
                seen.add(cid)
                break
    return sorted(seen)


def evaluate_findings(inp: CheckerInput) -> list[Issue]:
    # ── Artifact precondition gate ─────────────────────────────────────────────
    # Evidence authorizes release ONLY if it is present, bound to the current manifest,
    # and covers every element being evaluated. Anything short of that is ARTIFACT_PENDING -> STALE.
    if inp.layout_evidence is None or inp.visual_observations is None:
        return [
            _new_issue(
                IssueCode.ARTIFACT_PENDING,
                detail=(
                    "layout_evidence and visual_observations are required before "
                    "obligation evaluation. Render the artifact first."
                ),
            )
        ]

    if (
        inp.layout_evidence.manifest_id != inp.manifest.manifest_id
        or inp.visual_observations.manifest_id != inp.manifest.manifest_id
    ):
        return [
            _new_issue(
                IssueCode.ARTIFACT_PENDING,
                detail=(
                    f"Evidence manifest_id mismatch. Manifest: '{inp.manifest.manifest_id}', "
                    f"layout_evidence: '{inp.layout_evidence.manifest_id}', "
                    f"visual_observations: '{inp.visual_observations.manifest_id}'."
                ),
            )
        ]

    expected_ids = [entry.rendered_element_id for entry in inp.manifest.entries]
    expected_set = set(expected_ids)

    layout_assertions = inp.layout_evidence.assertions
    visual_observations = inp.visual_observations.observations

    layout_ids = [a.rendered_element_id for a in layout_assertions]
    visual_ids = [v.rendered_element_id for v in visual_observations]

    layout_counts: dict[str, int] = {}
    for lid in layout_ids:
        layout_counts[lid] = layout_counts.get(lid, 0) + 1

    visual_counts: dict[str, int] = {}
    for vid in visual_ids:
        visual_counts[vid] = visual_counts.get(vid, 0) + 1

    evidence_issues: list[Issue] = []

    # Unexpected or duplicate IDs fail closed with ARTIFACT_INTEGRITY_FAILURE
    unexpected_layout = [lid for lid in layout_counts if lid not in expected_set]
    unexpected_visual = [vid for vid in visual_counts if vid not in expected_set]
    duplicate_layout = [lid for lid, count in layout_counts.items() if count > 1]
    duplicate_visual = [vid for vid, count in visual_counts.items() if count > 1]

    if unexpected_layout or unexpected_visual or duplicate_layout or duplicate_visual:
        details: list[str] = []
        if unexpected_layout:
            details.append(f"Unexpected layout assertion IDs: {unexpected_layout}")
        if unexpected_visual:
            details.append(f"Unexpected visual observation IDs: {unexpected_visual}")
        if duplicate_layout:
            details.append(f"Duplicate layout assertion IDs: {duplicate_layout}")
        if duplicate_visual:
            details.append(f"Duplicate visual observation IDs: {duplicate_visual}")
        evidence_issues.append(
            _new_issue(
                IssueCode.ARTIFACT_INTEGRITY_FAILURE,
                detail="; ".join(details),
            )
        )

    # Missing evidence for any manifest entry emits ARTIFACT_PENDING
    missing_layout = [eid for eid in expected_ids if eid not in layout_counts]
    missing_visual = [eid for eid in expected_ids if eid not in visual_counts]
    missing_all = sorted(set(missing_layout) | set(missing_visual))

    if missing_all:
        evidence_issues.append(
            _new_issue(
                IssueCode.ARTIFACT_PENDING,
                manifest_refs=missing_all,
                detail=f"Missing render evidence for manifest elements: {missing_all}",
            )
        )

    if evidence_issues:
        return evidence_issues

    issues: list[Issue] = []

    manifest_by_contributor: dict[str, list[ManifestEntry]] = {}
    for entry in inp.manifest.entries:
        manifest_by_contributor.setdefault(entry.contributor_id, []).append(entry)

    manifest_contributor_ids = set(manifest_by_contributor.keys())

    from creditlock.domain.models import LayoutAssertion

    layout_by_id: dict[str, LayoutAssertion] = {}
    if inp.layout_evidence:
        for assertion in inp.layout_evidence.assertions:
            layout_by_id[assertion.rendered_element_id] = assertion

    for obl in inp.obligations:
        src_doc_id = obl.source_document_id
        src_doc_ver = obl.source_document_version
        src_span = obl.source_span

        # ── Lifecycle routing ─────────────────────────────────────────────────
        if obl.status == ObligationStatus.WAIVED:
            continue  # waiver removes enforcement basis — no issue
        if obl.status == ObligationStatus.SUPERSEDED:
            continue  # superseded by later document — no issue
        if obl.status == ObligationStatus.CONFLICTING:
            issues.append(
                _new_issue(
                    IssueCode.CONFLICTING_OBLIGATION,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Obligation {obl.obligation_id} is CONFLICTING: two or more documents "
                        f"modify the same field and neither explicitly supersedes the other. "
                        f"Human resolution required. Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue
        if obl.status == ObligationStatus.PENDING_EXTERNAL_AUTHORITY:
            issues.append(
                _new_issue(
                    IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Obligation {obl.obligation_id} is PENDING_EXTERNAL_AUTHORITY: "
                        f"governing authority '{obl.governing_authority}' has not yet confirmed. "
                        f"Human review required. Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue
        if obl.status in (ObligationStatus.CANDIDATE, ObligationStatus.NEEDS_CONFIRMATION):
            issues.append(
                _new_issue(
                    IssueCode.UNCONFIRMED_OBLIGATION,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Obligation {obl.obligation_id} is in status {obl.status.value} "
                        f"and has not been confirmed as ACTIVE. "
                        f"Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue

        # ── Only ACTIVE obligations reach here ────────────────────────────────

        # ── Duration: UNSUPPORTED_PRESENTATION_ASSERTION ──────────────────────
        if obl.minimum_visible_duration_ms is not None:
            issues.append(
                _new_issue(
                    IssueCode.UNSUPPORTED_PRESENTATION_ASSERTION,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Obligation {obl.obligation_id} requires minimum_visible_duration_ms="
                        f"{obl.minimum_visible_duration_ms}. Duration enforcement is not "
                        f"supported by the deterministic checker. Human review required."
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            # Continue — duration does not block other checks

        # ── Identity resolution ───────────────────────────────────────────────
        # Rule 1: credited_party_id set — resolve via exact match or alias.
        #         Failure → MISSING_CREDIT, always.
        # Rule 2: credited_party_id null, obligee_text set — text resolution.
        #         0 or ≥2 manifest matches → AMBIGUOUS_IDENTITY.
        #         Exactly 1 → bind and continue with field checks.
        # Rule 3: credited_party_id null, obligee_text null → UNCONFIRMED_OBLIGATION.

        if obl.credited_party_id is not None:
            # ── Rule 1 ────────────────────────────────────────────────────────
            # Exact manifest match only; alias is a source-name string, not a
            # contributor-id redirect.
            resolved_contributor_id = _resolve_contributor(
                obl.credited_party_id, manifest_contributor_ids
            )
            if resolved_contributor_id is None:
                issues.append(
                    _new_issue(
                        IssueCode.MISSING_CREDIT,
                        obligation_id=obl.obligation_id,
                        detail=(
                            f"credited_party_id '{obl.credited_party_id}' is absent from "
                            f"the manifest for surface {obl.credit_surface.value}. "
                            f"Credit is missing. "
                            f"Source: {src_doc_id} §{src_span.quote}"
                        ),
                        source_document_id=src_doc_id,
                        source_document_version=src_doc_ver,
                        source_span=src_span,
                    )
                )
                continue

        elif obl.obligee_text is not None and obl.obligee_text.strip():
            # ── Rule 2 ────────────────────────────────────────────────────────
            # If a human has already confirmed a specific contributor for this
            # obligation via CONFIRM_IDENTITY, use that binding directly.
            bound_id = (inp.identity_bindings or {}).get(obl.obligation_id)
            if bound_id is not None:
                # Human confirmed: skip registry resolution, proceed with field checks.
                resolved_contributor_id = bound_id
            else:
                # Stage 1: resolve against registry alone (no manifest filter).
                registry_matches = _resolve_obligee_text(obl.obligee_text, inp.contributor_registry)
                if len(registry_matches) != 1:
                    # 0 or ≥2 registry matches → AMBIGUOUS_IDENTITY.
                    issues.append(
                        _new_issue(
                            IssueCode.AMBIGUOUS_IDENTITY,
                            obligation_id=obl.obligation_id,
                            detail=(
                                f"obligee_text '{obl.obligee_text}' resolves to "
                                f"{len(registry_matches)} registry contributor(s) "
                                f"({registry_matches or 'none'}). "
                                f"Exactly one match required. Human confirmation required. "
                                f"Source: {src_doc_id} §{src_span.quote}"
                            ),
                            source_document_id=src_doc_id,
                            source_document_version=src_doc_ver,
                            source_span=src_span,
                        )
                    )
                    continue
                # Exactly 1 registry match.
                registry_resolved_id = registry_matches[0]
                # Stage 2: verify the registry-resolved party is in the manifest.
                if registry_resolved_id not in manifest_contributor_ids:
                    issues.append(
                        _new_issue(
                            IssueCode.MISSING_CREDIT,
                            obligation_id=obl.obligation_id,
                            detail=(
                                f"obligee_text '{obl.obligee_text}' resolves uniquely to "
                                f"'{registry_resolved_id}' in the registry, but that "
                                f"contributor is absent from the manifest. "
                                f"Source: {src_doc_id} §{src_span.quote}"
                            ),
                            source_document_id=src_doc_id,
                            source_document_version=src_doc_ver,
                            source_span=src_span,
                        )
                    )
                    continue
                resolved_contributor_id = registry_resolved_id

        else:
            # ── Rule 3 ────────────────────────────────────────────────────────
            issues.append(
                _new_issue(
                    IssueCode.UNCONFIRMED_OBLIGATION,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Obligation {obl.obligation_id} is ACTIVE but has neither "
                        f"credited_party_id nor obligee_text. Identity cannot be "
                        f"determined. Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue

        # ── Party resolved: check per-surface entry ───────────────────────────
        entries = manifest_by_contributor.get(resolved_contributor_id, [])
        surface_entries = [e for e in entries if e.credit_surface == obl.credit_surface]

        if not surface_entries:
            issues.append(
                _new_issue(
                    IssueCode.MISSING_CREDIT,
                    obligation_id=obl.obligation_id,
                    detail=(
                        f"Contributor '{resolved_contributor_id}' found in manifest but has "
                        f"no entry on surface {obl.credit_surface.value}. "
                        f"Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue

        entry = surface_entries[0]

        # ── Evidence coverage check for evaluated element ──────────────────────
        # Every rendered_element_id referenced by an active obligation's resolved
        # manifest entry must have a corresponding LayoutAssertion.
        if entry.rendered_element_id not in layout_by_id:
            issues.append(
                _new_issue(
                    IssueCode.ARTIFACT_PENDING,
                    obligation_id=obl.obligation_id,
                    manifest_refs=[entry.rendered_element_id],
                    detail=(
                        f"Missing layout assertion for evaluated element '{entry.rendered_element_id}' "
                        f"referenced by obligation {obl.obligation_id}."
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )
            continue

        # ── required_display_text vs display_name (NFC both sides) ───────────
        if obl.required_display_text is not None:
            expected = _nfc(obl.required_display_text)
            actual = _nfc(entry.display_name)
            if expected != actual:
                issues.append(
                    _new_issue(
                        IssueCode.ARTIFACT_TEXT_MISMATCH,
                        obligation_id=obl.obligation_id,
                        manifest_refs=[entry.rendered_element_id],
                        discriminator="display_name",
                        detail=(
                            f"display_name mismatch: required_display_text "
                            f"'{obl.required_display_text}' (NFC: '{expected}') does not "
                            f"match manifest display_name '{entry.display_name}' "
                            f"(NFC: '{actual}'). Source: {src_doc_id} §{src_span.quote}"
                        ),
                        source_document_id=src_doc_id,
                        source_document_version=src_doc_ver,
                        source_span=src_span,
                    )
                )

        # ── role_label vs role (NFC both sides) ───────────────────────────────
        if obl.role_label is not None:
            expected_role = _nfc(obl.role_label)
            actual_role = _nfc(entry.role)
            if expected_role != actual_role:
                issues.append(
                    _new_issue(
                        IssueCode.ARTIFACT_TEXT_MISMATCH,
                        obligation_id=obl.obligation_id,
                        manifest_refs=[entry.rendered_element_id],
                        discriminator="role",
                        detail=(
                            f"role mismatch: role_label '{obl.role_label}' "
                            f"(NFC: '{expected_role}') does not match manifest role "
                            f"'{entry.role}' (NFC: '{actual_role}'). "
                            f"Source: {src_doc_id} §{src_span.quote}"
                        ),
                        source_document_id=src_doc_id,
                        source_document_version=src_doc_ver,
                        source_span=src_span,
                    )
                )

        # ── card_type (grouping) check ────────────────────────────────────────
        if obl.card_type is not None:
            from creditlock.domain.models import CardType

            if obl.card_type == CardType.SOLO:
                if entry.group_id is not None or entry.shared_with:
                    issues.append(
                        _new_issue(
                            IssueCode.ARTIFACT_GROUPING_MISMATCH,
                            obligation_id=obl.obligation_id,
                            manifest_refs=[entry.rendered_element_id],
                            detail=(
                                f"Obligation requires SOLO card for "
                                f"'{resolved_contributor_id}' on "
                                f"{obl.credit_surface.value}, but manifest entry is grouped "
                                f"(group_id={entry.group_id!r}, "
                                f"shared_with={entry.shared_with}). "
                                f"Source: {src_doc_id} §{src_span.quote}"
                            ),
                            source_document_id=src_doc_id,
                            source_document_version=src_doc_ver,
                            source_span=src_span,
                        )
                    )
            elif (
                obl.card_type == CardType.SHARED
                and entry.group_id is None
                and not entry.shared_with
            ):
                issues.append(
                    _new_issue(
                        IssueCode.ARTIFACT_GROUPING_MISMATCH,
                        obligation_id=obl.obligation_id,
                        manifest_refs=[entry.rendered_element_id],
                        detail=(
                            f"Obligation requires SHARED card for "
                            f"'{resolved_contributor_id}' on "
                            f"{obl.credit_surface.value}, but manifest entry has no "
                            f"group. Source: {src_doc_id} §{src_span.quote}"
                        ),
                        source_document_id=src_doc_id,
                        source_document_version=src_doc_ver,
                        source_span=src_span,
                    )
                )

        # ── card_position (absolute ordinal) check ────────────────────────────
        if (
            obl.card_position is not None
            and isinstance(obl.card_position, AbsolutePosition)
            and entry.ordinal_position != obl.card_position.ordinal
        ):
            issues.append(
                _new_issue(
                    IssueCode.ARTIFACT_POSITION_MISMATCH,
                    obligation_id=obl.obligation_id,
                    manifest_refs=[entry.rendered_element_id],
                    detail=(
                        f"Obligation requires ordinal position "
                        f"{obl.card_position.ordinal} for "
                        f"'{resolved_contributor_id}' on "
                        f"{obl.credit_surface.value}, but manifest has "
                        f"ordinal_position={entry.ordinal_position}. "
                        f"Source: {src_doc_id} §{src_span.quote}"
                    ),
                    source_document_id=src_doc_id,
                    source_document_version=src_doc_ver,
                    source_span=src_span,
                )
            )

        # ── size check ────────────────────────────────────────────────────────
        if obl.minimum_relative_size is not None:
            if inp.layout_evidence is None:
                issues.append(
                    _new_issue(
                        IssueCode.ARTIFACT_PENDING,
                        obligation_id=obl.obligation_id,
                        manifest_refs=[entry.rendered_element_id],
                        detail=(
                            f"Obligation {obl.obligation_id} requires size check but "
                            f"layout evidence is absent. Render required."
                        ),
                        source_document_id=src_doc_id,
                        source_document_version=src_doc_ver,
                        source_span=src_span,
                    )
                )
            else:
                ref_elem_id = (obl.size_reference or {}).get("rendered_element_id")
                subject_assertion = layout_by_id.get(entry.rendered_element_id)
                ref_assertion = layout_by_id.get(ref_elem_id) if ref_elem_id else None

                if subject_assertion is None or ref_assertion is None:
                    issues.append(
                        _new_issue(
                            IssueCode.REQUIRED_EVIDENCE_UNAVAILABLE,
                            obligation_id=obl.obligation_id,
                            manifest_refs=[entry.rendered_element_id],
                            detail=(
                                f"Size check for obligation {obl.obligation_id} requires "
                                f"layout assertion for '{entry.rendered_element_id}' and "
                                f"reference '{ref_elem_id}', but one or both are absent."
                            ),
                            source_document_id=src_doc_id,
                            source_document_version=src_doc_ver,
                            source_span=src_span,
                        )
                    )
                else:
                    subject_px: float = subject_assertion.computed_font_size_px
                    ref_px: float = ref_assertion.computed_font_size_px
                    required_px = obl.minimum_relative_size * ref_px
                    if subject_px + 0.1 < required_px:
                        issues.append(
                            _new_issue(
                                IssueCode.ARTIFACT_SIZE_MISMATCH,
                                obligation_id=obl.obligation_id,
                                manifest_refs=[entry.rendered_element_id],
                                detail=(
                                    f"Size check failed for '{resolved_contributor_id}': "
                                    f"computed_font_size_px={subject_px}+0.1="
                                    f"{subject_px + 0.1:.4f} < required "
                                    f"{obl.minimum_relative_size}*{ref_px}="
                                    f"{required_px:.4f}. "
                                    f"Source: {src_doc_id} §{src_span.quote}"
                                ),
                                source_document_id=src_doc_id,
                                source_document_version=src_doc_ver,
                                source_span=src_span,
                            )
                        )

    # ── Visual observation flags check (element-scoped, independent of obligations) ──
    if inp.visual_observations:
        for obs in inp.visual_observations.observations:
            if obs.flagged:
                reason = obs.flag_reason or obs.observation
                issues.append(
                    _new_issue(
                        IssueCode.VISUAL_OBSERVATION_UNCERTAIN,
                        obligation_id=None,
                        manifest_refs=[obs.rendered_element_id],
                        discriminator=obs.rendered_element_id,
                        detail=(
                            f"Visual observation flagged for element '{obs.rendered_element_id}': "
                            f"{reason}. Human review required."
                        ),
                    )
                )

    return issues
