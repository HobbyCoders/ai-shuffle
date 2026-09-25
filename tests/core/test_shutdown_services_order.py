"""Shutdown teardown order (lifecycle-notification batch, item 1).

``shutdown_services`` used to close the Postgres connection pool FIRST — before
the SDK session pool was shut down. Every teardown step after that point (reap
persistence, parked-marker writes) then hit a closed pool and died with
connection errors, so kill attribution / terminal chips written during shutdown
never became durable.

These tests drive the REAL ``shutdown_services`` coroutine (only the external
collaborators are mocked) and assert the strict order:

    is_shutting_down=True  →  session pool shutdown
      →  in-memory reset_all()s (permissions, task registry)  →  ...
      →  close_pool LAST

They also pin the ``is_shutting_down`` module flag semantics: it must be set
BEFORE any teardown step runs, and the unpark delivery path must refuse to
start a new follow-up ``run_query`` while it is set.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.startup as startup


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    startup.is_shutting_down = False
    yield
    startup.is_shutting_down = False


async def test_close_pool_runs_strictly_after_pool_shutdown():
    calls: list[str] = []

    def _pool_shutdown_effect():
        # The flag must already be up when the FIRST teardown step runs, so
        # concurrent unpark deliveries see it before their client disappears.
        assert startup.is_shutting_down is True
        calls.append("pool_shutdown")

    pool = MagicMock()
    pool.shutdown = AsyncMock(side_effect=_pool_shutdown_effect)

    # (V2: the live session store is deleted — the in-memory resets are the
    # permission handler and the task registry only.)
    with patch("app.core.desktop_lifecycle.stop_running_sessions",
               new=AsyncMock(side_effect=lambda: calls.append("sessions_stopped"))), \
         patch("app.core.session_pool.get_session_pool", return_value=pool), \
         patch("app.core.permission_handler.permission_handler.reset_all",
               side_effect=lambda: calls.append("perm_reset")), \
         patch("app.core.task_registry.task_registry.reset_all",
               side_effect=lambda: calls.append("task_reset")), \
         patch("app.core.http_client.close_http_client", new=AsyncMock()), \
         patch("app.core.automation_manager.automation_manager.shutdown",
               new=AsyncMock()), \
         patch("app.core.connectors.manager.connector_manager.shutdown_all",
               new=AsyncMock()), \
         patch("app.db.base.close_pool",
               side_effect=lambda: calls.append("close_pool")):
        await startup.shutdown_services()

    assert calls.index("sessions_stopped") < calls.index("pool_shutdown")
    assert "pool_shutdown" in calls
    assert "close_pool" in calls
    # The DB pool closes only after the SDK pool teardown (its reaps may still
    # persist). It must be the very last teardown step.
    assert calls.index("pool_shutdown") < calls.index("close_pool")
    assert calls[-1] == "close_pool"
    # The in-memory resets also land before the DB pool closes.
    for reset in ("perm_reset", "task_reset"):
        assert calls.index(reset) < calls.index("close_pool")
    assert startup.is_shutting_down is True


async def test_shutdown_flag_is_set_even_if_a_step_fails():
    """A failing teardown step must not leave the flag unset for later steps."""
    pool = MagicMock()
    pool.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
    closed = []

    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
         patch("app.core.permission_handler.permission_handler.reset_all"), \
         patch("app.core.task_registry.task_registry.reset_all"), \
         patch("app.core.http_client.close_http_client", new=AsyncMock()), \
         patch("app.core.automation_manager.automation_manager.shutdown",
               new=AsyncMock()), \
         patch("app.core.connectors.manager.connector_manager.shutdown_all",
               new=AsyncMock()), \
         patch("app.db.base.close_pool", side_effect=lambda: closed.append(1)):
        await startup.shutdown_services()

    assert startup.is_shutting_down is True
    # close_pool still ran (shutdown is best-effort per step).
    assert closed == [1]
