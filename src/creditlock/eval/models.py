"""
Evaluation report and metric data models for CreditLock benchmark runner.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DeterministicEvalResult(BaseModel):
    model_config = {"extra": "forbid"}

    total_cases: int
    dev_cases: int
    sealed_cases: int
    summary_status: str
    gate_accuracy: float | None
    exact_issue_code_set_accuracy: float | None
    false_clear_count: int | None
    false_clear_rate: float | None
    false_block_count: int | None
    false_block_rate: float | None
    total_negative_cases: int
    total_positive_cases: int
    confusion_matrix: dict[str, int]
    replay_match_rate: float | None
    case_results: list[dict[str, Any]]


class AgentEvalResult(BaseModel):
    model_config = {"extra": "forbid"}

    execution_status: str = "UNVERIFIED_NO_LIVE_PROVIDER"
    completeness_status: str = "INCOMPLETE"
    quality_gate_status: str = "UNVERIFIED"
    summary_status: str = "UNVERIFIED_NO_LIVE_PROVIDER"
    total_cases: int
    provider_backend: str | None = None
    configured_model_id: str | None = None
    evaluated_cases: int | None = None
    excluded_cases: int | None = None
    excluded_reasons: list[str] = Field(default_factory=list)
    obligation_field_precision: float | None = None
    obligation_field_recall: float | None = None
    obligation_field_f1: float | None = None
    exact_source_span_accuracy: float | None = None
    span_denominator: int | None = None
    unsupported_invented_field_count: int | None = None
    abstention_accuracy: float | None = None
    abstention_denominator: int | None = None
    precedence_recommendation_accuracy: float | None = None
    precedence_denominator: int | None = None
    avg_model_calls: float | None = None
    avg_elapsed_time_ms: float | None = None
    attempted_model_calls_by_model: dict[str, int] = Field(default_factory=dict)
    successful_model_calls_by_model: dict[str, int] = Field(default_factory=dict)
    agent_invocation_counts: dict[str, int] = Field(default_factory=dict)
    fallback_counts_by_agent: dict[str, int] = Field(default_factory=dict)
    model_output_validation_failure_count: int = 0
    excluded_invalid_fixture_count: int = 0
    case_results: list[dict[str, Any]] = Field(default_factory=list)


class BenchmarkReport(BaseModel):
    model_config = {"extra": "forbid"}

    timestamp: str
    deterministic_eval: DeterministicEvalResult
    agent_eval: AgentEvalResult
    passed_negative_controls: bool
    summary_headline: str

    def to_markdown(self) -> str:
        """Generate human-readable Markdown report directly from computed result object."""
        d = self.deterministic_eval
        a = self.agent_eval
        md = []
        md.append("# CreditLock Benchmark Evaluation Report")
        md.append(f"**Timestamp:** {self.timestamp}")
        md.append(f"**Headline:** {self.summary_headline}")
        md.append(f"**Negative Controls Status:** {'PASSED' if self.passed_negative_controls else 'FAILED'}\n")

        md.append("## 1. Deterministic Compliance Benchmark")
        md.append(f"- **Summary Status:** {d.summary_status}")
        md.append(f"- **Total Cases:** {d.total_cases} (Dev: {d.dev_cases}, Sealed: {d.sealed_cases})")

        gate_acc_str = f"{d.gate_accuracy * 100:.2f}%" if d.gate_accuracy is not None else "None"
        md.append(f"- **Gate Accuracy:** {gate_acc_str}")

        issue_acc_str = f"{d.exact_issue_code_set_accuracy * 100:.2f}%" if d.exact_issue_code_set_accuracy is not None else "None"
        md.append(f"- **Issue-Code-Set Accuracy:** {issue_acc_str}")

        fc_count_str = str(d.false_clear_count) if d.false_clear_count is not None else "None"
        fc_rate_str = f"{d.false_clear_rate * 100:.2f}%" if d.false_clear_rate is not None else "None"
        md.append(f"- **False-Clear Count / Rate:** {fc_count_str} / {fc_rate_str} (Denominator: {d.total_negative_cases})")

        fb_count_str = str(d.false_block_count) if d.false_block_count is not None else "None"
        fb_rate_str = f"{d.false_block_rate * 100:.2f}%" if d.false_block_rate is not None else "None"
        md.append(f"- **False-Block Count / Rate:** {fb_count_str} / {fb_rate_str} (Denominator: {d.total_positive_cases})")

        replay_str = f"{d.replay_match_rate * 100:.2f}%" if d.replay_match_rate is not None else "None"
        md.append(f"- **Replay MATCH Rate:** {replay_str}\n")

        md.append("### Confusion Matrix")
        md.append("| Expected -> Actual | Count |")
        md.append("| :--- | :--- |")
        for pair, count in sorted(d.confusion_matrix.items()):
            md.append(f"| `{pair}` | {count} |")
        md.append("")

        md.append("## 2. AI Extraction and Precedence Benchmark")
        md.append(f"- **Execution Status:** {a.execution_status}")
        md.append(f"- **Completeness Status:** {a.completeness_status}")
        md.append(f"- **Quality Gate Status:** {a.quality_gate_status}")
        md.append(f"- **Summary Status:** {a.summary_status}")
        md.append(f"- **Total Cases:** {a.total_cases}")
        if a.evaluated_cases is not None:
            md.append(f"- **Evaluated Cases:** {a.evaluated_cases}")
            md.append(f"- **Excluded Cases:** {a.excluded_cases} ({', '.join(a.excluded_reasons)})")
        if a.provider_backend is not None:
            md.append(f"- **Provider Backend:** {a.provider_backend}")
            md.append(f"- **Configured Model ID:** {a.configured_model_id}")

        def fmt_pct(v: float | None, denom: int | None = None) -> str:
            if v is None:
                return f"N/A (Denominator: {denom if denom is not None else 0})"
            if denom is not None:
                return f"{v * 100:.2f}% (Denominator: {denom})"
            return f"{v * 100:.2f}%"

        md.append(f"- **Field Precision:** {fmt_pct(a.obligation_field_precision)}")
        md.append(f"- **Field Recall:** {fmt_pct(a.obligation_field_recall)}")
        md.append(f"- **Field F1 Score:** {fmt_pct(a.obligation_field_f1)}")
        md.append(f"- **Exact Source Span Accuracy:** {fmt_pct(a.exact_source_span_accuracy, a.span_denominator)}")
        inv_count = str(a.unsupported_invented_field_count) if a.unsupported_invented_field_count is not None else "N/A"
        md.append(f"- **Unsupported / Invented Fields:** {inv_count}")
        md.append(f"- **Abstention Accuracy:** {fmt_pct(a.abstention_accuracy, a.abstention_denominator)}")
        md.append(f"- **Precedence Recommendation Accuracy:** {fmt_pct(a.precedence_recommendation_accuracy, a.precedence_denominator)}")

        avg_calls = f"{a.avg_model_calls:.2f}" if a.avg_model_calls is not None else "N/A"
        avg_time = f"{a.avg_elapsed_time_ms:.2f}" if a.avg_elapsed_time_ms is not None else "N/A"
        md.append(f"- **Avg Model Calls per Document:** {avg_calls}")
        md.append(f"- **Avg Elapsed Time per Document:** {avg_time} ms\n")

        return "\n".join(md)
