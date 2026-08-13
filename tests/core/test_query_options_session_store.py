"""
Tests for PgSessionStore wire-up in build_options_from_profile.

Covers the _get_session_store() singleton logic. Postgres is the single source
of truth for session transcripts (the old SESSION_STORE_MODE flag was removed
after the cutover), so the store is always constructed — the only variation
left is construction success vs failure.

All tests operate by patching at the boundary used by query_options.py:
  - app.core.query_options._session_store_singleton / _session_store_init_attempted
    — to reset singleton state between calls (the singleton is module-level)
  - app.core.pg_session_store.PgSessionStore — to avoid real Postgres connections

We do NOT call build_options_from_profile (it makes DB calls); instead we
test _get_session_store() directly after resetting the module state.
"""

import logging
from unittest.mock import patch, MagicMock


def _reset_singleton(module):
    """Reset the module-level session store singleton between tests."""
    module._session_store_singleton = None
    module._session_store_init_attempted = False


# ---------------------------------------------------------------------------
# Import the module under test once — avoids re-import cost per test.
# ---------------------------------------------------------------------------
import app.core.query_options as qo_module


class TestSessionStoreAttached:
    """The PgSessionStore is always constructed and returned."""

    def setup_method(self):
        _reset_singleton(qo_module)

    def test_attaches_session_store(self):
        """When PgSessionStore() succeeds, the returned store is the instance."""
        fake_store = MagicMock(name="PgSessionStore_instance")
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.return_value = fake_store

        with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
            store = qo_module._get_session_store()

        assert store is fake_store
        mock_pg_module.PgSessionStore.assert_called_once()

    def test_singleton_reused(self):
        """_get_session_store() must return the same object on repeated calls."""
        fake_store = MagicMock(name="PgSessionStore_instance")
        mock_pg_module = MagicMock()
        mock_pg_module.PgSessionStore.return_value = fake_store

        with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
            store1 = qo_module._get_session_store()
            store2 = qo_module._get_session_store()

        assert store1 is store2
        # Constructor called exactly once — second call hits early-return
        assert mock_pg_module.PgSessionStore.call_count == 1


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

        with patch.dict("sys.modules", {"app.core.pg_session_store": mock_pg_module}):
            store1 = qo_module._get_session_store()
            store2 = qo_module._get_session_store()

        assert store1 is None
        assert store2 is None
        # Constructor only attempted once even though we called twice
        assert mock_pg_module.PgSessionStore.call_count == 1
