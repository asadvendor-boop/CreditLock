# Dataset Card — CreditLock Benchmark

## Methodology

All fixture files are synthetic original text authored for this project. No real production deal memos, real individual names from actual productions, or third-party IP are used. Every name is invented. Every obligation is derived from a fictitious contract.

## Fixture Counts

- **Development cases:** 8 (dev_001 through dev_008)
- **Sealed cases:** 4 (sealed_001 through sealed_004) — run once at final evaluation only
- **Demo production:** 1 fixture with exactly 3 deliberate issues matching the demo video

## Sealed Case Hashes

SHA-256 of each sealed fixture file (frozen after identity-schema correction in Task C).
Do not modify sealed files after recording.

| File | SHA-256 |
|---|---|
| sealed_001_clean.json | `14153938e620fba9c59938c0ae82d331babb70112ac3b447382718c72c80cab3` |
| sealed_002_missing_credit.json | `b857a49eab8841bb56ff0bc3ad0329271319e44ec3f8bdc448f77d5971b9c4b1` |
| sealed_003_ambiguous_identity.json | `90d076bc979d0ed4324289fb47871370d81eec3972b434ac8fd00dd9dca2b26a` |
| sealed_004_visual_mismatch.json | `22cae206189326554ec94dafeb0b199c6bf028530953c3d1ef834a13f4dd3efe` |

## Issue Coverage

| Issue Code | Dev Case | Sealed Case |
|---|---|---|
| MISSING_CREDIT | dev_002 | sealed_002 |
| ARTIFACT_TEXT_MISMATCH | dev_003 | — |
| ARTIFACT_GROUPING_MISMATCH | dev_005 | — |
| ARTIFACT_POSITION_MISMATCH | dev_006 | — |
| ARTIFACT_SIZE_MISMATCH | dev_007 | — |
| AMBIGUOUS_IDENTITY | dev_004 | sealed_003 |
| UNCONFIRMED_OBLIGATION | dev_008 | — |
| VISUAL_OBSERVATION_UNCERTAIN | — | sealed_004 |
| Clean / READY_TO_EXPORT | dev_001 | sealed_001 |
