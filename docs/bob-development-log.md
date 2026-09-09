# Bob Development Log

This log records IBM Bob's contribution to the CreditLock project. Each entry documents what was built with Bob's assistance, the prompt used, and the outcome.

## Instructions

For each session: record the date, task, the key prompt(s) given to Bob, the code Bob generated or modified, and the outcome. Include screenshots of Bob sessions in `docs/assets/` where relevant.

---

## 2026-07-30 — Task 0: Repository scaffold, domain models, canonical serialization, fixtures

**Bob used for:**
- Designing the full `Obligation` Pydantic model with discriminated union for `CardPosition`, size field validation, and `extra="forbid"` enforcement
- Generating `canonical_json_bytes` and `sha256_digest` with NFC normalization
- Scaffolding the full pytest test suite for models and canonicalization
- Writing 8 development fixture JSONs and 4 sealed fixture JSONs
- Writing the demo production fixture with 3 deliberate issues

**Prompt summary:** Asked Bob to write the Obligation Pydantic v2 model with the specified fields and validation rules; to write canonical JSON serialization with NFC normalization, sorted dict keys, and preserved array order; and to write pytest tests covering all 14 issue codes, all 6 gate states, and all obligation lifecycle states.

**Outcome:** All 60 tests pass. Contracts frozen.

---

## 2026-07-30 — Task A: `obligee_text` field (commit e978bbc)

**What was built:**
Added the `obligee_text: str | None` field to the `Obligation` model in `src/creditlock/domain/models.py` only. This is the free-text name as it appears in a source contract clause — distinct from `credited_party_id` (a stable internal identifier). The field is used by checker rule 2 to resolve an obligation to a contributor when the agent extracted a name string rather than a stable ID.

No fixtures or tests were modified by this commit. The default of `None` preserved all existing behaviour; 168 tests passed unchanged.

**Bob used for:**
- Adding `obligee_text` field to `Obligation` in `src/creditlock/domain/models.py` with correct placement and docstring

**Prompt summary:** Asked Bob to add an `obligee_text` field to Obligation — optional string, free-text name from the source clause, used for name-to-party resolution — placed after `media_versions`.

**Commit:** e978bbc
**Outcome:** Field added; all 168 pre-existing tests remain green.

---

## 2026-07-30 — Task B: Identity classifier rules + issue-id discriminator (commit b689b2a)

**What was built:**
Implemented the three identity resolution rules in `src/creditlock/domain/checker.py` and fixed the issue-id stability contract.

**Rule 1** (`credited_party_id` set): exact manifest match only. Failure always produces `MISSING_CREDIT`. Registry membership and surface population are irrelevant. Absence is never ambiguity.

**Rule 2** (`credited_party_id` null, `obligee_text` set): two-stage resolution. Stage 1 resolves the text against the registry (NFC canonical\_name or alias string exact match) to collect all matching `contributor_id`s. 0 or ≥2 registry matches → `AMBIGUOUS_IDENTITY`. Exactly 1 → stage 2 checks manifest presence; absent → `MISSING_CREDIT`; present → bind and run field checks.

Note: the initial Rule 2 implementation in this commit filtered by manifest presence inside the registry scan, which turned a unique-registry / absent-from-manifest case into `AMBIGUOUS_IDENTITY`. This was corrected in commit 7b4e122.

**Rule 3** (`credited_party_id` null, `obligee_text` null): `UNCONFIRMED_OBLIGATION`. The old silent `continue` was removed.

**Issue-id discriminator:** `_stable_issue_id` now accepts a `discriminator` string so that two findings with the same `(obligation_id, IssueCode)` — e.g. `display_name` mismatch and `role` mismatch — produce distinct stable IDs.

No fixtures were modified by this commit.

**Bob used for:**
- Writing `_resolve_contributor` and `_resolve_obligee_text` as separate pure functions
- Implementing the two-stage rule-2 logic
- Removing the old `if credited_party_id is None: continue` silent pass
- Adding discriminator parameter to `_stable_issue_id`
- Writing the `TestIdentityRules` and `TestBackdoorApiGuard` test classes (16 new tests)

