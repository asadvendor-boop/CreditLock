# docs/SHA-PROVENANCE.md

This document records the authoritative provenance chain for the published CreditLock public repository.

---

## Private Source SHA

```
1108bbe8717bc1c63c14f5455f6c4bc50045181d
```

The published runtime files are exported from this exact commit in the private source repository.
No working-tree copy was used.

This commit is in a **private development repository** and intentionally does not resolve in the
sanitized public repository. For released-file equality verification, see
[`docs/RUNTIME-FILE-SHA256.txt`](RUNTIME-FILE-SHA256.txt).

---

## Production Image (Immutable Digest)

```
us-central1-docker.pkg.dev/pak-uni-scraper/cloud-run-source-deploy/creditlock-c2@sha256:1ee93048ff8ab02d3dcf58f334756cefce23e1cfb83c3237b179d9ddd1521199
```

---

## Live Cloud Run Revision

```
creditlock-00027-jeh
```

This revision serves 100% of traffic at the live URL.

---

## Live URL

```
https://creditlock-fvyx7hpwvq-uc.a.run.app
```

---

## Public Repository History

The original two public commits remain in history:

```
5f8b1d3  docs: add SHA provenance record
96de017  Release CreditLock production build
```

This document is part of a **normal follow-up commit** that synchronizes the accepted production
repair (private source SHA `1108bbe8717bc1c63c14f5455f6c4bc50045181d`) and current evidence into
the public repository. It does not rewrite, amend, force-push, or rebase either existing public
commit.

The commit creating the current public `main` cannot embed its own SHA (doing so would create a
circular dependency). The canonical value is the remote `main` HEAD after push, verifiable with:

```bash
git ls-remote https://github.com/asadvendor-boop/CreditLock.git refs/heads/main
```

---

## PUBLIC_CONTENT_SHA

```
96de0175dd73d7dad84b5f5cadceec15bea35de4
```

First public commit containing all product source, tests, fixtures, scripts, and initial public
documentation.

---

## Runtime File Hash Manifest

**Manifest file:** [`docs/RUNTIME-FILE-SHA256.txt`](RUNTIME-FILE-SHA256.txt)

This manifest records SHA-256 hashes for runtime and product files (Dockerfile, pyproject.toml,
src/, scripts/, tests/, fixtures/) present in the current public working tree.

**Verification method:** For each entry, the candidate file is compared byte-for-byte with the
corresponding object at private source SHA `1108bbe8717bc1c63c14f5455f6c4bc50045181d`:

```bash
git show 1108bbe8717bc1c63c14f5455f6c4bc50045181d:<relative-path> | shasum -a 256
```

Zero mismatches across all runtime files.

**Intentionally updated public docs (may differ from private source):**
- `README.md` — revision, image digest, source SHA updated to current
- `BOB.md` — Confluent runtime scope correction, revision and SHA updated
- `docs/claim-to-evidence.md` — revision, image digest, source SHA updated
- `docs/bob-development-log.md` — new session entry added
- `docs/SHA-PROVENANCE.md` — this file
- `docs/RUNTIME-FILE-SHA256.txt` — regenerated from current public working tree

---

## Why Public Git SHA Differs from Private Source Git SHA

The public repository was initialized as a brand-new Git repository. It contains no import of the
private repository's commit graph, refs, tags, remotes, or reflogs.

The private source SHAs (`96de017…`, `95535e6…`, `1108bbe…`, etc.) refer to commits in a
**private development repository** whose history is not present here. They are preserved as
evidence of development provenance. They intentionally do not resolve in this sanitized public
repository.

File-level equality between the public tree and the private source SHA can be verified via
[`docs/RUNTIME-FILE-SHA256.txt`](RUNTIME-FILE-SHA256.txt). Note that file equality alone does not
prove authorship.

---

## Commit Graph (Public Repository)

```
<NEW_FOLLOW_UP_SHA>  release: sync durable export proof and current judge evidence
      │
5f8b1d3             docs: add SHA provenance record
      │
96de017             Release CreditLock production build  (root)
```

`main` HEAD is the follow-up synchronization commit. All original product runtime files remain
accessible at `96de017`. The SHA of the new follow-up commit is the remote `main` HEAD after push
(verifiable via `git ls-remote` above).
