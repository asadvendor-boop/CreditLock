"""
Runner for Versioned V2 Independent Component Benchmark Suite.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from creditlock.agents.extractor import ExtractorAgent
from creditlock.agents.models import DocumentReference
from creditlock.agents.provider import (
    FakeModelProvider,
    ModelOutputValidationError,
    ModelProvider,
)
from creditlock.agents.resolver import PrecedenceResolverAgent
from creditlock.agents.steward import StewardAgent
from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.domain.models import (
    AbsolutePosition,
    CreditManifest,
    Issue,
    Obligation,
    PatchOperation,
)
from creditlock.domain.patches import validate_patch
from creditlock.eval.v2_models import (
    VALID_SPLITS,
    ComponentEvalV2Result,
    ExtractorMetrics,
    PerCaseSanitizedProvenance,
    ResolverMetrics,
    StewardMetrics,
)
from creditlock.settings import get_settings


def compute_split_fixture_digest(fixtures_dir: Path, split: str | None = None) -> str:
    """Compute SHA-256 digest of fixtures. If split is provided, filter by exact split attribute."""
    h = hashlib.sha256()
    for p in sorted(fixtures_dir.rglob("*.json")):
        if split is not None:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or data.get("split") != split:
                continue
        h.update(p.relative_to(fixtures_dir).as_posix().encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


def compute_fixture_set_digest(fixtures_dir: Path) -> str:
    return compute_split_fixture_digest(fixtures_dir, split=None)


def compute_frozen_challenge_v2_digest(fixtures_dir: Path) -> str:
    """Compute SHA-256 digest of only FROZEN_CHALLENGE_V2 fixtures."""
    return compute_split_fixture_digest(fixtures_dir, split="FROZEN_CHALLENGE_V2")


def compute_git_commit_sha() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
        return "unknown-git-sha"
    except OSError:
        return "unknown-git-sha"


def get_agent_prompt_template_hashes() -> dict[str, str]:
    from creditlock.agents.extractor import (
        PROMPT_VERSION as EXT_PV,
    )
    from creditlock.agents.extractor import (
        SYSTEM_INSTRUCTION_TEMPLATE as EXT_SYS,
    )
    from creditlock.agents.extractor import (
        USER_PROMPT_TEMPLATE as EXT_USER,
    )
    from creditlock.agents.resolver import (
        PROMPT_VERSION as RES_PV,
    )
    from creditlock.agents.resolver import (
        SYSTEM_INSTRUCTION_TEMPLATE as RES_SYS,
    )
    from creditlock.agents.resolver import (
        USER_PROMPT_TEMPLATE as RES_USER,
    )
    from creditlock.agents.steward import (
        PROMPT_VERSION as STW_PV,
    )
    from creditlock.agents.steward import (
        SYSTEM_INSTRUCTION_TEMPLATE as STW_SYS,
    )
    from creditlock.agents.steward import (
        USER_PROMPT_TEMPLATE as STW_USER,
    )

    def _hash_templates(pv: str, sys: str, user: str) -> str:
        canonical = f"{pv}\n---\n{sys}\n---\n{user}"
        return sha256_bytes_digest(canonical.encode())

    return {
        "extractor": _hash_templates(EXT_PV, EXT_SYS, EXT_USER),
        "resolver": _hash_templates(RES_PV, RES_SYS, RES_USER),
        "steward": _hash_templates(STW_PV, STW_SYS, STW_USER),
    }


# ── Valid PatchOperation values ───────────────────────────────────────────────
_VALID_PATCH_OPS = {op.value for op in PatchOperation}


def _extract_field(
    obl: Obligation, field: str,
) -> object:
    """Explicit field adapter — no generic getattr."""
    if field == "required_display_text":
        return obl.required_display_text
    if field == "role_label":
        return obl.role_label
    if field == "credit_surface":
        return obl.credit_surface.value if obl.credit_surface else None
    if field == "card_type":
        return obl.card_type.value if obl.card_type else None
    if field == "card_position_ordinal":
        if isinstance(obl.card_position, AbsolutePosition):
            return obl.card_position.ordinal
        return None
    if field == "obligee_text":
        return obl.obligee_text
    if field == "credited_party_id":
        return obl.credited_party_id
    return None


def _extract_pred_dict(
    obl: Obligation, scored_fields: list[str],
) -> dict[str, object]:
    """Build prediction dict using explicit field adapter."""
    return {f: _extract_field(obl, f) for f in scored_fields}



def _compute_semantic_signature(agent_dir: str, data: dict[str, Any]) -> str:
    import json
    
    def _strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: _strip(v) for k, v in obj.items()
                if k not in {"id", "document_id"}
            }
        elif isinstance(obj, list):
            return [_strip(v) for v in obj]
        elif isinstance(obj, str):
            import re
            return re.sub(r'^\d+[\.\)]\s+', '', obj)
        return obj

    if agent_dir == "extractor":
        input_data = data.get("synthetic_document_text", "")
    elif agent_dir == "resolver":
        cands = data.get("input_candidate_obligations", [])
        input_data = _strip(cands)
    elif agent_dir == "steward":
        issue = data.get("input_issue", {})
        obl = data.get("input_obligation", {})
        man = data.get("input_manifest", {})
        input_data = _strip({"issue": issue, "obligation": obl, "manifest": man})
    else:
        input_data = {}
        
    return json.dumps(input_data, sort_keys=True)

def validate_fixtures_only(
    fixtures_dir: Path,
    target_split: str | None = None,
) -> list[str]:
    """
    Performs full schema validation on inputs consumed by the runner.
    Includes semantic duplicate detection for FROZEN_CHALLENGE_V2.
    """
    import json

    from pydantic import ValidationError

    from creditlock.agents.steward import is_admissible_steward_issue
    from creditlock.domain.models import CreditManifest, Issue, Obligation, Patch, PatchOperation
    from creditlock.domain.patches import validate_patch
    
    if target_split is not None and target_split not in VALID_SPLITS:
        return [f"Invalid target_split: '{target_split}', must be one of {sorted(VALID_SPLITS)} or None"]

    errors: list[str] = []
    seen_ids: set[str] = set()

    # Semantic uniqueness tracking
    semantic_signatures: dict[str, set[str]] = {
        "extractor": set(),
        "resolver": set(),
        "steward": set()
    }
    
    # Coverage tracking
    scenario_coverage: dict[str, set[str]] = {
        "extractor": set(),
        "resolver": set(),
        "steward": set()
    }

    # Expected scenario categories
    expected_categories = {
        "extractor": {
            "single-person positive", "two-person 'and' positive", "ampersand positive",
            "three-person list positive", "heading-separated binding clause positive",
            "repeated clause positive with exact unambiguous spans", "draft/non-binding negative",
            "conditional/future negative", "unfamiliar formatting positive", "unfamiliar role/names positive"
        },
        "resolver": {
            "explicit supersession", "explicit amendment",
            "date/version-only difference with no supersession language", "substantive incompatible obligations",
            "genuinely unrelated grouping identities", "same party with no controlling proof and no substantive incompatibility"
        },
        "steward": {
            "SUBSTITUTE_TEXT", "REORDER", "REGROUP", "REPOSITION",
            "deterministic abstention 1", "deterministic abstention 2", "deterministic abstention 3"
        }
    }

    for agent_dir in ("extractor", "resolver", "steward"):
        d = fixtures_dir / agent_dir
        if not d.exists():
            errors.append(f"Directory missing: {agent_dir}")
            continue

        for fpath in sorted(d.glob("*.json")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                err_type = type(e).__name__
                errors.append(
                    f"{fpath.name}: JSON parse failure: {err_type}"
                )
                continue

            case_id = data.get("case_id", "")
            if not case_id:
                errors.append(f"{fpath.name}: missing case_id")
                continue
            if case_id in seen_ids:
                errors.append(f"{fpath.name}: duplicate case_id={case_id}")
            seen_ids.add(case_id)

            split = data.get("split", "")
            if split not in VALID_SPLITS:
                errors.append(
                    f"{case_id}: invalid split '{split}'"
                )

            if target_split is not None and split != target_split:
                continue

            is_fv2 = (split == "FROZEN_CHALLENGE_V2")
            cat = data.get("scenario_category", "")
            
            if is_fv2:
                if cat:
                    scenario_coverage[agent_dir].add(cat)
                else:
                    errors.append(f"{case_id}: missing scenario_category")
                
                sig = _compute_semantic_signature(agent_dir, data)
                if sig in semantic_signatures[agent_dir]:
                    errors.append(f"{case_id}: semantic duplicate detected in {agent_dir}")
                semantic_signatures[agent_dir].add(sig)

            # Extractor span validation
            if agent_dir == "extractor":
                doc_text = data.get("synthetic_document_text")
                if not isinstance(doc_text, str):
                    errors.append(f"{case_id}: missing or invalid synthetic_document_text")
                
                exp_obls = data.get("expected_obligations", [])
                if not isinstance(exp_obls, list):
                    errors.append(f"{case_id}: expected_obligations must be a list")

                gspans = data.get("exact_gold_source_spans", [])
                if not isinstance(gspans, list):
                    errors.append(f"{case_id}: exact_gold_source_spans must be a list")
                    
                for idx, gspan in enumerate(gspans):
                    if not isinstance(gspan, dict):
                        errors.append(f"{case_id}: gold span {idx} is not an object")
                        continue
                    q = gspan.get("quote")
                    s = gspan.get("start_char")
                    end_c = gspan.get("end_char")
                    if not isinstance(q, str) or not isinstance(s, int) or not isinstance(end_c, int):
                        errors.append(f"{case_id}: invalid gold span types {idx}")
                        continue
                    if (
                        s < 0 or end_c < s or end_c > len(doc_text)
                        or doc_text[s:end_c] != q
                    ):
                        errors.append(
                            f"{case_id}: invalid gold span "
                            f"s={s} e={end_c} q={q!r}"
                        )

            # Resolver validation
            if agent_dir == "resolver":
                cands_raw = data.get("input_candidate_obligations", [])
                exp_rec = data.get("expected_recommendation")
                exp_ctrl = data.get("expected_controlling_id")
                
                try:
                    candidates = [Obligation.model_validate(c) for c in cands_raw]
                except ValidationError as e:
                    errors.append(f"{case_id}: input_candidate_obligations schema error: {e}")
                    candidates = []
                
                if exp_rec not in ("RESOLVE", "ABSTAIN", "CONFLICT"):
                    errors.append(f"{case_id}: expected_recommendation invalid: {exp_rec}")
                
                cand_ids = {c.obligation_id for c in candidates}
                if exp_rec == "RESOLVE":
                    if exp_ctrl not in cand_ids:
                        errors.append(f"{case_id}: expected_controlling_id {exp_ctrl} not in candidates")
                else:
                    if exp_ctrl is not None:
                        errors.append(f"{case_id}: expected_controlling_id must be null for {exp_rec}")

                if len(candidates) > 1:
                    keys = set()
                    for c in candidates:
                        # grouping identity based on what resolver expects
                        k = (c.credited_party_id, c.obligee_text, c.role_label, c.credit_surface.value if c.credit_surface else None)
                        keys.add(k)
                    
                    if cat == "genuinely unrelated grouping identities":
                        if len(keys) == 1:
                            errors.append(f"{case_id}: unrelated party case must have different grouping keys")
                    elif is_fv2 and len(keys) > 1:
                        errors.append(f"{case_id}: model-backed cases must have same real grouping identity")
                
                if "supersession" in cat or "amendment" in cat:
                    has_explicit = False
                    for c in candidates:
                        q = c.source_span.quote if c.source_span else ""
                        if "supersede" in q.lower() or "amend" in q.lower():
                            has_explicit = True
                    if not has_explicit and is_fv2 and "no supersession" not in cat:
                        errors.append(f"{case_id}: explicit supersession/amendment missing from source_span.quote")

            # Steward validation
            if agent_dir == "steward":
                try:
                    issue = Issue.model_validate(data.get("input_issue", {}))
                    obligation = Obligation.model_validate(data.get("input_obligation", {}))
                    manifest = CreditManifest.model_validate(data.get("input_manifest", {}))
                except ValidationError as e:
                    errors.append(f"{case_id}: steward inputs schema error: {e}")
                    continue

                exp_action = data.get("expected_action")
                exp_op = data.get("expected_patch_operation")
                exp_val = data.get("expected_patch_value")
                
                if exp_action not in ("PROPOSE_PATCH", "DETERMINISTIC_ABSTAIN"):
                    errors.append(f"{case_id}: expected_action invalid: {exp_action}")
                
                if exp_action == "PROPOSE_PATCH":
                    admissible, reason = is_admissible_steward_issue(issue, obligation)
                    if not admissible:
                        errors.append(
                            f"{case_id}: expected_action PROPOSE_PATCH is inadmissible for issue code '{issue.code.value}': {reason}"
                        )
                    else:
                        if exp_op not in _VALID_PATCH_OPS:
                            errors.append(
                                f"{case_id}: invalid operation "
                                f"'{exp_op}', must be one of "
                                f"{_VALID_PATCH_OPS}"
                            )
                        # Verify derivable patch validation
                        try:
                            patch_payload: dict[str, object] = {}
                            if exp_op == "SUBSTITUTE_TEXT":
                                patch_payload["new_text"] = str(exp_val)
                            elif exp_op == "REORDER":
                                patch_payload["new_ordinal"] = int(exp_val)
                            elif exp_op == "REGROUP":
                                patch_payload["new_card_type"] = str(exp_val)
                            elif exp_op == "REPOSITION":
                                patch_payload["new_surface"] = str(exp_val)

                            patch = Patch(
                                patch_id="test",
                                production_id=obligation.production_id,
                                manifest_id=manifest.manifest_id,
                                issue_id=issue.issue_id,
                                obligation_id=obligation.obligation_id,
                                operation=PatchOperation(exp_op),
                                target_rendered_element_id=(issue.manifest_refs[0] if issue.manifest_refs else (manifest.entries[0].rendered_element_id if manifest.entries else "test_target")),
                                payload=patch_payload,
                                proposed_by="test",
                                proposed_at="2026-01-01T00:00:00Z"
                            )
                            val_res = validate_patch(patch, obligation, manifest)
                            if not val_res.valid:
                                errors.append(f"{case_id}: expected patch is invalid: {val_res.rejection_reason}")
                        except (ValidationError, ValueError) as e:
                            errors.append(f"{case_id}: failed to validate derivable patch: {e}")

    # Check coverage requirements when target_split is None or "FROZEN_CHALLENGE_V2"
    if target_split is None or target_split == "FROZEN_CHALLENGE_V2":
        for agent_role, exp_cats in expected_categories.items():
            missing = exp_cats - scenario_coverage[agent_role]
            if missing:
                errors.append(f"Missing FROZEN_CHALLENGE_V2 scenarios for {agent_role}: {missing}")
            if len(semantic_signatures[agent_role]) != len(exp_cats):
                errors.append(
                    f"Expected {len(exp_cats)} unique semantic signatures for {agent_role}, got {len(semantic_signatures[agent_role])}"
                )

    return errors


def _steward_actual_value(
    patch: Any,
) -> str | None:
    """Extract actual patch value from payload."""
    if patch is None:
        return None
    payload = patch.payload
    for key in ("new_text", "new_card_type", "new_surface"):
        v = payload.get(key)
        if v is not None:
            return str(v)
    v = payload.get("new_ordinal")
    if v is not None:
        return str(v)
    return None


def run_component_eval_v2(
    fixtures_dir: Path,
    extractor_provider: ModelProvider | None = None,
    resolver_provider: ModelProvider | None = None,
    steward_provider: ModelProvider | None = None,
    *,
    agent_code_commit_sha: str | None = None,
    target_split: str | None = None,
) -> ComponentEvalV2Result:
    """
    Run versioned V2 independent component benchmark suite.

    If target_split is set, only evaluate fixtures in that split.
    """
    import os

    ext_dir = fixtures_dir / "extractor"
    res_dir = fixtures_dir / "resolver"
    stw_dir = fixtures_dir / "steward"

    is_live = bool(os.environ.get("CREDITLOCK_RUN_LIVE_GEMINI"))
    if not is_live:
        if extractor_provider is None:
            extractor_provider = FakeModelProvider(agent_role="extractor")
        if resolver_provider is None:
            resolver_provider = FakeModelProvider(agent_role="resolver")
        if steward_provider is None:
            steward_provider = FakeModelProvider(agent_role="steward")

    exec_status: Literal[
        "COMPLETED_LIVE_GEMINI",
        "COMPLETED_OFFLINE_FAKE",
        "FAILED_BEFORE_EXECUTION",
    ] = (
        "COMPLETED_LIVE_GEMINI" if is_live
        else "COMPLETED_OFFLINE_FAKE"
    )

    if not (ext_dir.exists() and res_dir.exists() and stw_dir.exists()):
        return ComponentEvalV2Result(
            execution_status=exec_status,
            completeness_status="INCOMPLETE_FIXTURE_SET",
            quality_gate_status="FAILED_QUALITY_GATE",
            summary_status="INCOMPLETE_FIXTURE_SET",
        )

    # Pre-validate all fixtures and target split BEFORE constructing providers
    validation_errors = validate_fixtures_only(fixtures_dir, target_split=target_split)
    if validation_errors:
        return ComponentEvalV2Result(
            execution_status="FAILED_BEFORE_EXECUTION",
            completeness_status="INVALID_FIXTURE_ANNOTATIONS",
            quality_gate_status="FAILED_QUALITY_GATE",
            summary_status="INVALID_FIXTURE_ANNOTATIONS",
            per_case_results=[
                {"validation_errors": validation_errors}
            ],
        )

    ext_files = sorted(ext_dir.glob("*.json"))
    res_files = sorted(res_dir.glob("*.json"))
    stw_files = sorted(stw_dir.glob("*.json"))

    if extractor_provider is None:
        extractor_agent = ExtractorAgent()
    else:
        extractor_agent = ExtractorAgent(provider=extractor_provider)

    if resolver_provider is None:
        resolver_agent = PrecedenceResolverAgent()
    else:
        resolver_agent = PrecedenceResolverAgent(
            provider=resolver_provider,
        )

    if steward_provider is None:
        steward_agent = StewardAgent()
    else:
        steward_agent = StewardAgent(provider=steward_provider)

    agent_execution_counts: dict[str, int] = {}
    model_backed_invocation_counts: dict[str, int] = {}
    fallback_counts_by_agent: dict[str, int] = {}
    attempted_calls: dict[str, int] = {}
    successful_calls: dict[str, int] = {}

    per_case_results: list[dict[str, Any]] = []
    per_case_sanitized_provenance: list[PerCaseSanitizedProvenance] = []
    seen_case_ids: set[str] = set()

    def record_prov(
        prov: Any,
        case_id: str,
        agent_role: str,
        execution_path: str,
        expected_outcome: str,
        actual_outcome: str,
        prompt_version: str = "v1",
    ) -> None:
        agent_execution_counts[agent_role] = (
            agent_execution_counts.get(agent_role, 0) + 1
        )

        if execution_path == "MODEL_BACKED" and prov is not None:
            model_backed_invocation_counts[agent_role] = (
                model_backed_invocation_counts.get(agent_role, 0) + 1
            )
            if prov.fallback_occurred:
                fallback_counts_by_agent[agent_role] = (
                    fallback_counts_by_agent.get(agent_role, 0) + 1
                )

            for att in prov.attempts:
                m = att.model_id
                attempted_calls[m] = attempted_calls.get(m, 0) + 1
                if att.outcome == "SUCCESS":
                    successful_calls[m] = (
                        successful_calls.get(m, 0) + 1
                    )

            prov_dump: dict[str, Any] | None = {
                "actual_model_used": prov.actual_model_used,
                "fallback_occurred": prov.fallback_occurred,
                "total_latency_ms": prov.total_latency_ms,
                "attempts": [
                    a.model_dump() for a in prov.attempts
                ],
            }
        else:
            prov_dump = None

        per_case_sanitized_provenance.append(
            PerCaseSanitizedProvenance(
                case_id=case_id,
                agent_role=agent_role,
                execution_path=(
                    "MODEL_BACKED"
                    if execution_path == "MODEL_BACKED"
                    else "DETERMINISTIC_SHORT_CIRCUIT"
                ),
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                prompt_version=prompt_version,
                provenance=prov_dump,
            )
        )

    # Accumulators per split
    splits = list(VALID_SPLITS)
    ext_data: dict[str, dict[str, Any]] = {
        sp: {
            "tp": 0, "fp": 0, "fn": 0,
            "valid_spans": 0, "valid_denom": 0,
            "gold_spans": 0, "gold_denom": 0,
            "non_binding_fp": 0, "invented": 0, "count": 0,
        }
        for sp in splits
    }
    res_data: dict[str, dict[str, Any]] = {
        sp: {
            "sys_rec_correct": 0, "sys_rec_denom": 0,
            "mb_rec_correct": 0, "mb_rec_denom": 0,
            "det_rec_correct": 0, "det_rec_denom": 0,
            "mb_ctrl_correct": 0, "mb_ctrl_denom": 0,
            "mb_abs_correct": 0, "mb_abs_denom": 0,
            "mb_conflict_correct": 0, "mb_conflict_denom": 0,
            "count": 0,
        }
        for sp in splits
    }
    stw_data: dict[str, dict[str, Any]] = {
        sp: {
            "act_correct": 0, "act_denom": 0,
            "op_correct": 0, "op_denom": 0,
            "val_correct": 0, "val_denom": 0,
            "patch_pass": 0, "patch_denom": 0,
            "count": 0,
        }
        for sp in splits
    }
    evaluated_count = 0

    # --- 1. Extractor Section ---
    for fpath in ext_files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        case_id = data["case_id"]
        if case_id in seen_case_ids:
            return ComponentEvalV2Result(
                execution_status=exec_status,
                completeness_status="INCOMPLETE_FIXTURE_SET",
                quality_gate_status="FAILED_QUALITY_GATE",
                summary_status="INCOMPLETE_FIXTURE_SET",
            )
        seen_case_ids.add(case_id)

        split = data.get("split", "DEV")
        if target_split and split != target_split:
            continue

        text = data["synthetic_document_text"]
        expected_obls = data["expected_obligations"]
        scored_fields = data.get(
            "scored_fields",
            ["required_display_text", "role_label"],
        )
        supported_fields = set(scored_fields) | set(
            data.get(
                "supported_unscored_fields",
                [
                    "credit_surface", "card_type",
                    "card_position_ordinal", "confidence",
                ],
            )
        )
        exact_gold_spans = data.get("exact_gold_source_spans", [])
        is_non_binding = data.get("is_non_binding", False)

        doc_ref = DocumentReference(
            uri=f"gs://eval-v2/{case_id}.txt",
            sha256_hash="v2hash",
        )

        # Handle MODEL_OUTPUT_SCHEMA_FAILURE separately
        try:
            res = extractor_agent.extract_from_document(
                doc_ref, text, "prod-v2",
            )
        except ModelOutputValidationError:
            per_case_results.append({
                "case_id": case_id,
                "component": "extractor",
                "split": split,
                "schema_failure": "MODEL_OUTPUT_SCHEMA_FAILURE",
            })
            continue

        extracted = res.extracted_obligations

        record_prov(
            res.provenance,
            case_id=case_id,
            agent_role="extractor",
            execution_path="MODEL_BACKED",
            expected_outcome=(
                f"{len(expected_obls)} candidate obligations"
            ),
            actual_outcome=(
                f"{len(extracted)} candidate obligations"
            ),
            prompt_version=ExtractorAgent.prompt_version,
        )

        acc = ext_data[split]
        acc["count"] += 1
        evaluated_count += 1

        case_span_valid = True
        case_gold_matched = True

        if is_non_binding:
            if len(extracted) > 0:
                acc["non_binding_fp"] += len(extracted)
        else:
            # Match scored fields using explicit adapter and compare source spans to expected obligation
            matched_expected: set[int] = set()
            for obl in extracted:
                acc["valid_denom"] += 1
                quote = obl.source_span.quote if obl.source_span else ""
                s = obl.source_span.start_char if obl.source_span else -1
                e = obl.source_span.end_char if obl.source_span else -1

                if (
                    obl.source_span
                    and 0 <= s < e <= len(text)
                    and text[s:e] == quote
                ):
                    acc["valid_spans"] += 1
                else:
                    case_span_valid = False

                pred = _extract_pred_dict(obl, scored_fields)
                matched_idx: int | None = None
                for idx, exp in enumerate(expected_obls):
                    if idx in matched_expected:
                        continue
                    field_match = all(
                        pred.get(f) == exp.get(f)
                        for f in scored_fields
                    )
                    if field_match:
                        matched_idx = idx
                        matched_expected.add(idx)
                        acc["tp"] += 1
                        break

                if matched_idx is None:
                    acc["fp"] += 1

                # Exact gold span comparison for predicted obligation
                acc["gold_denom"] += 1
                gold_match = False
                target_exp = expected_obls[matched_idx] if matched_idx is not None else None
                exp_span = (target_exp.get("source_span") if target_exp else None)

                if exp_span and isinstance(exp_span, dict):
                    if (
                        exp_span.get("quote") == quote
                        and exp_span.get("start_char") == s
                        and exp_span.get("end_char") == e
                    ):
                        gold_match = True
                elif matched_idx is not None and matched_idx < len(exact_gold_spans):
                    gspan = exact_gold_spans[matched_idx]
                    if (
                        gspan.get("quote") == quote
                        and gspan.get("start_char") == s
                        and gspan.get("end_char") == e
                    ):
                        gold_match = True
                else:
                    for gspan in exact_gold_spans:
                        if (
                            gspan.get("quote") == quote
                            and gspan.get("start_char") == s
                            and gspan.get("end_char") == e
                        ):
                            gold_match = True
                            break

                if gold_match:
                    acc["gold_spans"] += 1
                else:
                    case_gold_matched = False

            # Invented fields check
            all_known = {
                "credited_party_id", "obligee_text",
                "required_display_text", "role_label",
                "credit_surface", "card_type",
                "card_position_ordinal", "quote",
                "start_char", "end_char", "confidence",
            }
            for obl in extracted:
                for key in all_known:
                    val = _extract_field(obl, key)
                    if (
                        val is not None
                        and key in all_known
                        and key not in supported_fields
                        and key not in (
                            "quote", "start_char", "end_char",
                        )
                    ):
                        acc["invented"] += 1

            acc["fn"] += len(expected_obls) - len(matched_expected)

        per_case_results.append({
            "case_id": case_id,
            "component": "extractor",
            "split": split,
            "expected_scored_fields": expected_obls,
            "predicted_scored_fields": [
                _extract_pred_dict(o, scored_fields)
                for o in extracted
            ],
            "matched_status": (
                "MATCHED"
                if (
                    len(extracted) == len(expected_obls)
                    and not is_non_binding
                )
                else (
                    "FALSE_POSITIVE"
                    if is_non_binding and extracted
                    else "UNMATCHED"
                )
            ),
            "expected_gold_spans": exact_gold_spans,
            "predicted_spans": [
                {
                    "quote": o.source_span.quote,
                    "start_char": o.source_span.start_char,
                    "end_char": o.source_span.end_char,
                }
                for o in extracted
                if o.source_span
            ],
            "span_validity_passed": case_span_valid,
            "exact_gold_matched": case_gold_matched,
            "raw_candidate_count": (
                res.raw_candidate_count
            ),
            "accepted_count": res.accepted_count,
            "rejection_diagnostics": (
                res.rejection_diagnostics
            ),
        })

    # --- 2. Resolver Section ---
    for fpath in res_files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        case_id = data["case_id"]
        if case_id in seen_case_ids:
            return ComponentEvalV2Result(
                execution_status=exec_status,
                completeness_status="INCOMPLETE_FIXTURE_SET",
                quality_gate_status="FAILED_QUALITY_GATE",
                summary_status="INCOMPLETE_FIXTURE_SET",
            )
        seen_case_ids.add(case_id)

        split = data.get("split", "DEV")
        if target_split and split != target_split:
            continue

        cands_raw = data["input_candidate_obligations"]
        expected_rec = data["expected_recommendation"]
        expected_ctrl = data.get("expected_controlling_id")

        candidates = [
            Obligation.model_validate(c) for c in cands_raw
        ]
        recs = resolver_agent.recommend_precedence(candidates)

        acc = res_data[split]
        acc["count"] += 1
        acc["sys_rec_denom"] += 1
        evaluated_count += 1

        if recs:
            rec_obj = recs[0]
            exec_path = "MODEL_BACKED"
            prov = rec_obj.provenance
            pred_rec = rec_obj.recommendation
            pred_ctrl = rec_obj.controlling_obligation_id

            acc["mb_rec_denom"] += 1
            if pred_rec == expected_rec:
                acc["mb_rec_correct"] += 1

            if expected_ctrl is not None:
                acc["mb_ctrl_denom"] += 1
                if pred_ctrl == expected_ctrl:
                    acc["mb_ctrl_correct"] += 1

            if expected_rec == "ABSTAIN":
                acc["mb_abs_denom"] += 1
                if pred_rec == "ABSTAIN":
                    acc["mb_abs_correct"] += 1
            elif expected_rec == "CONFLICT":
                acc["mb_conflict_denom"] += 1
                if pred_rec == "CONFLICT":
                    acc["mb_conflict_correct"] += 1
        else:
            exec_path = "DETERMINISTIC_SHORT_CIRCUIT"
            prov = None
            pred_rec = "ABSTAIN"
            pred_ctrl = None

            acc["det_rec_denom"] += 1
            if pred_rec == expected_rec:
                acc["det_rec_correct"] += 1

        if pred_rec == expected_rec:
            acc["sys_rec_correct"] += 1

        record_prov(
            prov,
            case_id=case_id,
            agent_role="resolver",
            execution_path=exec_path,
            expected_outcome=(
                f"rec={expected_rec}, ctrl={expected_ctrl}"
            ),
            actual_outcome=(
                f"rec={pred_rec}, ctrl={pred_ctrl}"
            ),
            prompt_version=PrecedenceResolverAgent.prompt_version,
        )

        per_case_results.append({
            "case_id": case_id,
            "component": "resolver",
            "split": split,
            "expected_recommendation": expected_rec,
            "actual_recommendation": pred_rec,
            "expected_controlling_id": expected_ctrl,
            "actual_controlling_id": pred_ctrl,
            "execution_path": exec_path,
            "recommendation_matched": pred_rec == expected_rec,
            "controlling_id_matched": (
                pred_ctrl == expected_ctrl
                if expected_ctrl is not None
                else True
            ),
        })

    # --- 3. Steward Section ---
    for fpath in stw_files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        case_id = data["case_id"]
        if case_id in seen_case_ids:
            return ComponentEvalV2Result(
                execution_status=exec_status,
                completeness_status="INCOMPLETE_FIXTURE_SET",
                quality_gate_status="FAILED_QUALITY_GATE",
                summary_status="INCOMPLETE_FIXTURE_SET",
            )
        seen_case_ids.add(case_id)

        split = data.get("split", "DEV")
        if target_split and split != target_split:
            continue

        issue = Issue.model_validate(data["input_issue"])
        obligation = Obligation.model_validate(
            data["input_obligation"]
        )
        manifest = CreditManifest.model_validate(
            data["input_manifest"]
        )
        expected_action = data["expected_action"]
        expected_op = data.get("expected_patch_operation")
        expected_val = data.get("expected_patch_value")

        proposal = steward_agent.explain_finding_and_propose_patch(
            issue, obligation, manifest,
        )
        actual_action = (
            "PROPOSE_PATCH"
            if proposal.patch_proposal is not None
            else "DETERMINISTIC_ABSTAIN"
        )
        exec_path = (
            "MODEL_BACKED"
            if proposal.provenance is not None
            else "DETERMINISTIC_SHORT_CIRCUIT"
        )

        acc = stw_data[split]
        acc["count"] += 1
        acc["act_denom"] += 1
        evaluated_count += 1

        if actual_action == expected_action:
            acc["act_correct"] += 1

        patch_passed: bool | None = None
        rej_reason: str | None = None
        actual_op: str | None = None
        actual_val: str | None = None

        if (
            actual_action == "PROPOSE_PATCH"
            and proposal.patch_proposal is not None
        ):
            actual_op = proposal.patch_proposal.operation.value
            actual_val = _steward_actual_value(
                proposal.patch_proposal,
            )

            # Operation accuracy
            if expected_op is not None:
                acc["op_denom"] += 1
                if actual_op == expected_op:
                    acc["op_correct"] += 1

            # Value accuracy
            if expected_val is not None:
                acc["val_denom"] += 1
                if actual_val == expected_val:
                    acc["val_correct"] += 1

            # Patch validation
            acc["patch_denom"] += 1
            validation = validate_patch(
                proposal.patch_proposal, obligation, manifest,
            )
            patch_passed = validation.valid
            rej_reason = validation.rejection_reason
            if patch_passed:
                acc["patch_pass"] += 1
        else:
            from creditlock.domain.models import ObligationStatus
            if obligation.status != ObligationStatus.ACTIVE:
                rej_reason = (
                    f"Obligation status '{obligation.status.value}' is not "
                    f"ACTIVE; steward deterministic abstain."
                )
            else:
                rej_reason = (
                    proposal.validation_reason
                    or f"Steward deterministic abstain: {proposal.explanation}"
                )

        record_prov(
            proposal.provenance,
            case_id=case_id,
            agent_role="steward",
            execution_path=exec_path,
            expected_outcome=(
                f"action={expected_action}, "
                f"op={expected_op}, val={expected_val}"
            ),
            actual_outcome=(
                f"action={actual_action}, op={actual_op}"
            ),
            prompt_version=StewardAgent.prompt_version,
        )

        per_case_results.append({
            "case_id": case_id,
            "component": "steward",
            "split": split,
            "expected_action": expected_action,
            "actual_action": actual_action,
            "expected_operation": expected_op,
            "expected_value": expected_val,
            "actual_operation": actual_op,
            "actual_value": actual_val,
            "execution_path": exec_path,
            "validate_patch_passed": patch_passed,
            "rejection_reason": rej_reason,
        })

    # --- Compute Helper ---
    def calc_ext_metrics(
        acc: dict[str, Any],
    ) -> ExtractorMetrics:
        tp, fp, fn = acc["tp"], acc["fp"], acc["fn"]
        prec = tp / (tp + fp) if (tp + fp) > 0 else None
        rec = tp / (tp + fn) if (tp + fn) > 0 else None
        f1 = (
            (2 * prec * rec) / (prec + rec)
            if (
                prec is not None
                and rec is not None
                and (prec + rec) > 0
            )
            else None
        )

        valid_rate = (
            acc["valid_spans"] / acc["valid_denom"]
            if acc["valid_denom"] > 0 else None
        )
        gold_acc = (
            acc["gold_spans"] / acc["gold_denom"]
            if acc["gold_denom"] > 0 else None
        )

        return ExtractorMetrics(
            total_cases=acc["count"],
            field_precision=prec,
            field_recall=rec,
            field_f1=f1,
            source_span_validity_rate=valid_rate,
            span_validity_denominator=acc["valid_denom"],
            exact_gold_source_span_accuracy=gold_acc,
            exact_gold_span_denominator=acc["gold_denom"],
            non_binding_false_positive_count=acc["non_binding_fp"],
            unsupported_invented_field_count=acc["invented"],
        )

    def calc_res_metrics(
        acc: dict[str, Any],
    ) -> ResolverMetrics:
        def _safe_div(n: int, d: int) -> float | None:
            return n / d if d > 0 else None

        return ResolverMetrics(
            total_cases=acc["count"],
            system_recommendation_accuracy=_safe_div(
                acc["sys_rec_correct"], acc["sys_rec_denom"],
            ),
            system_rec_denominator=acc["sys_rec_denom"],
            model_backed_recommendation_accuracy=_safe_div(
                acc["mb_rec_correct"], acc["mb_rec_denom"],
            ),
            model_backed_rec_denominator=acc["mb_rec_denom"],
            deterministic_short_circuit_accuracy=_safe_div(
                acc["det_rec_correct"], acc["det_rec_denom"],
            ),
            deterministic_short_circuit_denominator=(
                acc["det_rec_denom"]
            ),
            model_backed_controlling_id_accuracy=_safe_div(
                acc["mb_ctrl_correct"], acc["mb_ctrl_denom"],
            ),
            model_backed_ctrl_id_denominator=acc["mb_ctrl_denom"],
            model_backed_abstention_accuracy=_safe_div(
                acc["mb_abs_correct"], acc["mb_abs_denom"],
            ),
            model_backed_abstention_denominator=(
                acc["mb_abs_denom"]
            ),
            model_backed_conflict_accuracy=_safe_div(
                acc["mb_conflict_correct"],
                acc["mb_conflict_denom"],
            ),
            model_backed_conflict_denominator=(
                acc["mb_conflict_denom"]
            ),
        )

    def calc_stw_metrics(
        acc: dict[str, Any],
    ) -> StewardMetrics:
        def _safe_div(n: int, d: int) -> float | None:
            return n / d if d > 0 else None

        return StewardMetrics(
            total_cases=acc["count"],
            expected_action_accuracy=_safe_div(
                acc["act_correct"], acc["act_denom"],
            ),
            action_denominator=acc["act_denom"],
            patch_operation_accuracy=_safe_div(
                acc["op_correct"], acc["op_denom"],
            ),
            patch_operation_denominator=acc["op_denom"],
            patch_value_accuracy=_safe_div(
                acc["val_correct"], acc["val_denom"],
            ),
            patch_value_denominator=acc["val_denom"],
            validate_patch_pass_rate=_safe_div(
                acc["patch_pass"], acc["patch_denom"],
            ),
            patch_validation_denominator=acc["patch_denom"],
        )

    # Per-split metrics
    dev_ext = calc_ext_metrics(ext_data["DEV"])
    dev_res = calc_res_metrics(res_data["DEV"])
    dev_stw = calc_stw_metrics(stw_data["DEV"])

    reg_ext = calc_ext_metrics(ext_data["RECORDED_REGRESSION"])
    reg_res = calc_res_metrics(res_data["RECORDED_REGRESSION"])
    reg_stw = calc_stw_metrics(stw_data["RECORDED_REGRESSION"])

    cv1_ext = calc_ext_metrics(
        ext_data["RECORDED_CHALLENGE_V1"],
    )
    cv1_res = calc_res_metrics(
        res_data["RECORDED_CHALLENGE_V1"],
    )
    cv1_stw = calc_stw_metrics(
        stw_data["RECORDED_CHALLENGE_V1"],
    )

    fv2_ext = calc_ext_metrics(
        ext_data["FROZEN_CHALLENGE_V2"],
    )
    fv2_res = calc_res_metrics(
        res_data["FROZEN_CHALLENGE_V2"],
    )
    fv2_stw = calc_stw_metrics(
        stw_data["FROZEN_CHALLENGE_V2"],
    )

    # Combined metrics (exclude FROZEN_CHALLENGE_V2 from
    # combined to keep challenge independent)
    non_frozen_splits = [
        "DEV", "RECORDED_REGRESSION", "RECORDED_CHALLENGE_V1",
    ]
    comb_ext_acc = {
        k: sum(ext_data[sp][k] for sp in non_frozen_splits)
        for k in ext_data["DEV"]
    }
    comb_res_acc = {
        k: sum(res_data[sp][k] for sp in non_frozen_splits)
        for k in res_data["DEV"]
    }
    comb_stw_acc = {
        k: sum(stw_data[sp][k] for sp in non_frozen_splits)
        for k in stw_data["DEV"]
    }

    comb_ext = calc_ext_metrics(comb_ext_acc)
    comb_res = calc_res_metrics(comb_res_acc)
    comb_stw = calc_stw_metrics(comb_stw_acc)

    # Quality gate: use target_split metrics if specified,
    # otherwise combined
    if target_split == "FROZEN_CHALLENGE_V2":
        gate_ext = fv2_ext
        gate_res = fv2_res
        gate_stw = fv2_stw
    elif target_split:
        gate_ext = calc_ext_metrics(ext_data[target_split])
        gate_res = calc_res_metrics(res_data[target_split])
        gate_stw = calc_stw_metrics(stw_data[target_split])
    else:
        gate_ext = comb_ext
        gate_res = comb_res
        gate_stw = comb_stw

    gate_passed = True

    # Extractor gate
    if (
        gate_ext.field_f1 is None
        or gate_ext.field_f1 < 0.80
    ):
        gate_passed = False
    if (
        gate_ext.exact_gold_source_span_accuracy is None
        or gate_ext.exact_gold_source_span_accuracy < 1.0
    ):
        gate_passed = False
    if (
        gate_ext.source_span_validity_rate is None
        or gate_ext.source_span_validity_rate < 1.0
    ):
        gate_passed = False
    if gate_ext.non_binding_false_positive_count > 0:
        gate_passed = False
    if (
        gate_ext.unsupported_invented_field_count is not None
        and gate_ext.unsupported_invented_field_count > 0
    ):
        gate_passed = False

    # Resolver gate
    if (
        gate_res.system_rec_denominator > 0
        and (
            gate_res.system_recommendation_accuracy is None
            or gate_res.system_recommendation_accuracy < 1.0
        )
    ):
        gate_passed = False
    if (
        gate_res.model_backed_abstention_denominator > 0
        and (
            gate_res.model_backed_abstention_accuracy is None
            or gate_res.model_backed_abstention_accuracy < 1.0
        )
    ):
        gate_passed = False
    if (
        gate_res.model_backed_conflict_denominator > 0
        and (
            gate_res.model_backed_conflict_accuracy is None
            or gate_res.model_backed_conflict_accuracy < 1.0
        )
    ):
        gate_passed = False

    # Steward gate: all four must be 1.0
    if (
        gate_stw.action_denominator == 0
        or gate_stw.expected_action_accuracy is None
        or gate_stw.expected_action_accuracy < 1.0
    ):
        gate_passed = False
    if (
        gate_stw.patch_operation_denominator > 0
        and (
            gate_stw.patch_operation_accuracy is None
            or gate_stw.patch_operation_accuracy < 1.0
        )
    ):
        gate_passed = False
    if (
        gate_stw.patch_value_denominator > 0
        and (
            gate_stw.patch_value_accuracy is None
            or gate_stw.patch_value_accuracy < 1.0
        )
    ):
        gate_passed = False
    if (
        gate_stw.patch_validation_denominator > 0
        and (
            gate_stw.validate_patch_pass_rate is None
            or gate_stw.validate_patch_pass_rate < 1.0
        )
    ):
        gate_passed = False

    qg_status: Literal[
        "PASSED_QUALITY_GATE", "FAILED_QUALITY_GATE"
    ] = (
        "PASSED_QUALITY_GATE" if gate_passed
        else "FAILED_QUALITY_GATE"
    )

    # Compute reproducibility metadata
    full_corpus_digest = compute_split_fixture_digest(fixtures_dir, split=None)
    eval_digest = compute_split_fixture_digest(fixtures_dir, split=target_split)
    git_sha = compute_git_commit_sha()
    prompt_hashes = get_agent_prompt_template_hashes()
    settings = get_settings()

    primaries = {
        "extractor": settings.gemini_extractor_model,
        "resolver": settings.gemini_resolver_model,
        "steward": settings.gemini_steward_model,
    }
    fallbacks = {
        "extractor": settings.gemini_extractor_fallback_model,
        "resolver": settings.gemini_resolver_fallback_model,
        "steward": settings.gemini_steward_fallback_model,
    }

    cmd_mode: Literal["LIVE_GEMINI", "OFFLINE_FAKE"] = (
        "LIVE_GEMINI"
        if exec_status == "COMPLETED_LIVE_GEMINI"
        else "OFFLINE_FAKE"
    )

    has_fv2 = fv2_ext.total_cases > 0
    ev_class: Literal[
        "TUNED_RECORDED_REGRESSION",
        "RECORDED_CHALLENGE_V1_FAILED",
        "FROZEN_CHALLENGE_V2",
    ]
    if target_split == "FROZEN_CHALLENGE_V2" or has_fv2:
        ev_class = "FROZEN_CHALLENGE_V2"
    else:
        ev_class = "TUNED_RECORDED_REGRESSION"

    return ComponentEvalV2Result(
        execution_status=exec_status,
        completeness_status="COMPLETE_ALL_CASES",
        quality_gate_status=qg_status,
        summary_status=qg_status,
        evidence_class=ev_class,
        command_mode=cmd_mode,
        generated_at_utc=datetime.now(UTC).isoformat(),
        git_commit_sha=git_sha,
        agent_code_commit_sha=(agent_code_commit_sha or git_sha),
        fixture_definition_commit_sha=git_sha,
        fixture_set_digest=eval_digest,
        evaluated_fixture_set_digest=eval_digest,
        full_fixture_corpus_digest=full_corpus_digest,
        agent_prompt_template_hashes=prompt_hashes,
        configured_primary_models=primaries,
        configured_fallback_models=fallbacks,
        split_disclaimer=(
            "FROZEN_CHALLENGE_V2 results recorded from a "
            "single frozen live evaluation run."
            if ev_class == "FROZEN_CHALLENGE_V2"
            else "RECORDED_REGRESSION has been inspected "
            "and directly used for prompt tuning."
        ),
        dev_metrics={
            "extractor": dev_ext.model_dump(),
            "resolver": dev_res.model_dump(),
            "steward": dev_stw.model_dump(),
        },
        recorded_regression_metrics={
            "extractor": reg_ext.model_dump(),
            "resolver": reg_res.model_dump(),
            "steward": reg_stw.model_dump(),
        },
        recorded_challenge_v1_metrics=(
            {
                "extractor": cv1_ext.model_dump(),
                "resolver": cv1_res.model_dump(),
                "steward": cv1_stw.model_dump(),
            }
            if cv1_ext.total_cases > 0
            else None
        ),
        frozen_challenge_v2_metrics=(
            {
                "extractor": fv2_ext.model_dump(),
                "resolver": fv2_res.model_dump(),
                "steward": fv2_stw.model_dump(),
            }
            if has_fv2
            else None
        ),
        combined_metrics={
            "extractor": comb_ext.model_dump(),
            "resolver": comb_res.model_dump(),
            "steward": comb_stw.model_dump(),
        },
        agent_execution_counts=agent_execution_counts,
        model_backed_invocation_counts=(
            model_backed_invocation_counts
        ),
        fallback_counts_by_agent=fallback_counts_by_agent,
        attempted_model_calls_by_model=attempted_calls,
        successful_model_calls_by_model=successful_calls,
        per_case_results=per_case_results,
        per_case_sanitized_provenance=(
            per_case_sanitized_provenance
        ),
    )
