"""
Component-Level Evaluation V2 Models.

Independent evaluation across Extractor, Resolver, and Steward.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EXPECTED_EXTRACTOR_CATEGORIES = {
    "single-person positive",
    "two-person 'and' positive",
    "ampersand positive",
    "three-person list positive",
    "heading-separated binding clause positive",
    "repeated clause positive with exact unambiguous spans",
    "draft/non-binding negative",
    "conditional/future negative",
    "unfamiliar formatting positive",
    "unfamiliar role/names positive",
}

EXPECTED_RESOLVER_CATEGORIES = {
    "explicit supersession",
    "explicit amendment",
    "date/version-only difference with no supersession language",
    "substantive incompatible obligations",
    "genuinely unrelated grouping identities",
    "same party with no controlling proof and no substantive incompatibility",
}

EXPECTED_STEWARD_CATEGORIES = {
    "SUBSTITUTE_TEXT",
    "REORDER",
    "REGROUP",
    "REPOSITION",
    "deterministic abstention 1",
    "deterministic abstention 2",
    "deterministic abstention 3",
}

ALLOWED_MODELS = {
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
}


class FrozenChallengeManifest(BaseModel):
    model_config = {"extra": "forbid"}

    agent_code_commit_sha: str
    frozen_prompt_template_hashes: dict[str, str]
    configured_primary_models: dict[str, str]
    configured_fallback_models: dict[str, str]
    fixture_counts: dict[str, int]
    semantic_uniqueness_counts: dict[str, int]
    fixture_set_digest: str
    coverage_categories: dict[str, list[str]]

    @field_validator("fixture_set_digest")
    @classmethod
    def check_digest_hex(cls, v: str) -> str:
        if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError("fixture_set_digest must be 64-character hex")
        return v

    @model_validator(mode="after")
    def check_exact_keys_and_counts(self) -> FrozenChallengeManifest:
        agents = {"extractor", "resolver", "steward"}

        if set(self.frozen_prompt_template_hashes.keys()) != agents:
            raise ValueError(
                "frozen_prompt_template_hashes must have exactly extractor, resolver, steward"
            )
        for v in self.frozen_prompt_template_hashes.values():
            if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
                raise ValueError("prompt hashes must be 64-character hex")

        if set(self.configured_primary_models.keys()) != agents:
            raise ValueError(
                "configured_primary_models must have exactly extractor, resolver, steward"
            )
        for m in self.configured_primary_models.values():
            if m not in ALLOWED_MODELS:
                raise ValueError(
                    f"configured_primary_model '{m}' is not in allowed list: {ALLOWED_MODELS}"
                )

        if set(self.configured_fallback_models.keys()) != agents:
            raise ValueError(
                "configured_fallback_models must have exactly extractor, resolver, steward"
            )
        for m in self.configured_fallback_models.values():
            if m not in ALLOWED_MODELS:
                raise ValueError(
                    f"configured_fallback_model '{m}' is not in allowed list: {ALLOWED_MODELS}"
                )

        if (
            self.fixture_counts.get("extractor") != 10
            or self.fixture_counts.get("resolver") != 6
            or self.fixture_counts.get("steward") != 7
        ):
            raise ValueError("fixture_counts must be exactly 10/6/7")

        if (
            self.semantic_uniqueness_counts.get("extractor") != 10
            or self.semantic_uniqueness_counts.get("resolver") != 6
            or self.semantic_uniqueness_counts.get("steward") != 7
        ):
            raise ValueError("semantic_uniqueness_counts must be exactly 10/6/7")

        if set(self.coverage_categories.keys()) != agents:
            raise ValueError(
                "coverage_categories must have exactly extractor, resolver, steward"
            )

        ext_cats = self.coverage_categories["extractor"]
        if set(ext_cats) != EXPECTED_EXTRACTOR_CATEGORIES or len(ext_cats) != len(
            EXPECTED_EXTRACTOR_CATEGORIES
        ):
            raise ValueError(
                "coverage_categories['extractor'] set mismatch or contains duplicates"
            )

        res_cats = self.coverage_categories["resolver"]
        if set(res_cats) != EXPECTED_RESOLVER_CATEGORIES or len(res_cats) != len(
            EXPECTED_RESOLVER_CATEGORIES
        ):
            raise ValueError(
                "coverage_categories['resolver'] set mismatch or contains duplicates"
            )

        stw_cats = self.coverage_categories["steward"]
        if set(stw_cats) != EXPECTED_STEWARD_CATEGORIES or len(stw_cats) != len(
            EXPECTED_STEWARD_CATEGORIES
        ):
            raise ValueError(
                "coverage_categories['steward'] set mismatch or contains duplicates"
            )

        return self


class ExtractorMetrics(BaseModel):
    model_config = {"extra": "forbid"}

    total_cases: int = 0
    field_precision: float | None = None
    field_recall: float | None = None
    field_f1: float | None = None
    source_span_validity_rate: float | None = None
    span_validity_denominator: int = 0
    exact_gold_source_span_accuracy: float | None = None
    exact_gold_span_denominator: int = 0
    non_binding_false_positive_count: int = 0
    unsupported_invented_field_count: int | None = 0


class ResolverMetrics(BaseModel):
    model_config = {"extra": "forbid"}

    total_cases: int = 0
    system_recommendation_accuracy: float | None = None
    system_rec_denominator: int = 0
    model_backed_recommendation_accuracy: float | None = None
    model_backed_rec_denominator: int = 0
    deterministic_short_circuit_accuracy: float | None = None
    deterministic_short_circuit_denominator: int = 0
    model_backed_controlling_id_accuracy: float | None = None
    model_backed_ctrl_id_denominator: int = 0
    model_backed_abstention_accuracy: float | None = None
    model_backed_abstention_denominator: int = 0
    model_backed_conflict_accuracy: float | None = None
    model_backed_conflict_denominator: int = 0


class StewardMetrics(BaseModel):
    model_config = {"extra": "forbid"}

    total_cases: int = 0
    expected_action_accuracy: float | None = None
    action_denominator: int = 0
    patch_operation_accuracy: float | None = None
    patch_operation_denominator: int = 0
    patch_value_accuracy: float | None = None
    patch_value_denominator: int = 0
    validate_patch_pass_rate: float | None = None
    patch_validation_denominator: int = 0


SplitName = Literal[
    "DEV",
    "RECORDED_REGRESSION",
    "RECORDED_CHALLENGE_V1",
    "FROZEN_CHALLENGE_V2",
]

VALID_SPLITS: set[str] = {
    "DEV",
    "RECORDED_REGRESSION",
    "RECORDED_CHALLENGE_V1",
    "FROZEN_CHALLENGE_V2",
}


class PerCaseSanitizedProvenance(BaseModel):
    model_config = {"extra": "forbid"}

    case_id: str
    agent_role: str
    execution_path: Literal[
        "MODEL_BACKED", "DETERMINISTIC_SHORT_CIRCUIT"
    ]
    expected_outcome: str
    actual_outcome: str
    prompt_version: str = "v1"
    provenance: dict[str, Any] | None = None


EvidenceClass = Literal[
    "TUNED_RECORDED_REGRESSION",
    "RECORDED_CHALLENGE_V1_FAILED",
    "FROZEN_CHALLENGE_V2",
]


class ComponentEvalV2Result(BaseModel):
    model_config = {"extra": "forbid"}

    execution_status: Literal[
        "COMPLETED_LIVE_GEMINI",
        "COMPLETED_OFFLINE_FAKE",
        "FAILED_BEFORE_EXECUTION",
    ] = "COMPLETED_OFFLINE_FAKE"
    completeness_status: Literal[
        "COMPLETE_ALL_CASES",
        "INCOMPLETE_FIXTURE_SET",
        "INVALID_FIXTURE_ANNOTATIONS",
    ] = "INCOMPLETE_FIXTURE_SET"
    quality_gate_status: Literal[
        "PASSED_QUALITY_GATE", "FAILED_QUALITY_GATE"
    ] = "FAILED_QUALITY_GATE"
    summary_status: Literal[
        "PASSED_QUALITY_GATE",
        "FAILED_QUALITY_GATE",
        "INCOMPLETE_FIXTURE_SET",
        "INVALID_FIXTURE_ANNOTATIONS",
    ] = "INCOMPLETE_FIXTURE_SET"

    evidence_class: EvidenceClass = "TUNED_RECORDED_REGRESSION"
    command_mode: Literal["LIVE_GEMINI", "OFFLINE_FAKE"] = "OFFLINE_FAKE"

    generated_at_utc: str = ""
    git_commit_sha: str = ""
    agent_code_commit_sha: str = ""
    fixture_definition_commit_sha: str = ""
    fixture_set_digest: str = ""
    evaluated_fixture_set_digest: str | None = None
    full_fixture_corpus_digest: str | None = None
    agent_prompt_template_hashes: dict[str, str] = Field(
        default_factory=dict
    )
    configured_primary_models: dict[str, str] = Field(
        default_factory=dict
    )
    configured_fallback_models: dict[str, str] = Field(
        default_factory=dict
    )

    split_disclaimer: str = (
        "RECORDED_REGRESSION has been inspected and directly used for "
        "prompt tuning."
    )

    dev_metrics: dict[str, Any] = Field(default_factory=dict)
    recorded_regression_metrics: dict[str, Any] = Field(
        default_factory=dict
    )
    recorded_challenge_v1_metrics: dict[str, Any] | None = None
    frozen_challenge_v2_metrics: dict[str, Any] | None = None
    combined_metrics: dict[str, Any] = Field(default_factory=dict)

    agent_execution_counts: dict[str, int] = Field(
        default_factory=dict
    )
    model_backed_invocation_counts: dict[str, int] = Field(
        default_factory=dict
    )
    fallback_counts_by_agent: dict[str, int] = Field(
        default_factory=dict
    )
    attempted_model_calls_by_model: dict[str, int] = Field(
        default_factory=dict
    )
    successful_model_calls_by_model: dict[str, int] = Field(
        default_factory=dict
    )

    per_case_results: list[dict[str, Any]] = Field(
        default_factory=list
    )
    per_case_sanitized_provenance: list[PerCaseSanitizedProvenance] = (
        Field(default_factory=list)
    )
