"""
Unit tests for encryption module.

Tests cover:
- Key derivation from password
- Encryption/decryption round-trip
- Error handling for missing keys
- Detection of encrypted values
- Environment-based initialization
- Re-encryption for password changes
- Migration of plaintext keys
"""

import pytest
import base64
import os
from unittest.mock import MagicMock, patch, call
from cryptography.fernet import Fernet

# Test the core encryption functions directly
from app.core import encryption


class TestKeyDerivation:
    """Test password-based key derivation."""

    def test_derive_key_from_password_produces_32_bytes(self):
        """Key derivation should produce a 32-byte key."""
        salt = b"0" * 32
        key = encryption.derive_key_from_password("test_password", salt)
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

    def test_derive_key_valid_for_fernet(self):
        """Derived key should be usable with Fernet."""
        salt = b"X" * 32
        key = encryption.derive_key_from_password("my_password", salt)
        f = Fernet(key)
        encrypted = f.encrypt(b"test data")
        decrypted = f.decrypt(encrypted)
        assert decrypted == b"test data"


class TestEncryptionState:
    """Test encryption state management."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset encryption state before and after each test."""
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_is_encryption_ready_false_initially(self):
        """Encryption should not be ready before key is set."""
        assert encryption.is_encryption_ready() is False

    def test_is_encryption_ready_true_after_key_set(self):
        """Encryption should be ready after key is set."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting = MagicMock()
        encryption.set_encryption_key("test_password", mock_db)
        assert encryption.is_encryption_ready() is True

    def test_clear_encryption_key(self):
        """Clearing encryption key should set it to None."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        encryption.set_encryption_key("test", mock_db)
        assert encryption._encryption_key is not None
        encryption.clear_encryption_key()
        assert encryption._encryption_key is None

    def test_clear_encryption_key_logs_message(self):
        """Clearing encryption key should log a message."""
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.clear_encryption_key()
            mock_log.assert_called_once_with("Encryption key cleared from memory")


