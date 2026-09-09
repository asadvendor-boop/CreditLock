# Verification and Evaluation Requirements

This document outlines the requirements for evaluation and verification of CreditLock.

---

## 1. Local & Unit Verification Requirements

- **Pure Deterministic Engine:** `checker.py`, `gate.py`, `patches.py` require that the complete test suite exits successfully without AI or I/O.
- **PNG Binary Auditor:** `audit.py` verifies magic PNG headers, chunk CRC32 checksums, IEND chunk termination, exact dimensions, and extra trailing bytes.
- **Offline Anti-Tamper Replay Engine:** `replay_bundle()` verifies package integrity, release evidence hash-binding, mandatory external `--expected-release-digest`, re-evaluates findings, and verifies gate state fold without network, credentials, or AI models. Verified to detect tampered-and-resealed ZIP packages (`VERIFIED_LOCAL`).
- **Real Bounded Agent Boundary:** `GoogleModelProvider` uses `google.genai` SDK with Pydantic `extra="forbid"` structured output validation. Fails closed with `HTTP 503` when credentials are absent (`ModelUnavailableError`).

---

## 2. Benchmark Evaluation Framework (`VERIFIED_LOCAL`)

The evaluation runner (`scripts/run_evaluation.py`) executes two benchmark suites and 3 negative controls:

1. **Deterministic Compliance Benchmark:**
   - Evaluates 12 fixtures across `fixtures/benchmark/dev/` and `fixtures/benchmark/sealed/` exactly as stored without evidence synthesis.
   - Measures total cases, gate accuracy, exact issue-code-set accuracy, false-clear count/rate, false-block count/rate, per-gate confusion matrix, and replay match rate.

2. **AI Extraction & Precedence Benchmark:**
   - Evaluates synthetic dataset across `fixtures/agent_eval/dev/` and `fixtures/agent_eval/sealed/`.
   - Returns `UNVERIFIED_NO_LIVE_PROVIDER` when live credentials are absent. Measures obligation field precision/recall/F1, exact source-span accuracy, unsupported/invented field counts, abstention accuracy, precedence recommendation accuracy, avg model calls, and avg elapsed time when `--live-gemini` is passed.

3. **Verified Negative Controls:**
   - **Control 1:** Forced false clear causes false-clear evaluation failure.
   - **Control 2:** Fabricated extractions reduce precision below 1.0.
   - **Control 3:** Date-only supersession failure reduces abstention accuracy below 1.0.
