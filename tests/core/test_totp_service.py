"""
Unit tests for TOTP (Two-Factor Authentication) service.

Tests cover:
- Secret generation
- TOTP URI generation
- QR code generation
- TOTP code verification
- Recovery code generation and verification
"""

import pytest
import json
import hashlib
import time
from unittest.mock import patch, MagicMock

from app.core import totp_service


class TestSecretGeneration:
    """Test TOTP secret generation."""

    def test_generate_secret_returns_string(self):
        """Should return a base32 encoded string."""
        secret = totp_service.generate_secret()
        assert isinstance(secret, str)

    def test_generate_secret_length(self):
        """Secret should have reasonable length."""
        secret = totp_service.generate_secret()
        # pyotp.random_base32() returns 32 character secrets by default
        assert len(secret) >= 16

    def test_generate_secret_uniqueness(self):
        """Each call should produce unique secrets."""
        secrets = [totp_service.generate_secret() for _ in range(100)]
        assert len(set(secrets)) == 100

    def test_generate_secret_is_base32(self):
        """Secret should only contain valid base32 characters."""
        secret = totp_service.generate_secret()
        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")
        assert all(c in valid_chars for c in secret)


class TestTotpUri:
    """Test TOTP URI generation."""

    def test_get_totp_uri_format(self):
        """Should return valid otpauth:// URI."""
        secret = "JBSWY3DPEHPK3PXP"
        email = "test@example.com"

        uri = totp_service.get_totp_uri(secret, email)

        assert uri.startswith("otpauth://totp/")
        assert secret in uri
        assert "test%40example.com" in uri or "test@example.com" in uri

    def test_get_totp_uri_with_custom_issuer(self):
        """Should include custom issuer in URI."""
        secret = "JBSWY3DPEHPK3PXP"
        email = "user@test.com"
        issuer = "MyApp"

        uri = totp_service.get_totp_uri(secret, email, issuer)

        assert "issuer=MyApp" in uri

    def test_get_totp_uri_default_issuer(self):
        """Should use default issuer when not specified."""
        secret = "JBSWY3DPEHPK3PXP"
        email = "user@test.com"

        uri = totp_service.get_totp_uri(secret, email)

        assert "issuer=" in uri


class TestQrCodeGeneration:
    """Test QR code generation."""

    def test_generate_qr_code_returns_data_uri(self):
        """Should return a base64 data URI."""
        uri = "otpauth://totp/Test:user@example.com?secret=JBSWY3DPEHPK3PXP"

        result = totp_service.generate_qr_code(uri)

        assert result.startswith("data:image/png;base64,")

    def test_generate_qr_code_contains_base64_data(self):
        """Should contain valid base64 encoded image data."""
        import base64
        uri = "otpauth://totp/Test:user@example.com?secret=JBSWY3DPEHPK3PXP"

        result = totp_service.generate_qr_code(uri)

        # Extract base64 part and verify it decodes
        base64_part = result.split(",")[1]
        decoded = base64.b64decode(base64_part)

        # PNG files start with specific magic bytes
        assert decoded[:8] == b'\x89PNG\r\n\x1a\n'


