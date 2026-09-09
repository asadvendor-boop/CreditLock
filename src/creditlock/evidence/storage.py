"""
Content-addressed evidence storage interfaces and implementations.

Provides LocalDirectoryStore (for tests/local) and GCSStore (for production) with
write-once preconditions (if_generation_match=0 / exclusive creation).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from google.api_core import exceptions as gcs_exceptions

from creditlock.domain.canonical import sha256_bytes_digest


class EvidenceStorageError(RuntimeError):
    """Base exception for all evidence storage failures."""


class StorageCollisionError(EvidenceStorageError):
    """Raised when writing to an existing content-addressed target with different bytes."""


class StorageUnavailableError(EvidenceStorageError):
    """Raised when storage backend operations fail due to auth, network, permission, or service issues."""


class StorageIntegrityError(EvidenceStorageError):
    """Raised when data readback or digest verification fails."""


def validate_if_generation_match(if_generation_match: int) -> int:
    """Validate that if_generation_match is strictly int 0 for immutable create-only writes."""
    if type(if_generation_match) is not int or if_generation_match != 0:
        raise ValueError(
            f"CreditLock evidence storage requires if_generation_match=0 (create-only), got {if_generation_match!r}."
        )
    return if_generation_match


def validate_storage_path(path: str) -> str:
    """Validate storage path before any I/O or backend operations."""
    if not isinstance(path, str):
        raise TypeError("Storage path must be a string.")
    if not path or path.strip() != path:
        raise ValueError(f"Invalid storage path '{path}': empty or surrounding whitespace.")
    if "\\" in path:
        raise ValueError(f"Invalid storage path '{path}': backslashes not allowed.")
    if path.startswith("/"):
        raise ValueError(f"Invalid storage path '{path}': absolute path or leading slash not allowed.")

    parts = path.split("/")
    if any(p == "" for p in parts):
        raise ValueError(f"Invalid storage path '{path}': empty component or consecutive slashes.")
    if any(p in (".", "..") for p in parts):
        raise ValueError(f"Invalid storage path '{path}': relative navigation components ('.' or '..') not allowed.")

    norm = Path(path).as_posix()
    if norm != path:
        raise ValueError(f"Invalid storage path '{path}': normalized representation '{norm}' differs.")
    return path


class EvidenceStorage(ABC):
    """Abstract interface for immutable evidence storage."""

    @abstractmethod
    def put(self, path: str, data: bytes, if_generation_match: int = 0) -> str:
        """Store bytes at path. Returns URI."""

    @abstractmethod
    def get(self, path: str) -> bytes | None:
        """Retrieve bytes from path, or None if missing."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if path exists."""

    def put_verified(
        self,
        path: str,
        data: bytes,
        expected_sha256: str,
        *,
        if_generation_match: int = 0,
    ) -> str:
        """Store data with pre-and-post verification of SHA-256 digest."""
        validate_if_generation_match(if_generation_match)
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or not all(c in "0123456789abcdef" for c in expected_sha256)
        ):
            raise ValueError("expected_sha256 must be exactly 64 lowercase hexadecimal characters.")

        computed = sha256_bytes_digest(data)
        if computed != expected_sha256:
            raise StorageIntegrityError(
                f"Content SHA-256 digest '{computed}' does not match expected digest '{expected_sha256}' before writing."
            )

        uri = self.put(path, data, if_generation_match=if_generation_match)

        read_back = self.get(path)
        if read_back is None:
            raise StorageIntegrityError(f"Verification readback failed: path '{path}' is missing after write.")

        if read_back != data:
            raise StorageIntegrityError(f"Verification readback failed: content at '{path}' differs from written data.")

        read_digest = sha256_bytes_digest(read_back)
        if read_digest != expected_sha256:
            raise StorageIntegrityError(
                f"Verification readback failed: content digest '{read_digest}' differs from expected '{expected_sha256}'."
            )

        return uri


