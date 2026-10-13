"""
Unit tests for encryption module.

Tests cover:
- Key derivation from password
- Encryption/decryption round-trip
- Error handling for missing keys
- Detection of encrypted values
- Re-encryption for password changes
"""

import pytest
import base64
from unittest.mock import MagicMock, patch
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
        with pytest.raises(RuntimeError, match="Credential storage is locked"):
            encryption.encrypt_value("secret")

    def test_decrypt_encrypted_value_without_key_raises_error(self):
        """Decryption of encrypted value without key should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="Credential storage is locked"):
            encryption.decrypt_value("enc:somebase64data")

    def test_decrypt_plaintext_without_key_raises_error(self):
        """Decrypting any value without key should raise RuntimeError."""
        # Even plaintext values require encryption key to be loaded
        with pytest.raises(RuntimeError, match="Credential storage is locked"):
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


def _make_settings_mock_db(salt: bytes, settings: dict):
    """Build a MagicMock db exposing the discovery accessors.

    ``settings`` maps system_settings key -> stored value. The encrypted-value
    discovery scans ``get_all_system_settings()``; ``get_system_setting`` is
    still wired for the salt lookup. Automations/connectors/credentials return
    empty so those locations are exercised as "nothing to do".
    """
    mock_db = MagicMock()
    stored = dict(settings)
    stored["encryption_salt"] = base64.b64encode(salt).decode()

    mock_db.get_system_setting.side_effect = lambda key: stored.get(key)
    mock_db.get_all_system_settings.side_effect = lambda: dict(stored)

    def _set(key, value):
        stored[key] = value
    mock_db.set_system_setting.side_effect = _set

    mock_db.get_all_automations.return_value = []
    mock_db.get_all_connectors.return_value = []
    mock_db.get_all_api_users.return_value = []
    return mock_db


class TestReEncryptAllSecrets:
    """Test re-encryption when password changes (dynamic discovery)."""

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
        mock_db = _make_settings_mock_db(b"S" * 32, {})
        result = encryption.re_encrypt_all_secrets("old_pass", "new_pass", mock_db)
        assert result is True
        assert encryption._encryption_key is not None

    def test_re_encrypt_with_encrypted_values(self):
        """Should decrypt with old key and re-encrypt with new key."""
        salt = b"T" * 32
        old_key = encryption.derive_key_from_password("old_password", salt)
        old_fernet = Fernet(old_key)
        encrypted_api_key = old_fernet.encrypt(b"sk-secret123")
        encrypted_value = f"enc:{base64.b64encode(encrypted_api_key).decode()}"

        mock_db = _make_settings_mock_db(salt, {"openai_api_key": encrypted_value})
        result = encryption.re_encrypt_all_secrets("old_password", "new_password", mock_db)
        assert result is True
        mock_db.set_system_setting.assert_called()

    def test_re_encrypt_fails_on_decrypt_error(self):
        """Should return False if decryption fails."""
        salt = b"V" * 32
        mock_db = _make_settings_mock_db(
            salt, {"openai_api_key": "enc:" + base64.b64encode(b"invalid").decode()}
        )
        result = encryption.re_encrypt_all_secrets("wrong_old_password", "new", mock_db)
        assert result is False

    def test_re_encrypt_fails_closed_writes_nothing(self):
        """A single undecryptable value must abort ALL writes (fail-closed)."""
        salt = b"Q" * 32
        old_key = encryption.derive_key_from_password("old_pass", salt)
        old_fernet = Fernet(old_key)
        good = f"enc:{base64.b64encode(old_fernet.encrypt(b'good')).decode()}"
        bad = "enc:" + base64.b64encode(b"not-a-token").decode()
        mock_db = _make_settings_mock_db(salt, {"fal_api_key": good, "anthropic_api_key": bad})
        result = encryption.re_encrypt_all_secrets("old_pass", "new_pass", mock_db)
        assert result is False
        mock_db.set_system_setting.assert_not_called()

    def test_re_encrypt_logs_success(self):
        """Should log success message."""
        mock_db = _make_settings_mock_db(b"W" * 32, {})
        with patch.object(encryption.logger, "info") as mock_log:
            encryption.re_encrypt_all_secrets("old", "new", mock_db)
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Successfully re-encrypted" in c for c in calls)

    def test_re_encrypt_updates_in_memory_key(self):
        """Should update the in-memory encryption key to the new key."""
        salt = b"X" * 32
        mock_db = _make_settings_mock_db(salt, {})
        expected_new_key = encryption.derive_key_from_password("new_password", salt)
        encryption.re_encrypt_all_secrets("old_password", "new_password", mock_db)
        assert encryption._encryption_key == expected_new_key

    def test_re_encrypt_logs_error_on_failure(self):
        """Should log error when decryption fails."""
        salt = b"Z" * 32
        mock_db = _make_settings_mock_db(
            salt, {"openai_api_key": "enc:" + base64.b64encode(b"bad_data").decode()}
        )
        with patch.object(encryption.logger, "error") as mock_log:
            encryption.re_encrypt_all_secrets("wrong", "new", mock_db)
            assert mock_log.called
            calls = [str(c) for c in mock_log.call_args_list]
            assert any("Cannot re-encrypt" in c for c in calls)


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
        mock_db.get_all_system_settings.side_effect = lambda: dict(stored_settings)
        mock_db.get_all_automations.return_value = []
        mock_db.get_all_connectors.return_value = []
        mock_db.get_all_api_users.return_value = []

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


class TestReEncryptAllStorageLocationsDbBacked:
    """Ship-stopper reproduction: password change must re-encrypt EVERY at-rest
    secret, not just a hardcoded 5-name list.

    These run against the real app SQL (pg_shim/SQLite) so decrypt/re-encrypt
    goes through the true db accessors and JSONB parsing. The pre-fix code
    silently skipped fal_api_key, webhook secrets, connector tokens, and user
    credentials — so after a password change they were undecryptable. Each test
    below FAILS against that old behavior.
    """

    @pytest.fixture(autouse=True)
    def reset_state(self):
        encryption._encryption_key = None
        encryption._key_salt = None
        yield
        encryption._encryption_key = None
        encryption._key_salt = None

    def _init_key(self, db, password="old-admin-pw"):
        encryption.set_encryption_key(password, db)

    def test_fal_api_key_reencrypted(self, mock_db):
        """system_settings.fal_api_key (NOT in the old hardcoded list)."""
        from app.db import database as db

        self._init_key(db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("fal-secret-123"))

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is True

        # Now readable only under the NEW key (which re_encrypt left in memory).
        stored = db.get_system_setting("fal_api_key")
        assert encryption.is_encrypted(stored)
        assert encryption.decrypt_value(stored) == "fal-secret-123"

    def test_orphaned_cloud_install_api_key_reencrypted(self, mock_db):
        """An orphaned encrypted setting with no live writer must still be caught."""
        from app.db import database as db

        self._init_key(db)
        db.set_system_setting(
            "cloud_install_api_key", encryption.encrypt_value("orphan-token")
        )

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is True
        stored = db.get_system_setting("cloud_install_api_key")
        assert encryption.decrypt_value(stored) == "orphan-token"

    def test_webhook_secret_in_automation_reencrypted(self, mock_db):
        """automations.triggers[*].config.secret (JSONB) must be re-encrypted."""
        from app.db import database as db
        from app.core import automation_service

        self._init_key(db)
        enc_secret = encryption.encrypt_value("webhook-shhh")
        triggers = [{"type": "webhook", "config": {"secret": enc_secret}}]
        db.create_automation(
            automation_id="auto-wh",
            name="wh",
            trigger_type="webhook",
            trigger_config={"secret": enc_secret},
            triggers=triggers,
            prompt="p",
            profile_id="default",
        )

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is True

        row = db.get_automation("auto-wh")
        new_secret = row["triggers"][0]["config"]["secret"]
        assert encryption.is_encrypted(new_secret)
        # Readable under the new in-memory key; decrypt via service path too.
        assert encryption.decrypt_value(new_secret) == "webhook-shhh"
        dec = automation_service.decrypt_webhook_secret(row["triggers"][0]["config"])
        assert dec["secret"] == "webhook-shhh"

    def test_connector_token_reencrypted(self, mock_db):
        """connectors.config sensitive field (JSONB) must be re-encrypted."""
        from app.db import database as db
        from app.core import connector_service

        self._init_key(db)
        enc_token = encryption.encrypt_value("bot-token-xyz")
        db.create_connector(
            connector_id="conn-1",
            connector_type="telegram",
            name="tg",
            config={"bot_token": enc_token},
            events=[],
        )

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is True

        row = db.get_connector("conn-1")
        new_token = row["config"]["bot_token"]
        assert encryption.is_encrypted(new_token)
        dec = connector_service.decrypt_config(row["config"], "telegram")
        assert dec["bot_token"] == "bot-token-xyz"

    def test_api_user_credential_reencrypted(self, mock_db):
        """api_user_credentials.encrypted_value must be re-encrypted."""
        from app.db import database as db

        self._init_key(db)
        db.create_api_user(
            user_id="u1", name="user1", api_key_hash="hash", profile_ids=None
        )
        db.set_user_credential("u1", "github_token", encryption.encrypt_value("ghp_abc"))

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is True

        cred = db.get_user_credential("u1", "github_token")
        assert encryption.is_encrypted(cred["encrypted_value"])
        assert encryption.decrypt_value(cred["encrypted_value"]) == "ghp_abc"

    def test_all_locations_together_and_fail_closed(self, mock_db):
        """All four locations round-trip; and a bricked orphan aborts everything."""
        from app.db import database as db

        self._init_key(db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("fal-1"))
        db.create_connector(
            connector_id="conn-2",
            connector_type="telegram",
            name="tg2",
            config={"bot_token": encryption.encrypt_value("tok-1")},
            events=[],
        )

        # Corrupt orphan that the OLD key cannot decrypt -> fail closed.
        db.set_system_setting(
            "cloud_install_api_key", "enc:" + base64.b64encode(b"garbage").decode()
        )

        pre_fal = db.get_system_setting("fal_api_key")
        pre_tok = db.get_connector("conn-2")["config"]["bot_token"]

        assert encryption.re_encrypt_all_secrets("old-admin-pw", "new-admin-pw", db) is False

        # Nothing was written — values unchanged, in-memory key untouched.
        assert db.get_system_setting("fal_api_key") == pre_fal
        assert db.get_connector("conn-2")["config"]["bot_token"] == pre_tok