**Prompt summary:** Asked Bob to implement the three identity resolution rules in checker.py (rule 1 exact-match, rule 2 two-stage registry then manifest, rule 3 unconfirmed) deleting the old silent pass; and to add a discriminator to `_stable_issue_id` so display_name and role mismatches on the same obligation get distinct issue IDs.

**Commit:** b689b2a
**Outcome:** 184 tests green. Issue-id stability contract holds.

---

## 2026-07-30 — Rule-2 registry-filter fix + identity bindings (commit 7b4e122)

**What was built:**
Fixed the critical rule-2 bug where `_resolve_obligee_text` was filtering by manifest presence inside the registry scan. This made a case where `obligee_text` resolved to exactly one contributor who was absent from the manifest appear as 0 manifest matches → `AMBIGUOUS_IDENTITY` (incorrectly authorizable). The fix moves manifest presence to a dedicated stage-2 check in the caller so the registry-only count is always used for the 0/1/≥2 branch.

Also introduced in this commit:
- `selected_contributor_id` field on `Authorization` with a validator requiring it non-null/non-empty when `action == CONFIRM_IDENTITY`
- `_build_identity_bindings` in `src/creditlock/api/export.py`: extracts valid `CONFIRM_IDENTITY` authorizations, maps `issue_id → selected_contributor_id`, fails closed on conflict
- Preliminary checker run in `export_production` builds the `ambiguous_issue_id → obligation_id` map used to scope identity bindings
- `CheckerInput.identity_bindings` field so the final checker pass receives resolved bindings
- Reshaped dev_004, sealed_003, and the demo `obl_demo_003` fixture to use `credited_party_id=null` + `obligee_text` with a second registry entry producing ≥2 registry matches → `AMBIGUOUS_IDENTITY`; sealed SHA-256 hashes refrozen

**Bob used for:**
- Refactoring `_resolve_obligee_text` to return registry-only matches
- Adding stage-2 manifest presence check in `evaluate_findings`
- Adding `selected_contributor_id` to `Authorization` model with validator
- Implementing `_build_identity_bindings` and preliminary-run logic in `src/creditlock/api/export.py`
- Reshaping identity fixtures and updating test expectations
- Writing `TestIdentityAuthorization` and related test classes

**Prompt summary:** Asked Bob to fix `_resolve_obligee_text` by removing the manifest filter and returning all registry matches, with stage-2 added in `evaluate_findings`; and to add the `CONFIRM_IDENTITY` authorization path with `selected_contributor_id`, a preliminary checker run, the ambiguous_issue_id→obligation_id map, and conflict detection.

**Commit:** 7b4e122
**Outcome:** 197 tests green. Registry-filter backdoor closed.

---

## 2026-07-30 — Scope identity bindings to current AMBIGUOUS_IDENTITY issues (commit 0be2428)

**What was built:**
Restricted `CONFIRM_IDENTITY` authorization processing so that a binding is only applied when the preliminary checker pass confirms the targeted issue is currently classified as `AMBIGUOUS_IDENTITY`. Bindings targeting any other issue type are ignored. Added deduplication and whitespace-strip handling for `obligee_text` in the checker. Strengthened the `dev_002` fixture and added tests covering the new scoping behaviour.

This commit also introduced README.md, docs/bob-development-log.md, and docs/findings.md.

**Bob used for:**
- Restricting identity-binding application to current AMBIGUOUS_IDENTITY issues in `src/creditlock/api/export.py`
- Adding deduplication and whitespace handling for `obligee_text` in `src/creditlock/domain/checker.py`
- Strengthening `dev_002` fixture
- Writing tests covering the scoping restriction

**Prompt summary:** Asked Bob to restrict CONFIRM_IDENTITY bindings to obligations whose current checker output is AMBIGUOUS_IDENTITY, add obligee_text deduplication and whitespace stripping, strengthen dev_002, and produce a 202-test checkpoint.

