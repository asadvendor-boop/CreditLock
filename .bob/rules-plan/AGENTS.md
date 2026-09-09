# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Non-Obvious Architectural Constraints

- **Domain layer must remain pure.** `checker.py`, `gate.py`, `patches.py` have zero I/O and zero AI calls by design. Any feature that needs external data must pass it in via `CheckerInput` or equivalent input dataclass — never add I/O inside these functions.
- **The export endpoint recomputes the full gate on every call.** There is no cached gate token. Any plan that introduces caching must account for the four-hash staleness model (manifest, obligations, artifact, visual observations).
- **Authorization validity is hash-bound to all four inputs simultaneously.** An auth that matches on three of four hashes is silently ignored, not partially applied. New authorization types must follow this same all-or-nothing pattern.
- **`resolution.recorded` is append-only immutable evidence.** It cannot be used to clear issues. Plans that rely on recording a resolution to unblock export are architecturally incorrect — a separate overlay mechanism (like `_resolve_open_issues`) is required.
- **`aggregate_version` is production-wide, not per-event-type.** Any event-sourcing extension must allocate from the same sequence per `production_id`.
- **`InMemoryTransport` is the test/dev Kafka stand-in.** `ConfluentTransport` in `src/creditlock/events/transport.py` is the production transport implementing `EventTransport`. It requires `confluent-kafka` and live credentials. No domain code changes are needed to switch transports.
- **`agents/app.py` is a FastAPI agent-route scaffold**, not an ADK runner. Only ExtractorAgent runs in the hosted demo flow; Resolver and Steward are unit-tested locally.
- **Renderer and replay are operational.** `RendererService` runs Chromium in the hosted Cloud Run environment. Offline replay returns `MATCH` on unmodified bundles (HOSTED_VERIFIED). Confluent produce/consume was recorded live on partition 0; Schema Registry, poison-message recovery, and full streaming ingestion are outside the verified scope.
- **Gate precedence is immutable once established.** Adding a new `GateState` requires inserting it at the correct position in `GATE_PRECEDENCE` in `models.py` and adding it to `ISSUE_GATE_MAP` — both lists must be updated atomically.
- **All Pydantic models use `extra="forbid"`.** Any schema extension that adds optional fields to an existing model must also update all call sites that construct those models from raw dicts (e.g., fixture loaders) to avoid `ValidationError` at runtime.