class LocalDirectoryStore(EvidenceStorage):
    """Local filesystem implementation of EvidenceStorage."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def put(self, path: str, data: bytes, if_generation_match: int = 0) -> str:
        validate_if_generation_match(if_generation_match)
        validate_storage_path(path)
        target = (self.root_dir / path).resolve()
        try:
            if not target.is_relative_to(self.root_dir):
                raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.")
        except ValueError as err:
            raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.") from err

        if target.exists():
            existing_bytes = target.read_bytes()
            if existing_bytes == data:
                return f"file://{target}"
            raise StorageCollisionError(f"File '{path}' already exists with different content.")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"file://{target}"

    def get(self, path: str) -> bytes | None:
        validate_storage_path(path)
        target = (self.root_dir / path).resolve()
        try:
            if not target.is_relative_to(self.root_dir):
                raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.")
        except ValueError as err:
            raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.") from err

        if not target.exists():
            return None
        return target.read_bytes()

    def exists(self, path: str) -> bool:
        validate_storage_path(path)
        target = (self.root_dir / path).resolve()
        try:
            if not target.is_relative_to(self.root_dir):
                raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.")
        except ValueError as err:
            raise ValueError(f"Path '{path}' escapes root directory '{self.root_dir}'.") from err

        return target.exists()


class GCSStore(EvidenceStorage):
    """Cloud Storage implementation using write-once preconditions (if_generation_match=0)."""

    def __init__(self, bucket_name: str, client: Any = None) -> None:
        if (
            not isinstance(bucket_name, str)
            or not bucket_name
            or bucket_name.strip() != bucket_name
            or bucket_name.startswith("gs://")
            or "/" in bucket_name
            or " " in bucket_name
            or "\\" in bucket_name
        ):
            raise ValueError(f"Invalid GCS bucket_name '{bucket_name}': must be raw non-empty name.")
        self.bucket_name = bucket_name
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import (  # type: ignore[attr-defined,import-untyped,unused-ignore]
                storage,
            )

            self._client = storage.Client()
        return self._client

    def put(self, path: str, data: bytes, if_generation_match: int = 0) -> str:
        validate_if_generation_match(if_generation_match)
        validate_storage_path(path)
        bucket = self.client.bucket(self.bucket_name)
        blob = bucket.blob(path)

        try:
            blob.upload_from_string(data, if_generation_match=0)
            return f"gs://{self.bucket_name}/{path}"
        except gcs_exceptions.PreconditionFailed:
            try:
                existing_bytes: bytes = blob.download_as_bytes()
            except Exception as ex:
                raise StorageUnavailableError(
                    f"Precondition failed for 'gs://{self.bucket_name}/{path}' but unable to retrieve existing data: {ex}"
                ) from ex

            if existing_bytes == data:
                return f"gs://{self.bucket_name}/{path}"
            raise StorageCollisionError(
                f"GCS object 'gs://{self.bucket_name}/{path}' already exists with different content."
            )
        except Exception as ex:
            raise StorageUnavailableError(
                f"GCS operation failed for 'gs://{self.bucket_name}/{path}': {ex}"
            ) from ex

    def get(self, path: str) -> bytes | None:
        validate_storage_path(path)
        bucket = self.client.bucket(self.bucket_name)
        blob = bucket.blob(path)
        try:
            res: bytes = blob.download_as_bytes()
            return res
        except gcs_exceptions.NotFound:
            return None
        except Exception as ex:
            raise StorageUnavailableError(
                f"Failed to retrieve GCS object 'gs://{self.bucket_name}/{path}': {ex}"
            ) from ex

    def exists(self, path: str) -> bool:
        validate_storage_path(path)
        bucket = self.client.bucket(self.bucket_name)
        blob = bucket.blob(path)
        try:
            res: bool = blob.exists()
            return res
        except Exception as ex:
            raise StorageUnavailableError(
                f"Failed to check existence for GCS object 'gs://{self.bucket_name}/{path}': {ex}"
            ) from ex
