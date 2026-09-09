# docs/SHA-PROVENANCE.md

This document records the authoritative provenance chain for the published CreditLock public repository.

---

## Private Source SHA

```
95535e6f3d4aba18b5c8b5d9d67c9efc9a3eadb5
```

The published runtime files are exported from this exact commit in the private source repository. No working-tree copy was used.

---

## Production Image (Immutable Digest)

```
us-central1-docker.pkg.dev/pak-uni-scraper/cloud-run-source-deploy/creditlock-c2@sha256:ae19b5350bc8bbc83f593454b8bcf6f2e6608ac75b2e5504ea33e3fceaf6f745
```

---

## Live Cloud Run Revision

```
creditlock-00024-4h2
```

This revision serves 100% of traffic at the live URL.

---

## Live URL

```
https://creditlock-fvyx7hpwvq-uc.a.run.app
```

---

## PUBLIC_CONTENT_SHA

```
96de0175dd73d7dad84b5f5cadceec15bea35de4
```

This is the root content commit of the public repository — the first commit, containing all product source, tests, fixtures, scripts, and updated public documentation. It is the parent of the current `main` HEAD.

---

## PUBLIC_HEAD_SHA

The current `main` HEAD is the docs-only child of `PUBLIC_CONTENT_SHA`. It adds this `docs/SHA-PROVENANCE.md` file only.

PUBLIC_HEAD_SHA is the SHA of the tip of `main` on the published GitHub repository. It can be verified with:

```bash
git ls-remote https://github.com/asadvendor-boop/CreditLock.git refs/heads/main
```

`git log --oneline` on the published repository will show exactly two commits:
```
<PUBLIC_HEAD_SHA>  docs: add SHA provenance record
96de017            Release CreditLock production build
```

Note: Because this document (`docs/SHA-PROVENANCE.md`) is part of the commit whose SHA it would describe, the PUBLIC_HEAD_SHA cannot be embedded here without creating a circular dependency. The canonical value is the remote `main` HEAD after push, verifiable via the command above.

---

## Runtime File Hash Manifest

**Manifest file:** `docs/RUNTIME-FILE-SHA256.txt`

This manifest records SHA-256 hashes for 185 runtime and product files (Dockerfile, pyproject.toml, src/, scripts/ excluding verify_judge_journey.py, tests/, fixtures/) copied unchanged from private source SHA `95535e6f3d4aba18b5c8b5d9d67c9efc9a3eadb5`.

**Verification method:** For each entry in the manifest, the candidate file was compared byte-for-byte with:

```bash
git show 95535e6f3d4aba18b5c8b5d9d67c9efc9a3eadb5:<relative-path> | shasum -a 256
```

Zero mismatches were found across all 185 runtime files.

**Intentionally updated public docs (excluded from manifest):**
- `README.md` — Live URL correction (`creditlock-851586299411...` → `creditlock-fvyx7hpwvq-uc.a.run.app`), Cloud Run revision, Firestore and Confluent runtime status
- `BOB.md` — Confluent runtime scope correction, removal of `memory_demo` claim
- `docs/claim-to-evidence.md` — Updated live URL, Firestore and Confluent classifications
- `docs/impact-evidence.md` — Live URL correction
- `scripts/verify_judge_journey.py` — Default base URL updated to current live URL
- `docs/RUNTIME-FILE-SHA256.txt` — This manifest itself (generated during publication)

---

## Why Public Git SHA Differs from Private Source Git SHA

The public repository was initialized as a brand-new Git repository (`git init`). It contains no import of the private repository's commit graph, refs, tags, remotes, or reflogs.

The public history starts from a fresh root commit (`PUBLIC_CONTENT_SHA`) containing the exported file tree. The private source SHA (`95535e6f3d4aba18b5c8b5d9d67c9efc9a3eadb5`) refers to a commit in a different, private Git object store whose history is not present here.

Additionally, the public tree differs from the private source tree in exactly the intentionally updated public docs listed above. Even if the public repository had been created by `git clone`, the SHA would differ because the file contents differ.

---

## Relationship: PUBLIC_CONTENT_SHA → PUBLIC_HEAD_SHA

```
PUBLIC_HEAD_SHA  (this docs-only commit: docs/SHA-PROVENANCE.md)
      │
      └── parent: PUBLIC_CONTENT_SHA  96de0175dd73d7dad84b5f5cadceec15bea35de4
                  (all product code, tests, fixtures, scripts, docs)
```

`main` HEAD is the docs-only provenance commit. All product runtime files live in `PUBLIC_CONTENT_SHA`. The two-commit structure allows a judge to inspect product code and runtime evidence separately from this provenance record.
