"""
Extractor Agent.

Reads deal memos and amendments, emitting typed CANDIDATE obligations with source spans.
Never activates obligations.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from creditlock.agents.models import DocumentReference, ExtractionResult, StructuredGeneration
from creditlock.agents.provider import (
    GoogleModelProvider,
    ModelInvocationUnavailableError,
    ModelProvider,
)
from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.domain.models import (
    AbsolutePosition,
    CardPositionKind,
    CardType,
    CreditSurface,
    Obligation,
    ObligationStatus,
    SourceSpan,
)
from creditlock.settings import get_settings

# ── Stable prompt template constants for reproducible hashing ─────────────────
PROMPT_VERSION = "extractor-v2"

SYSTEM_INSTRUCTION_TEMPLATE = (
    "You are an expert entertainment contract obligation extractor. "
    "CRITICAL NON-BINDING SAFETY RULE: If the text is marked as a discussion "
    "draft, subject to negotiation, term sheet, non-binding preliminary "
    "discussion, or contains future-contract language without binding "
    "commitment, return candidates = []. "
    "For binding text, extract exact verbatim quotes from the text and "
    "calculate exact character offsets start_char and end_char such that "
    "text_content[start_char:end_char] == quote. "
    "For multi-person clauses (e.g. 'Produced by Person A and Person B'), "
    "emit separate candidates for each person with required_display_text "
    "equal ONLY to their individual name, sharing the exact verbatim quote."
)

USER_PROMPT_TEMPLATE = (
    "Extract all credit obligations from the following contract text.\n"
    'Text:\n"""\n{text_content}\n"""\n\n'
    "Instructions:\n"
    "For each credit obligation found in the text, extract:\n"
    "- quote: the exact verbatim substring from the text representing the "
    "credit requirement clause.\n"
    "- start_char: 0-indexed character offset where quote starts in the text.\n"
    "- end_char: 0-indexed character offset where quote ends in the text "
    "(start_char + len(quote)).\n"
    "- required_display_text: ONLY the single credited person's name or title "
    "(e.g. 'Person A'). Never put multiple names into required_display_text.\n"
    "- role_label: the role (e.g. Composer, Director, Producer, Executive "
    "Producer).\n"
    "- credit_surface: END_CARDS, MAIN_TITLES, BILLING_BLOCK, or END_CRAWL.\n"
    "- card_type: SOLO, SHARED, or CRAWL.\n"
    "- card_position_ordinal: integer >= 1 ONLY if the source text explicitly "
    "states a position (e.g. 'first position', '2nd card', 'position 3'). "
    "Use null if the source does not explicitly state a position.\n"
    "- confidence: float 0.0 to 1.0.\n\n"
    "CONJUNCTIVE / MULTI-PERSON CLAUSES: If a clause specifies credit for "
    "multiple individuals (e.g., 'Produced by Person A and Person B', "
    "'Written by Person A, Person B & Person C'), emit a separate candidate "
    "obligation for EACH individual. Each candidate must set "
    "required_display_text to ONLY that single person's name. Both candidates "
    "MUST share the exact verbatim quote from the text."
)



class ExtractedCandidateSchema(BaseModel):
    model_config = {"extra": "forbid"}

    credited_party_id: str | None = None
    obligee_text: str | None = None
    required_display_text: str
    role_label: str
    credit_surface: str = "END_CARDS"
    card_type: str = "SOLO"
    card_position_ordinal: int | None = Field(default=None, ge=1)
    quote: str
    start_char: int | None = None
    end_char: int | None = None
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class ExtractorOutputSchema(BaseModel):
    model_config = {"extra": "forbid"}

    candidates: list[ExtractedCandidateSchema]


class ExtractorAgent:
    """Bounded extractor agent using injectable ModelProvider or real Gemini call."""

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
                agent_role="extractor",
                primary_model_id=settings.gemini_extractor_model,
                fallback_model_id=settings.gemini_extractor_fallback_model,
            )

    def extract_from_document(
        self,
        document_ref: DocumentReference,
        text_content: str,
        production_id: str,
    ) -> ExtractionResult:
        """
        Extract candidate obligations from text content.

        Invariants:
        - All obligations receive status = ObligationStatus.CANDIDATE.
        - text_content[start_char:end_char] MUST exactly equal quote.
        - Source spans and source_hash are attached.
        - Provenance recorded strictly from the active model provider path.
        - Invalid schema, missing source support, or missing credentials
          fail closed.
        - Never activates an obligation.
        """
        computed_hash = sha256_bytes_digest(text_content.encode())

        if not self.provider.is_available():
            raise ModelInvocationUnavailableError(
                "Gemini model provider is unavailable: missing API key "
                "credentials."
            )

        prompt = USER_PROMPT_TEMPLATE.format(text_content=text_content)

        generation: StructuredGeneration[ExtractorOutputSchema] = (
            self.provider.generate_structured(
                prompt=prompt,
                response_schema=ExtractorOutputSchema,
                system_instruction=SYSTEM_INSTRUCTION_TEMPLATE,
            )
        )
        output = generation.output
        provenance = generation.provenance

        obligations: list[Obligation] = []
        rejections: list[dict[str, str]] = []
        raw_candidate_count = len(output.candidates)

        for candidate in output.candidates:
            quote = candidate.quote or ""
            if not quote:
                rejections.append({"code": "INVALID_SPAN", "detail": "empty"})
                continue

            start_char = candidate.start_char
            end_char = candidate.end_char

            if start_char is not None and end_char is not None:
                if (
                    start_char >= 0
                    and end_char <= len(text_content)
                    and start_char < end_char
                    and text_content[start_char:end_char] == quote
                ):
                    pass
                elif (
                    text_content.count(quote) == 1
                    and abs(start_char - text_content.find(quote)) <= 5
                ):
                    start_char = text_content.find(quote)
                    end_char = start_char + len(quote)
                else:
                    rejections.append(
                        {"code": "INVALID_SPAN", "detail": "mismatch"}
                    )
                    continue
            else:
                count = text_content.count(quote)
                if count == 1:
                    start_char = text_content.find(quote)
                    end_char = start_char + len(quote)
                elif count == 0:
                    rejections.append(
                        {"code": "INVALID_SPAN", "detail": "not_found"}
                    )
                    continue
                else:
                    rejections.append(
                        {"code": "AMBIGUOUS_QUOTE", "detail": "multiple"}
                    )
                    continue

            try:
                surface = CreditSurface(candidate.credit_surface.upper())
            except Exception:  # noqa: BLE001
                rejections.append(
                    {"code": "INVALID_ENUM", "detail": "credit_surface"}
                )
                continue

            try:
                c_type = CardType(candidate.card_type.upper())
            except Exception:  # noqa: BLE001
                rejections.append(
                    {"code": "INVALID_ENUM", "detail": "card_type"}
                )
                continue

            # Position semantics: only create AbsolutePosition when the
            # model explicitly supplies a valid ordinal.
            card_pos: AbsolutePosition | None = None
            if candidate.card_position_ordinal is not None:
                if candidate.card_position_ordinal >= 1:
                    card_pos = AbsolutePosition(
                        kind=CardPositionKind.ABSOLUTE,
                        ordinal=candidate.card_position_ordinal,
                    )
                else:
                    rejections.append(
                        {
                            "code": "UNSUPPORTED_POSITION",
                            "detail": "invalid_ordinal",
                        }
                    )
                    continue

            model_id = (
                provenance.actual_model_used or provenance.primary_model
            )
            obl = Obligation(
                obligation_id=f"obl-{uuid.uuid4().hex[:12]}",
                production_id=production_id,
                credited_party_id=candidate.credited_party_id,
                obligee_text=candidate.obligee_text,
                required_display_text=candidate.required_display_text,
                role_label=candidate.role_label,
                credit_surface=surface,
                card_type=c_type,
                card_position=card_pos,
                source_document_id=document_ref.uri,
                source_document_version=1,
                source_span=SourceSpan(
                    start_char=start_char,
                    end_char=end_char,
                    quote=quote,
                ),
                source_hash=computed_hash,
                agent_reported_confidence=candidate.confidence,
                extraction_model_id=model_id,
                prompt_version=PROMPT_VERSION,
                status=ObligationStatus.CANDIDATE,
            )
            obligations.append(obl)

        return ExtractionResult(
            document_ref=document_ref,
            extracted_obligations=obligations,
            provenance=provenance,
            raw_candidate_count=raw_candidate_count,
            accepted_count=len(obligations),
            rejection_diagnostics=rejections,
        )