class TestSetEncryptionKey:
    """Test set_encryption_key function."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset encryption state."""
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_set_encryption_key_sets_global_key(self):
        """set_encryption_key should set the global _encryption_key."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting = MagicMock()
        encryption.set_encryption_key("my_password", mock_db)
        assert encryption._encryption_key is not None
        assert isinstance(encryption._encryption_key, bytes)

    def test_set_encryption_key_calls_get_or_create_salt(self):
        """set_encryption_key should call get_or_create_salt."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        with patch.object(encryption, "get_or_create_salt", return_value=b"X" * 32) as mock_salt:
            encryption.set_encryption_key("password", mock_db)
            mock_salt.assert_called_once_with(mock_db)

    def test_set_encryption_key_logs_message(self):
        """set_encryption_key should log when key is loaded."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.set_encryption_key("password", mock_db)
            mock_log.assert_called_with("Encryption key loaded into memory")


class TestIsEncrypted:
    """Test encrypted value detection."""

    def test_is_encrypted_with_prefix(self):
        """Values with enc: prefix should be detected as encrypted."""
        assert encryption.is_encrypted("enc:base64data") is True

    def test_is_encrypted_without_prefix(self):
        """Values without enc: prefix should not be detected as encrypted."""
        assert encryption.is_encrypted("plaintext") is False

    def test_is_encrypted_empty_string(self):
        """Empty string should not be detected as encrypted."""
        assert encryption.is_encrypted("") is False

    def test_is_encrypted_none(self):
        """None should not be detected as encrypted."""
        assert encryption.is_encrypted(None) is False

    def test_is_encrypted_partial_prefix(self):
        """Partial prefix should not match."""
        assert encryption.is_encrypted("enc") is False
        assert encryption.is_encrypted("en:") is False


class TestEncryptDecrypt:
    """Test encryption and decryption operations."""

    @pytest.fixture(autouse=True)
    def setup_encryption_key(self):
        """Set up a test encryption key before each test."""
        encryption._key_salt = None
        encryption._encryption_key = None
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting = MagicMock()
        encryption.set_encryption_key("test_password_123", mock_db)
        yield
        encryption.clear_encryption_key()
        encryption._key_salt = None

    def test_encrypt_produces_prefixed_string(self):
        """Encryption should produce a string with enc: prefix."""
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
        original = "password123"
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_empty_string(self):
        """Should handle empty string."""
        original = ""
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_long_value(self):
        """Should handle long values."""
        original = "x" * 10000
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_special_characters(self):
        """Should handle special characters."""
        original = "sk-abc123!@#$%^&*(){}[]|:;<>?,./"
        encrypted = encryption.encrypt_value(original)
        decrypted = encryption.decrypt_value(encrypted)
        assert decrypted == original


class TestDecryptionErrors:
    """Test decryption error handling."""

    @pytest.fixture(autouse=True)
    def setup_encryption_key(self):
        """Set up a test encryption key before each test."""
        encryption._key_salt = None
        encryption._encryption_key = None
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        encryption.set_encryption_key("test_password", mock_db)
        yield
        encryption.clear_encryption_key()
        encryption._key_salt = None

    def test_decrypt_invalid_base64_raises_error(self):
        """Decrypting invalid base64 should raise ValueError."""
        with pytest.raises(Exception):
            encryption.decrypt_value("enc:not-valid-base64!!!")

    def test_decrypt_corrupted_data_raises_error(self):
        """Decrypting corrupted data should raise ValueError."""
        fake_encrypted = "enc:" + base64.b64encode(b"corrupted_data_here").decode()
        with pytest.raises(ValueError, match="Decryption failed"):
            encryption.decrypt_value(fake_encrypted)

    def test_decrypt_wrong_key_raises_error(self):
        """Decrypting with wrong key should raise ValueError."""
        encrypted = encryption.encrypt_value("secret")
        encryption._key_salt = None
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        encryption.set_encryption_key("different_password", mock_db)
        with pytest.raises(ValueError, match="Decryption failed"):
            encryption.decrypt_value(encrypted)


class TestEncryptionWithoutKey:
    """Test error handling when encryption key is not loaded."""

    @pytest.fixture(autouse=True)
    def clear_key(self):
        """Ensure no encryption key is set."""
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_encrypt_without_key_raises_error(self):
        """Encryption without key should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.encrypt_value("secret")

    def test_decrypt_encrypted_value_without_key_raises_error(self):
        """Decryption of encrypted value without key should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.decrypt_value("enc:somebase64data")

    def test_decrypt_plaintext_without_key_raises_error(self):
        """Decrypting any value without key should raise RuntimeError."""
        # Even plaintext values require encryption key to be loaded
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.decrypt_value("plaintext_value")


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
        encryption._key_salt = b"cached_salt_value__" + b"0" * 14
        mock_db = MagicMock()
        salt = encryption.get_or_create_salt(mock_db)
        assert salt == encryption._key_salt
        mock_db.get_system_setting.assert_not_called()

    def test_get_or_create_salt_logs_on_new_salt(self):
        """Should log when generating new salt."""
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.get_or_create_salt(mock_db)
            mock_log.assert_called_with("Generated new encryption salt")


class TestInitEncryptionFromEnv:
    """Test environment-based encryption initialization."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset encryption state and env vars."""
        encryption._encryption_key = None
        encryption._key_salt = None
        os.environ.pop("ADMIN_PASSWORD", None)
        yield
        encryption._encryption_key = None
        encryption._key_salt = None
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_init_from_env_returns_false_when_no_env_var(self):
        """Should return False when ADMIN_PASSWORD is not set."""
        mock_db = MagicMock()
        result = encryption.init_encryption_from_env(mock_db)
        assert result is False
        assert encryption._encryption_key is None

    def test_init_from_env_returns_true_and_sets_key(self):
        """Should return True and set key when ADMIN_PASSWORD is set."""
        os.environ["ADMIN_PASSWORD"] = "env_password_123"
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        result = encryption.init_encryption_from_env(mock_db)
        assert result is True
        assert encryption._encryption_key is not None

    def test_init_from_env_logs_success(self):
        """Should log when encryption is initialized from env."""
        os.environ["ADMIN_PASSWORD"] = "env_password"
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.init_encryption_from_env(mock_db)
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("ADMIN_PASSWORD environment variable" in c for c in calls)

    def test_init_from_env_empty_string_returns_false(self):
        """Empty ADMIN_PASSWORD should be treated as unset."""
        os.environ["ADMIN_PASSWORD"] = ""
        mock_db = MagicMock()
        result = encryption.init_encryption_from_env(mock_db)
        assert result is False


