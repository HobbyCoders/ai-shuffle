"""API tests for the model-provider registry router.

Mirrors ``tests/api/test_claude_accounts.py``: unauthenticated rejection for
every route, then CRUD/behavior against ``authenticated_client``. Extra focus
on the token-secrecy contract (Chunk 2 of docs/MULTI_PROVIDER_PLAN.md):
``auth_token`` must never appear in any response body, ``auth_token_set`` /
``auth_token_masked`` must reflect the stored state, and a blank token on
PATCH must leave the stored token untouched.
"""

import pytest

from app.core import encryption


ROOT = "/api/v1/model-providers"

RAW_TOKEN = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"


def _unlock_encryption(mock_db):
    from app.db import database as db
    encryption.set_encryption_key("test-password", db)


class TestUnauthenticated:
    """Every route must reject requests without an admin session (401/403)."""

    @pytest.mark.parametrize("method,path", [
        ("get", ROOT),
        ("post", ROOT),
        ("patch", f"{ROOT}/some-id"),
        ("delete", f"{ROOT}/some-id"),
        ("post", f"{ROOT}/some-id/toggle"),
    ])
    def test_requires_admin(self, client, method, path):
        kwargs = {"json": {}} if method in ("post", "patch") else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code in (401, 403), (method, path, resp.status_code)


