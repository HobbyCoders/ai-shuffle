"""Tests for the Codex OAuth token-refresh worker.

Exercises the refresh worker against the real SQLite DB shim (``mock_db``) with
encryption unlocked and all upstream HTTP mocked via ``httpx.MockTransport``.
Covers: rotated bundle persisted atomically + still ``enc:``-prefixed; the
per-provider refresh lock serializing two concurrent refreshes (second sees the
first's rotated token, NO double POST); permanent-failure codes flipping
``needs_reauth`` while preserving the bundle; and the refresh-on-401-once helper.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core import codex_oauth, codex_token_refresh, encryption
from app.db import database as db


PROVIDER_ID = "prov-codex-1"


def _unlock(mock_db):
    # mock_db activates the SQLite shim as app.db.database; set_encryption_key
    # wants the database MODULE (it calls get_system_setting on it), not the
    # raw connection the fixture yields.
    encryption.set_encryption_key("test-password", db)


def _iso(dt):
    return dt.isoformat()


def _seed_provider(*, access="access-old", refresh="refresh-old",
                   last_refresh=None, account_id="acct-1"):
    """Create a provider + persist an encrypted Codex bundle."""
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Codex",
        provider_type="custom",
        base_url="https://chatgpt.com/backend-api/codex",
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
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_locks():
    """Each test starts with a clean per-provider lock table."""
    codex_token_refresh._locks.clear()
    yield
    codex_token_refresh._locks.clear()


# ---------------------------------------------------------------------------
# _needs_refresh policy
# ---------------------------------------------------------------------------

class TestNeedsRefreshPolicy:
    def test_no_last_refresh_is_due(self):
        assert codex_token_refresh._needs_refresh({"codex_last_refresh": None}, None)

    def test_recent_refresh_not_due(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        assert not codex_token_refresh._needs_refresh(
            {"codex_last_refresh": recent}, None
        )

    def test_eight_days_old_is_due(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=9))
        assert codex_token_refresh._needs_refresh({"codex_last_refresh": old}, None)

    def test_exp_within_5min_is_due(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        access = _jwt_with_exp(soon)
        assert codex_token_refresh._needs_refresh(
            {"codex_last_refresh": recent}, access
        )


def _jwt_with_exp(dt) -> str:
    def _seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{_seg({'alg':'none'})}.{_seg({'exp': int(dt.timestamp())})}.sig"


# ---------------------------------------------------------------------------
# Rotation persisted atomically + encrypted
# ---------------------------------------------------------------------------

class TestRefreshRotation:
    async def test_rotated_bundle_persisted_encrypted(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)))

        def handler(request):
            return httpx.Response(200, json={
                "access_token": "access-new", "refresh_token": "refresh-new",
            })

        async with _client(handler) as c:
            did = await codex_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is True

        stored = db.get_codex_tokens(PROVIDER_ID)
        # Blobs are enc:-prefixed and decrypt to the ROTATED values.
        assert stored["codex_access_token"].startswith("enc:")
        assert stored["codex_refresh_token"].startswith("enc:")
        assert encryption.decrypt_value(stored["codex_access_token"]) == "access-new"
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "refresh-new"
        # account id preserved, needs_reauth stays clear, last_refresh advanced.
        assert stored["codex_account_id"] == "acct-1"
        assert not stored["needs_reauth"]
        assert stored["codex_last_refresh"] is not None

    async def test_not_due_skips(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc)))
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={
                "access_token": "x", "refresh_token": "y"})

        async with _client(handler) as c:
            did = await codex_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is False
        assert posted == []  # no HTTP at all


# ---------------------------------------------------------------------------
# CONCURRENCY — the ship-blocking per-provider lock
# ---------------------------------------------------------------------------

class TestConcurrencySerialization:
    async def test_two_concurrent_refreshes_do_one_post(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)))

        post_count = 0
        gate = asyncio.Event()

        async def handler(request):
            nonlocal post_count
            post_count += 1
            # Hold the first refresh open briefly so the second contends on the
            # per-provider lock rather than racing the DB write.
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={
                "access_token": "access-rot", "refresh_token": "refresh-rot",
            })

        async with _client(handler) as c:
            results = await asyncio.gather(
                codex_token_refresh.refresh_provider(PROVIDER_ID, client=c),
                codex_token_refresh.refresh_provider(PROVIDER_ID, client=c),
            )

        # Exactly one POST: the second caller acquired the lock AFTER the first
        # rotated + advanced last_refresh, saw it was no longer due, and skipped.
        assert post_count == 1
        assert sorted(results) == [False, True]
        # The stored token is the first refresh's rotated value.
        stored = db.get_codex_tokens(PROVIDER_ID)
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "refresh-rot"


# ---------------------------------------------------------------------------
# Permanent failure → needs_reauth, bundle preserved
# ---------------------------------------------------------------------------

class TestPermanentFailure:
    @pytest.mark.parametrize("code", [
        "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated",
    ])
    async def test_permanent_failure_sets_needs_reauth_preserves_bundle(
        self, mock_db, code, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider(
            access="access-keep", refresh="refresh-keep",
            last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)),
        )

        notified = []

        async def _fake_notify(pid):
            notified.append(pid)

        monkeypatch.setattr(codex_token_refresh, "_notify_reauth", _fake_notify)

        def handler(request):
            return httpx.Response(400, json={"error": code})

        async with _client(handler) as c:
            did = await codex_token_refresh.refresh_provider(PROVIDER_ID, client=c)
        assert did is False

        stored = db.get_codex_tokens(PROVIDER_ID)
        assert stored["needs_reauth"] is True or stored["needs_reauth"] == 1
        # Bundle PRESERVED (not wiped) so the row still shows the last credential.
        assert encryption.decrypt_value(stored["codex_access_token"]) == "access-keep"
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "refresh-keep"
        assert notified == [PROVIDER_ID]

    async def test_flagged_provider_not_refreshed_again(self, mock_db):
        _unlock(mock_db)
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc) - timedelta(days=9)))
        # Mark dead.
        stored = db.get_codex_tokens(PROVIDER_ID)
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=stored["codex_access_token"],
            refresh_token=stored["codex_refresh_token"],
            account_id=stored["codex_account_id"],
            needs_reauth=True,
        )
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={"access_token": "x", "refresh_token": "y"})

        async with _client(handler) as c:
            did = await codex_token_refresh.refresh_provider(
                PROVIDER_ID, force=True, client=c
            )
        assert did is False
        assert posted == []  # dead credential is never auto-refreshed


# ---------------------------------------------------------------------------
# refresh-on-401-once
# ---------------------------------------------------------------------------

class TestRefreshOn401:
    async def test_forces_exactly_one_refresh(self, mock_db):
        _unlock(mock_db)
        # last_refresh recent → NOT due by policy, but 401 forces it anyway.
        _seed_provider(last_refresh=_iso(datetime.now(timezone.utc)))
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={
                "access_token": "a2", "refresh_token": "r2"})

        async with _client(handler) as c:
            ok = await codex_token_refresh.refresh_on_401_once(PROVIDER_ID, client=c)
        assert ok is True
        assert len(posted) == 1
        stored = db.get_codex_tokens(PROVIDER_ID)
        assert encryption.decrypt_value(stored["codex_refresh_token"]) == "r2"

    async def test_stale_token_guard_skips_redundant_post(self, mock_db):
        _unlock(mock_db)
        _seed_provider(access="current-access", last_refresh=_iso(datetime.now(timezone.utc)))
        posted = []

        def handler(request):
            posted.append(1)
            return httpx.Response(200, json={"access_token": "x", "refresh_token": "y"})

        # The caller's 401 was on an OLD access token; a peer already rotated to
        # "current-access" → we must NOT POST again.
        async with _client(handler) as c:
            ok = await codex_token_refresh.refresh_on_401_once(
                PROVIDER_ID, stale_access_token="old-access", client=c
            )
        assert ok is True
        assert posted == []
