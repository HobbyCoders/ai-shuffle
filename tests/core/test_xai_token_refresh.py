"""Tests for the Grok/SuperGrok OAuth token-refresh worker.

Exercises the worker against the SQLite DB shim (``mock_db``) with encryption
unlocked and all upstream HTTP mocked via ``httpx.MockTransport``. Covers
rotated bundle persist with ``auth_mode='grok'`` and permanent failure →
``needs_reauth``.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core import encryption, xai_token_refresh
from app.db import database as db


PROVIDER_ID = "prov-grok-1"


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _iso(dt):
    return dt.isoformat()


def _seed_provider(*, access="access-old", refresh="refresh-old",
                   last_refresh=None, account_id="team-1"):
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Grok",
        provider_type="grok",
        base_url="https://api.x.ai/v1",
        enabled=True,
    )
    db.set_codex_token_bundle(
        PROVIDER_ID,
        access_token=encryption.encrypt_value(access),
        refresh_token=encryption.encrypt_value(refresh),
        account_id=account_id,
        id_token_expires_at=None,
        last_refresh=last_refresh,
        needs_reauth=False,
        auth_mode="grok",
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_locks():
    xai_token_refresh._locks.clear()
    yield
    xai_token_refresh._locks.clear()


class TestRefreshRotation:
    async def test_rotated_bundle_persisted_encrypted_keeps_grok_mode(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)))

        def handler(request):
            return httpx.Response(200, json={
                "access_token": "access-new", "refresh_token": "refresh-new",
            })

        async with _client(handler) as c:
            did = await xai_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is True

        stored = db.get_codex_tokens(PROVIDER_ID)
        assert stored["auth_mode"] == "grok"
        assert stored["codex_access_token"].startswith("enc:")
        assert stored["codex_refresh_token"].startswith("enc:")
        assert encryption.decrypt_value(stored["codex_access_token"]) == "access-new"
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "refresh-new"
        assert stored["codex_account_id"] == "team-1"
        assert not stored["needs_reauth"]

    async def test_not_due_skips(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc)))
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={
                "access_token": "x", "refresh_token": "y"})

        async with _client(handler) as c:
            did = await xai_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is False
        assert posted == []

    async def test_chatgpt_bundle_is_not_refreshed(self, mock_db):
        _unlock(mock_db)
        db.create_model_provider(
            provider_id=PROVIDER_ID,
            name="ChatGPT",
            provider_type="chatgpt",
            base_url="https://chatgpt.com/backend-api/codex",
            enabled=True,
        )
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value("access-old"),
            refresh_token=encryption.encrypt_value("refresh-old"),
            account_id="acct-1",
            last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)),
            needs_reauth=False,
            auth_mode="chatgpt",
        )
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={
                "access_token": "x", "refresh_token": "y"})

        async with _client(handler) as c:
            did = await xai_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is False
        assert posted == []


class TestPermanentFailure:
    async def test_invalid_grant_sets_needs_reauth_preserves_bundle(
        self, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider(
            access="access-keep", refresh="refresh-keep",
            last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)),
        )

        notified = []

        async def _fake_notify(pid):
            notified.append(pid)

        monkeypatch.setattr(xai_token_refresh, "_notify_reauth", _fake_notify)

        def handler(request):
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with _client(handler) as c:
            did = await xai_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is False

        stored = db.get_codex_tokens(PROVIDER_ID)
        assert stored["needs_reauth"] is True or stored["needs_reauth"] == 1
        assert stored["auth_mode"] == "grok"
        assert encryption.decrypt_value(stored["codex_access_token"]) == "access-keep"
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "refresh-keep"
        assert notified == [PROVIDER_ID]


class TestRefreshOn401:
    async def test_forces_exactly_one_refresh(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc)))
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={
                "access_token": "access-401", "refresh_token": "refresh-401",
            })

        async with _client(handler) as c:
            did = await xai_token_refresh.refresh_on_401_once(
                PROVIDER_ID, stale_access_token="access-old", client=c
            )
        assert did is True
        assert posted == [1]
        stored = db.get_codex_tokens(PROVIDER_ID)
        assert encryption.decrypt_value(stored["codex_access_token"]) == "access-401"
        assert stored["auth_mode"] == "grok"
