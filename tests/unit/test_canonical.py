"""
Tests for canonical JSON serialization and SHA-256 digesting.
"""

from creditlock.domain.canonical import (
    canonical_json_bytes,
    sha256_bytes_digest,
    sha256_digest,
)


class TestCanonicalJsonBytes:
    def test_dict_keys_sorted(self):
        a = canonical_json_bytes({"z": 1, "a": 2})
        b = canonical_json_bytes({"a": 2, "z": 1})
        assert a == b

    def test_nested_keys_sorted(self):
        a = canonical_json_bytes({"z": {"b": 1, "a": 2}, "a": 3})
        b = canonical_json_bytes({"a": 3, "z": {"a": 2, "b": 1}})
        assert a == b

    def test_array_order_preserved(self):
        a = canonical_json_bytes([3, 1, 2])
        b = canonical_json_bytes([1, 2, 3])
        assert a != b

    def test_array_order_same(self):
        a = canonical_json_bytes([1, 2, 3])
        b = canonical_json_bytes([1, 2, 3])
        assert a == b

    def test_string_nfc_normalized(self):
        # café: NFC vs NFD
        nfc = canonical_json_bytes("caf\u00e9")  # NFC: single codepoint
        nfd = canonical_json_bytes("cafe\u0301")  # NFD: e + combining accent
        assert nfc == nfd

    def test_dict_key_nfc_normalized(self):
        nfc = canonical_json_bytes({"caf\u00e9": 1})
        nfd = canonical_json_bytes({"cafe\u0301": 1})
        assert nfc == nfd

    def test_compact_encoding_no_spaces(self):
        result = canonical_json_bytes({"a": 1, "b": 2})
        assert b" " not in result

    def test_utf8_encoding(self):
        result = canonical_json_bytes("日本語")
        assert isinstance(result, bytes)
        # Should be valid UTF-8 with actual characters not escaped
        assert "日本語".encode() in result

    def test_none_value(self):
        a = canonical_json_bytes({"a": None})
        assert b"null" in a

    def test_integer_value(self):
        a = canonical_json_bytes(42)
        assert a == b"42"

    def test_list_of_dicts_keys_sorted(self):
        a = canonical_json_bytes([{"z": 1, "a": 2}, {"b": 3, "a": 4}])
        assert b'"a":2,"z":1' in a
        assert b'"a":4,"b":3' in a


class TestSha256Digest:
    def test_returns_lowercase_hex(self):
        digest = sha256_digest({"a": 1})
        assert digest == digest.lower()
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_same_input_same_digest(self):
        a = sha256_digest({"x": 1, "y": 2})
        b = sha256_digest({"y": 2, "x": 1})
        assert a == b

    def test_different_input_different_digest(self):
        a = sha256_digest({"x": 1})
        b = sha256_digest({"x": 2})
        assert a != b

    def test_nfc_equivalence(self):
        nfc = sha256_digest("caf\u00e9")
        nfd = sha256_digest("cafe\u0301")
        assert nfc == nfd

    def test_array_order_matters(self):
        a = sha256_digest([1, 2, 3])
        b = sha256_digest([3, 2, 1])
        assert a != b


class TestSha256BytesDigest:
    def test_known_value(self):
        import hashlib

        data = b"hello"
        expected = hashlib.sha256(data).hexdigest()
        assert sha256_bytes_digest(data) == expected

    def test_returns_lowercase_hex(self):
        result = sha256_bytes_digest(b"test")
        assert result == result.lower()
        assert len(result) == 64