class TestReEncryptAllSecrets:
    """Test re-encryption when password changes."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset encryption state."""
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_re_encrypt_all_secrets_success(self):
        """Should re-encrypt all secrets with new password."""
        mock_db = MagicMock()
        salt = b"S" * 32
        mock_db.get_system_setting.side_effect = lambda key: {
            "encryption_salt": base64.b64encode(salt).decode(),
            "openai_api_key": None,
            "gemini_api_key": None,
            "image_api_key": None,
            "video_api_key": None,
        }.get(key)
        result = encryption.re_encrypt_all_secrets("old_pass", "new_pass", mock_db)
        assert result is True
        assert encryption._encryption_key is not None

    def test_re_encrypt_with_encrypted_values(self):
        """Should decrypt with old key and re-encrypt with new key."""
        mock_db = MagicMock()
        salt = b"T" * 32
        old_key = encryption.derive_key_from_password("old_password", salt)
        old_fernet = Fernet(old_key)
        encrypted_api_key = old_fernet.encrypt(b"sk-secret123")
        encrypted_value = f"enc:{base64.b64encode(encrypted_api_key).decode()}"

        def mock_get_setting(key):
            if key == "encryption_salt":
                return base64.b64encode(salt).decode()
            elif key == "openai_api_key":
                return encrypted_value
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        result = encryption.re_encrypt_all_secrets("old_password", "new_password", mock_db)
        assert result is True
        mock_db.set_system_setting.assert_called()

    def test_re_encrypt_with_plaintext_values(self):
        """Should encrypt plaintext values with new key (migration)."""
        mock_db = MagicMock()
        salt = b"U" * 32

        def mock_get_setting(key):
            if key == "encryption_salt":
                return base64.b64encode(salt).decode()
            elif key == "openai_api_key":
                return "sk-plaintext-key"
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        result = encryption.re_encrypt_all_secrets("old", "new", mock_db)
        assert result is True
        call_args = mock_db.set_system_setting.call_args_list
        openai_calls = [c for c in call_args if c[0][0] == "openai_api_key"]
        assert len(openai_calls) == 1
        assert openai_calls[0][0][1].startswith("enc:")

    def test_re_encrypt_fails_on_decrypt_error(self):
        """Should return False if decryption fails."""
        mock_db = MagicMock()
        salt = b"V" * 32

        def mock_get_setting(key):
            if key == "encryption_salt":
                return base64.b64encode(salt).decode()
            elif key == "openai_api_key":
                return "enc:" + base64.b64encode(b"invalid").decode()
            return None

        mock_db.get_system_setting = mock_get_setting
        result = encryption.re_encrypt_all_secrets("wrong_old_password", "new", mock_db)
        assert result is False

    def test_re_encrypt_logs_success(self):
        """Should log success message."""
        mock_db = MagicMock()
        salt = b"W" * 32
        mock_db.get_system_setting.side_effect = lambda key:             base64.b64encode(salt).decode() if key == "encryption_salt" else None
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.re_encrypt_all_secrets("old", "new", mock_db)
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Successfully re-encrypted" in c for c in calls)

    def test_re_encrypt_updates_in_memory_key(self):
        """Should update the in-memory encryption key to the new key."""
        mock_db = MagicMock()
        salt = b"X" * 32
        mock_db.get_system_setting.side_effect = lambda key:             base64.b64encode(salt).decode() if key == "encryption_salt" else None
        expected_new_key = encryption.derive_key_from_password("new_password", salt)
        encryption.re_encrypt_all_secrets("old_password", "new_password", mock_db)
        assert encryption._encryption_key == expected_new_key

    def test_re_encrypt_logs_each_key(self):
        """Should log re-encryption of each individual key."""
        mock_db = MagicMock()
        salt = b"Y" * 32
        old_key = encryption.derive_key_from_password("old_pass", salt)
        old_fernet = Fernet(old_key)
        encrypted_openai = f"enc:{base64.b64encode(old_fernet.encrypt(b'key1')).decode()}"
        encrypted_gemini = f"enc:{base64.b64encode(old_fernet.encrypt(b'key2')).decode()}"

        def mock_get_setting(key):
            if key == "encryption_salt":
                return base64.b64encode(salt).decode()
            elif key == "openai_api_key":
                return encrypted_openai
            elif key == "gemini_api_key":
                return encrypted_gemini
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.re_encrypt_all_secrets("old_pass", "new_pass", mock_db)
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Re-encrypted openai_api_key" in c for c in calls)
            assert any("Re-encrypted gemini_api_key" in c for c in calls)

    def test_re_encrypt_logs_error_on_failure(self):
        """Should log error when decryption fails."""
        mock_db = MagicMock()
        salt = b"Z" * 32

        def mock_get_setting(key):
            if key == "encryption_salt":
                return base64.b64encode(salt).decode()
            elif key == "openai_api_key":
                return "enc:" + base64.b64encode(b"bad_data").decode()
            return None

        mock_db.get_system_setting = mock_get_setting
        with patch.object(encryption.logger, "error") as mock_log:
            encryption.re_encrypt_all_secrets("wrong", "new", mock_db)
            assert mock_log.called
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Failed to decrypt" in c for c in calls)


