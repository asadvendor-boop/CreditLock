"""
Adversarial Unit Tests for Extractor Source-Span Rules (Commit G).
"""

from __future__ import annotations

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference
from creditlock.agents.provider import FakeModelProvider


def test_wrong_offsets_rejected() -> None:
    text = "DEAL MEMO\nProducer Credit Required: Person A."
    doc_ref = DocumentReference(uri="gs://test/doc1.txt", sha256_hash="hash1")

    # Present quote "Person A." but wrong start_char/end_char (0, 10) -> "DEAL MEMO\n"
    fake = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Person A",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Person A.",
                        "start_char": 0,
                        "end_char": 10,
                    }
                ]
            }
        },
    )

    agent = ExtractorAgent(provider=fake)
    res = agent.extract_from_document(doc_ref, text, "prod-1")
    assert len(res.extracted_obligations) == 0


def test_ambiguous_repeated_quote_rejected() -> None:
    text = "DEAL MEMO\nProducer: Person A.\nAnother section Producer: Person A."
    doc_ref = DocumentReference(uri="gs://test/doc2.txt", sha256_hash="hash2")

    # Quote occurs twice, offsets are None -> ambiguous, fail closed
    fake = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Person A",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Person A.",
                        "start_char": None,
                        "end_char": None,
                    }
                ]
            }
        },
    )

    agent = ExtractorAgent(provider=fake)
    res = agent.extract_from_document(doc_ref, text, "prod-1")
    assert len(res.extracted_obligations) == 0


def test_unique_exact_quote_located() -> None:
    text = "DEAL MEMO\nExecutive Producer: Person A."
    # len("DEAL MEMO\nExecutive Producer: ") == 30
    # len("Person A.") == 9 -> 30..39
    doc_ref = DocumentReference(uri="gs://test/doc3.txt", sha256_hash="hash3")

    fake = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Person A",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Person A.",
                        "start_char": None,
                        "end_char": None,
                    }
                ]
            }
        },
    )

    agent = ExtractorAgent(provider=fake)
    res = agent.extract_from_document(doc_ref, text, "prod-1")
    assert len(res.extracted_obligations) == 1
    span = res.extracted_obligations[0].source_span
    assert span is not None
    assert span.quote == "Person A."
    assert span.start_char == 30
    assert span.end_char == 39


def test_no_punctuation_or_label_manufactured() -> None:
    text = "DEAL MEMO\nCREDIT: Person A."
    doc_ref = DocumentReference(uri="gs://test/doc4.txt", sha256_hash="hash4")

    # Model returned quote "Person A" with no trailing dot and no CREDIT: prefix
    # len("DEAL MEMO\nCREDIT: ") == 18
    # len("Person A") == 8 -> 18..26
    fake = FakeModelProvider(
        agent_role="extractor",
        responses={
            "default": {
                "candidates": [
                    {
                        "required_display_text": "Person A",
                        "role_label": "Producer",
                        "credit_surface": "END_CARDS",
                        "card_type": "SOLO",
                        "quote": "Person A",
                        "start_char": 18,
                        "end_char": 26,
                    }
                ]
            }
        },
    )

    agent = ExtractorAgent(provider=fake)
    res = agent.extract_from_document(doc_ref, text, "prod-1")
    assert len(res.extracted_obligations) == 1
    span = res.extracted_obligations[0].source_span
    assert span is not None
    # Quote must remain exact model quote, not prepended with "CREDIT: " or appended with "."
    assert span.quote == "Person A"
    assert span.start_char == 18
    assert span.end_char == 26
