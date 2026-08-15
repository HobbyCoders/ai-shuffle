"""Tests for the multiple-Claude-accounts core service.

Exercises the DB-backed account registry, the credentials-file sync-back logic,
switching (verbatim writes + sync-back-first), add_account, seeding, delete
rules, and the oauth post-write hook — all over the pg_shim SQLite DB.
"""

import json
import time
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
    svc._last_used_stamp_monotonic = None
    svc._last_dangling_warn = 0.0
    with patch("app.core.claude_accounts.direct_oauth", inst), \
            patch("app.core.oauth.direct_oauth", inst):
        yield inst
    svc._last_synced_mtime = None
    svc._last_used_stamp_monotonic = None
    svc._last_dangling_warn = 0.0
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

    def test_switch_bumps_usage_generation(self, mock_db, oauth_tmp):
        """A successful switch must invalidate any in-flight usage poll so the
        indicator can re-poll the new account without an old poll overwriting it.
        """
        from app.core import claude_accounts as svc
        from app.core.usage_limits import usage_limits_monitor
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

        before = usage_limits_monitor.current_generation()
        assert svc.switch_account("acct-b")["success"]
        # Strictly greater (bump may jump to the current epoch, not just +1).
        assert usage_limits_monitor.current_generation() > before

    def test_failed_switch_does_not_bump_generation(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.core.usage_limits import usage_limits_monitor
        from app.db import database as db

        _set_key(db)
        before = usage_limits_monitor.current_generation()
        assert "error" in svc.switch_account("missing")
        assert usage_limits_monitor.current_generation() == before

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
# switch_account: refresh-on-switch for a stored inactive account whose
# short-lived access token expired while idle (the multi-account bug).
# ---------------------------------------------------------------------------

class TestSwitchRefresh:
    def _seed_active_a(self, svc, oauth_tmp):
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="rA"))
        return svc.seed_from_credentials_file()

    def _add_b(self, svc, db, *, refresh, expires):
        b_oauth = _oauth_dict(access="B-old", refresh=refresh, expires=expires)
        db.create_claude_account(
            account_id="acct-b", name="B",
            credentials=encryption.encrypt_value(json.dumps(b_oauth)),
            credential_fingerprint=svc._fingerprint(b_oauth),
            expires_at=expires, has_refresh_token=bool(refresh),
        )

    def test_expired_target_with_refresh_token_is_refreshed_and_persisted(
        self, mock_db, oauth_tmp
    ):
        """Switching to a secondary whose access token expired while idle must
        refresh it (using its own refresh token), activate the FRESH token, and
        persist the rotation back to the row — not present the stale token."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        self._seed_active_a(svc, oauth_tmp)
        expired = int(time.time() * 1000) - 60_000
        self._add_b(svc, db, refresh="rB", expires=expired)

        fresh = _oauth_dict(
            access="B-NEW", refresh="rB2", expires=int(time.time() * 1000) + 3_600_000
        )
        with patch.object(oauth_tmp, "refresh_oauth_dict", return_value=(fresh, None)) as rf:
            res = svc.switch_account("acct-b")

        assert res["success"], res
        rf.assert_called_once()
        # Disk holds the REFRESHED credential (what SDK subprocesses will read).
        on_disk = json.loads((oauth_tmp.config_dir / ".credentials.json").read_text())
        assert on_disk["claudeAiOauth"]["accessToken"] == "B-NEW"
        # The row was updated so the stored secondary no longer goes stale and the
        # UI reports it valid.
        row = db.get_claude_account("acct-b", include_credentials=True)
        stored = json.loads(encryption.decrypt_value(row["credentials"]))
        assert stored["accessToken"] == "B-NEW"
        assert stored["refreshToken"] == "rB2"
        assert row["expires_at"] == fresh["expiresAt"]
        assert svc.get_active_account_id() == "acct-b"

    def test_expired_target_without_refresh_token_errors_and_keeps_active(
        self, mock_db, oauth_tmp
    ):
        """Expired secondary with NO refresh token (e.g. a credentials.json shared
        with Claude Desktop that carried an empty refreshToken) → actionable
        error, no crash, and the current active account is left untouched."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        a_id = self._seed_active_a(svc, oauth_tmp)
        expired = int(time.time() * 1000) - 60_000
        self._add_b(svc, db, refresh="", expires=expired)

        with patch.object(oauth_tmp, "refresh_oauth_dict") as rf:
            res = svc.switch_account("acct-b")

        assert res["success"] is False
        assert res["error"] == "reauth_required"
        rf.assert_not_called()  # nothing to refresh with
        # Active pointer unchanged and the file still holds account A verbatim.
        assert svc.get_active_account_id() == a_id
        on_disk = json.loads((oauth_tmp.config_dir / ".credentials.json").read_text())
        assert on_disk["claudeAiOauth"]["accessToken"] == "A"

    def test_expired_target_refresh_failure_errors_and_keeps_active(
        self, mock_db, oauth_tmp
    ):
        """A stored refresh token that the endpoint rejects (revoked) must abort
        the switch with ``refresh_failed`` rather than activating a dead token."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        a_id = self._seed_active_a(svc, oauth_tmp)
        expired = int(time.time() * 1000) - 60_000
        self._add_b(svc, db, refresh="rB", expires=expired)

        with patch.object(
            oauth_tmp, "refresh_oauth_dict",
            return_value=(None, "refresh_failed: 401 invalid_grant"),
        ) as rf:
            res = svc.switch_account("acct-b")

        assert res["success"] is False
        assert res["error"] == "refresh_failed"
        rf.assert_called_once()
        assert svc.get_active_account_id() == a_id

    def test_valid_target_is_activated_without_a_refresh_call(self, mock_db, oauth_tmp):
        """A target whose token is still comfortably valid must NOT trigger a
        network refresh (no needless OAuth round-trip on every switch)."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        self._seed_active_a(svc, oauth_tmp)
        future = int(time.time() * 1000) + 3_600_000
        self._add_b(svc, db, refresh="rB", expires=future)

        with patch.object(oauth_tmp, "refresh_oauth_dict") as rf:
            res = svc.switch_account("acct-b")

        assert res["success"], res
        rf.assert_not_called()
        assert svc.get_active_account_id() == "acct-b"


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

        # add_account also asks the OAuth profile endpoint what plan the new
        # account is on — stubbed so the suite stays offline.
        async def _plan(token):
            return "max"

        with patch.object(oauth_tmp, "exchange_code_for_tokens", _exchange), \
                patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
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

    async def test_add_stores_the_subscription_plan(self, mock_db, oauth_tmp, fake_exchange):
        """The token exchange doesn't report the plan, so add_account asks the
        profile endpoint and seeds the row (and its credential blob) with it —
        no waiting for the usage poller to heal a NULL."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        res = await svc.add_account("code", "state", make_active=False)

        assert res["account"]["subscription_type"] == "max"
        row = db.get_claude_account(res["account"]["id"], include_credentials=True)
        stored = json.loads(encryption.decrypt_value(row["credentials"]))
        assert stored["subscriptionType"] == "max"

    async def test_add_survives_an_unavailable_profile_endpoint(
        self, mock_db, oauth_tmp
    ):
        """A failing plan lookup must never fail the login — the row lands with
        a NULL plan and the usage poller heals it later."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        async def _exchange(code, state):
            return OAuthTokens(
                access_token="new-access", refresh_token="new-refresh",
                expires_at=9999999999000, scopes=["user:inference"],
                account_email="user@example.com",
            ), None

        async def _boom(token):
            raise RuntimeError("profile endpoint down")

        _set_key(db)
        with patch.object(oauth_tmp, "exchange_code_for_tokens", _exchange), \
                patch.object(oauth_tmp, "fetch_subscription_plan", _boom):
            res = await svc.add_account("code", "state", make_active=False)

        assert res["success"]
        assert res["account"]["subscription_type"] is None

    async def test_add_encryption_locked(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        encryption.clear_encryption_key()
        res = await svc.add_account("code", "state")
        assert res == {"success": False, "error": "encryption_locked"}


# ---------------------------------------------------------------------------
# ensure_subscription_type (plan heal/refresh, driven by the usage poller)
# ---------------------------------------------------------------------------

class TestEnsureSubscriptionType:
    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        from app.core import claude_accounts as svc
        svc._plan_refresh = {"account_id": None, "at": 0.0}
        yield
        svc._plan_refresh = {"account_id": None, "at": 0.0}

    def _seed_active(self, db, subscription_type=None):
        from app.core import claude_accounts as svc
        db.create_claude_account(
            account_id="acct-1", name="Primary", credentials="enc:x",
            credential_fingerprint=None, expires_at=None, has_refresh_token=True,
            subscription_type=subscription_type,
        )
        svc._set_active_account_id("acct-1")

    async def test_heals_a_null_plan(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db)
        _write_file(oauth_tmp, _oauth_dict())

        async def _plan(token):
            return "max"

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()

        assert db.get_claude_account("acct-1")["subscription_type"] == "max"

    async def test_tracks_a_plan_change(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db, subscription_type="pro")
        _write_file(oauth_tmp, _oauth_dict())

        async def _plan(token):
            return "max"

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()

        assert db.get_claude_account("acct-1")["subscription_type"] == "max"

    async def test_throttles_to_one_lookup_per_interval(self, mock_db, oauth_tmp):
        """The plan changes about never; the poller runs every ~5 min. One
        profile GET per hour per account, and a FAILED lookup waits too (the
        stamp is recorded before the fetch)."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db)
        _write_file(oauth_tmp, _oauth_dict())
        calls = []

        async def _plan(token):
            calls.append(token)
            return None  # endpoint unavailable

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()
            await svc.ensure_subscription_type()
            await svc.ensure_subscription_type()

        assert len(calls) == 1

    async def test_a_different_active_account_bypasses_the_throttle(
        self, mock_db, oauth_tmp
    ):
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db)
        db.create_claude_account(
            account_id="acct-2", name="Second", credentials="enc:y",
            credential_fingerprint=None, expires_at=None, has_refresh_token=True,
        )
        _write_file(oauth_tmp, _oauth_dict())
        calls = []

        async def _plan(token):
            calls.append(token)
            return "pro"

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()
            svc._set_active_account_id("acct-2")
            await svc.ensure_subscription_type()

        assert len(calls) == 2
        assert db.get_claude_account("acct-2")["subscription_type"] == "pro"

    async def test_no_token_makes_no_request(self, mock_db, oauth_tmp):
        """API-key mode / logged out — nothing to ask the profile endpoint."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db)
        calls = []

        async def _plan(token):
            calls.append(token)
            return "max"

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()

        assert calls == []
        assert db.get_claude_account("acct-1")["subscription_type"] is None

    async def test_no_active_account_is_a_noop(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        await svc.ensure_subscription_type()  # must not raise

    async def test_unknown_plan_leaves_the_row_alone(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db, subscription_type="max")
        _write_file(oauth_tmp, _oauth_dict())

        async def _plan(token):
            return None

        with patch.object(oauth_tmp, "fetch_subscription_plan", _plan):
            await svc.ensure_subscription_type()

        # A failed lookup must never blank a known plan.
        assert db.get_claude_account("acct-1")["subscription_type"] == "max"

    async def test_never_raises_on_a_db_failure(self, mock_db, oauth_tmp, monkeypatch):
        from app.core import claude_accounts as svc
        from app.db import database as db

        self._seed_active(db)
        _write_file(oauth_tmp, _oauth_dict())

        def _raise(*_args, **_kwargs):
            raise Exception("db down")

        monkeypatch.setattr(db, "get_claude_account", _raise)
        await svc.ensure_subscription_type()  # must not raise


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


# ---------------------------------------------------------------------------
# last_used_at (per-account usage stamp)
# ---------------------------------------------------------------------------

class TestLastUsed:
    """last_used_at must advance per-account for the account actually in use,
    persist in the DB, and never advance for idle siblings.

    Regression: it used to be written ONLY on an explicit switch, so the account
    you were actually using never had its timestamp updated — every account
    showed the same stale/blank "last used" and real usage was never recorded.
    """

    def _seed_three(self, svc, db, oauth_tmp):
        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A"))
        a = svc.seed_from_credentials_file()
        for name in ("acct-b", "acct-c"):
            o = _oauth_dict(access=name)
            enc = encryption.encrypt_value(json.dumps(o))
            db.create_claude_account(
                account_id=name, name=name, credentials=enc,
                credential_fingerprint=svc._fingerprint(o),
                expires_at=o["expiresAt"], has_refresh_token=True,
            )
        return a

    def test_switch_stamps_only_incoming(self, mock_db, oauth_tmp):
        """Switching stamps the INCOMING account only; siblings stay untouched."""
        from app.core import claude_accounts as svc
        from app.db import database as db
        a = self._seed_three(svc, db, oauth_tmp)

        svc.switch_account("acct-b")
        rows = {r["id"]: r for r in db.get_all_claude_accounts()}
        assert rows["acct-b"]["last_used_at"] is not None
        assert rows["acct-c"]["last_used_at"] is None
        assert rows[a]["last_used_at"] is None  # switched away, not "used" by it

    def test_use_advances_only_active(self, mock_db, oauth_tmp):
        """A hot status poll / post-refresh sync-back advances ONLY the active
        account — not idle siblings (the core of the reported bug)."""
        import time
        from app.core import claude_accounts as svc
        from app.db import database as db
        a = self._seed_three(svc, db, oauth_tmp)

        svc.switch_account("acct-b")
        b_after_switch = db.get_claude_account("acct-b")["last_used_at"]

        # Simulate the active account (B) being used: the file on disk holds B's
        # credential and a status endpoint runs sync-back.
        svc._last_used_stamp_monotonic = None  # clear throttle (time passed)
        time.sleep(0.01)
        assert svc.sync_back_active_account() in ("clean", "synced")

        rows = {r["id"]: r for r in db.get_all_claude_accounts()}
        assert rows["acct-b"]["last_used_at"] != b_after_switch  # advanced
        assert rows["acct-c"]["last_used_at"] is None             # idle sibling
        assert rows[a]["last_used_at"] is None                    # idle sibling

    def test_record_active_account_used_persists_in_db(self, mock_db, oauth_tmp):
        """The stamp lives in the DB (survives module-state reset ~ restart)."""
        from app.core import claude_accounts as svc
        from app.db import database as db
        self._seed_three(svc, db, oauth_tmp)
        svc.switch_account("acct-b")

        svc.record_active_account_used()
        saved = db.get_claude_account("acct-b")["last_used_at"]
        assert saved is not None

        # Simulate a process restart: wipe in-memory state, re-read from DB.
        svc._last_synced_mtime = None
        svc._last_used_stamp_monotonic = None
        assert db.get_claude_account("acct-b")["last_used_at"] == saved

    def test_record_throttled(self, mock_db, oauth_tmp):
        """Repeated in-use signals are throttled to bound the DB write rate."""
        import time
        from app.core import claude_accounts as svc
        from app.db import database as db
        self._seed_three(svc, db, oauth_tmp)
        svc.switch_account("acct-b")

        svc._last_used_stamp_monotonic = None
        svc.record_active_account_used()
        first = db.get_claude_account("acct-b")["last_used_at"]
        time.sleep(0.01)
        svc.record_active_account_used()  # within throttle window → no-op
        assert db.get_claude_account("acct-b")["last_used_at"] == first


# ---------------------------------------------------------------------------
# dangling active-pointer repair
# ---------------------------------------------------------------------------

class TestDanglingPointerRepair:
    """The per-install active pointer can name an id with no row behind it —
    typically because ANOTHER install deleted the (shared) account row. It used
    to stay broken forever: sync-back returned ``no_active`` and usage snapshots
    were stamped with an id that resolved to no email/name.
    """

    def _add_row(self, svc, db, account_id, *, access):
        o = _oauth_dict(access=access)
        db.create_claude_account(
            account_id=account_id, name=account_id,
            credentials=encryption.encrypt_value(json.dumps(o)),
            credential_fingerprint=svc._fingerprint(o),
            expires_at=o["expiresAt"], has_refresh_token=True,
        )
        return o

    def _rotate_file_raw(self, svc, oauth_tmp, **kwargs):
        """Rewrite the credentials file bypassing the post-write hook."""
        rotated = _oauth_dict(**kwargs)
        (oauth_tmp.config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": rotated})
        )
        svc._last_synced_mtime = None
        return rotated

    def test_no_pointer_resolves_to_none(self, mock_db, oauth_tmp):
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        assert svc.resolve_active_account_row() is None

    def test_fingerprint_match_repairs_pointer(self, mock_db, oauth_tmp):
        """With several accounts stored, the row whose stored fingerprint equals
        the on-disk credential is unambiguously the one in use — adopt it."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A"))
        a_id = svc.seed_from_credentials_file()
        # A sibling with a DIFFERENT fingerprint, so the sole-account fallback
        # cannot be what repairs the pointer.
        self._add_row(svc, db, "acct-b", access="B")

        svc._set_active_account_id("nonexistent")
        row = svc.resolve_active_account_row()

        assert row is not None
        assert row["id"] == a_id
        assert svc.get_active_account_id() == a_id

    def test_sole_account_fallback_repairs_rotated_pointer(self, mock_db, oauth_tmp):
        """The common case: while the pointer dangles nothing syncs back, so the
        file rotates away from every stored fingerprint. One stored account = no
        ambiguity, so adopt it anyway."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="old"))
        a_id = svc.seed_from_credentials_file()
        self._rotate_file_raw(svc, oauth_tmp, access="A", refresh="ROTATED")

        svc._set_active_account_id("nonexistent")
        row = svc.resolve_active_account_row()

        assert row is not None
        assert row["id"] == a_id
        assert svc.get_active_account_id() == a_id

    def test_multi_account_no_match_leaves_pointer_alone(self, mock_db, oauth_tmp):
        """Two candidates and no fingerprint match → guessing could write one
        account's tokens into another's row, so refuse and leave it dangling."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="old"))
        svc.seed_from_credentials_file()
        self._add_row(svc, db, "acct-b", access="B")
        self._rotate_file_raw(svc, oauth_tmp, access="A", refresh="ROTATED")

        svc._set_active_account_id("nonexistent")
        assert svc.resolve_active_account_row() is None
        assert svc.get_active_account_id() == "nonexistent"

    def test_sync_back_self_heals_dangling_pointer(self, mock_db, oauth_tmp):
        """End-to-end: a repaired pointer falls through to the normal sync flow,
        so the adopted row's credentials are reconciled from disk immediately."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="old"))
        a_id = svc.seed_from_credentials_file()
        rotated = self._rotate_file_raw(svc, oauth_tmp, access="A", refresh="ROTATED")

        svc._set_active_account_id("nonexistent")
        assert svc.sync_back_active_account() == "synced"

        assert svc.get_active_account_id() == a_id
        row = db.get_claude_account(a_id, include_credentials=True)
        assert row["credential_fingerprint"] == svc._fingerprint(rotated)
        stored = json.loads(encryption.decrypt_value(row["credentials"]))
        assert stored["refreshToken"] == "ROTATED"

    def test_delete_clears_other_installs_pointers(self, mock_db, oauth_tmp):
        """Deleting the row must not leave a peer install pointing at it."""
        from app.core import claude_accounts as svc
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A"))
        a_id = svc.seed_from_credentials_file()
        db.set_system_setting("claude_active_account:other-install", a_id)

        assert svc.delete_account(a_id)["success"]

        assert db.get_system_setting("claude_active_account:other-install") is None
        assert svc.get_active_account_id() is None

    def test_usage_meta_all_none_on_unrepairable_dangle(self, mock_db, oauth_tmp):
        """An unrepairable dangle must NOT stamp usage snapshots with an id that
        resolves to no email/name."""
        from app.core import claude_accounts as svc
        from app.core.usage_limits import usage_limits_monitor
        from app.db import database as db

        _set_key(db)
        _write_file(oauth_tmp, _oauth_dict(access="A", refresh="old"))
        svc.seed_from_credentials_file()
        self._add_row(svc, db, "acct-b", access="B")
        self._rotate_file_raw(svc, oauth_tmp, access="A", refresh="ROTATED")
        svc._set_active_account_id("nonexistent")

        meta = usage_limits_monitor._resolve_account_meta()
        assert meta == {
            "account_id": None, "account_email": None, "account_name": None,
        }
