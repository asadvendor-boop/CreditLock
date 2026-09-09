"""
Live Google Cloud Storage (GCS) contract integration tests.

Gated behind CREDITLOCK_RUN_LIVE_GCS=1.
Requires GOOGLE_CLOUD_PROJECT and EVIDENCE_BUCKET env vars when enabled.
"""

from __future__ import annotations

import os
import uuid

import pytest

from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.evidence.storage import GCSStore, StorageCollisionError


def test_gcs_live_storage_contract() -> None:
    run_live = os.environ.get("CREDITLOCK_RUN_LIVE_GCS") == "1"
    if not run_live:
        pytest.skip("SKIPPED_NO_LIVE_FLAG: Set CREDITLOCK_RUN_LIVE_GCS=1 to run live GCS integration test.")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bucket_name = os.environ.get("EVIDENCE_BUCKET")

    if not project or not bucket_name:
        pytest.fail(
            "FAILED_LIVE_GCS_CONFIGURATION_OR_OPERATION: "
            "CREDITLOCK_RUN_LIVE_GCS=1 is set but GOOGLE_CLOUD_PROJECT or EVIDENCE_BUCKET is missing."
        )

    from google.cloud import storage

    client = storage.Client(project=project)
    store = GCSStore(bucket_name=bucket_name, client=client)

    test_prefix = f"live-test-{uuid.uuid4().hex[:8]}"
    path = f"{test_prefix}/evidence.bin"
    data = b"live gcs evidence verification payload"
    digest = sha256_bytes_digest(data)

    try:
        # 1. Perform put_verified
        uri1 = store.put_verified(path, data, digest, if_generation_match=0)
        assert uri1 == f"gs://{bucket_name}/{path}"

        # 2. Download and compare exact bytes
        read_back = store.get(path)
        assert read_back == data

        # 3. Assert exists() is True
        assert store.exists(path) is True

        # 4. Retry identical bytes successfully (returns same URI)
        uri2 = store.put(path, data, if_generation_match=0)
        assert uri2 == uri1

        # 5. Retry different bytes and receive StorageCollisionError
        with pytest.raises(StorageCollisionError):
            store.put(path, b"different payload", if_generation_match=0)

    finally:
        # Strict cleanup: delete ONLY the exact unique test object
        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(path)
            blob.delete()
        except Exception:  # noqa: BLE001, S110
            pass
