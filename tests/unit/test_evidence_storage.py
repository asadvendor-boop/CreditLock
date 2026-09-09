"""
Unit tests for EvidenceStorage, LocalDirectoryStore, and GCSStore.
"""

from __future__ import annotations

import tempfile
from typing import Any

import pytest
from google.api_core import exceptions as gcs_exceptions

from creditlock.domain.canonical import sha256_bytes_digest
from creditlock.evidence.storage import (
    EvidenceStorageError,
    GCSStore,
    LocalDirectoryStore,
    StorageCollisionError,
    StorageIntegrityError,
    StorageUnavailableError,
    validate_storage_path,
)


class FakeGCSBlob:
    def __init__(self, name: str, bucket_data: dict[str, bytes], upload_exc: Exception | None = None):
        self.name = name
        self._bucket_data = bucket_data
        self.upload_exc = upload_exc
        self.download_exc: Exception | None = None
        self.exists_exc: Exception | None = None

    def upload_from_string(self, data: bytes, if_generation_match: int | None = None) -> None:
        if self.upload_exc:
            raise self.upload_exc
        if if_generation_match == 0 and self.name in self._bucket_data:
            raise gcs_exceptions.PreconditionFailed("Object already exists")
        self._bucket_data[self.name] = data

    def download_as_bytes(self) -> bytes:
        if self.download_exc:
            raise self.download_exc
        if self.name not in self._bucket_data:
            raise gcs_exceptions.NotFound(f"Blob {self.name} not found")
        return self._bucket_data[self.name]

    def exists(self) -> bool:
        if self.exists_exc:
            raise self.exists_exc
        return self.name in self._bucket_data


class FakeGCSBucket:
    def __init__(self, name: str):
        self.name = name
        self._bucket_data: dict[str, bytes] = {}
        self._blobs: dict[str, FakeGCSBlob] = {}

    def blob(self, blob_name: str) -> FakeGCSBlob:
        if blob_name not in self._blobs:
            self._blobs[blob_name] = FakeGCSBlob(blob_name, self._bucket_data)
        return self._blobs[blob_name]


class FakeGCSClient:
    def __init__(self) -> None:
        self._buckets: dict[str, FakeGCSBucket] = {}

    def bucket(self, bucket_name: str) -> FakeGCSBucket:
        if bucket_name not in self._buckets:
            self._buckets[bucket_name] = FakeGCSBucket(bucket_name)
        return self._buckets[bucket_name]


class PoisonGCSClient:
    def __init__(self) -> None:
        self.bucket_called = False

    def bucket(self, bucket_name: str) -> Any:
        self.bucket_called = True
        raise RuntimeError("PoisonGCSClient: bucket() must not be called when validation fails.")


