"""Tests for the multiple-Claude-accounts core service.

Exercises the DB-backed account registry, the credentials-file sync-back logic,
switching (verbatim writes + sync-back-first), add_account, seeding, delete
rules, and the oauth post-write hook — all over the pg_shim SQLite DB.
"""

import json
from unittest.mock import patch

import pytest

from app.core import encryption
from app.core.oauth import DirectOAuth, OAuthTokens, set_post_write_hook


def _oauth_dict(access="atok", refresh="rtok", expires=9999999999000, sub=None):
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires,
        "scopes": ["user:inference"],
        "subscriptionType": sub,
    }


@pytest.fixture
def oauth_tmp(tmp_path):
    """A DirectOAuth pointed at a tmp config dir, patched in as the singleton.

    Also resets the module mtime cache before/after each test.
    """
    from app.core import claude_accounts as svc

    inst = DirectOAuth(config_dir=tmp_path)
    svc._last_synced_mtime = None
    with patch("app.core.claude_accounts.direct_oauth", inst), \
            patch("app.core.oauth.direct_oauth", inst):
        yield inst
    svc._last_synced_mtime = None
    set_post_write_hook(None)


def _write_file(inst: DirectOAuth, oauth_dict: dict):
    inst.write_credentials_dict(oauth_dict)


def _set_key(db):
    encryption.set_encryption_key("test-pw", db)


# ---------------------------------------------------------------------------
# seed_from_credentials_file
# ---------------------------------------------------------------------------

