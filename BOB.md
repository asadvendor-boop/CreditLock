Project-authored IBM Bob development evidence; this is not an IBM-generated manifest.

# BOB.md — IBM Bob Development Evidence

This document records which parts of CreditLock were built with IBM Bob, the final hosted verifier Bob implemented, and the accurate scope of Confluent Cloud's contribution. It does not claim Bob wrote the entire product.

---

## What Bob Built

IBM Bob assisted with the following specific parts of CreditLock:

1. **Domain model scaffolding** — Pydantic v2 domain models in `src/creditlock/domain/models.py` including `Obligation`, `CreditManifest`, `Issue`, `Authorization`, `Proposal`, and related gate and checker data structures, as documented in [`docs/bob-development-log.md`](docs/bob-development-log.md) Task 0 (commit `e978bbc`).
2. **NFC-normalized canonical hashing** — `sha256_digest()` in `src/creditlock/domain/canonical.py`; the NFC-normalization and deterministic key-sorting logic that content-addresses all structured state.
3. **Identity classifier/binding implementation** — `_resolve_obligee_text()`, `evaluate_findings()`, and `_build_identity_bindings()` implementing the three identity-resolution rules and the `CONFIRM_IDENTITY` authorization path. Commits `b689b2a` (classifier rules and issue-id discriminator), `7b4e122` (registry-filter fix, `selected_contributor_id`, identity bindings), and `0be2428` (scoping to current `AMBIGUOUS_IDENTITY` issues).
4. **Unit test suites** — Focused unit tests added by the commits above: `TestIdentityRules`, `TestBackdoorApiGuard`, `TestIdentityAuthorization`, and related classes in `tests/unit/` covering identity resolution, gate folding, canonical hashing, patch application, and replay adversarial cases.
5. **Kafka event transport** — `ConfluentTransport` in `src/creditlock/events/transport.py`; the live Confluent Cloud produce/consume transport class, recorded live on partition 0 at private-development-repository commit `cba7227` (intentionally does not resolve in this sanitized public repository).
6. **Hosted judge-journey verifier** — `scripts/verify_judge_journey.py`; a 15-stage hosted verifier with 24 checks that exercises the live Cloud Run judge journey end to end. Implemented at private-development-repository commits `5f968444cb383a14967d99e4375d8782d65a7373` and `5b317032f554af384d79b0cfee4862146a20f5b4` (these SHAs intentionally do not resolve in the sanitized public repository; see `docs/RUNTIME-FILE-SHA256.txt` for released-file equality).
7. **UI contributor-selection fix and server-side candidate derivation** — Replaced the hardcoded readonly contributor input with an explicit two-candidate radio group and moved candidate derivation to the server in `src/creditlock/api/demo.py`, `src/creditlock/api/resolutions.py`, and `src/creditlock/static/index.html`; focused tests at `tests/unit/test_ui_contributor_selection.py`. Deployed at private-development-repository commit `6e4ffa9`.
8. **Candidate enrichment and safe DOM rendering** — Enriched `AMBIGUOUS_IDENTITY` candidate objects server-side with `display_name`, `role`, `credit_surface`, and `present_in_manifest` (derived only from the existing obligation, contributor registry, and manifest — no hardcoded names or IDs). Rewrote `_renderCandidates()` and `_clearCandidates()` in `index.html` to use `document.createElement` / `textContent` / `.value` instead of unescaped `innerHTML` template interpolation. 16 new tests in `tests/unit/test_candidate_enrichment.py`. Deployed at private-development-repository commit `fcc5d4a`.

IBM Bob assisted with all of the above. IBM Bob did not write the whole product. Later persistence, event-sync, delivery-determinism, and CSS work were contributed by other permitted means and must not be misattributed to Bob.

Prompt summaries for each session exist in [`docs/bob-development-log.md`](docs/bob-development-log.md). Only raw task exports, secrets, and private recording transcripts are excluded from public documentation.

Private-development-repository commits with direct Bob contribution: `e978bbc`, `b689b2a`, `7b4e122`, `0be2428`, `cba7227`, `5f96844`, `5b31703`, `73b5acf`, `6e4ffa9`, `fcc5d4a`. These SHAs are in the private development repository and intentionally do not resolve in this sanitized public repository. For released-file equality, see [`docs/RUNTIME-FILE-SHA256.txt`](docs/RUNTIME-FILE-SHA256.txt). Note: file equality alone does not prove authorship.

