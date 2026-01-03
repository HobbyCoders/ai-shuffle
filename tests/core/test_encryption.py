"""
Unit tests for encryption module.

Tests cover:
- Key derivation from password
- Encryption/decryption round-trip
- Error handling for missing keys
- Detection of encrypted values
"""

import pytest
import base64
from unittest.mock import MagicMock, patch

# Test the core encryption functions directly
from app.core import encryption


class TestKeyDerivation:
    """Test password-based key derivation."""

    def test_derive_key_from_password_produces_32_bytes(self):
        """Key derivation should produce a 32-byte key."""
        salt = b"0" * 32
        key = encryption.derive_key_from_password("test_password", salt)

        # Fernet keys are base64-encoded, so decode first
        decoded = base64.urlsafe_b64decode(key)
        assert len(decoded) == 32

    def test_derive_key_is_deterministic(self):
        """Same password + salt should produce same key."""
        salt = b"0" * 32
        key1 = encryption.derive_key_from_password("test_password", salt)
        key2 = encryption.derive_key_from_password("test_password", salt)

        assert key1 == key2

    def test_different_passwords_produce_different_keys(self):
        """Different passwords should produce different keys."""
        salt = b"0" * 32
        key1 = encryption.derive_key_from_password("password1", salt)
        key2 = encryption.derive_key_from_password("password2", salt)

        assert key1 != key2

    def test_different_salts_produce_different_keys(self):
        """Different salts should produce different keys."""
        key1 = encryption.derive_key_from_password("same_password", b"A" * 32)
        key2 = encryption.derive_key_from_password("same_password", b"B" * 32)

        assert key1 != key2


class TestEncryptionState:
    """Test encryption state management."""

    def test_is_encryption_ready_false_initially(self):
        """Encryption should not be ready before key is set."""
        # Clear any existing key
        encryption.clear_encryption_key()
        assert encryption.is_encryption_ready() is False

    def test_clear_encryption_key(self):
        """Clearing encryption key should set it to None."""
        encryption.clear_encryption_key()
        assert encryption._encryption_key is None


class TestIsEncrypted:
    """Test encrypted value detection."""

    def test_is_encrypted_with_prefix(self):
        """Values with 'enc:' prefix should be detected as encrypted."""
        assert encryption.is_encrypted("enc:base64data") is True

    def test_is_encrypted_without_prefix(self):
        """Values without 'enc:' prefix should not be detected as encrypted."""
        assert encryption.is_encrypted("plaintext") is False

    def test_is_encrypted_empty_string(self):
        """Empty string should not be detected as encrypted."""
        assert encryption.is_encrypted("") is False

    def test_is_encrypted_none(self):
        """None should not be detected as encrypted."""
        assert encryption.is_encrypted(None) is False


class TestEncryptDecrypt:
    """Test encryption and decryption operations."""

    @pytest.fixture(autouse=True)
    def setup_encryption_key(self):
        """Set up a test encryption key before each test."""
        # Reset the cached salt
        encryption._key_salt = None

        # Create a mock db_module
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting = MagicMock()

        # Set up encryption with a test password
        encryption.set_encryption_key("test_password_123", mock_db)
        yield
        encryption.clear_encryption_key()

    def test_encrypt_produces_prefixed_string(self):
        """Encryption should produce a string with 'enc:' prefix."""
        result = encryption.encrypt_value("secret_data")
        assert result.startswith("enc:")

    def test_encrypt_decrypt_roundtrip(self):
        """Decrypting an encrypted value should return the original."""
        original = "my_secret_api_key"
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)

        assert decrypted == original

    def test_encrypt_different_values_produce_different_ciphertext(self):
        """Different values should produce different ciphertext."""
        enc1 = encryption.encrypt_value("secret1")
        enc2 = encryption.encrypt_value("secret2")

        assert enc1 != enc2

    def test_decrypt_unencrypted_value_returns_as_is(self):
        """Decrypting an unencrypted value should return it unchanged."""
        plaintext = "not_encrypted"
        result = encryption.decrypt_value(plaintext)

        assert result == plaintext

    def test_encrypt_unicode_value(self):
        """Should handle Unicode values correctly."""
        original = "パスワード123"
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)

        assert decrypted == original

    def test_encrypt_empty_string(self):
        """Should handle empty string."""
        original = ""
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)

        assert decrypted == original


class TestEncryptionWithoutKey:
    """Test error handling when encryption key is not loaded."""

    @pytest.fixture(autouse=True)
    def clear_key(self):
        """Ensure no encryption key is set."""
        encryption.clear_encryption_key()
        yield
        encryption.clear_encryption_key()

    def test_encrypt_without_key_raises_error(self):
        """Encryption without key should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.encrypt_value("secret")

    def test_decrypt_encrypted_value_without_key_raises_error(self):
        """Decryption of encrypted value without key should raise RuntimeError."""
        # This is an encrypted-looking value (has enc: prefix)
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.decrypt_value("enc:somebase64data")


class TestSaltManagement:
    """Test encryption salt management."""

    @pytest.fixture(autouse=True)
    def reset_salt(self):
        """Reset global salt before each test."""
        encryption._key_salt = None
        yield
        encryption._key_salt = None

    def test_get_or_create_salt_creates_new_salt(self):
        """Should create a new salt if none exists."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting = MagicMock()

        salt = encryption.get_or_create_salt(mock_db)

        assert salt is not None
        assert len(salt) == 32
        mock_db.set_system_setting.assert_called_once()

    def test_get_or_create_salt_uses_existing_salt(self):
        """Should use existing salt from database."""
        existing_salt = base64.b64encode(b"X" * 32).decode()
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = existing_salt

        salt = encryption.get_or_create_salt(mock_db)

        assert salt == b"X" * 32
        mock_db.set_system_setting.assert_not_called()

    def test_get_or_create_salt_uses_cached_salt(self):
        """Should use cached salt if available."""
        # Set a cached salt
        encryption._key_salt = b"cached_salt_value__" + b"0" * 14

        mock_db = MagicMock()
        salt = encryption.get_or_create_salt(mock_db)

        assert salt == encryption._key_salt
        mock_db.get_system_setting.assert_not_called()
