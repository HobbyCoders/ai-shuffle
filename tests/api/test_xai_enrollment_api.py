"""API tests for the Grok/xAI device-code enrollment routes.

Mounted on the model-providers router. Every route requires admin. The token
blobs must NEVER appear in a response body (only the ``_set`` flags). All
upstream OAuth calls are stubbed via monkeypatch on ``app.core.xai_oauth`` —
NO network.
"""

import pytest

from app.core import encryption, xai_oauth
from app.db import database as db


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-xai-api"


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _make_provider():
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Grok",
        provider_type="grok",
        base_url="https://api.x.ai/v1",
        enabled=True,
    )


class TestUnauthenticated:
    @pytest.mark.parametrize("path", [
        f"{ROOT}/{PROVIDER_ID}/xai/device/start",
        f"{ROOT}/{PROVIDER_ID}/xai/device/poll",
        f"{ROOT}/{PROVIDER_ID}/xai/clear-reauth",
    ])
    def test_requires_admin(self, client, path):
        resp = client.post(path, json={})
        assert resp.status_code in (401, 403), (path, resp.status_code)


class TestDeviceStart:
    def test_start_prefers_verification_uri_complete(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider()

        async def _fake_start(*, client=None):
            return xai_oauth.DeviceCodeStart(
                device_code="dev-1", user_code="ABCD-1234",
                verification_uri="https://auth.x.ai/activate",
                verification_uri_complete=(
                    "https://auth.x.ai/activate?user_code=ABCD-1234"
                ),
            )

        monkeypatch.setattr(xai_oauth, "start_device_code", _fake_start)

        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/start", json={}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_code"] == "ABCD-1234"
        assert body["verification_uri"].endswith("user_code=ABCD-1234")
        assert "dev-1" not in resp.text

    def test_start_unknown_provider_404(self, authenticated_client, mock_db):
        _unlock(mock_db)
        resp = authenticated_client.post(
            f"{ROOT}/nope/xai/device/start", json={}
        )
        assert resp.status_code == 404


class TestDevicePoll:
    def _start(self, authenticated_client, monkeypatch):
        async def _fake_start(*, client=None):
            return xai_oauth.DeviceCodeStart(
                device_code="dev-1", user_code="ABCD-1234",
                verification_uri="https://auth.x.ai/activate",
            )
        monkeypatch.setattr(xai_oauth, "start_device_code", _fake_start)
        r = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/start", json={}
        )
        assert r.status_code == 200, r.text

    def test_poll_pending(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        async def _fake_poll(device_code, *, client=None):
            raise xai_oauth.XaiAuthPending(slow_down=False)

        monkeypatch.setattr(xai_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/poll", json={}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"

    def test_poll_slow_down(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        async def _fake_poll(device_code, *, client=None):
            raise xai_oauth.XaiAuthPending(slow_down=True)

        monkeypatch.setattr(xai_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/poll", json={}
        )
        assert resp.json()["status"] == "slow_down"

    def test_poll_without_start_400(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider()
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/poll", json={}
        )
        assert resp.status_code == 400

    def test_poll_complete_persists_encrypted_seeds_preset(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider()
        self._start(authenticated_client, monkeypatch)

        SECRET_ACCESS = "super-secret-grok-access"
        SECRET_REFRESH = "super-secret-grok-refresh"

        async def _fake_poll(device_code, *, client=None):
            return xai_oauth.TokenBundle(
                access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH,
                id_token=None, account_id="team-xyz",
            )

        monkeypatch.setattr(xai_oauth, "poll_device_code", _fake_poll)
        resp = authenticated_client.post(
            f"{ROOT}/{PROVIDER_ID}/xai/device/poll", json={}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "complete"
        prov = body["provider"]
        assert prov["auth_mode"] == "grok"
        assert prov["codex_account_id"] == "team-xyz"
        assert prov["needs_reauth"] is False
        assert prov["codex_access_token_set"] is True
        assert prov["codex_refresh_token_set"] is True
        assert prov["default_model"] == "grok-4.6"
        assert any(m["id"] == "grok-4.6" for m in prov["models"])

        assert SECRET_ACCESS not in resp.text
        assert SECRET_REFRESH not in resp.text
        assert "codex_access_token" not in prov

        stored = db.get_codex_tokens(PROVIDER_ID)
        assert stored["auth_mode"] == "grok"
        assert stored["codex_access_token"].startswith("enc:")
        assert encryption.decrypt_value(stored["codex_access_token"]) == SECRET_ACCESS
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == SECRET_REFRESH