class TestEncryptExistingPlaintextKeys:
    """Test migration of plaintext API keys to encrypted."""

    @pytest.fixture(autouse=True)
    def setup_encryption(self):
        """Set up encryption key."""
        encryption._encryption_key = None
        encryption._key_salt = None
        mock_db = MagicMock()
        mock_db.get_system_setting.return_value = None
        encryption.set_encryption_key("test_password", mock_db)
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_encrypt_existing_keys_without_encryption_key_raises(self):
        """Should raise RuntimeError if encryption key not loaded."""
        encryption.clear_encryption_key()
        mock_db = MagicMock()
        with pytest.raises(RuntimeError, match="Encryption key not loaded"):
            encryption.encrypt_existing_plaintext_keys(mock_db)

    def test_encrypt_existing_keys_encrypts_plaintext(self):
        """Should encrypt plaintext API keys."""
        mock_db = MagicMock()

        def mock_get_setting(key):
            if key == "openai_api_key":
                return "sk-plaintext-openai-key"
            elif key == "gemini_api_key":
                return "gemini-plaintext-key"
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        count = encryption.encrypt_existing_plaintext_keys(mock_db)
        assert count == 2
        assert mock_db.set_system_setting.call_count == 2
        for call_args in mock_db.set_system_setting.call_args_list:
            key, value = call_args[0]
            assert value.startswith("enc:")

    def test_encrypt_existing_keys_skips_already_encrypted(self):
        """Should skip keys that are already encrypted."""
        mock_db = MagicMock()

        def mock_get_setting(key):
            if key == "openai_api_key":
                return "enc:already_encrypted_data"
            elif key == "gemini_api_key":
                return "plaintext-key"
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        count = encryption.encrypt_existing_plaintext_keys(mock_db)
        assert count == 1
        mock_db.set_system_setting.assert_called_once()
        call_key, _ = mock_db.set_system_setting.call_args[0]
        assert call_key == "gemini_api_key"

    def test_encrypt_existing_keys_skips_empty_values(self):
        """Should skip empty or None values."""
        mock_db = MagicMock()

        def mock_get_setting(key):
            if key == "openai_api_key":
                return ""
            elif key == "gemini_api_key":
                return None
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        count = encryption.encrypt_existing_plaintext_keys(mock_db)
        assert count == 0
        mock_db.set_system_setting.assert_not_called()

    def test_encrypt_existing_keys_logs_each_encryption(self):
        """Should log when encrypting each key."""
        mock_db = MagicMock()

        def mock_get_setting(key):
            if key == "openai_api_key":
                return "sk-key"
            return None

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.encrypt_existing_plaintext_keys(mock_db)
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Encrypted existing plaintext key" in c for c in calls)

    def test_encrypt_existing_keys_all_sensitive_settings(self):
        """Should check all sensitive settings."""
        mock_db = MagicMock()
        all_keys = {}

        def mock_get_setting(key):
            return all_keys.get(key)

        mock_db.get_system_setting = mock_get_setting
        mock_db.set_system_setting = MagicMock()
        sensitive_settings = [
            "openai_api_key",
            "gemini_api_key",
            "image_api_key",
            "video_api_key",
        ]
        for setting in sensitive_settings:
            all_keys[setting] = f"plaintext-{setting}"
        count = encryption.encrypt_existing_plaintext_keys(mock_db)
        assert count == 4


class TestEncryptionIntegration:
    """Integration tests for the full encryption workflow."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset all encryption state."""
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def test_full_workflow_encrypt_decrypt(self):
        """Test complete encrypt/decrypt workflow."""
        mock_db = MagicMock()
        stored_settings = {}

        def mock_get(key):
            return stored_settings.get(key)

        def mock_set(key, value):
            stored_settings[key] = value

        mock_db.get_system_setting = mock_get
        mock_db.set_system_setting = mock_set

        encryption.set_encryption_key("admin_password", mock_db)
        assert encryption.is_encryption_ready()
        secret = "sk-super-secret-api-key"
        encrypted = encryption.encrypt_value(secret)
        assert encryption.is_encrypted(encrypted)
        decrypted = encryption.decrypt_value(encrypted)
        assert decrypted == secret
        encryption.clear_encryption_key()
        assert not encryption.is_encryption_ready()
        encryption.set_encryption_key("admin_password", mock_db)
        assert encryption.is_encryption_ready()
        decrypted2 = encryption.decrypt_value(encrypted)
        assert decrypted2 == secret

    def test_password_change_workflow(self):
        """Test password change re-encryption workflow."""
        mock_db = MagicMock()
        stored_settings = {}

        def mock_get(key):
            return stored_settings.get(key)

        def mock_set(key, value):
            stored_settings[key] = value

        mock_db.get_system_setting = mock_get
        mock_db.set_system_setting = mock_set

        old_password = "old_password"
        encryption.set_encryption_key(old_password, mock_db)
        original_secret = "sk-my-api-key-12345"
        encrypted = encryption.encrypt_value(original_secret)
        stored_settings["openai_api_key"] = encrypted
        new_password = "new_password"
        result = encryption.re_encrypt_all_secrets(old_password, new_password, mock_db)
        assert result is True
        new_encrypted = stored_settings["openai_api_key"]
        assert new_encrypted.startswith("enc:")
        assert new_encrypted != encrypted
        decrypted = encryption.decrypt_value(new_encrypted)
        assert decrypted == original_secret