**Commit:** 0be2428
**Outcome:** 202 tests green. Identity binding scope locked to current AMBIGUOUS_IDENTITY issues.

---

## 2026-07-31 — Task F: Confluent Cloud event transport + live round-trip (commit cba7227)

**IBM Bob task:** Connect CreditLock's existing event transport to the real Confluent Cloud Kafka cluster and prove one live event round trip.

**Files implemented:**
- `src/creditlock/events/transport.py` — added `ConfluentTransport` and `DeliveryMetadata` (InMemoryTransport unchanged)
- `tests/unit/test_confluent_transport.py` — 9 offline unit tests (no credentials required)
- `tests/integration/test_confluent_live.py` — credentials-gated live integration test (skip unless `CREDITLOCK_RUN_LIVE_CONFLUENT=1`)
- `scripts/confluent_smoke.py` — safe live round-trip verification script

**Implementation details:**
- `ConfluentTransport` uses SASL_SSL / PLAIN authentication, idempotent producer, `acks=all`, 30 s delivery timeout
- Producer: `production_id` as Kafka message key (UTF-8), Pydantic JSON serialization
- Consumer: `auto.offset.reset=earliest`, manual offset commits, bounded polling, malformed envelopes fail closed
- Credentials injected via `get_settings()` — never logged, printed, or raised
- `_producer_factory` / `_consumer_factory` injection points enable fully offline unit tests

**Test commands:**
```bash
# Offline unit tests only
.venv/bin/python -m pytest tests/unit/test_confluent_transport.py -q

# Full test suite (live test auto-skips)
.venv/bin/python -m pytest tests/ -q

# Ruff
.venv/bin/ruff check src/creditlock/events/transport.py tests/unit/test_confluent_transport.py tests/integration/test_confluent_live.py scripts/confluent_smoke.py

# mypy
.venv/bin/mypy src/creditlock/events/transport.py scripts/confluent_smoke.py

# Live round-trip (requires CREDITLOCK_RUN_LIVE_CONFLUENT=1 and credentials in .env)
CREDITLOCK_RUN_LIVE_CONFLUENT=1 .venv/bin/python scripts/confluent_smoke.py
CREDITLOCK_RUN_LIVE_CONFLUENT=1 .venv/bin/python -m pytest tests/integration/test_confluent_live.py -v -s
```

**Live round-trip result:**
- Cluster: Confluent Cloud, GCP us-central1
- Topic: `creditlock.production.events`
- Smoke script: `PASS  event_id=9f5fab94-4ce6-4b81-805a-70b4bd0fc32b  topic=creditlock.production.events  partition=0  offset=1`
- Integration test: `PASS  event_id=cde337ab-0a61-44fa-babb-32fa09b518a6  topic=creditlock.production.events  partition=0  offset=2`
- All 318 tests pass; 1 skipped (live test when gate not set)

**Remaining limitations:**
- `ConfluentTransport.consume()` uses `auto.offset.reset=earliest` with unique-per-run consumer groups. It does not seek to a specific offset and cannot replay from a known position — long-term event replay requires offset management outside the current `EventTransport` interface.
- librdkafka emits an informational telemetry warning (`Failed to acquire idempotence PID ... retrying`) on first connection; this is benign and does not affect delivery.

**Commit:** cba7227

---

## 2026-09-05 — Final IBM Bob Task: Hosted Judge Journey Verifier (commit 5f96844)

**IBM Bob task:** Add a small, honest, reusable verifier for CreditLock's customer-visible hosted judge journey that proves real public application behavior through live HTTP requests and offline replay.

**Files implemented:**
- `scripts/verify_judge_journey.py` — 15-step live HTTP verifier for the hosted judge journey
- `tests/unit/test_verify_judge_journey.py` — 31 focused offline unit tests for the verifier
- `docs/evidence/final-hosted-judge-journey.txt` — literal sanitized output of the successful real hosted run

