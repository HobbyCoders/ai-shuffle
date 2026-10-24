"""Drive real close_pool from shutdown_services without conftest's DB stubs."""
from __future__ import annotations
from typing import Any
from pytest import MonkeyPatch
from pathlib import Path

pytest_plugins = ["pytester"]


def test_shutdown_services_drains_pool_off_event_loop(
    pytester: Any, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    pytester.makepyfile("""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import startup
from app.db import base
from tests.db.test_base_pool_reclaim import _PhysicalConn


@pytest.mark.parametrize("saturated, unavailable", [(False, False), (True, False), (False, True)])
def test_real_close_pool_keeps_coroutine_borrower_running(monkeypatch, saturated, unavailable):
    monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: _PhysicalConn())
    async def exercise():
        pool = base._RetainingThreadedConnectionPool(1, 2)
        base._pg_pool = pool
        conn = pool.getconn()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        release = threading.Event()
        entered = asyncio.Event()
        def blocked_borrower():
            borrowed = pool.getconn()
            borrowed.cancel = release.set
            loop.call_soon_threadsafe(entered.set)
            release.wait(10)
            pool.putconn(borrowed)
        blocked = loop.run_in_executor(None, blocked_borrower) if saturated else None
        if blocked:
            await entered.wait()
        draining = asyncio.Event()
        notified = pool._notify_available
        def notify():
            if pool._closing:
                loop.call_soon_threadsafe(draining.set)
            notified()
        pool._notify_available = notify
        returned = []
        async def borrower():
            await draining.wait()
            returned.append(not conn.closed)
            pool.putconn(conn)
        task = asyncio.create_task(borrower()) if not unavailable else None
        if unavailable:
            pool.putconn(conn)
            def no_executor(*args):
                raise RuntimeError("executor unavailable during teardown")
            monkeypatch.setattr(loop, "run_in_executor", no_executor)
        close_pool = base.close_pool
        close_threads = []
        def close():
            close_threads.append(threading.get_ident())
            close_pool()
        session_pool = MagicMock()
        session_pool.shutdown = AsyncMock()
        with patch("app.core.watch_manager.watch_manager.shutdown", new=AsyncMock()), \
             patch("app.core.global_events.global_events.shutdown", new=AsyncMock()), \
             patch("app.core.composer_suggestions.stop", new=AsyncMock()), \
             patch("app.core.lifecycle_events.stop", new=AsyncMock()), \
             patch("app.core.desktop_lifecycle.stop_running_sessions", new=AsyncMock()), \
             patch("app.core.session_pool.get_session_pool", return_value=session_pool), \
             patch("app.core.permission_handler.permission_handler.reset_all"), \
             patch("app.core.task_registry.task_registry.reset_all"), \
             patch("app.core.http_client.close_http_client", new=AsyncMock()), \
             patch("app.core.automation_manager.automation_manager.shutdown", new=AsyncMock()), \
             patch("app.core.connectors.manager.connector_manager.shutdown_all", new=AsyncMock()), \
             patch("app.db.base.close_pool", side_effect=close):
            try:
                await asyncio.wait_for(startup.shutdown_services(), timeout=5)
            finally:
                release.set()
                if blocked:
                    await blocked
                if task:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        if unavailable:
            assert returned == []
            assert close_threads == [threading.get_ident()]
        else:
            assert returned == [True], "event loop was blocked until the borrower's socket closed"
            assert close_threads and close_threads[0] != threading.get_ident()
        assert base._pg_pool is None and pool.closed
    asyncio.run(exercise())
""")
    result = pytester.runpytest_subprocess("-q", "-p", "no:randomly")
    result.assert_outcomes(passed=3)
