"""R6 (binding) — account failover must NO-OP for non-Anthropic provider sessions.

A gateway 429 is not an Anthropic-account limit; rotating Claude accounts +
evicting the pooled session would be pure damage. ``attempt_failover`` gates on
the session's resolved profile carrying a non-None ``provider``.

The gate reads session → profile via the ``database`` module (looked up lazily
inside ``_session_uses_provider``), so patching ``app.db.database.get_session`` /
``get_profile`` reaches the consumer's binding (mock-freeze gotcha honored).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import account_failover as af
from app.core import encryption


def _seed_two_and_activate(db):
    from app.core import claude_accounts as svc
    encryption.set_encryption_key("pw", db)
    db.create_claude_account(
        account_id="a", name="a", credentials="enc:x",
        credential_fingerprint="fp", expires_at=9999999999000, has_refresh_token=True,
    )
    db.create_claude_account(
        account_id="b", name="b", credentials="enc:x",
        credential_fingerprint="fp", expires_at=9999999999000, has_refresh_token=True,
    )
    svc._set_active_account_id("a")


@pytest.fixture(autouse=True)
def _reset_failover_state():
    af._last_switch_at = 0.0
    yield
    af._last_switch_at = 0.0


# ---------------------------------------------------------------------------
# _session_uses_provider (the gate's resolver)
# ---------------------------------------------------------------------------

class TestSessionUsesProvider:
    def test_none_session_id(self):
        assert af._session_uses_provider(None) is False

    def test_provider_profile_true(self):
        with patch("app.db.database.get_session", return_value={"profile_id": "p"}), \
             patch("app.db.database.get_profile",
                   return_value={"config": {"provider": "op"}}):
            assert af._session_uses_provider("s") is True

    def test_native_profile_false(self):
        with patch("app.db.database.get_session", return_value={"profile_id": "p"}), \
             patch("app.db.database.get_profile",
                   return_value={"config": {"model": "opus"}}):
            assert af._session_uses_provider("s") is False

    def test_missing_session_false(self):
        with patch("app.db.database.get_session", return_value=None):
            assert af._session_uses_provider("s") is False

    def test_lookup_error_treated_native(self):
        with patch("app.db.database.get_session", side_effect=RuntimeError("boom")):
            assert af._session_uses_provider("s") is False

    def test_pooled_state_with_base_url_gated_even_when_config_native(self):
        """FIX 8: the LIVE pooled subprocess env wins over the profile config. A
        pooled state carrying ANTHROPIC_BASE_URL gates as a provider session even
        if the profile config has NO provider (mid-session profile edit)."""
        state = MagicMock()
        state.connect_options = MagicMock()
        state.connect_options.env = {"ANTHROPIC_BASE_URL": "https://gw.example"}
        pool = MagicMock()
        pool.peek.return_value = state

        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.db.database.get_session",
                   return_value={"profile_id": "p"}), \
             patch("app.db.database.get_profile",
                   return_value={"config": {"model": "opus"}}):  # native config
            assert af._session_uses_provider("s") is True
        pool.peek.assert_called_once_with("s")  # peek, not get (no LRU promotion)

    def test_pooled_native_state_wins_over_provider_config(self):
        """FIX 8: reverse — a native pooled state (no base URL) gates as native
        even if the profile config was edited mid-session to ADD a provider."""
        state = MagicMock()
        state.connect_options = MagicMock()
        state.connect_options.env = {}  # no ANTHROPIC_BASE_URL
        pool = MagicMock()
        pool.peek.return_value = state

        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.db.database.get_session",
                   return_value={"profile_id": "p"}), \
             patch("app.db.database.get_profile",
                   return_value={"config": {"provider": "op"}}):  # provider config
            assert af._session_uses_provider("s") is False

    def test_not_pooled_falls_back_to_config(self):
        """FIX 8: when the session is not pooled (peek → None), fall back to the
        profile-config read."""
        pool = MagicMock()
        pool.peek.return_value = None

        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.db.database.get_session",
                   return_value={"profile_id": "p"}), \
             patch("app.db.database.get_profile",
                   return_value={"config": {"provider": "op"}}):
            assert af._session_uses_provider("s") is True


# ---------------------------------------------------------------------------
# attempt_failover gate
# ---------------------------------------------------------------------------

class TestAttemptFailoverProviderGate:
    async def test_provider_session_no_switch(self, mock_db):
        """Even with the toggle ON and >=2 accounts, a provider session must not
        rotate accounts: retry=False and switch_account is never called."""
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        with patch.object(af, "_session_uses_provider", return_value=True), \
             patch("app.core.claude_accounts.switch_account") as sw, \
             patch.object(af, "_notify", new=AsyncMock()) as notify:
            decision = await af.attempt_failover("sess-prov", None, set())

        assert decision.retry is False
        sw.assert_not_called()
        notify.assert_not_awaited()  # silent no-op; no exhausted toast

    async def test_native_session_still_switches(self, mock_db):
        """Regression: a native session with the gate returning False still runs
        the normal switch path."""
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        pool = MagicMock()
        pool.remove_broken = AsyncMock()

        with patch.object(af, "_session_uses_provider", return_value=False), \
             patch("app.core.claude_accounts.switch_account",
                   return_value={"success": True, "active_account_id": "b"}) as sw, \
             patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch.object(af, "_notify", new=AsyncMock()), \
             patch.object(af, "schedule_usage_refresh"):
            decision = await af.attempt_failover("sess-1", None, set())

        assert decision.retry is True
        sw.assert_called_once_with("b")