**Verifier implementation:**
- Accepts `--base-url` with `CREDITLOCK_PUBLIC_URL` as an environment-variable alternative
- Uses existing hosted API contracts; no new endpoints added
- Makes real network requests against the hosted URL
- Never simulates HTTP responses or hardcodes PASS
- Exits 0 only if every mandatory step passes
- Exits nonzero on the first failed invariant while printing a sanitized failure report
- Never prints access tokens, JWTs, credentials, secrets, or complete Authorization headers
- Uses temporary storage for the downloaded ZIP
- Runs `scripts/replay.py` against the downloaded ZIP and the server-returned release digest
- Prints a concise numbered transcript with timestamp, URL, git commit, step results, model ID, GCS URI, release digest, and offline replay result

**Test coverage (31 tests, all pass):**
- Wrong HTTP status fails the verifier
- Ungrounded / incorrectly indexed Gemini quote fails
- Model outside the allowlist fails
- Missing release digest or non-GCS package URI fails
- Replay divergence fails
- Generated output never contains bearer tokens or JWT values

**Verifier commit:** 5f96844

**Hosted command:**
```bash
.venv/bin/python scripts/verify_judge_journey.py \
  --base-url https://creditlock-851586299411.us-central1.run.app
```

**Literal final hosted result (at time of implementation):**
- Status: PASS (15 numbered stages)
- Gemini model used: `gemini-3.6-flash`
- GCS delivery URI: `gs://creditlock-evidence-pak-uni-scraper-4157a70c/deliveries/prod_demo_21c89a7cbd1b/e8bae96635cd9503b7307a87613bac2cc65149415e792804f50adbf1444a8c31.zip`
- Release digest: `e8bae96635cd9503b7307a87613bac2cc65149415e792804f50adbf1444a8c31`
- Offline replay result: MATCH (VERIFIED_LOCAL against hosted package)
- Exit code: 0

**Confluent boundary:**
Confluent Cloud transport is separately RECORDED_LIVE through commit cba7227 and is not a runtime dependency of this hosted judge journey.

**Hosted-journey limitation:**
This verifies the current hosted CreditLock judge journey. It does not establish generic enterprise deployment readiness.

---

---

## 2026-09-05 — Candidate enrichment, safe DOM rendering, verifier correction (commit fcc5d4a)

**IBM Bob task:** Correct the AMBIGUOUS_IDENTITY candidate presentation so the reviewer's identity choice is meaningful and fail-closed; rewrite the rendering path to use safe DOM creation; add the verifier count corrections throughout documentation.

**Files implemented:**
- `src/creditlock/api/demo.py` — enriched `candidates` list with `display_name` (from manifest entry), `role`, `credit_surface`, `present_in_manifest` derived only from the existing obligation, contributor registry, and manifest
- `src/creditlock/static/index.html` — rewrote `_renderCandidates()` and `_clearCandidates()` to use `document.createElement` / `.textContent` / `.value` instead of unescaped `innerHTML` template interpolation; labels now render as e.g. "David Park — Associate Producer — End Cards — present in current manifest" vs "D. Park — Associate Producer — End Cards — no matching current manifest credit entry"
- `tests/unit/test_candidate_enrichment.py` — 16 new failing-first tests covering enriched candidate metadata, label distinguishability, CONFIRM_IDENTITY 422 fail-closed scenarios, and DOM-creation rendering contract

**Bob used for:**
- Writing 16 failing tests before implementing the fix (TDD)
- Implementing server-side candidate enrichment with manifest cross-reference
- Rewriting the `_renderCandidates` function using DOM creation and textContent
- Correcting verifier stage/check count throughout BOB.md and documentation

**Hosted result after deployment to `creditlock-00019-7bd`:**
- 15-stage hosted verifier: 24 checks passed, exit code 0
- Offline replay: MATCH (VERIFIED_LOCAL against package produced by hosted verified journey)
- Full transcript: `docs/evidence/final-hosted-judge-journey.txt`

**Commit:** fcc5d4a

---

_(Continue adding entries as development progresses)_
