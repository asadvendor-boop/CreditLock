# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Non-Obvious Documentation Context

- `src/creditlock/domain/checker.py` module docstring is the authoritative specification for identity resolution rules (3 rules), lifecycle mapping, size-check formula, and issue_id stability contract. Read it before answering any question about checker behaviour.
- `src/creditlock/events/projection.py` module docstring contains the full shape of the projection dict (the in-memory Firestore mirror) and the processing order contract for `apply()`. This is the closest thing to a schema doc for event-sourced state.
- `src/creditlock/events/models.py` module docstring contains the `aggregate_version` contract and the canonical EventType → payload class table. These are not in the README.
- The `fixtures/benchmark/` directory contains dev and sealed JSON test cases that encode the expected checker behaviour. `dev_001_clean.json` through `dev_008_*.json` cover each issue code; `sealed_*` are locked regression fixtures.
- `src/creditlock/api/auth.py` is explicitly demo-grade auth (comment in file). Do not recommend hardening it without noting this context.
- `src/creditlock/agents/` contains ExtractorAgent, PrecedenceResolverAgent, StewardAgent, and the FastAPI agent-route scaffold in `app.py` (not an ADK runner). Only ExtractorAgent runs in the hosted demo flow; Resolver and Steward are verified locally.
- `src/creditlock/renderer/` contains Chromium card frame renderer (`render.py`), strict PNG binary auditor (`audit.py`), and renderer service (`service.py`). Chromium rendering is operational in the hosted Cloud Run environment.
- `src/creditlock/evidence/` contains content-addressed storage (`storage.py`), release evidence binding (`release.py`), delivery package assembler (`delivery.py`), and offline replay engine (`replay.py`). Offline replay performs full deterministic hash verification and returns `MATCH` on unmodified bundles (HOSTED_VERIFIED).
- Confluent Cloud produce/consume (raw JSON) was recorded live on partition 0. Schema Registry, poison-message recovery, key validation, and full streaming ingestion are outside the verified scope.
