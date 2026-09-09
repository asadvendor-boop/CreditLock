"""
Precedence Resolver Agent.

Analyzes candidate obligations across deal memos and amendments.
Emits RESOLVE, ABSTAIN, or CONFLICT recommendations via Gemini LLM provider.
Dates alone never prove supersession.
Never writes ACTIVE obligations.
"""
from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel

from creditlock.agents.models import PrecedenceRecommendation, StructuredGeneration
from creditlock.agents.provider import (
    GoogleModelProvider,
    ModelInvocationUnavailableError,
    ModelProvider,
)
from creditlock.domain.models import Obligation
from creditlock.settings import get_settings

# ── Stable prompt template constants for reproducible hashing ─────────────────
PROMPT_VERSION = "resolver-v2"

SYSTEM_INSTRUCTION_TEMPLATE = (
    "You are an expert entertainment contract precedence resolver. "
    "Require explicit supersession clause for RESOLVE. "
    "Use ABSTAIN for date-only ambiguity. "
    "Use CONFLICT for incompatible mutually exclusive obligations."
)

USER_PROMPT_TEMPLATE = (
    "Evaluate precedence between these candidate obligations:\n"
    "{obligation_lines}\n"
    "Rules:\n"
    "- RESOLVE: Requires explicit document amendment clause or keywords "
    "(e.g. 'supersedes', 'amends', 'replaces', 'overrides') specifying "
    "which document controls.\n"
    "- ABSTAIN: Use ABSTAIN when documents differ by date/version for the "
    "same person without explicit supersession language.\n"
    "- CONFLICT: Use CONFLICT when two candidate obligations for the same "
    "role/credit are mutually exclusive (e.g. different names both claiming "
    "exclusive solo credit) without explicit supersession language.\n"
    "Recommend RESOLVE, ABSTAIN, or CONFLICT."
)


class ResolverLLMOutputSchema(BaseModel):
    model_config = {"extra": "forbid"}

    recommendation: str
    controlling_obligation_id: str | None = None
    rationale: str


EXPLICIT_SUPERSEDING_KEYWORDS = (
    "supersede",
    "amend",
    "replace",
    "cancel",
    "override",
    "amendment",
)


class PrecedenceResolverAgent:
    """Precedence resolver agent evaluating candidate obligation relationships."""

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        provider: ModelProvider | None = None,
    ) -> None:
        if provider is not None:
            self.provider = provider
        else:
            settings = get_settings()
            self.provider = GoogleModelProvider(
                agent_role="resolver",
                primary_model_id=settings.gemini_resolver_model,
                fallback_model_id=settings.gemini_resolver_fallback_model
            )

    def recommend_precedence(
        self,
        candidate_obligations: list[Obligation],
    ) -> list[PrecedenceRecommendation]:
        """
        Evaluate candidate obligations for precedence.

        Invariants:
        - Fail closed if model provider unavailable.
        - controlling_obligation_id must belong to candidate_obligation_ids.
        - Dates/timestamps or document version alone can NEVER yield RESOLVE.
        - RESOLVE requires cited explicit supersession language in text.
        - Never activates obligations or mutates the registry. Returns recommendations only.
        """
        if not self.provider.is_available():
            raise ModelInvocationUnavailableError(
                "Gemini model provider is unavailable: missing API key credentials."
            )

        recommendations: list[PrecedenceRecommendation] = []
        if len(candidate_obligations) <= 1:
            return recommendations

        # Group candidates targeting the same credit
        by_role: dict[str, list[Obligation]] = {}
        for obl in candidate_obligations:
            key = (
                (obl.role_label or "")
                + ":"
                + (obl.credited_party_id or obl.obligee_text or obl.required_display_text or "")
            )
            by_role.setdefault(key, []).append(obl)

        for group in by_role.values():
            if len(group) < 2:
                continue

            ids = [o.obligation_id for o in group]
            obl_lines = "\n".join(
                [
                    f"- ID: {o.obligation_id}, "
                    f"DocID: {o.source_document_id}, "
                    f"DocVersion: {o.source_document_version}, "
                    f"Text: {o.required_display_text}, "
                    f"Quote: {o.source_span.quote if o.source_span else ''}"
                    for o in group
                ]
            )
            prompt = USER_PROMPT_TEMPLATE.format(
                obligation_lines=obl_lines,
            )

            generation: StructuredGeneration[ResolverLLMOutputSchema] = (
                self.provider.generate_structured(
                    prompt=prompt,
                    response_schema=ResolverLLMOutputSchema,
                    system_instruction=SYSTEM_INSTRUCTION_TEMPLATE,
                )
            )
            output = generation.output
            provenance = generation.provenance

            rec_str = output.recommendation.upper()
            if rec_str not in ("RESOLVE", "ABSTAIN", "CONFLICT"):
                rec_str = "ABSTAIN"

            controlling_id = output.controlling_obligation_id

            # Deterministic Post-Validation 1: controlling_obligation_id MUST belong to candidate IDs
            if rec_str == "RESOLVE" and (not controlling_id or controlling_id not in ids):
                rec_str = "ABSTAIN"
                controlling_id = None

            # Deterministic Post-Validation 2: RESOLVE requires explicit supersession keywords in text
            if rec_str == "RESOLVE":
                has_explicit_language = False
                for o in group:
                    quote_text = (o.source_span.quote if o.source_span else "").lower()
                    doc_id_text = str(o.source_document_id or "").lower()
                    if any(kw in quote_text or kw in doc_id_text for kw in EXPLICIT_SUPERSEDING_KEYWORDS):
                        has_explicit_language = True
                        break

                if not has_explicit_language:
                    # Date-only or version-only difference without explicit language -> ABSTAIN
                    rec_str = "ABSTAIN"
                    controlling_id = None

            final_rec = cast(Literal["RESOLVE", "ABSTAIN", "CONFLICT"], rec_str)

            recommendations.append(
                PrecedenceRecommendation(
                    recommendation=final_rec,
                    candidate_obligation_ids=ids,
                    controlling_obligation_id=controlling_id if final_rec == "RESOLVE" else None,
                    rationale=output.rationale,
                    provenance=provenance,
                )
            )

        return recommendations
