"""
AI extraction and precedence benchmark runner.

Evaluates ExtractorAgent and PrecedenceResolverAgent against synthetic agent_eval dataset.
Computes field precision/recall/F1, exact source-span accuracy, unsupported field counts,
abstention accuracy, precedence recommendation accuracy, avg model calls, and elapsed time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import CallProvenance, DocumentReference
from creditlock.agents.provider import GoogleModelProvider, ModelProvider
from creditlock.agents.resolver import PrecedenceResolverAgent
from creditlock.domain.models import Obligation, ObligationStatus, SourceSpan
from creditlock.eval.models import AgentEvalResult
from creditlock.settings import get_settings


def run_agent_benchmark(
    base_dir: str | Path = "./fixtures/agent_eval",
    provider: ModelProvider | None = None,
    live_gemini: bool = False,
    fabricate_extractions: bool = False,
    fail_date_abstention: bool = False,
) -> AgentEvalResult:
    """
    Run AI extraction and precedence benchmark across synthetic dataset.

    Invariants:
    - Offline / default runs without live Gemini flag return UNVERIFIED_NO_LIVE_PROVIDER status with null metrics.
    - When live_gemini is True (or provider is explicitly passed), runs real agent inference and computes metrics.
    - Any metric with a zero denominator is returned as None (N/A) with denominator recorded.
    """
    base_path = Path(base_dir)
    dev_dir = base_path / "dev"
    sealed_dir = base_path / "sealed"

    dev_files = list(dev_dir.glob("*.json"))
    sealed_files = list(sealed_dir.glob("*.json"))

    if not sealed_files:
        raise RuntimeError("Evaluation failed: no sealed agent fixtures found under fixtures/agent_eval/sealed/.")

    all_files = dev_files + sealed_files
    total_cases = len(all_files)

    active_provider = provider
    provider_backend = None
    configured_model_id = None

    if active_provider is not None:
        provider_backend = getattr(active_provider, "auth_mode", "custom")
        configured_model_id = getattr(active_provider, "primary_model_id", "custom")
    elif live_gemini:
        try:
            s = get_settings()
            live_p = GoogleModelProvider("eval", s.gemini_extractor_model, s.gemini_extractor_fallback_model)
            if live_p.is_available():
                provider_backend = live_p.auth_mode
                configured_model_id = "multiple_models"
        except Exception:  # noqa: BLE001, S110
            pass

    if (active_provider is None and not live_gemini) and not (fabricate_extractions or fail_date_abstention):
        return AgentEvalResult(
            execution_status="UNVERIFIED_NO_LIVE_PROVIDER",
            completeness_status="INCOMPLETE",
            quality_gate_status="UNVERIFIED",
            summary_status="UNVERIFIED_NO_LIVE_PROVIDER",
            total_cases=total_cases,
            evaluated_cases=None,
            excluded_cases=None,
            excluded_reasons=[],
            provider_backend=None,
            configured_model_id=None,
            obligation_field_precision=None,
            obligation_field_recall=None,
            obligation_field_f1=None,
            exact_source_span_accuracy=None,
            span_denominator=None,
            unsupported_invented_field_count=None,
            abstention_accuracy=None,
            abstention_denominator=None,
            precedence_recommendation_accuracy=None,
            precedence_denominator=None,
            avg_model_calls=None,
            avg_elapsed_time_ms=None,
            case_results=[],
        )

    attempted_calls_by_model: dict[str, int] = {}
    successful_calls_by_model: dict[str, int] = {}
    agent_invocations: dict[str, int] = {}
    fallback_counts: dict[str, int] = {}
    val_failure_count = 0
    excluded_invalid_count = 0

    total_tp = 0
    total_fp = 0
    total_fn = 0
    total_span_correct = 0
    total_span_eval = 0
    unsupported_invented_count = 0

    abstention_correct = 0
    abstention_total = 0
    precedence_correct = 0
    precedence_total = 0

    total_model_calls = 0
    start_time = time.perf_counter()

    case_results: list[dict[str, Any]] = []
    evaluated_cases = 0
    excluded_cases = 0
    excluded_reasons: list[str] = []

    def record_provenance(prov: CallProvenance | None) -> None:
        nonlocal total_model_calls, val_failure_count
        if not prov:
            return
        agent_invocations[prov.agent_role] = agent_invocations.get(prov.agent_role, 0) + 1
        if prov.fallback_occurred:
            fallback_counts[prov.agent_role] = fallback_counts.get(prov.agent_role, 0) + 1

        for att in prov.attempts:
            total_model_calls += 1
            attempted_calls_by_model[att.model_id] = attempted_calls_by_model.get(att.model_id, 0) + 1
            if att.outcome == "SUCCESS":
                successful_calls_by_model[att.model_id] = successful_calls_by_model.get(att.model_id, 0) + 1
            elif att.sanitized_error_code == "VALIDATION_ERROR":
                val_failure_count += 1

    for file_path in all_files:
        content = json.loads(file_path.read_bytes())
        case_id = content.get("case_id", file_path.stem)
        doc_text = content.get("synthetic_document_text", "")
        expected_fields = content.get("expected_obligation_fields", [])
        gold_spans = content.get("exact_gold_source_spans", [])
        exp_ambiguity = content.get("expected_ambiguity_result", False)
        prec_cases = content.get("precedence_cases", [])

        # Internally inconsistent fixture check
        if len(expected_fields) > 1 and len(gold_spans) < len(expected_fields):
            excluded_cases += 1
            excluded_invalid_count += 1
            excluded_reasons.append(f"{case_id}: INVALID_FIXTURE")
            continue

        evaluated_cases += 1
        doc_ref = DocumentReference(uri=f"gs://eval/{case_id}.txt", sha256_hash="hash")

        case_rejection_reasons: list[str] = []
        accepted_candidates = 0
        rejected_candidates = 0

        if fabricate_extractions:
            extracted_obs = [
                Obligation(
                    obligation_id=f"fake-{i}",
                    production_id="prod-eval",
                    status=ObligationStatus.CANDIDATE,
                    extraction_model_id="fake-model",
                    required_display_text=e.get("required_display_text", ""),
                    role_label="Invented Role",
                    credit_surface=e.get("credit_surface", "MAIN_TITLES"),
                    card_type=e.get("card_type"),
                    source_document_id=case_id,
                    source_document_version=1,
                    source_hash="fakehash",
                    agent_reported_confidence=1.0,
                    prompt_version="v1",
                    source_span=SourceSpan(quote="Invented", start_char=0, end_char=5),
                )
                for i, e in enumerate(expected_fields)
            ]
            accepted_candidates = len(extracted_obs)
            total_model_calls += 1
        elif fail_date_abstention:
            extracted_obs = [
                Obligation(
                    obligation_id=f"fake-{i}",
                    production_id="prod-eval",
                    status=ObligationStatus.CANDIDATE,
                    extraction_model_id="fake-model",
                    required_display_text=e.get("required_display_text", ""),
                    role_label=e.get("role_label", ""),
                    credit_surface=e.get("credit_surface", "MAIN_TITLES"),
                    card_type=e.get("card_type"),
                    source_document_id=case_id,
                    source_document_version=1,
                    source_hash="fakehash",
                    agent_reported_confidence=1.0,
                    prompt_version="v1",
                    source_span=SourceSpan(quote="CREDIT", start_char=0, end_char=6),
                )
                for i, e in enumerate(expected_fields)
            ]
            accepted_candidates = len(extracted_obs)
            total_model_calls += 1
        else:
            extractor = ExtractorAgent(provider=active_provider)
            ext_res = extractor.extract_from_document(doc_ref, doc_text, "prod-eval")
            extracted_obs = ext_res.extracted_obligations
            accepted_candidates = len(extracted_obs)
            if ext_res.provenance:
                record_provenance(ext_res.provenance)
            else:
                total_model_calls += 1

        def to_tuple(obj: dict[str, Any] | Any) -> tuple[Any, ...]:
            if isinstance(obj, dict):
                return (
                    obj.get("required_display_text"),
                    obj.get("role_label"),
                    obj.get("credit_surface"),
                    obj.get("card_type"),
                )
            return (
                obj.required_display_text,
                obj.role_label,
                obj.credit_surface.value if hasattr(obj.credit_surface, "value") else str(obj.credit_surface),
                obj.card_type.value if hasattr(obj.card_type, "value") else str(obj.card_type),
            )

        exp_tuples = {to_tuple(e) for e in expected_fields if e.get("required_display_text")}
        ext_tuples = {to_tuple(o) for o in extracted_obs}

        tp = len(exp_tuples.intersection(ext_tuples))
        fp = len(ext_tuples - exp_tuples)
        fn = len(exp_tuples - ext_tuples)

        total_tp += tp
        total_fp += fp
        total_fn += fn

        for o in extracted_obs:
            total_span_eval += 1
            if o.source_span and doc_text[o.source_span.start_char : o.source_span.end_char] == o.source_span.quote:
                total_span_correct += 1
            else:
                unsupported_invented_count += 1
                case_rejection_reasons.append("INVALID_SOURCE_SPAN_QUOTE_MISMATCH")

        recs = None
        if prec_cases and not fabricate_extractions:
            if fail_date_abstention:
                from creditlock.agents.models import PrecedenceRecommendation

                recs = [
                    PrecedenceRecommendation(
                        candidate_obligation_ids=p.get("candidate_ids", []),
                        recommendation="RESOLVE",
                        controlling_obligation_id=(
                            p.get("candidate_ids", [])[0] if p.get("candidate_ids") else None
                        ),
                        rationale="Failed negative control",
                    )
                    for p in prec_cases
                ]
                total_model_calls += 1
            else:
                resolver = PrecedenceResolverAgent(provider=active_provider)
                recs = resolver.recommend_precedence(extracted_obs)
                for r in recs:
                    if r.provenance:
                        record_provenance(r.provenance)

            for p_case in prec_cases:
                precedence_total += 1
                exp_rec = p_case.get("expected_recommendation")
                if recs and getattr(recs[0].recommendation, "value", recs[0].recommendation).upper() == exp_rec.upper():
                    precedence_correct += 1

        if exp_ambiguity:
            abstention_total += 1
            if not extracted_obs or (
                prec_cases
                and recs
                and getattr(recs[0].recommendation, "value", recs[0].recommendation).upper() == "ABSTAIN"
            ):
                abstention_correct += 1

        case_results.append(
            {
                "case_id": case_id,
                "expected_count": len(expected_fields),
                "extracted_count": len(extracted_obs),
                "accepted_candidate_count": accepted_candidates,
                "rejected_candidate_count": rejected_candidates,
                "rejection_reasons": case_rejection_reasons,
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        )

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    precision = (total_tp / (total_tp + total_fp)) if (total_tp + total_fp) > 0 else None
    recall = (total_tp / (total_tp + total_fn)) if (total_tp + total_fn) > 0 else None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = (2 * precision * recall) / (precision + recall)
    elif precision == 0.0 or recall == 0.0:
        f1 = 0.0
    else:
        f1 = None

    span_acc = (total_span_correct / total_span_eval) if total_span_eval > 0 else None
    abstention_acc = (abstention_correct / abstention_total) if abstention_total > 0 else None
    prec_acc = (precedence_correct / precedence_total) if precedence_total > 0 else None

    avg_calls = (total_model_calls / evaluated_cases) if evaluated_cases > 0 else 0.0
    avg_elapsed = (elapsed_ms / evaluated_cases) if evaluated_cases > 0 else 0.0

    if fabricate_extractions or fail_date_abstention:
        exec_status = "NEGATIVE_CONTROL"
    else:
        exec_status = "COMPLETED" if (live_gemini or active_provider is not None) else "UNVERIFIED_NO_LIVE_PROVIDER"

    comp_status = "COMPLETE" if excluded_cases == 0 else "INCOMPLETE_EXCLUDED_CASES"

    if exec_status == "UNVERIFIED_NO_LIVE_PROVIDER":
        qg_status = "UNVERIFIED"
    elif f1 is not None and f1 >= 0.8 and (span_acc is None or span_acc >= 0.8):
        qg_status = "PASSED"
    else:
        qg_status = "FAILED"

    if fabricate_extractions or fail_date_abstention:
        sum_status = "VERIFIED_NEGATIVE_CONTROL"
    elif exec_status == "UNVERIFIED_NO_LIVE_PROVIDER":
        sum_status = "UNVERIFIED_NO_LIVE_PROVIDER"
    elif qg_status == "PASSED" and comp_status == "COMPLETE":
        sum_status = "VERIFIED"
    elif qg_status == "FAILED":
        sum_status = "FAILED_QUALITY_GATE"
    else:
        sum_status = "INCOMPLETE_BENCHMARK"

    return AgentEvalResult(
        execution_status=exec_status,
        completeness_status=comp_status,
        quality_gate_status=qg_status,
        summary_status=sum_status,
        total_cases=total_cases,
        evaluated_cases=evaluated_cases,
        excluded_cases=excluded_cases,
        excluded_reasons=excluded_reasons,
        provider_backend=provider_backend,
        configured_model_id=configured_model_id,
        obligation_field_precision=precision,
        obligation_field_recall=recall,
        obligation_field_f1=f1,
        exact_source_span_accuracy=span_acc,
        span_denominator=total_span_eval,
        unsupported_invented_field_count=unsupported_invented_count,
        abstention_accuracy=abstention_acc,
        abstention_denominator=abstention_total,
        precedence_recommendation_accuracy=prec_acc,
        precedence_denominator=precedence_total,
        avg_model_calls=avg_calls,
        avg_elapsed_time_ms=avg_elapsed,
        attempted_model_calls_by_model=attempted_calls_by_model,
        successful_model_calls_by_model=successful_calls_by_model,
        agent_invocation_counts=agent_invocations,
        fallback_counts_by_agent=fallback_counts,
        model_output_validation_failure_count=val_failure_count,
        excluded_invalid_fixture_count=excluded_invalid_count,
        case_results=case_results,
    )