class TestTotpVerification:
    """Test TOTP code verification."""

    def test_verify_totp_empty_code(self):
        """Empty code should be rejected."""
        assert totp_service.verify_totp("JBSWY3DPEHPK3PXP", "") is False

    def test_verify_totp_empty_secret(self):
        """Empty secret should be rejected."""
        assert totp_service.verify_totp("", "123456") is False

    def test_verify_totp_none_values(self):
        """None values should be rejected."""
        assert totp_service.verify_totp(None, "123456") is False
        assert totp_service.verify_totp("JBSWY3DPEHPK3PXP", None) is False

    def test_verify_totp_wrong_length(self):
        """Non-6-digit codes should be rejected."""
        secret = "JBSWY3DPEHPK3PXP"
        assert totp_service.verify_totp(secret, "12345") is False
        assert totp_service.verify_totp(secret, "1234567") is False

    def test_verify_totp_non_numeric(self):
        """Non-numeric codes should be rejected."""
        secret = "JBSWY3DPEHPK3PXP"
        assert totp_service.verify_totp(secret, "abcdef") is False
        assert totp_service.verify_totp(secret, "12345a") is False

    def test_verify_totp_strips_spaces(self):
        """Spaces in code should be ignored."""
        secret = "JBSWY3DPEHPK3PXP"
        # Generate a valid code
        import pyotp
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        # Add spaces - should still work
        spaced_code = f"{valid_code[:3]} {valid_code[3:]}"
        assert totp_service.verify_totp(secret, spaced_code) is True

    def test_verify_totp_strips_dashes(self):
        """Dashes in code should be ignored."""
        secret = "JBSWY3DPEHPK3PXP"
        import pyotp
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        # Add dashes - should still work
        dashed_code = f"{valid_code[:3]}-{valid_code[3:]}"
        assert totp_service.verify_totp(secret, dashed_code) is True

    def test_verify_totp_with_valid_code(self):
        """Valid current code should be accepted."""
        secret = totp_service.generate_secret()
        import pyotp
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        assert totp_service.verify_totp(secret, valid_code) is True

    def test_verify_totp_wrong_code(self):
        """Invalid code should be rejected."""
        secret = "JBSWY3DPEHPK3PXP"
        # Very unlikely to be the current valid code
        assert totp_service.verify_totp(secret, "000000") is False


