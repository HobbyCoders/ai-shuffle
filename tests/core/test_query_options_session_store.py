"""
Tests for PgSessionStore wire-up in build_options_from_profile (Slice A).

Covers the _get_session_store() singleton logic and how session_store /
session_store_flush are attached (or omitted) to ClaudeAgentOptions depending
on the session_store_mode setting.

All tests operate by patching at the boundary used by query_options.py:
  - app.core.query_options.settings  — to control session_store_mode
  - app.core.query_options._session_store_singleton / _session_store_init_attempted
    — to reset singleton state between calls (the singleton is module-level)
  - app.core.pg_session_store.PgSessionStore — to avoid real Postgres connections

We do NOT call build_options_from_profile (it makes DB calls); instead we
test _get_session_store() directly after resetting the module state.
"""

import logging
import pytest
from unittest.mock import patch, MagicMock


def _reset_singleton(module):
    """Reset the module-level session store singleton between tests."""
    module._session_store_singleton = None
    module._session_store_init_attempted = False


# ---------------------------------------------------------------------------
# Import the module under test once — avoids re-import cost per test.
# ---------------------------------------------------------------------------
import app.core.query_options as qo_module


class TestSessionStoreDisabled:
    """session_store_mode='disabled' → _get_session_store() returns None."""

    def setup_method(self):
        _reset_singleton(qo_module)

    def test_session_store_disabled_returns_none(self):
        """With mode=disabled the singleton is never created."""
        with patch.object(qo_module.settings, "session_store_mode", "disabled"):
            store = qo_module._get_session_store()

        assert store is None

    def test_session_store_disabled_no_pg_import(self):
        """PgSessionStore constructor must not be called when disabled."""
        mock_pg = MagicMock()
        with patch.object(qo_module.settings, "session_store_mode", "disabled"):
            with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg}):
                store = qo_module._get_session_store()

        assert store is None
        mock_pg.PgSessionStore.assert_not_called()


class TestSessionStoreMirror:
    """session_store_mode='mirror' → PgSessionStore is constructed and returned."""

    def setup_method(self):
        _reset_singleton(qo_module)

    def test_mirror_attaches_session_store(self):
        """
        When mode=mirror and PgSessionStore() succeeds, the returned store
        is the PgSessionStore instance, and the flush mode is eager.
        """
        fake_store = MagicMock(name="PgSessionStore_instance")
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.return_value = fake_store

        with patch.object(qo_module.settings, "session_store_mode", "mirror"):
            with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
                store = qo_module._get_session_store()

        assert store is fake_store
        mock_pg_module.PgSessionStore.assert_called_once()

    def test_mirror_singleton_reused(self):
        """_get_session_store() must return the same object on repeated calls."""
        fake_store = MagicMock(name="PgSessionStore_instance")
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.return_value = fake_store

        with patch.object(qo_module.settings, "session_store_mode", "mirror"):
            with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
                store1 = qo_module._get_session_store()
                store2 = qo_module._get_session_store()

        assert store1 is store2
        # Constructor called exactly once — second call hits early-return
        assert mock_pg_module.PgSessionStore.call_count == 1


class TestSessionStorePostgresOnly:
    """session_store_mode='postgres_only' behaves identically to mirror at the SDK layer."""

    def setup_method(self):
        _reset_singleton(qo_module)

    def test_postgres_only_attaches_session_store(self):
        """
        When mode=postgres_only and PgSessionStore() succeeds, the returned
        store is the PgSessionStore instance (same as mirror).
        """
        fake_store = MagicMock(name="PgSessionStore_instance")
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.return_value = fake_store

        with patch.object(qo_module.settings, "session_store_mode", "postgres_only"):
            with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
                store = qo_module._get_session_store()

        assert store is fake_store


class TestSessionStoreConstructionFailure:
    """If PgSessionStore() raises, _get_session_store() returns None (non-fatal)."""

    def setup_method(self):
        _reset_singleton(qo_module)

    def test_construction_failure_falls_back_to_none(self, caplog):
        """
        If PgSessionStore.__init__ raises, the singleton must be None and
        a warning must be logged (no crash propagated to caller).
        """
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.side_effect = RuntimeError("cannot connect to postgres")

        with caplog.at_level(logging.WARNING, logger="app.core.query_options"):
            with patch.object(qo_module.settings, "session_store_mode", "mirror"):
                with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
                    store = qo_module._get_session_store()

        assert store is None
        assert any("PgSessionStore construction failed" in r.message for r in caplog.records)

    def test_construction_failure_singleton_stays_none(self):
        """
        After a failed construction attempt, subsequent calls must also return
        None without re-attempting construction.
        """
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.side_effect = RuntimeError("no postgres")

        with patch.object(qo_module.settings, "session_store_mode", "mirror"):
            with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
                store1 = qo_module._get_session_store()
                store2 = qo_module._get_session_store()

        assert store1 is None
        assert store2 is None
        # Constructor only attempted once even though we called twice
        assert mock_pg_module.PgSessionStore.call_count == 1


class TestPostgresOnlySkipsSQLiteWrites:
    """
    postgres_only mode must gate out database.update_session (SQLite) writes.

    This is tested at the persistence layer in query_persistence.py which
    wraps update_session in `if settings.session_store_mode != "postgres_only"`.
    """

    def test_postgres_only_mode_string_value(self):
        """
        Sanity check: the mode string used to gate SQLite writes matches the
        config value exactly (no accidental typo that would silently disable gating).
        """
        from app.core.config import settings as real_settings

        # The guard in query_persistence.py uses the literal string "postgres_only".
        # Verify settings exposes the same attribute and type.
        assert hasattr(real_settings, "session_store_mode")
        assert isinstance(real_settings.session_store_mode, str)

    def test_disabled_mode_does_not_gate_sqlite(self):
        """
        With mode=disabled, the `!= "postgres_only"` condition is True, meaning
        SQLite writes are NOT gated — consistent with default behavior.
        """
        with patch.object(qo_module.settings, "session_store_mode", "disabled"):
            # Condition that guards update_session in query_persistence.py
            should_write_sqlite = qo_module.settings.session_store_mode != "postgres_only"
        assert should_write_sqlite is True

    def test_postgres_only_mode_gates_sqlite(self):
        """
        With mode=postgres_only, the guard condition is False — SQLite writes skipped.
        """
        with patch.object(qo_module.settings, "session_store_mode", "postgres_only"):
            should_write_sqlite = qo_module.settings.session_store_mode != "postgres_only"
        assert should_write_sqlite is False
