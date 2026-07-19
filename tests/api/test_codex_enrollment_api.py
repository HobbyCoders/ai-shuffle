"""API tests for the Codex device-code enrollment routes.

Mounted on the model-providers router. Every route requires admin. The token
blobs must NEVER appear in a response body (only the ``_set`` flags), and after a
successful poll the stored blobs must be ``enc:``-prefixed. All upstream OAuth
calls are stubbed via monkeypatch on ``app.core.codex_oauth`` — NO network.
"""

import pytest

from app.core import codex_oauth, encryption
from app.db import database as db


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-codex-api"


def _unlock(mock_db):
    # mock_db activates the SQLite shim as app.db.database; set_encryption_key
    # wants the database MODULE, not the raw connection the fixture yields.
    encryption.set_encryption_key("test-password", db)


def _make_provider():
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Codex",
        provider_type="custom",
        base_url="https://chatgpt.com/backend-api/codex",
        enabled=True,
    )


class TestUnauthenticated:
    @pytest.mark.parametrize("path", [
        f"{ROOT}/{PROVIDER_ID}/codex/device/start",
        f"{ROOT}/{PROVIDER_ID}/codex/device/poll",
        f"{ROOT}/{PROVIDER_ID}/codex/clear-reauth",
    ])
    def test_requires_admin(self, client, path):
        resp = client.post(path, json={})
        assert resp.status_code in (401, 403), (path, resp.status_code)


class TestDeviceStart:
    def test_start_returns_user_code(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()

        async def _fake_start(*, client=None):
            return codex_oauth.DeviceCodeStart(
                device_code="dev-1", user_code="ABCD-1234",
                verification_uri="https://auth.openai.com/codex/device",
                code_verifier="verifier-1",
            )

        monkeypatch.setattr(codex_oauth, "start_device_code", _fake_start)

        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/start", json={}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_code"] == "ABCD-1234"
        assert body["verification_uri"].endswith("/codex/device")
        # The PKCE verifier / device_code are server-side only — never returned.
        assert "verifier" not in resp.text
        assert "dev-1" not in resp.text

    def test_start_unknown_provider_404(self, authenticated_client, mock_db):
        _unlock(mock_db)
        resp = authenticated_client.post(
            f"{ROOT}/nope/codex/device/start", json={}
        )
        assert resp.status_code == 404


class TestDevicePoll:
    def _start(self, authenticated_client, monkeypatch):
        async def _fake_start(*, client=None):
            return codex_oauth.DeviceCodeStart(
                device_code="dev-1", user_code="ABCD-1234",
                verification_uri="https://auth.openai.com/codex/device",
                code_verifier="verifier-1",
            )
        monkeypatch.setattr(codex_oauth, "start_device_code", _fake_start)
        r = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/start", json={}
        )
        assert r.status_code == 200, r.text

    def test_poll_pending(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        async def _fake_poll(device_code, code_verifier, user_code, *, client=None):
            raise codex_oauth.CodexAuthPending(slow_down=False)

        monkeypatch.setattr(codex_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/poll", json={}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"

    def test_poll_slow_down(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        async def _fake_poll(device_code, code_verifier, user_code, *, client=None):
            raise codex_oauth.CodexAuthPending(slow_down=True)

        monkeypatch.setattr(codex_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/poll", json={}
        )
        assert resp.json()["status"] == "slow_down"

    def test_poll_without_start_400(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider()
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/poll", json={}
        )
        assert resp.status_code == 400

    def test_poll_complete_persists_encrypted_and_hides_blobs(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        SECRET_ACCESS = "super-secret-access-token"
        SECRET_REFRESH = "super-secret-refresh-token"

        async def _fake_poll(device_code, code_verifier, user_code, *, client=None):
            return codex_oauth.TokenBundle(
                access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH,
                id_token=None, account_id="acct-xyz",
            )

        monkeypatch.setattr(codex_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/poll", json={}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "complete"
        prov = body["provider"]
        assert prov["auth_mode"] == "chatgpt"
        assert prov["codex_account_id"] == "acct-xyz"
        assert prov["needs_reauth"] is False
        assert prov["codex_access_token_set"] is True
        assert prov["codex_refresh_token_set"] is True

        # Raw secrets NEVER appear anywhere in the response.
        assert SECRET_ACCESS not in resp.text
        assert SECRET_REFRESH not in resp.text
        assert "codex_access_token" not in prov  # only the _set flag is public

        # Stored blobs are enc:-prefixed and decrypt to the secrets.
        stored = db.get_codex_tokens(PROVIDER_ID)
        assert stored["codex_access_token"].startswith("enc:")
        assert stored["codex_refresh_token"].startswith("enc:")
        assert encryption.decrypt_value(stored["codex_access_token"]) == SECRET_ACCESS
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == SECRET_REFRESH

    def test_poll_terminal_error_400(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        async def _fake_poll(device_code, code_verifier, user_code, *, client=None):
            raise codex_oauth.CodexAuthError("access_denied")

        monkeypatch.setattr(codex_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/device/poll", json={}
        )
        assert resp.status_code == 400


class TestClearReauth:
    def test_clear_reauth_clears_flag_preserves_bundle(
        self, authenticated_client, mock_db
    ):
        _unlock(mock_db)
        _make_provider()
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value("acc"),
            refresh_token=encryption.encrypt_value("ref"),
            account_id="acct-1",
            needs_reauth=True,
        )
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/codex/clear-reauth", json={}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["needs_reauth"] is False
        # Bundle preserved.
        stored = db.get_codex_tokens(PROVIDER_ID)
        assert encryption.decrypt_value(stored["codex_access_token"]) == "acc"
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "ref"

    def test_clear_reauth_unknown_provider_404(self, authenticated_client, mock_db):
        _unlock(mock_db)
        resp = authenticated_client.post(
            f"{ROOT}/nope/codex/clear-reauth", json={}
        )
        assert resp.status_code == 404


class TestDeleteRevoke:
    def test_delete_best_effort_revokes_codex(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider()
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value("acc"),
            refresh_token=encryption.encrypt_value("the-refresh"),
            account_id="acct-1",
        )
        revoked = []

        async def _fake_revoke(token, *, client=None):
            revoked.append(token)
            return True

        monkeypatch.setattr(codex_oauth, "revoke_token", _fake_revoke)
        resp = authenticated_client.request(
            "DELETE", f"{ROOT}/{PROVIDER_ID}"
        )
        assert resp.status_code == 200, resp.text
        # Revoke was called with the DECRYPTED refresh token before deletion.
        assert revoked == ["the-refresh"]
        assert db.get_model_provider(PROVIDER_ID) is None

    def test_delete_never_blocked_by_revoke_failure(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider()
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value("acc"),
            refresh_token=encryption.encrypt_value("the-refresh"),
            account_id="acct-1",
        )

        async def _boom(token, *, client=None):
            raise RuntimeError("network down")

        monkeypatch.setattr(codex_oauth, "revoke_token", _boom)
        resp = authenticated_client.request("DELETE", f"{ROOT}/{PROVIDER_ID}")
        assert resp.status_code == 200
        assert db.get_model_provider(PROVIDER_ID) is None