class TestRecoveryCodeGeneration:
    """Test recovery code generation."""

    def test_generate_recovery_codes_default_count(self):
        """Should generate 10 codes by default."""
        plaintext, hashed = totp_service.generate_recovery_codes()

        assert len(plaintext) == 10
        assert len(hashed) == 10

    def test_generate_recovery_codes_custom_count(self):
        """Should generate specified number of codes."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=5)

        assert len(plaintext) == 5
        assert len(hashed) == 5

    def test_generate_recovery_codes_format(self):
        """Plaintext codes should be in XXXX-XXXX-XXXX format."""
        plaintext, _ = totp_service.generate_recovery_codes()

        for code in plaintext:
            parts = code.split("-")
            assert len(parts) == 3
            for part in parts:
                assert len(part) == 4
                assert part.isalnum()

    def test_generate_recovery_codes_hashes_are_sha256(self):
        """Hashed codes should be SHA256 hex strings."""
        _, hashed = totp_service.generate_recovery_codes()

        for h in hashed:
            assert len(h) == 64  # SHA256 hex length
            assert all(c in "0123456789abcdef" for c in h)

    def test_generate_recovery_codes_uniqueness(self):
        """All generated codes should be unique."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=20)

        assert len(set(plaintext)) == 20
        assert len(set(hashed)) == 20

    def test_generate_recovery_codes_hash_matches_plaintext(self):
        """Hashes should correspond to plaintext codes."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=3)

        for i, code in enumerate(plaintext):
            # Remove dashes and lowercase for hashing
            normalized = code.replace("-", "").lower()
            expected_hash = hashlib.sha256(normalized.encode()).hexdigest()
            assert hashed[i] == expected_hash


class TestRecoveryCodeVerification:
    """Test recovery code verification."""

    def test_verify_recovery_code_empty_inputs(self):
        """Empty inputs should be rejected."""
        valid, used_hash = totp_service.verify_recovery_code("", "ABCD-EFGH-IJKL")
        assert valid is False
        assert used_hash is None

        valid, used_hash = totp_service.verify_recovery_code("[]", "")
        assert valid is False
        assert used_hash is None

    def test_verify_recovery_code_none_inputs(self):
        """None inputs should be rejected."""
        valid, used_hash = totp_service.verify_recovery_code(None, "ABCD-EFGH-IJKL")
        assert valid is False
        assert used_hash is None

    def test_verify_recovery_code_invalid_json(self):
        """Invalid JSON should be rejected."""
        valid, used_hash = totp_service.verify_recovery_code("not json", "ABCD-EFGH-IJKL")
        assert valid is False
        assert used_hash is None

    def test_verify_recovery_code_non_list_json(self):
        """Non-list JSON should be rejected."""
        valid, used_hash = totp_service.verify_recovery_code('{"key": "value"}', "ABCD-EFGH-IJKL")
        assert valid is False
        assert used_hash is None

    def test_verify_recovery_code_wrong_length(self):
        """Codes with wrong length should be rejected."""
        hashes = json.dumps(["somehash"])

        valid, used_hash = totp_service.verify_recovery_code(hashes, "ABCD-EFGH")  # Too short
        assert valid is False

        valid, used_hash = totp_service.verify_recovery_code(hashes, "ABCD-EFGH-IJKL-MNOP")  # Too long
        assert valid is False

    def test_verify_recovery_code_valid_code(self):
        """Valid recovery code should be accepted."""
        # Generate codes and verify one works
        plaintext, hashed = totp_service.generate_recovery_codes(count=3)
        stored_json = json.dumps(hashed)

        # Verify the first code works
        valid, used_hash = totp_service.verify_recovery_code(stored_json, plaintext[0])

        assert valid is True
        assert used_hash == hashed[0]

    def test_verify_recovery_code_without_dashes(self):
        """Code without dashes should still work."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=1)
        stored_json = json.dumps(hashed)

        # Remove dashes from code
        code_no_dashes = plaintext[0].replace("-", "")

        valid, used_hash = totp_service.verify_recovery_code(stored_json, code_no_dashes)
        assert valid is True

    def test_verify_recovery_code_case_insensitive(self):
        """Verification should be case-insensitive."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=1)
        stored_json = json.dumps(hashed)

        # Try lowercase
        valid, used_hash = totp_service.verify_recovery_code(stored_json, plaintext[0].lower())
        assert valid is True

    def test_verify_recovery_code_invalid_code(self):
        """Invalid code should be rejected."""
        plaintext, hashed = totp_service.generate_recovery_codes(count=3)
        stored_json = json.dumps(hashed)

        # Try a code that wasn't generated
        valid, used_hash = totp_service.verify_recovery_code(stored_json, "ZZZZ-ZZZZ-ZZZZ")

        assert valid is False
        assert used_hash is None


class TestRemoveUsedRecoveryCode:
    """Test removal of used recovery codes."""

    def test_remove_used_recovery_code(self):
        """Should remove the specified hash from the list."""
        hashes = ["hash1", "hash2", "hash3"]
        stored_json = json.dumps(hashes)

        result = totp_service.remove_used_recovery_code(stored_json, "hash2")

        result_list = json.loads(result)
        assert "hash2" not in result_list
        assert "hash1" in result_list
        assert "hash3" in result_list

    def test_remove_used_recovery_code_nonexistent(self):
        """Should handle non-existent hash gracefully."""
        hashes = ["hash1", "hash2"]
        stored_json = json.dumps(hashes)

        result = totp_service.remove_used_recovery_code(stored_json, "nonexistent")

        result_list = json.loads(result)
        assert len(result_list) == 2

    def test_remove_used_recovery_code_invalid_json(self):
        """Should return original string for invalid JSON."""
        invalid_json = "not json"

        result = totp_service.remove_used_recovery_code(invalid_json, "hash")

        assert result == invalid_json


class TestGetRecoveryCodesCount:
    """Test recovery codes count retrieval."""

    def test_get_recovery_codes_count_normal(self):
        """Should return correct count."""
        hashes = ["hash1", "hash2", "hash3"]
        stored_json = json.dumps(hashes)

        count = totp_service.get_recovery_codes_count(stored_json)

        assert count == 3

    def test_get_recovery_codes_count_empty(self):
        """Empty list should return 0."""
        stored_json = json.dumps([])

        count = totp_service.get_recovery_codes_count(stored_json)

        assert count == 0

    def test_get_recovery_codes_count_none(self):
        """None should return 0."""
        count = totp_service.get_recovery_codes_count(None)

        assert count == 0

    def test_get_recovery_codes_count_invalid_json(self):
        """Invalid JSON should return 0."""
        count = totp_service.get_recovery_codes_count("not json")

        assert count == 0

    def test_get_recovery_codes_count_non_list(self):
        """Non-list JSON should return 0."""
        count = totp_service.get_recovery_codes_count('{"key": "value"}')

        assert count == 0
