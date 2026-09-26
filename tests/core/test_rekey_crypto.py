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

    def test_uses_shared_discovery_no_mirrored_list(self):
        """The script must reuse the encryption module's dynamic discovery
        (single source of truth) rather than a mirrored hardcoded name list."""
        from scripts import rekey
        from app.core import encryption

        # The old mirrored list is gone.
        assert not hasattr(rekey, "_sensitive_settings")
        # And the shared plan builder exists to be reused.
        assert callable(encryption.build_reencryption_plan)
        assert callable(encryption.apply_reencryption_plan)

    def test_rekey_exposes_skip_undecryptable(self):
        """rekey() must accept the --skip-undecryptable operator escape hatch."""
        import inspect
        from scripts.rekey import rekey
        params = inspect.signature(rekey).parameters
        assert "skip_undecryptable" in params

    def test_server_probe_returns_none_when_nothing_listening(self, monkeypatch):
        """With no server, the probe reports clear (None)."""
        import socket

        from scripts import rekey

        # Discover a genuinely free port at runtime: bind to port 0, let the OS
        # assign a free one, read it back, then release it. The tiny
        # close-to-use race is acceptable for this test — hardcoding a port
        # (e.g. 59999) breaks on machines where something happens to listen.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()

        class _S:
            host = "127.0.0.1"
            port = free_port
        monkeypatch.setattr("app.core.config.settings", _S(), raising=False)
        assert rekey._server_appears_up() is None


class TestRekeyDbBacked:
    """Drive scripts.rekey.rekey() end-to-end over the pg_shim DB.

    Covers all four storage locations through the offline script's own code
    path (shared discovery), plus --dry-run (writes nothing) and
    --skip-undecryptable (skips bricked orphans, rekeys the rest).
    """

    def _seed_all(self, db):
        from app.core import encryption
        encryption.set_encryption_key("old-pw", db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("fal-secret"))
        db.create_connector(
            connector_id="conn-r",
            connector_type="telegram",
            name="tg",
            config={"bot_token": encryption.encrypt_value("tok-r")},
            events=[],
        )
        db.create_automation(
            automation_id="auto-r",
            name="wh",
            trigger_type="webhook",
            trigger_config={"secret": encryption.encrypt_value("wh-r")},
            triggers=[{"type": "webhook", "config": {"secret": encryption.encrypt_value("wh-r")}}],
            prompt="p",
            profile_id="default",
        )

    def test_rekey_reencrypts_all_locations(self, mock_db):
        from scripts.rekey import rekey
        from app.core import encryption
        from app.db import database as db

        self._seed_all(db)
        rc = rekey("old-pw", "new-pw", dry_run=False, update_admin_hash=False)
        assert rc == 0

        # In-memory key is now the new key; every location decrypts.
        assert encryption.decrypt_value(db.get_system_setting("fal_api_key")) == "fal-secret"
        conn_tok = db.get_connector("conn-r")["config"]["bot_token"]
        assert encryption.decrypt_value(conn_tok) == "tok-r"
        wh = db.get_automation("auto-r")["triggers"][0]["config"]["secret"]
        assert encryption.decrypt_value(wh) == "wh-r"

    def test_rekey_dry_run_writes_nothing(self, mock_db):
        from scripts.rekey import rekey
        from app.db import database as db

        self._seed_all(db)
        before = db.get_system_setting("fal_api_key")
        rc = rekey("old-pw", "new-pw", dry_run=True, update_admin_hash=False)
        assert rc == 0
        assert db.get_system_setting("fal_api_key") == before

    def test_rekey_aborts_on_undecryptable_by_default(self, mock_db):
        from scripts.rekey import rekey
        from app.db import database as db

        self._seed_all(db)
        db.set_system_setting(
            "cloud_install_api_key", "enc:" + base64.b64encode(b"garbage").decode()
        )
        before = db.get_system_setting("fal_api_key")
        rc = rekey("old-pw", "new-pw", dry_run=False, update_admin_hash=False)
        assert rc == 3
        assert db.get_system_setting("fal_api_key") == before  # nothing written

    def test_rekey_skip_undecryptable_rekeys_the_rest(self, mock_db):
        from scripts.rekey import rekey
        from app.core import encryption
        from app.db import database as db

        self._seed_all(db)
        bricked = "enc:" + base64.b64encode(b"garbage").decode()
        db.set_system_setting("cloud_install_api_key", bricked)

        rc = rekey(
            "old-pw", "new-pw",
            dry_run=False, update_admin_hash=False, skip_undecryptable=True,
        )
        assert rc == 0
        # Good values rekeyed and readable under the new key.
        assert encryption.decrypt_value(db.get_system_setting("fal_api_key")) == "fal-secret"
        # The bricked orphan was left exactly as-is (not touched).
        assert db.get_system_setting("cloud_install_api_key") == bricked


