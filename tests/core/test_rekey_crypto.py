"""Unit tests for the rekey primitives (Phase 4.2).

These exercise the pure crypto path scripts/rekey.py depends on — that a secret
encrypted under an OLD admin-password-derived key, when decrypted with the old
key and re-encrypted under a NEW key, is (a) undecryptable with the old key and
(b) decryptable with the new key. No DB or live server involved: the same
salt/KDF/Fernet primitives the script imports from app.core.encryption.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.core.encryption import derive_key_from_password, is_encrypted


SALT = b"\x00" * 32  # fixed salt; the real one comes from the DB, irrelevant here


def _encrypt(key: bytes, plaintext: str) -> str:
    enc = Fernet(key).encrypt(plaintext.encode())
    return f"enc:{base64.b64encode(enc).decode()}"


def _decrypt(key: bytes, value: str) -> str:
    return Fernet(key).decrypt(base64.b64decode(value[4:])).decode()


class TestRekeyRoundTrip:
    def test_new_key_decrypts_reencrypted_value(self):
        old_key = derive_key_from_password("old-pw", SALT)
        new_key = derive_key_from_password("new-pw", SALT)

        secret = "sk-super-secret-value"
        old_blob = _encrypt(old_key, secret)
        assert is_encrypted(old_blob)

        # Simulate the rekey: decrypt with old, re-encrypt with new.
        recovered = _decrypt(old_key, old_blob)
        assert recovered == secret
        new_blob = _encrypt(new_key, recovered)

        # New key round-trips.
        assert _decrypt(new_key, new_blob) == secret

    def test_old_key_cannot_decrypt_new_blob(self):
        old_key = derive_key_from_password("old-pw", SALT)
        new_key = derive_key_from_password("new-pw", SALT)
        new_blob = _encrypt(new_key, "value")

        try:
            _decrypt(old_key, new_blob)
            assert False, "old key must not decrypt a new-key blob"
        except InvalidToken:
            pass

    def test_wrong_old_password_fails_to_decrypt(self):
        """The verify step: a wrong old password derives a key that can't decrypt."""
        real_old = derive_key_from_password("correct-old", SALT)
        wrong_old = derive_key_from_password("wrong-old", SALT)
        blob = _encrypt(real_old, "value")

        try:
            _decrypt(wrong_old, blob)
            assert False, "wrong old password must fail decryption"
        except InvalidToken:
            pass

    def test_derive_is_deterministic_for_same_inputs(self):
        assert derive_key_from_password("pw", SALT) == derive_key_from_password("pw", SALT)
        assert derive_key_from_password("pw", SALT) != derive_key_from_password("pw2", SALT)


class TestRekeyModuleImports:
    def test_module_imports_and_exposes_main(self):
        from scripts import rekey
        assert hasattr(rekey, "main")
        assert hasattr(rekey, "rekey")
        assert callable(rekey.main)

    def test_sensitive_settings_list_matches_encryption_module(self):
        """The script's list must stay in lockstep with re_encrypt_all_secrets."""
        from scripts.rekey import _sensitive_settings
        expected = {
            "openai_api_key",
            "gemini_api_key",
            "image_api_key",
            "video_api_key",
            "anthropic_api_key",
        }
        assert set(_sensitive_settings()) == expected

    def test_server_probe_returns_none_when_nothing_listening(self, monkeypatch):
        """With no server, the probe reports clear (None)."""
        from scripts import rekey

        # Point at a very unlikely port so the connection is refused fast.
        class _S:
            host = "127.0.0.1"
            port = 59999
        monkeypatch.setattr("app.core.config.settings", _S(), raising=False)
        assert rekey._server_appears_up() is None