class TestEvidenceStorageContract:
    def test_exception_taxonomy(self) -> None:
        assert issubclass(StorageCollisionError, EvidenceStorageError)
        assert issubclass(StorageUnavailableError, EvidenceStorageError)
        assert issubclass(StorageIntegrityError, EvidenceStorageError)
        assert issubclass(EvidenceStorageError, RuntimeError)

    def test_unsafe_paths_rejected(self) -> None:
        with pytest.raises(TypeError):
            validate_storage_path(123)  # type: ignore[arg-type]
        unsafe_paths = [
            "",
            "   ",
            "/absolute/path.txt",
            "../outside.txt",
            "folder/../file.txt",
            "./file.txt",
            "folder/./file.txt",
            "a//b",
            "a/b/",
            "a\\b",
            " a/b.txt",
            "a/b.txt ",
        ]
        for p in unsafe_paths:
            with pytest.raises(ValueError):
                validate_storage_path(p)

    @pytest.mark.parametrize("bad_gen", [None, 1, -1, False, True, "0", 100])
    def test_invalid_generation_match_precondition_rejected(self, bad_gen: Any) -> None:
        # Poison GCS Client to prove no backend calls occur
        poison_client = PoisonGCSClient()
        gcs_store = GCSStore("evidence-bucket", client=poison_client)
        with pytest.raises(ValueError):
            gcs_store.put("path.txt", b"data", if_generation_match=bad_gen)
        with pytest.raises(ValueError):
            gcs_store.put_verified("path.txt", b"data", sha256_bytes_digest(b"data"), if_generation_match=bad_gen)
        assert poison_client.bucket_called is False

        # Local Directory Store
        with tempfile.TemporaryDirectory() as tmpdir:
            local_store = LocalDirectoryStore(tmpdir)
            with pytest.raises(ValueError):
                local_store.put("path.txt", b"data", if_generation_match=bad_gen)
            with pytest.raises(ValueError):
                local_store.put_verified("path.txt", b"data", sha256_bytes_digest(b"data"), if_generation_match=bad_gen)
            assert local_store.exists("path.txt") is False

    def test_gcs_store_invalid_bucket_name(self) -> None:
        invalid_buckets = ["", "gs://my-bucket", "my bucket", "bucket/with/slash"]
        for b in invalid_buckets:
            with pytest.raises(ValueError):
                GCSStore(b)

    def test_local_store_traversal_prevention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            with pytest.raises(ValueError):
                store.put("../outside.txt", b"data")
            with pytest.raises(ValueError):
                store.get("../outside.txt")
            with pytest.raises(ValueError):
                store.exists("../outside.txt")

    def test_local_store_idempotence_and_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            path = "valid/object.bin"
            data = b"hello evidence"

            uri1 = store.put(path, data, if_generation_match=0)
            assert uri1.startswith("file://")
            assert store.exists(path) is True
            assert store.get(path) == data

            # Identical write succeeds idempotently
            uri2 = store.put(path, data, if_generation_match=0)
            assert uri2 == uri1

            # Differing write raises StorageCollisionError
            with pytest.raises(StorageCollisionError):
                store.put(path, b"different evidence", if_generation_match=0)
            assert store.get(path) == data

    def test_gcs_store_idempotence_and_collision(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("my-evidence-bucket", client=client)
        path = "evidence/data.bin"
        data = b"gcs evidence bytes"

        uri1 = store.put(path, data, if_generation_match=0)
        assert uri1 == "gs://my-evidence-bucket/evidence/data.bin"
        assert store.exists(path) is True
        assert store.get(path) == data

        # Retry identical
        uri2 = store.put(path, data, if_generation_match=0)
        assert uri2 == uri1

        # Retry differing
        with pytest.raises(StorageCollisionError):
            store.put(path, b"different bytes", if_generation_match=0)

    def test_gcs_store_precondition_download_failure_becomes_unavailable(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("my-evidence-bucket", client=client)
        path = "evidence/corrupt.bin"
        store.put(path, b"data 1", if_generation_match=0)

        # Set download exception on existing blob
        blob = client.bucket("my-evidence-bucket").blob(path)
        blob.download_exc = gcs_exceptions.ServiceUnavailable("GCS network down")

        with pytest.raises(StorageUnavailableError):
            store.put(path, b"data 2", if_generation_match=0)

    def test_gcs_store_sdk_failures_become_unavailable(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("my-bucket", client=client)
        bucket = client.bucket("my-bucket")

        # 1. Upload failure with Forbidden
        f1_blob = bucket.blob("f1")
        f1_blob.upload_exc = gcs_exceptions.Forbidden("Permission denied")

        with pytest.raises(StorageUnavailableError):
            store.put("f1", b"data", if_generation_match=0)

        # 2. Get non-NotFound failure
        f2_blob = bucket.blob("f2")
        f2_blob.download_exc = gcs_exceptions.Forbidden("Access denied")

        with pytest.raises(StorageUnavailableError):
            store.get("f2")

        # 3. Exists failure
        f3_blob = bucket.blob("f3")
        f3_blob.exists_exc = gcs_exceptions.ServiceUnavailable("Service down")

        with pytest.raises(StorageUnavailableError):
            store.exists("f3")

    def test_gcs_store_get_not_found_returns_none(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("my-bucket", client=client)
        assert store.get("nonexistent.bin") is None

    def test_put_verified_digest_validation_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            data = b"verified content"
            correct_digest = sha256_bytes_digest(data)
            wrong_digest = "00" * 32

            # 1. Invalid hex string for digest
            with pytest.raises(ValueError):
                store.put_verified("v1.txt", data, "invalid-hex")

            # 2. Supplied digest mismatch before writing
            with pytest.raises(StorageIntegrityError):
                store.put_verified("v1.txt", data, wrong_digest)
            assert store.exists("v1.txt") is False

            # 3. Successful verified write
            uri = store.put_verified("v1.txt", data, correct_digest)
            assert uri.startswith("file://")
            assert store.get("v1.txt") == data

    def test_put_verified_readback_divergence_raises_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalDirectoryStore(tmpdir)
            data = b"good data"
            digest = sha256_bytes_digest(data)

            # Corrupt get readback to return different data
            orig_get = store.get

            def mock_corrupt_get(path: str) -> bytes | None:
                if path == "corrupt.txt":
                    return b"tampered data"
                return orig_get(path)

            store.get = mock_corrupt_get  # type: ignore[assignment]

            with pytest.raises(StorageIntegrityError):
                store.put_verified("corrupt.txt", data, digest)

    def test_precondition_bypass_prevention_red(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("evidence-bucket", client=client)
        path = "releases/prod/package.zip"
        original = b"original evidence"
        replacement = b"replacement evidence"
        digest_orig = sha256_bytes_digest(original)
        digest_repl = sha256_bytes_digest(replacement)

        store.put_verified(path, original, digest_orig)

        for bad_gen in [None, 1, False]:
            with pytest.raises(ValueError, match="if_generation_match"):
                store.put_verified(path, replacement, digest_repl, if_generation_match=bad_gen)  # type: ignore[arg-type]

        assert store.get(path) == original

    def test_default_calls_without_explicit_argument_remain_create_only(self) -> None:
        client = FakeGCSClient()
        store = GCSStore("evidence-bucket", client=client)
        path = "default/create_only.bin"
        original = b"original"
        replacement = b"replacement"
        digest_orig = sha256_bytes_digest(original)
        digest_repl = sha256_bytes_digest(replacement)

        # 1. put default argument
        store.put(path, original)
        with pytest.raises(StorageCollisionError):
            store.put(path, replacement)
        assert store.get(path) == original

        # 2. put_verified default argument
        path_v = "default/create_only_v.bin"
        store.put_verified(path_v, original, digest_orig)
        with pytest.raises(StorageCollisionError):
            store.put_verified(path_v, replacement, digest_repl)
        assert store.get(path_v) == original