class TestCreate:
    def test_create_happy_path(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        resp = authenticated_client.post(ROOT, json={
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "base_url": "https://openrouter.ai/api",
            "auth_token": RAW_TOKEN,
            "models": [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
            "default_model": "openai/gpt-5.6",
            "small_fast_model": "openai/gpt-5.6-mini",
            "extra_env": {"FOO": "bar"},
            "context_window": 200000,
            "max_output_tokens": 8192,
            "enabled": True,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "OpenRouter"
        assert body["provider_type"] == "openrouter"
        assert body["base_url"] == "https://openrouter.ai/api"
        assert body["models"] == [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}]
        assert body["default_model"] == "openai/gpt-5.6"
        assert body["small_fast_model"] == "openai/gpt-5.6-mini"
        assert body["extra_env"] == {"FOO": "bar"}
        assert body["context_window"] == 200000
        assert body["max_output_tokens"] == 8192
        assert body["enabled"] is True
        # Token contract.
        assert body["auth_token_set"] is True
        assert body["auth_token_masked"] != ""
        assert "auth_token" not in body
        assert RAW_TOKEN not in resp.text

    def test_create_without_token(self, authenticated_client, mock_db):
        resp = authenticated_client.post(ROOT, json={
            "name": "Custom",
            "provider_type": "custom",
            "base_url": "https://gw.example/api",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["auth_token_set"] is False
        assert body["auth_token_masked"] == ""
        assert body["models"] == []
        assert body["extra_env"] == {}

    @pytest.mark.parametrize("provider_type,base_url", [
        ("openai", "https://api.openai.com/v1"),
        ("chatgpt", "https://chatgpt.com/backend-api/codex"),
    ])
    def test_create_accepts_new_provider_types(
        self, authenticated_client, mock_db, provider_type, base_url
    ):
        # The two typed rows added for the providers-UI restructure (plan PR1)
        # must create successfully now that _VALID_PROVIDER_TYPES includes them.
        _unlock_encryption(mock_db)
        resp = authenticated_client.post(ROOT, json={
            "name": provider_type,
            "provider_type": provider_type,
            "base_url": base_url,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["provider_type"] == provider_type

    def test_create_rejects_invalid_provider_type(self, authenticated_client, mock_db):
        resp = authenticated_client.post(ROOT, json={
            "name": "Bad",
            "provider_type": "bogus",
            "base_url": "https://gw.example/api",
        })
        assert resp.status_code in (400, 422), resp.text

    def test_create_rejects_empty_base_url(self, authenticated_client, mock_db):
        resp = authenticated_client.post(ROOT, json={
            "name": "Bad",
            "provider_type": "custom",
            "base_url": "   ",
        })
        assert resp.status_code in (400, 422), resp.text

    def test_create_rejects_malformed_models(self, authenticated_client, mock_db):
        resp = authenticated_client.post(ROOT, json={
            "name": "Bad",
            "provider_type": "custom",
            "base_url": "https://gw.example/api",
            "models": [{"id": "x"}],  # missing "label"
        })
        assert resp.status_code in (400, 422), resp.text

    def test_create_rejects_malformed_extra_env(self, authenticated_client, mock_db):
        resp = authenticated_client.post(ROOT, json={
            "name": "Bad",
            "provider_type": "custom",
            "base_url": "https://gw.example/api",
            "extra_env": {"FOO": 123},
        })
        assert resp.status_code in (400, 422), resp.text

    def test_create_token_without_encryption_ready_400(self, authenticated_client, mock_db):
        # Encryption locked (mirrors test_claude_accounts's encryption_locked case) →
        # storing a token must fail loudly rather than silently persisting plaintext.
        encryption.clear_encryption_key()
        resp = authenticated_client.post(ROOT, json={
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "base_url": "https://openrouter.ai/api",
            "auth_token": RAW_TOKEN,
        })
        assert resp.status_code == 400, resp.text


class TestListAndGet:
    def test_list_masks_tokens(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        authenticated_client.post(ROOT, json={
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "base_url": "https://openrouter.ai/api",
            "auth_token": RAW_TOKEN,
        })
        resp = authenticated_client.get(ROOT)
        assert resp.status_code == 200
        assert RAW_TOKEN not in resp.text
        body = resp.json()
        assert len(body["providers"]) == 1
        provider = body["providers"][0]
        assert provider["auth_token_set"] is True
        assert "auth_token" not in provider


class TestCodexOAuthReadSurface:
    """GET exposes the Codex ``auth_mode`` / ``codex_account_id`` /
    ``needs_reauth`` fields and the ``codex_*_token_set`` flags, but NEVER the
    encrypted token blobs (parity with the auth_token contract)."""

    def _create_with_codex_bundle(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        resp = authenticated_client.post(ROOT, json={
            "name": "Codex",
            "provider_type": "custom",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "auth_token": RAW_TOKEN,
        })
        assert resp.status_code == 200, resp.text
        provider_id = resp.json()["id"]
        # Store an encrypted Codex bundle at the DB layer (no public write path).
        from app.db import database as db
        db.set_codex_token_bundle(
            provider_id,
            access_token=encryption.encrypt_value("codex-access-secret"),
            refresh_token=encryption.encrypt_value("codex-refresh-secret"),
            account_id="acct-xyz",
        )
        return provider_id

    def test_get_exposes_flags_not_blobs(self, authenticated_client, mock_db):
        self._create_with_codex_bundle(authenticated_client, mock_db)
        resp = authenticated_client.get(ROOT)
        assert resp.status_code == 200
        # No blob or plaintext ever appears in the response.
        assert "codex-access-secret" not in resp.text
        assert "codex-refresh-secret" not in resp.text
        provider = resp.json()["providers"][0]
        assert "codex_access_token" not in provider
        assert "codex_refresh_token" not in provider
        # Flags + non-secret fields present.
        assert provider["codex_access_token_set"] is True
        assert provider["codex_refresh_token_set"] is True
        assert provider["auth_mode"] == "chatgpt"
        assert provider["codex_account_id"] == "acct-xyz"
        assert provider["needs_reauth"] is False

    def test_provider_without_codex_bundle_reports_false_flags(
        self, authenticated_client, mock_db
    ):
        _unlock_encryption(mock_db)
        authenticated_client.post(ROOT, json={
            "name": "Plain",
            "provider_type": "openrouter",
            "base_url": "https://openrouter.ai/api",
        })
        provider = authenticated_client.get(ROOT).json()["providers"][0]
        assert provider["codex_access_token_set"] is False
        assert provider["codex_refresh_token_set"] is False
        assert provider["auth_mode"] is None
        assert provider["codex_account_id"] is None
        assert provider["needs_reauth"] is False


class TestUpdate:
    def _create(self, authenticated_client, mock_db, **overrides):
        _unlock_encryption(mock_db)
        payload = {
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "base_url": "https://openrouter.ai/api",
            "auth_token": RAW_TOKEN,
        }
        payload.update(overrides)
        resp = authenticated_client.post(ROOT, json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    def test_partial_update(self, authenticated_client, mock_db):
        provider_id = self._create(authenticated_client, mock_db)
        resp = authenticated_client.patch(f"{ROOT}/{provider_id}", json={"name": "Renamed"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Renamed"
        assert body["base_url"] == "https://openrouter.ai/api"

    def test_blank_token_preserves_stored_token(self, authenticated_client, mock_db):
        provider_id = self._create(authenticated_client, mock_db)
        resp = authenticated_client.patch(
            f"{ROOT}/{provider_id}", json={"name": "Renamed", "auth_token": ""}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auth_token_set"] is True

        # Verify at the DB layer that the original blob is untouched.
        from app.db import database as db
        row = db.get_model_provider(provider_id, include_token=True)
        assert row["auth_token"] == encryption.encrypt_value(RAW_TOKEN) or encryption.is_encrypted(row["auth_token"])
        decrypted = encryption.decrypt_value(row["auth_token"])
        assert decrypted == RAW_TOKEN

    def test_new_token_replaces_stored_token(self, authenticated_client, mock_db):
        provider_id = self._create(authenticated_client, mock_db)
        new_token = "sk-or-v1-brandnewtoken0123456789"
        resp = authenticated_client.patch(
            f"{ROOT}/{provider_id}", json={"auth_token": new_token}
        )
        assert resp.status_code == 200, resp.text
        assert new_token not in resp.text

        from app.db import database as db
        row = db.get_model_provider(provider_id, include_token=True)
        assert encryption.decrypt_value(row["auth_token"]) == new_token

    def test_update_unknown_id_404(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        resp = authenticated_client.patch(f"{ROOT}/nope", json={"name": "X"})
        assert resp.status_code == 404

    def test_update_rejects_invalid_provider_type(self, authenticated_client, mock_db):
        provider_id = self._create(authenticated_client, mock_db)
        resp = authenticated_client.patch(
            f"{ROOT}/{provider_id}", json={"provider_type": "bogus"}
        )
        assert resp.status_code in (400, 422), resp.text


class TestDelete:
    def test_delete_happy_path(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        resp = authenticated_client.post(ROOT, json={
            "name": "Custom", "provider_type": "custom",
            "base_url": "https://gw.example/api",
        })
        provider_id = resp.json()["id"]
        resp = authenticated_client.delete(f"{ROOT}/{provider_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"success": True}
        assert authenticated_client.get(ROOT).json()["providers"] == []

    def test_delete_unknown_id_404(self, authenticated_client, mock_db):
        resp = authenticated_client.delete(f"{ROOT}/nope")
        assert resp.status_code == 404


class TestToggle:
    def test_toggle_flips_enabled(self, authenticated_client, mock_db):
        _unlock_encryption(mock_db)
        resp = authenticated_client.post(ROOT, json={
            "name": "Custom", "provider_type": "custom",
            "base_url": "https://gw.example/api", "enabled": True,
        })
        provider_id = resp.json()["id"]
        assert resp.json()["enabled"] is True

        resp = authenticated_client.post(f"{ROOT}/{provider_id}/toggle")
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False

        resp = authenticated_client.post(f"{ROOT}/{provider_id}/toggle")
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True

    def test_toggle_unknown_id_404(self, authenticated_client, mock_db):
        resp = authenticated_client.post(f"{ROOT}/nope/toggle")
        assert resp.status_code == 404
