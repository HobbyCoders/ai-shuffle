"""API tests for the Claude accounts router.

Verifies the router is registered, every route rejects unauthenticated
requests, and POST /complete returns 409 when encryption is locked.
"""

import pytest

from app.core import encryption


ROOT = "/api/v1/auth/claude/accounts"


class TestUnauthenticated:
    """Every route must reject requests without an admin session (401/403)."""

    @pytest.mark.parametrize("method,path", [
        ("get", ROOT),
        ("post", f"{ROOT}/start"),
        ("post", f"{ROOT}/complete"),
        ("post", f"{ROOT}/some-id/activate"),
        ("patch", f"{ROOT}/some-id"),
        ("delete", f"{ROOT}/some-id"),
    ])
    def test_requires_admin(self, client, method, path):
        kwargs = {"json": {}} if method in ("post", "patch") else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code in (401, 403), (method, path, resp.status_code)


class TestRegisteredAndAuthed:
    def test_list_route_registered(self, authenticated_client):
        resp = authenticated_client.get(ROOT)
        assert resp.status_code == 200
        body = resp.json()
        assert "accounts" in body
        assert "active_account_id" in body
        assert "encryption_ready" in body

    def test_complete_409_when_encryption_locked(self, authenticated_client):
        encryption.clear_encryption_key()
        resp = authenticated_client.post(
            f"{ROOT}/complete",
            json={"code": "abc", "state": "xyz", "make_active": True},
        )
        assert resp.status_code == 409

    def test_complete_requires_code(self, authenticated_client):
        resp = authenticated_client.post(f"{ROOT}/complete", json={"state": "xyz"})
        assert resp.status_code == 400

    def test_activate_missing_account_when_key_ready(self, authenticated_client, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        resp = authenticated_client.post(f"{ROOT}/missing/activate")
        assert resp.status_code == 404

    def test_activate_schedules_usage_refresh(self, authenticated_client, mock_db, oauth_tmp):
        """A successful activate must schedule a fresh usage re-poll+broadcast
        so the limits indicator reflects the newly-active account immediately.
        """
        from unittest.mock import patch
        from app.db import database as db
        import json

        encryption.set_encryption_key("pw", db)
        oauth_tmp.write_credentials_dict({
            "accessToken": "atok", "refreshToken": "rtok",
            "expiresAt": 9999999999000, "scopes": ["user:inference"],
            "subscriptionType": None,
        })
        db.create_claude_account(
            account_id="acct-x", name="X",
            credentials=encryption.encrypt_value(json.dumps({
                "accessToken": "atok", "refreshToken": "rtok",
                "expiresAt": 9999999999000,
            })),
            credential_fingerprint="fp", expires_at=9999999999000,
            has_refresh_token=True,
        )

        with patch("app.api.claude_accounts._schedule_usage_refresh") as sched:
            resp = authenticated_client.post(f"{ROOT}/acct-x/activate")

        assert resp.status_code == 200, resp.text
        sched.assert_called_once()

    def test_activate_missing_does_not_schedule(self, authenticated_client, mock_db):
        from unittest.mock import patch
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        with patch("app.api.claude_accounts._schedule_usage_refresh") as sched:
            resp = authenticated_client.post(f"{ROOT}/missing/activate")

        assert resp.status_code == 404
        sched.assert_not_called()

    def test_delete_missing_account(self, authenticated_client, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        resp = authenticated_client.delete(f"{ROOT}/missing")
        assert resp.status_code == 404


class TestSerializedTimestampTypes:
    """last_used_at / created_at must serialize as int (epoch ms) | null.

    The columns are TIMESTAMPTZ; without server-side normalization they reach
    the client as ISO strings, and ConnectionsSection's relativeLastUsed does
    Date.now() - ts → NaN → "NaNy ago". _public_account converts to epoch ms.
    """

    def test_timestamps_serialize_as_int_or_null(self, authenticated_client, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        # Seed an account (created_at set) then switch to it (last_used_at set).
        oauth_tmp.write_credentials_dict({
            "accessToken": "atok", "refreshToken": "rtok",
            "expiresAt": 9999999999000, "scopes": ["user:inference"],
            "subscriptionType": None,
        })
        # Table may already hold a startup-seeded row; create our own account
        # explicitly and switch to it so its last_used_at is populated.
        import json
        aid = "acct-ts"
        db.create_claude_account(
            account_id=aid, name="TS",
            credentials=encryption.encrypt_value(json.dumps({
                "accessToken": "atok", "refreshToken": "rtok",
                "expiresAt": 9999999999000,
            })),
            credential_fingerprint="fp", expires_at=9999999999000,
            has_refresh_token=True,
        )
        svc.switch_account(aid)

        resp = authenticated_client.get(ROOT)
        assert resp.status_code == 200
        accounts = {a["id"]: a for a in resp.json()["accounts"]}
        acc = accounts[aid]
        # created_at + last_used_at are populated ints; expires_at is an int|null.
        assert isinstance(acc["created_at"], int)
        assert isinstance(acc["last_used_at"], int)
        assert acc["expires_at"] is None or isinstance(acc["expires_at"], int)
        # Every account's timestamps obey the int|null contract (never a string).
        for a in accounts.values():
            assert a["created_at"] is None or isinstance(a["created_at"], int)
            assert a["last_used_at"] is None or isinstance(a["last_used_at"], int)


@pytest.fixture
def oauth_tmp(tmp_path):
    """A DirectOAuth pointed at a tmp config dir, patched in as the singleton."""
    from unittest.mock import patch
    from app.core import claude_accounts as svc
    from app.core.oauth import DirectOAuth

    inst = DirectOAuth(config_dir=tmp_path)
    svc._last_synced_mtime = None
    with patch("app.core.claude_accounts.direct_oauth", inst), \
            patch("app.core.oauth.direct_oauth", inst):
        yield inst
    svc._last_synced_mtime = None


class TestStatusExtension:
    def test_claude_status_has_account_fields(self, authenticated_client):
        resp = authenticated_client.get("/api/v1/auth/claude/status")
        assert resp.status_code == 200
        body = resp.json()
        # New fields present, existing ones preserved.
        assert "active_account" in body
        assert "accounts_count" in body
        assert "authenticated" in body
        assert "token_valid" in body
