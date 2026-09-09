"""
Canonical JSON serialization and SHA-256 digesting.

Rules:
- Keys sorted lexicographically at every nesting level.
- No trailing whitespace or newlines in the digest input.
- Arrays retain their original order (do not sort array elements).
- Strings are Unicode NFC-normalized before serialization.
- sha256_digest(value) returns lowercase hex.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


def _normalize(value: Any) -> Any:
    """Recursively NFC-normalize all strings; sort dict keys."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {unicodedata.normalize("NFC", k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical JSON bytes for *value*.

    - All string values and keys are NFC-normalized.
    - Dict keys are sorted at every level.
    - Array element order is preserved.
    - Compact encoding: no spaces after separators.
    """
    normalized = _normalize(value)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(value: Any) -> str:
    """Return lowercase hex SHA-256 of the canonical JSON encoding of *value*."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes_digest(data: bytes) -> str:
    """Return lowercase hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()
