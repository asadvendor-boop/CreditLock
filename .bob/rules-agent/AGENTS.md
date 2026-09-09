# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Non-Obvious Coding Rules

- Use `sha256_digest(value)` from `src/creditlock/domain/canonical.py` for all structured-data hashing. It applies NFC-normalization and sorts dict keys recursively before serializing. Never call `hashlib.sha256` directly on Pydantic `.model_dump()` output — key ordering is not guaranteed without this function.
- `_stable_issue_id(obligation_id, code, discriminator)` in `checker.py` generates deterministic 32-hex-char IDs. When adding a new issue code that can fire multiple times for one obligation, always pass a distinct `discriminator` string (e.g., field name) to avoid ID collision.
- All string equality checks in the domain layer must use `_nfc()` (NFC normalization). Raw string comparison on `display_name`, `role_label`, `required_display_text` will produce false mismatches on composed/decomposed Unicode.
- `evaluate_findings()` must never silently skip non-waived, non-superseded obligations. Every code path for `CANDIDATE`, `NEEDS_CONFIRMATION`, `CONFLICTING`, and `PENDING_EXTERNAL_AUTHORITY` statuses must emit exactly one issue — see the lifecycle mapping in `checker.py` module docstring.
- `fold_gate()` uses list index in `GATE_PRECEDENCE` as sort key — when adding a new `GateState`, insert it at the correct precedence position in that list in `models.py`.
- Do not import `AbsolutePosition` at module level inside `patches.py` — it uses a deferred `from creditlock.domain.models import AbsolutePosition` inside the function body (pattern already established there; keep it consistent).
- `get_settings()` is `@lru_cache`-wrapped — never instantiate `Settings()` directly in application code. In tests, monkeypatch `creditlock.settings.get_settings` return value or use environment overrides.
- State is managed via the `ProductionStore` interface (`src/creditlock/domain/store.py`). In tests and integration code, use `register_production()` / `clear_productions()` from `src/creditlock/api/export.py` which delegate to the active store. Do not access a `_productions` dict directly — the store is injected via `set_production_store()`.
- `asyncio_mode = "auto"` in `pyproject.toml` — do not add `@pytest.mark.asyncio` decorators; they are redundant and generate warnings.
- Integration tests must call `clear_productions()` in an `autouse` fixture (before and after yield) — leaving state between tests causes false positives.
- `ConfluentTransport` in `src/creditlock/events/transport.py` is a fully implemented Kafka transport; it requires the `confluent-kafka` package and live Confluent Cloud credentials. Schema Registry, poison-message recovery, and full streaming ingestion are outside the verified scope.
- `RendererService` in `src/creditlock/renderer/service.py` orchestrates Chromium rendering, PNG auditing, and artifact index creation. Rendering passes in the hosted Cloud Run environment.
- Offline replay in `src/creditlock/evidence/replay.py` performs full deterministic hash verification and returns `MATCH` on unmodified bundles. Replay is HOSTED_VERIFIED.
