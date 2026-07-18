"""Re-encryption coverage for the 6th storage location: model_providers.auth_token.

A password change re-derives the at-rest key, so every ``enc:``-prefixed value
must be re-encrypted under the new key or it becomes permanently undecryptable.
This mirrors tests/core/test_rekey_crypto.py::TestReencryptClaudeAccounts for
the model-provider gateway token: the plan discovers the provider row, apply
re-encrypts only the token (leaving other columns intact), and an undecryptable
token fails the plan closed. Runs on the pg_shim DB via ``mock_db``.
"""

import base64

from app.core import encryption
from app.db import database as db


def _seed_provider(plaintext="gw-secret"):
    encryption.set_encryption_key("old-pw", db)
    db.create_model_provider(
        provider_id="prov-1",
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token=encryption.encrypt_value(plaintext),
        models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        default_model="openai/gpt-5.6",
    )


class TestReencryptModelProviders:
    def test_plan_discovers_model_provider_rows(self, mock_db):
        _seed_provider()
        old_key = encryption.derive_key_from_password("old-pw", encryption.get_or_create_salt(db))
        new_key = encryption.derive_key_from_password("new-pw", encryption.get_or_create_salt(db))
        plan, failures = encryption.build_reencryption_plan(old_key, new_key, db)
        assert failures == []
        kinds = [item.kind for item in plan]
        assert "model_provider" in kinds

    def test_apply_reencrypts_token_and_preserves_other_columns(self, mock_db):
        _seed_provider()
        before = db.get_model_provider("prov-1")  # public (no token)
        old_key = encryption.derive_key_from_password("old-pw", encryption.get_or_create_salt(db))
        new_key = encryption.derive_key_from_password("new-pw", encryption.get_or_create_salt(db))

        plan, failures = encryption.build_reencryption_plan(old_key, new_key, db)
        assert not failures
        encryption.apply_reencryption_plan(plan, db)

        after = db.get_model_provider("prov-1", include_token=True)
        # Non-token columns are untouched (token-only update).
        assert after["name"] == before["name"]
        assert after["base_url"] == before["base_url"]
        assert after["default_model"] == before["default_model"]
        assert after["models_json"] == before["models_json"]
        # New key decrypts the re-encrypted blob back to the original secret.
        encryption._encryption_key = new_key
        assert encryption.decrypt_value(after["auth_token"]) == "gw-secret"

    def test_password_change_round_trip(self, mock_db):
        """End-to-end: re_encrypt_all_secrets under a new password, provider token
        still decrypts under the new key."""
        _seed_provider()
        assert encryption.re_encrypt_all_secrets("old-pw", "new-pw", db) is True
        # In-memory key is now the new key; the provider token decrypts.
        row = db.get_model_provider("prov-1", include_token=True)
        assert encryption.decrypt_value(row["auth_token"]) == "gw-secret"

    def test_fail_closed_on_undecryptable_token(self, mock_db):
        encryption.set_encryption_key("old-pw", db)
        db.create_model_provider(
            provider_id="prov-bad",
            name="Bad",
            provider_type="custom",
            base_url="https://gw/api",
            auth_token="enc:" + base64.b64encode(b"garbage").decode(),
        )
        old_key = encryption.derive_key_from_password("old-pw", encryption.get_or_create_salt(db))
        new_key = encryption.derive_key_from_password("new-pw", encryption.get_or_create_salt(db))
        plan, failures = encryption.build_reencryption_plan(old_key, new_key, db)
        assert any("model_providers:prov-bad" in label for label, _ in failures)

    def test_provider_without_token_is_ignored_by_plan(self, mock_db):
        encryption.set_encryption_key("old-pw", db)
        db.create_model_provider(
            provider_id="prov-none",
            name="NoToken",
            provider_type="custom",
            base_url="https://gw/api",
        )
        old_key = encryption.derive_key_from_password("old-pw", encryption.get_or_create_salt(db))
        new_key = encryption.derive_key_from_password("new-pw", encryption.get_or_create_salt(db))
        plan, failures = encryption.build_reencryption_plan(old_key, new_key, db)
        assert failures == []
        refs = [item.ref for item in plan if item.kind == "model_provider"]
        assert "prov-none" not in refs