---

## The Final Verifier Bob Implemented

IBM Bob implemented `scripts/verify_judge_journey.py`, a 15-stage hosted verifier with 24 checks that exercises the live hosted judge journey at `https://creditlock-fvyx7hpwvq-uc.a.run.app`.

The verifier exercises and asserts:

1. Public application loads (HTTP 200)
2. Deprecated token-mint endpoint unavailable
3. Unauthenticated request refused (HTTP 401/403)
4. Fresh demo session initialized and unresolved export blocked (HTTP 409)
5. Gemini analyzes deal memo and returns grounded candidate (HTTP 200)
6. State endpoint returns current issues with deterministic AMBIGUOUS_IDENTITY confirmed
7. Server returns `candidate_ids` list for AMBIGUOUS_IDENTITY (two genuine candidates)
8. `contrib_david_park` is in server-returned candidate set
9. Reported Gemini model is in the allowlist (`gemini-3.6-flash`)
10. Selected contributor ID is in server-returned candidate set (not fabricated)
11. REVIEWER creates proposal (HTTP 201) with selected contributor ID
12. REVIEWER self-confirmation refused (HTTP 403)
13. REVIEWER export refused (HTTP 403)
14. Distinct RELEASE_APPROVER confirms proposal (HTTP 201)
15. Authorized export succeeds (HTTP 200)
16. Export provides real 64-character release digest and `gs://` GCS URI
17. Authenticated download succeeds and ZIP is valid
18. Offline replay of downloaded bundle returns MATCH

**Verification result (current revision `creditlock-00027-jeh`, private-development-repository commit `fcc5d4a`):** All 15 stages (24 checks) passed. Offline replay returned MATCH (VERIFIED_LOCAL against the package produced by the hosted verified journey — not independently HOSTED_VERIFIED). The current final hosted transcript reflects this result.

Full transcript: [`docs/evidence/final-hosted-judge-journey.txt`](docs/evidence/final-hosted-judge-journey.txt)

---

## Confluent Cloud Contribution — Accurate Scope

IBM Bob built `ConfluentTransport` and `scripts/confluent_smoke.py`. A live raw-JSON produce/consume round trip was recorded on partition 0 of Confluent Cloud Kafka cluster (topic: `creditlock.production.events`) at private-development-repository commit `cba7227`.

**Confluent Cloud is a runtime dependency of the current authorized export path.** The hosted export flow emits the `resolution.recorded` event type through Confluent Cloud Kafka. A warm worker subscribes to the topic and projects the event into Firestore; the UI displays `Confluent Cloud`, event type `resolution.recorded`, and `Firestore projection: SYNCHRONIZED` after a successful authorized export. Step 12 of the hosted verifier confirms this synchronization.

Bob built the transport layer and smoke script. The end-to-end event-sync integration connecting Confluent to Firestore projection was contributed by other permitted means and must not be misattributed to Bob.

---

## What Bob Did Not Build

Bob did not write every component of CreditLock. The following were contributed by other permitted means:

- Gemini agent framework integration and agent output parsing
- Cloud Run deployment configuration and GCS evidence storage layer
- FastAPI route scaffolding and demo session management
- Firestore production store integration, event projection from Confluent to Firestore, delivery-determinism work, and later CSS work

---

## Evidence Links

- IBM Bob development log (with session records): [`docs/bob-development-log.md`](docs/bob-development-log.md)
- Sanitized hosted transcript: [`docs/evidence/final-hosted-judge-journey.txt`](docs/evidence/final-hosted-judge-journey.txt)

---

## Honest Boundaries

- This document was authored by the project operator. It is not an IBM-generated manifest.
- No prompts, token counts, raw task exports, private recording instructions, or inflated ownership claims are included.
- Bob's development assistance is classified as `VERIFIED_LOCAL` in `docs/claim-to-evidence.md`.
- The verifier Bob created is classified as `HOSTED_VERIFIED` because it exercised a live hosted journey, not because Bob's development contributions generally upgraded to that classification.
- Offline replay is classified as `VERIFIED_LOCAL` against a package produced by the hosted verified journey — not as independently `HOSTED_VERIFIED`.
- The demo uses synthetic enterprise-shaped production data. It is not evidence of customer adoption.
