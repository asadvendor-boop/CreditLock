# Findings and Architectural Log

Record architectural decisions, unexpected constraints, and key discoveries as development progresses.

---

## 2026-08-04 — Phase B: Trust and Evidence Chain Architecture

- **12 Release Root Bindings:** `ReleaseEvidence` was expanded to bind all 12 release root items: `manifest_hash`, `obligation_registry_version_hash`, `render_profile_version`, `ordered_png_frame_hashes`, `layout_evidence_hash`, `visual_observations_hash`, `deterministic_findings_hash`, `identity_bindings`, `authorizations`, `proposals`, `final_gate_state`, and `artifact_index_digest`.
- **ZIP Package Embedding:** Delivery ZIP packages embed all 11 required JSON files (`manifest.json`, `obligations.json`, `layout_evidence.json`, `visual_observations.json`, `contributor_registry.json`, `authorizations.json`, `proposals.json`, `artifact_index.json`, `deterministic_findings.json`, `release_evidence.json`, `bundle_index.json`) and ordered frame PNGs (`frames/*.png`).
- **Anti-Tamper Replay:** `replay_bundle()` requires a mandatory 64-character hex `--expected-release-digest`. If an attacker modifies `manifest.json` inside a ZIP and updates internal `bundle_index.json` hashes, the recomputed `release_evidence_digest` differs from `--expected-release-digest`, producing a `DIVERGENCE` result.

---

## 2026-08-04 — Phase C: Bounded Gemini Services Architecture (Option B)

- **Option B Adoption:** Removed unused `google-adk` dependency. Described the implementation as three bounded Gemini-backed services (`ExtractorAgent`, `PrecedenceResolverAgent`, `StewardAgent`).
- **`GoogleModelProvider` & `FakeModelProvider`:** `GoogleModelProvider` uses official `google.genai.Client` with `get_settings().gemini_model` and strict Pydantic models (`extra="forbid"`).
- **Fail-Closed Credential Security:** When `GEMINI_API_KEY` is not present, `GoogleModelProvider.is_available()` returns `False`. Attempts to invoke agents raise `ModelUnavailableError`, caught in `src/creditlock/agents/app.py` and returned as `HTTP 503 Service Unavailable`.
- **Extractor Fail-Closed Rules:** Extractor enforces exact `text_content[start:end] == quote` matching and strict enum validation (`CreditSurface`, `CardType`). Hallucinated or misaligned quotes are rejected.
- **Resolver Post-Validation:** Enforces `controlling_obligation_id` must belong to candidate IDs and cited explicit supersession keywords must be present in source text. Date differences alone convert recommendations to `ABSTAIN`.
- **Steward Post-Validation:** Compares LLM proposed operation and proposed value against exact deterministic derivations from controlling obligations.

---

## 2026-08-04 — Phase D: Honest Benchmark Evaluation Framework

- **Deterministic Compliance Benchmark:** Runner in `src/creditlock/eval/deterministic_runner.py` evaluates all 12 fixtures across `fixtures/benchmark/dev/` and `fixtures/benchmark/sealed/` exactly as stored without evidence synthesis. Reports metrics dynamically and exits successfully for incomplete evidence.
- **AI Extraction & Precedence Benchmark:** Runner in `src/creditlock/eval/agent_runner.py` evaluates synthetic dataset across `fixtures/agent_eval/dev/` and `fixtures/agent_eval/sealed/`. Offline runs emit `UNVERIFIED_NO_LIVE_PROVIDER` status with null metrics, avoiding fake prediction synthesis from gold fields. Live AI metrics require `--live-gemini`.
- **Evaluation CLI:** `scripts/run_evaluation.py` supports `--output-dir` and `--live-gemini` arguments, leaving git tracking clean.
- **Negative Controls:** Verified that forced false clears fail the run, fabricated extractions reduce precision, and date-only supersession failures reduce abstention accuracy.
