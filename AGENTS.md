# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Stack

Python 3.12+, FastAPI, Pydantic v2, pytest-asyncio, ruff, mypy (strict). Installed as an editable package via `pip install -e ".[dev]"`.

## Commands

```bash
# Run all tests
pytest -q

# Run a single test file
pytest tests/unit/test_checker.py -v

# Run a single test by name
pytest tests/unit/test_checker.py::test_function_name -v

# Lint
ruff check .

# Type-check
mypy src/

# Start API (dev)
uvicorn creditlock.api.app:app --reload
```

`asyncio_mode = "auto"` is set in `pyproject.toml` — async tests do not need `@pytest.mark.asyncio`.

## Code Style

- `from __future__ import annotations` at the top of every module.
- Line length 100 (ruff). `mypy --strict` must pass.
- All Pydantic models use `model_config = {"extra": "forbid"}`.
- String comparisons on credit fields must be NFC-normalized via `unicodedata.normalize("NFC", s)`.
- `sha256_digest()` in `src/creditlock/domain/canonical.py` is the canonical hash function — always use it for content-addressed fields (never `hashlib` directly on structured data).
- Issue IDs are deterministic (`_stable_issue_id` in `checker.py`): `SHA-256(obligation_id:code:discriminator)[:32]`. Stability is required so authorizations survive re-evaluations.

## Critical Architecture Rules

- **No AI, no I/O in domain layer.** `checker.py`, `gate.py`, `patches.py` are pure deterministic functions. Keep them that way.
- **`evaluate_findings` never skips silently.** Every obligation that is not WAIVED or SUPERSEDED must produce at least one issue or pass cleanly — see lifecycle mapping in `checker.py` module docstring.
- **`agent_reported_confidence` is routing metadata only.** It cannot activate, clear, or waive any obligation — enforced by convention, not code.
- **`aggregate_version` is production-wide**, not per event type. A single monotonically increasing sequence across all event types for a given `production_id`.
- **`event_id` is the idempotency key.** Never regenerate a UUID on retry; the projector silently drops duplicate `event_id` values.
- **`resolution.recorded` is immutable evidence only.** It does NOT clear any open issue and does NOT change the gate. Issue clearing requires separate downstream workflows.
- **`artifact.rendered` is Stage 1 only.** It records the digest but does NOT remove `ARTIFACT_PENDING` and does NOT produce `READY_TO_EXPORT`.
- **Authorization hash-binding:** an `Authorization` is only valid if all four hashes (`manifest_hash`, `obligation_registry_version_hash`, `artifact_index_digest`, `visual_observations_hash`) match current state. Stale authorizations are silently ignored.
- **`CONFLICTING_OBLIGATION` and `UNSUPPORTED_PRESENTATION_ASSERTION` are never clearable by authorization.** Only `VISUAL_OBSERVATION_UNCERTAIN` can be cleared via `CONFIRM_VISUAL`.
- **`GATE_PRECEDENCE` order** (index 0 = highest): `DEGRADED > BLOCKED > NEEDS_HUMAN > NEEDS_CONFIRMATION > STALE > READY_TO_EXPORT`.

## Testing Patterns

- Integration tests use `FastAPI TestClient` (sync), not `httpx.AsyncClient`.
- `register_production()` and `clear_productions()` in `src/creditlock/api/export.py` seed and reset state for integration tests via the active `ProductionStore` (injected with `set_production_store()`).
- Fixture JSON files live under `fixtures/benchmark/dev/`, `fixtures/benchmark/sealed/`, and `fixtures/demo/`.
- `InMemoryTransport` in `src/creditlock/events/transport.py` is the test double for Kafka; use `inject_out_of_order()` to simulate partition reordering.
- `ConfluentTransport` in `src/creditlock/events/transport.py` is a fully implemented production Kafka transport; it requires `confluent-kafka` and live Confluent Cloud credentials.

## Settings

Loaded from `.env` via `pydantic-settings`. Unit/integration tests run without cloud credentials. Cloud-backed features (Kafka, Gemini, Firestore, GCS) require the corresponding env vars. Call `get_settings()` (cached with `@lru_cache`) — never instantiate `Settings()` directly in application code.
