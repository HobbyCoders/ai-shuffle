"""Lifecycle shutdown shares the query stop deadline and reports unfinished saving (#1305).

The slow step is real: a turn execution whose terminal write blocks a thread,
or a task-persistence flush that blocks a thread, both released by the test.
The route, ``stop_running_sessions`` and ``shutdown_services`` are the real
code; only the stop request itself and external teardown collaborators are
stand-ins.
"""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import internal
from app.core import desktop_lifecycle, memory_sync, query_shutdown, session_runtime, startup

BUDGET = 1.0  # Stands in for the 10 s desktop SIGTERM budget.
SETTLE = 3.0  # Stands in for SHUTDOWN_SETTLE_SECONDS (25 s): longer than the budget.
SLACK = 0.5


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("SHUFFLE_SUPERVISOR_STOP_SECONDS", raising=False)
    monkeypatch.setattr(query_shutdown, "_closing_loop", None)
    monkeypatch.setattr(query_shutdown, "_turns", set())
    monkeypatch.setattr(query_shutdown, "_retired", set())
    monkeypatch.setattr(query_shutdown, "configured_stop_budget", lambda environ=None: BUDGET)
    monkeypatch.setattr(desktop_lifecycle, "SHUTDOWN_SETTLE_SECONDS", SETTLE)
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids",
                        lambda: set(session_runtime.active_executions().values()))
    monkeypatch.setattr(internal, "require_desktop", lambda request: None)
    monkeypatch.setattr(internal, "_shutdown_task", None)
    startup.is_shutting_down = False
    session_runtime._executions.clear()
    memory_sync.reset_state()
    yield
    startup.is_shutting_down = False
    session_runtime._executions.clear()
    memory_sync.reset_state()


class SlowSave:
    """One slow lifecycle step: a turn's terminal write or the persistence flush."""

    def __init__(self, monkeypatch, step):
        self.release = threading.Event()
        self.writing = asyncio.Event()
        self.saved = []
        self.done = asyncio.Event()
        self.task = None
        interrupted = asyncio.Event()

        async def write():
            self.writing.set()
            # A real blocking write on a worker thread, as the DB layer does.
            assert await asyncio.to_thread(self.release.wait, 10)
            self.saved.append(step)
            self.done.set()

        async def stop_request(sid, **kwargs):
            interrupted.set()

        async def turn():
            await interrupted.wait()
            await write()

        monkeypatch.setattr(desktop_lifecycle, "stop_session_work", stop_request)
        if step == "turn":
            self.task = asyncio.create_task(turn())
            session_runtime._executions[self.task] = "busy-chat"
            monkeypatch.setattr("app.core.task_persistence.flush", AsyncMock())
        else:
            monkeypatch.setattr("app.core.task_persistence.flush", write)

    async def finish(self):
        self.release.set()
        if self.writing.is_set():
            await asyncio.wait_for(self.done.wait(), 5)
        if self.task is not None and self.writing.is_set():
            await self.task
        elif self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@pytest.mark.parametrize("step", ["turn", "flush"])
async def test_pre_stop_reports_unfinished_saving_at_the_shared_deadline(monkeypatch, step):
    slow = SlowSave(monkeypatch, step)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(RuntimeError, match="not finished saving"):
            await internal.desktop_stop_sessions(SimpleNamespace())
        elapsed = loop.time() - started
        assert elapsed < BUDGET + SLACK  # The shared deadline, not the 25 s settle cap.
        assert slow.writing.is_set() and slow.saved == []
        if slow.task is not None:
            assert not slow.task.done()  # Not cancelled: left to finish or for recovery.

        # A second stop signal (a retried pre-stop) does not renew the deadline.
        again = loop.time()
        with pytest.raises(RuntimeError, match="not finished saving"):
            await internal.desktop_stop_sessions(SimpleNamespace())
        assert loop.time() - again < SLACK
        assert query_shutdown.begin_shutdown() == pytest.approx(started + BUDGET - 0.1, abs=0.05)
    finally:
        await slow.finish()
    assert set(slow.saved) == {step}  # The retry issued its own flush; none was lost.
    # Once the write has landed and nothing is left queued, a retry past the
    # deadline reports the clean stop truthfully (an idle flush is one step).
    monkeypatch.setattr("app.core.task_persistence.flush", AsyncMock())
    assert await internal.desktop_stop_sessions(SimpleNamespace()) == {"stopped": True}


async def test_pre_stop_reports_an_unfinished_memory_publication(monkeypatch, tmp_path):
    monkeypatch.setattr("app.core.task_persistence.flush", AsyncMock())
    monkeypatch.setattr(memory_sync, "settings", SimpleNamespace(
        agent_memory_sync=True, effective_instance_id="test", effective_data_dir=tmp_path))
    assert memory_sync._queue_publication("project", str(tmp_path), "native")
    with pytest.raises(RuntimeError, match="memory publication"):
        await internal.desktop_stop_sessions(SimpleNamespace())


async def test_idle_pre_stop_still_acknowledges_a_clean_stop(monkeypatch):
    monkeypatch.setattr("app.core.task_persistence.flush", AsyncMock())
    # A default drain inside the stop deadline, so a capture without grace=0 shows.
    monkeypatch.setattr(query_shutdown, "configured_grace_seconds", lambda environ=None: 0.8)
    started = asyncio.get_running_loop().time()
    assert await internal.desktop_stop_sessions(SimpleNamespace()) == {"stopped": True}
    # Pre-stop still retires request reads at once (grace=0), not after the
    # default finite-request drain, although it now captures the deadline itself.
    assert query_shutdown._retire_at <= started + SLACK


@pytest.mark.parametrize("step", ["turn", "flush"])
async def test_lifespan_stop_shares_the_deadline_and_reports_unfinished(monkeypatch, step, caplog):
    slow = SlowSave(monkeypatch, step)
    pool = MagicMock()
    pool.shutdown = AsyncMock()
    loop = asyncio.get_running_loop()
    started = loop.time()
    query_shutdown.begin_shutdown()  # SIGTERM: Uvicorn's handle_exit, the first stop signal.
    try:
        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.core.permission_handler.permission_handler.reset_all"), \
             patch("app.core.task_registry.task_registry.reset_all"), \
             patch("app.core.http_client.close_http_client", new=AsyncMock()), \
             patch("app.core.automation_manager.automation_manager.shutdown", new=AsyncMock()), \
             patch("app.core.connectors.manager.connector_manager.shutdown_all", new=AsyncMock()), \
             patch("app.db.base.close_pool"), \
             caplog.at_level(logging.WARNING):
            settled = await startup.shutdown_services()
        assert settled is False
        assert loop.time() - started < BUDGET + SLACK
        assert desktop_lifecycle.unfinished_saving(sessions=settled, queries=True) == ["session turns"]
        assert "stop deadline" in caplog.text
    finally:
        await slow.finish()


def test_shutdown_banner_is_not_clean_while_saving_is_unfinished(caplog):
    from app.core.file_logging import log_shutdown_banner

    with caplog.at_level(logging.INFO):
        log_shutdown_banner(["session turns", "memory publication"])
    assert "saving unfinished" in caplog.text and "session turns, memory publication" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        log_shutdown_banner()
    assert "=== AI Shuffle shutting down (pid=" in caplog.text