class TestSeed:
    def test_seed_creates_row_and_pointer(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(sub="pro"))

        aid = svc.seed_from_credentials_file()
        assert aid is not None

        row = db.get_claude_account(aid, include_credentials=True)
        assert row["name"] == "Default account"
        assert row["subscription_type"] == "pro"  # preserved from the file
        assert svc.get_active_account_id() == aid
        # Credential round-trips.
        stored = json.loads(encryption.decrypt_value(row["credentials"]))
        assert stored["accessToken"] == "atok"

    def test_seed_idempotent(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict())
        first = svc.seed_from_credentials_file()
        second = svc.seed_from_credentials_file()
        assert first is not None
        assert second is None  # table already non-empty
        assert db.count_claude_accounts() == 1

    def test_seed_noop_without_key(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        encryption.clear_encryption_key()
        _write_file(oauth_tmp, _oauth_dict())
        assert svc.seed_from_credentials_file() is None
        assert db.count_claude_accounts() == 0

    def test_seed_noop_without_file(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        # No credentials file written.
        assert svc.seed_from_credentials_file() is None
        assert db.count_claude_accounts() == 0


# ---------------------------------------------------------------------------
# switch_account
# ---------------------------------------------------------------------------

class TestSwitch:
    def test_switch_writes_verbatim_preserving_subscription(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        # Seed account A (active) from the file.
        _write_file(oauth_tmp, _oauth_dict(access="A", sub="pro"))
        a_id = svc.seed_from_credentials_file()

        # Add account B with a distinct subscription type, without touching file.
        b_oauth = _oauth_dict(access="B", refresh="rB", sub="max")
        b_enc = encryption.encrypt_value(json.dumps(b_oauth))
        db.create_claude_account(
            account_id="acct-b", name="B", credentials=b_enc,
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=b_oauth["expiresAt"], has_refresh_token=True,
            subscription_type="max",
        )

        res = svc.switch_account("acct-b")
        assert res["success"], res

        # File now holds B verbatim, subscriptionType preserved.
        on_disk = json.loads((oauth_tmp.config_dir / ".credentials.json").read_text())
        assert on_disk["claudeAiOauth"]["accessToken"] == "B"
        assert on_disk["claudeAiOauth"]["subscriptionType"] == "max"

        assert svc.get_active_account_id() == "acct-b"
        assert db.get_claude_account("acct-b")["last_used_at"] is not None
        _ = a_id  # A remains a row

    def test_switch_invalidates_validity_cache(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A"))
        svc.seed_from_credentials_file()

        b_oauth = _oauth_dict(access="B")
        db.create_claude_account(
            account_id="acct-b", name="B",
            credentials=encryption.encrypt_value(json.dumps(b_oauth)),
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=b_oauth["expiresAt"], has_refresh_token=True,
        )

        with patch.object(oauth_tmp, "invalidate_credentials_cache") as inval:
            svc.switch_account("acct-b")
            inval.assert_called()

    def test_switch_syncs_back_diverged_outgoing_file_first(self, mock_db, oauth_tmp):
        """Before overwriting, the outgoing account's rotated token is captured."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="rA-old"))
        a_id = svc.seed_from_credentials_file()

        # Simulate a CLI subprocess rotating the refresh token on disk.
        rotated = _oauth_dict(access="A2", refresh="rA-NEW")
        # Write raw (bypass hook) then bust the mtime cache so sync sees change.
        (oauth_tmp.config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": rotated})
        )
        svc._last_synced_mtime = None

        # Add B and switch to it.
        b_oauth = _oauth_dict(access="B")
        db.create_claude_account(
            account_id="acct-b", name="B",
            credentials=encryption.encrypt_value(json.dumps(b_oauth)),
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=b_oauth["expiresAt"], has_refresh_token=True,
        )
        res = svc.switch_account("acct-b")
        assert res["success"]

        # Account A's row must now carry the ROTATED refresh token.
        a_row = db.get_claude_account(a_id, include_credentials=True)
        a_stored = json.loads(encryption.decrypt_value(a_row["credentials"]))
        assert a_stored["refreshToken"] == "rA-NEW"

    def test_switch_does_not_clobber_outgoing_row_via_post_write_hook(
        self, mock_db, oauth_tmp
    ):
        """Switching A→B must leave A's stored credential byte-identical.

        Regression for the post-write-hook clobber: the hook fires from inside
        write_credentials_dict (which switch uses to write B verbatim) BEFORE the
        active pointer moves to B. Without the suppression guard the hook sees
        file=B, pointer=A, fingerprint mismatch → it encrypts B's tokens into
        A's row (silent cross-account corruption). Register the REAL sync-back as
        the hook so the write path exercises it exactly as production does.
        """
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        # Seed account A (active) from the file.
        _write_file(oauth_tmp, _oauth_dict(access="A-access", refresh="A-refresh"))
        a_id = svc.seed_from_credentials_file()

        # Snapshot A's row exactly as stored right after seeding.
        a_before = db.get_claude_account(a_id, include_credentials=True)
        a_creds_before = a_before["credentials"]
        a_fp_before = a_before["credential_fingerprint"]

        # Add account B (distinct tokens), without touching the file.
        b_oauth = _oauth_dict(access="B-access", refresh="B-refresh")
        db.create_claude_account(
            account_id="acct-b", name="B",
            credentials=encryption.encrypt_value(json.dumps(b_oauth)),
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=b_oauth["expiresAt"], has_refresh_token=True,
        )

        # Wire the real production post-write hook.
        set_post_write_hook(svc.sync_back_active_account)

        res = svc.switch_account("acct-b")
        assert res["success"], res

        # A's row must be BYTE-IDENTICAL to before the switch.
        a_after = db.get_claude_account(a_id, include_credentials=True)
        assert a_after["credentials"] == a_creds_before
        assert a_after["credential_fingerprint"] == a_fp_before
        # And decrypt to A's own tokens, NOT B's.
        a_stored = json.loads(encryption.decrypt_value(a_after["credentials"]))
        assert a_stored["accessToken"] == "A-access"
        assert a_stored["refreshToken"] == "A-refresh"

        # B's row is untouched and the pointer now names B.
        b_after = db.get_claude_account("acct-b", include_credentials=True)
        b_stored = json.loads(encryption.decrypt_value(b_after["credentials"]))
        assert b_stored["accessToken"] == "B-access"
        assert svc.get_active_account_id() == "acct-b"

    def test_switch_encryption_locked(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc

        encryption.clear_encryption_key()
        res = svc.switch_account("whatever")
        assert res == {"success": False, "error": "encryption_locked"}

    def test_switch_not_found(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        res = svc.switch_account("missing")
        assert res["error"] == "not_found"


# ---------------------------------------------------------------------------
# sync_back_active_account matrix
# ---------------------------------------------------------------------------

class TestSyncBack:
    def test_no_active(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        assert svc.sync_back_active_account() == "no_active"

    def test_no_file(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        db.create_claude_account(
            account_id="a", name="A", credentials="enc:x",
            credential_fingerprint="fp", expires_at=1, has_refresh_token=True,
        )
        svc._set_active_account_id("a")
        # No file on disk.
        assert svc.sync_back_active_account() == "no_file"

    def test_clean_mtime_fast_path(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict())
        aid = svc.seed_from_credentials_file()
        # seed reset the mtime cache; a subsequent sync is the fast path.
        assert svc.sync_back_active_account() == "clean"
        _ = aid

    def test_diverged_with_key_synced(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(refresh="old"))
        aid = svc.seed_from_credentials_file()

        # Rotate file + bust cache.
        (oauth_tmp.config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": _oauth_dict(refresh="new")})
        )
        svc._last_synced_mtime = None

        assert svc.sync_back_active_account() == "synced"
        stored = json.loads(
            encryption.decrypt_value(
                db.get_claude_account(aid, include_credentials=True)["credentials"]
            )
        )
        assert stored["refreshToken"] == "new"

    def test_diverged_no_key_pending(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(refresh="old"))
        aid = svc.seed_from_credentials_file()

        # Rotate file, bust cache, then drop the key.
        (oauth_tmp.config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": _oauth_dict(refresh="new")})
        )
        svc._last_synced_mtime = None
        encryption.clear_encryption_key()

        assert svc.sync_back_active_account() == "pending"
        assert bool(db.get_claude_account(aid)["sync_pending"]) is True

    def test_db_error_swallowed(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict())
        svc.seed_from_credentials_file()
        svc._last_synced_mtime = None

        with patch("app.core.claude_accounts.db.get_claude_account",
                   side_effect=RuntimeError("boom")):
            # Must not raise; returns the swallowed sentinel.
            assert svc.sync_back_active_account() == "clean"


# ---------------------------------------------------------------------------
# post-write hook
# ---------------------------------------------------------------------------

class TestPostWriteHook:
    def test_hook_fires_on_write_credentials_dict(self, mock_db, oauth_tmp):
        calls = []
        set_post_write_hook(lambda: calls.append(1))
        oauth_tmp.write_credentials_dict(_oauth_dict())
        assert calls == [1]

    def test_raising_hook_does_not_break_write(self, mock_db, oauth_tmp):
        def _boom():
            raise RuntimeError("nope")

        set_post_write_hook(_boom)
        # Write still succeeds and the file is written.
        assert oauth_tmp.write_credentials_dict(_oauth_dict()) is True
        assert (oauth_tmp.config_dir / ".credentials.json").exists()


# ---------------------------------------------------------------------------
# add_account
# ---------------------------------------------------------------------------

class TestAddAccount:
    @pytest.fixture
    def fake_exchange(self, oauth_tmp):
        async def _exchange(code, state):
            return OAuthTokens(
                access_token="new-access", refresh_token="new-refresh",
                expires_at=9999999999000, scopes=["user:inference"],
                account_email="user@example.com",
            ), None
        with patch.object(oauth_tmp, "exchange_code_for_tokens", _exchange):
            yield

    async def test_add_inactive_never_touches_file(self, mock_db, oauth_tmp, fake_exchange):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        creds_path = oauth_tmp.config_dir / ".credentials.json"
        assert not creds_path.exists()

        res = await svc.add_account("code", "state", make_active=False)
        assert res["success"]
        # File was NOT written (adding never clobbers the active account).
        assert not creds_path.exists()
        # Name defaulted to the account email.
        assert res["account"]["name"] == "user@example.com"
        assert res["account"]["email"] == "user@example.com"
        assert db.count_claude_accounts() == 1

    async def test_add_make_active_writes_file(self, mock_db, oauth_tmp, fake_exchange):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        res = await svc.add_account("code", "state", name="Work", make_active=True)
        assert res["success"]
        assert res["account"]["name"] == "Work"

        on_disk = json.loads((oauth_tmp.config_dir / ".credentials.json").read_text())
        assert on_disk["claudeAiOauth"]["accessToken"] == "new-access"
        assert svc.get_active_account_id() == res["active_account_id"]

    async def test_add_encryption_locked(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        encryption.clear_encryption_key()
        res = await svc.add_account("code", "state")
        assert res == {"success": False, "error": "encryption_locked"}


# ---------------------------------------------------------------------------
# delete rules
# ---------------------------------------------------------------------------

class TestDelete:
    def _seed_two(self, oauth_tmp, db):
        from app.core import claude_accounts as svc
        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A"))
        a_id = svc.seed_from_credentials_file()
        b_oauth = _oauth_dict(access="B")
        db.create_claude_account(
            account_id="acct-b", name="B",
            credentials=encryption.encrypt_value(json.dumps(b_oauth)),
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=b_oauth["expiresAt"], has_refresh_token=True,
        )
        return a_id, "acct-b"

    def test_delete_active_with_siblings_rejected(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        a_id, _ = self._seed_two(oauth_tmp, db)
        res = svc.delete_account(a_id)
        assert res["error"] == "active_with_siblings"
        assert db.get_claude_account(a_id) is not None

    def test_delete_inactive_ok(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        _a_id, b_id = self._seed_two(oauth_tmp, db)
        res = svc.delete_account(b_id)
        assert res["success"]
        assert db.get_claude_account(b_id) is None

    def test_delete_sole_active_is_logout(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict())
        a_id = svc.seed_from_credentials_file()

        res = svc.delete_account(a_id)
        assert res["success"]
        assert db.get_claude_account(a_id) is None
        assert svc.get_active_account_id() is None
        # Credentials file deleted (logout semantics).
        assert not (oauth_tmp.config_dir / ".credentials.json").exists()

    def test_delete_not_found(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        _set_key(db)
        assert svc.delete_account("nope")["error"] == "not_found"


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

class TestRename:
    def test_rename(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict())
        a_id = svc.seed_from_credentials_file()
        res = svc.rename_account(a_id, "Renamed")
        assert res["success"]
        assert db.get_claude_account(a_id)["name"] == "Renamed"

    def test_rename_not_found(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db
        _set_key(db)
        assert svc.rename_account("nope", "x")["error"] == "not_found"
