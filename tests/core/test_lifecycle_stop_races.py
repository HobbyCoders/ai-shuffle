"""Correctness regressions for the September lifecycle review; no providers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import provider_agent as pa, task_dispatch as td, turn_scheduler as ts
from app.core import monitor_spawn
from app.core.task_registry import task_registry
from tests.core.test_task_worker_lifecycle import worker_env, _roomy_pool


@pytest.fixture(autouse=True)
def clean_registry():
    task_registry.reset_all()
    yield
    task_registry.reset_all()


@pytest.mark.parametrize("stop_at", ["attempt", "backoff", "admission"])
async def test_stop_then_resume_does_not_revive_old_retry(monkeypatch, stop_at):
    from app.core.config import settings
    monkeypatch.setattr(settings, "task_worker_start_retries", 1)
    calls = []
    released = []
    def stop_resume():
        task_registry.mark_user_stopped("root")
        task_registry.clear_user_stop("root")
    async def dispatch(**kw):
        calls.append(kw)
        if stop_at == "attempt":
            stop_resume()
        return pa.WorkerOutcome(ok=False, session_id=kw["worker_session_id"],
                                error="RemoteProtocolError: peer closed connection", frames_forwarded=0)
    async def sleep(_):
        if stop_at == "backoff":
            stop_resume()
    async def reserve(worker_id, **kw):
        if stop_at == "admission":
            stop_resume()
        return None, 0
    monkeypatch.setattr(td.asyncio, "sleep", sleep)
    monkeypatch.setattr(td, "_reserve_admission", reserve)
    monkeypatch.setattr(td, "_release_admission", released.append)
    result = await td._dispatch_worker_with_retries(
        kwargs={"worker_session_id": "first", "prompt": "fixture"},
        background=True, lane="native", provider_id=None,
        parent_session_id="root", dispatcher=dispatch)
    assert result.killed
    assert len(calls) == 1
    assert released
    if stop_at == "admission":
        assert released[-1] != "first", "release the reserved retry slot"


async def test_stop_cancels_detached_supervisor_during_backoff():
    started = asyncio.Event()
    async def supervisor():
        started.set()
        await asyncio.Future()
    task = asyncio.create_task(supervisor())
    task_registry.supervise_until_stop("root", task)
    await started.wait()
    task_registry.mark_user_stopped("root")
    task_registry.clear_user_stop("root")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not task_registry._stop_supervisors


@pytest.mark.parametrize("stop_at", ["count", "drop", "enqueue"])
async def test_failed_wake_stop_race_never_kicks(monkeypatch, stop_at):
    def stop_resume():
        task_registry.mark_user_stopped("s")
        task_registry.clear_user_stop("s")
    def count(*_):
        if stop_at == "count":
            stop_resume()
        return 0
    async def drop(*_):
        if stop_at == "drop":
            stop_resume()
    enqueued, removed, kicked = [], [], []
    def enqueue(*args, **kw):
        enqueued.append(kw)
        if stop_at == "enqueue":
            stop_resume()
        return {"id": "stale"}
    monkeypatch.setattr(ts, "_turn_model_call_count", count)
    monkeypatch.setattr(ts, "_drop_pooled_client", drop)
    monkeypatch.setattr(ts, "_enqueue", enqueue)
    monkeypatch.setattr(ts.turns_db, "remove_queued_turn", removed.append)
    monkeypatch.setattr(ts, "kick", kicked.append)
    result = await ts._maybe_recover_failed_wake("s", {
        "id": "dead", "source": "unpark", "state": "aborted", "stop_reason": "error"})
    assert result is False
    assert kicked == []
    assert removed == (["stale"] if stop_at == "enqueue" else [])


async def test_queued_kick_racing_stop_resume_cannot_execute(monkeypatch):
    from app.core import session_runtime
    def peek(_):
        task_registry.mark_user_stopped("s")
        task_registry.clear_user_stop("s")
        return {"id": "stale"}
    execute = AsyncMock()
    monkeypatch.setattr(ts.turns_db, "pop_ready", peek)
    monkeypatch.setattr(session_runtime, "execute", execute)
    await ts._kick_impl("s")
    execute.assert_not_awaited()


@pytest.mark.parametrize("stop_at", ["claim", "start", "recorder"])
async def test_runtime_stop_during_startup_never_connects_provider(monkeypatch, mock_db, stop_at):
    from app.core import session_runtime
    from app.db import turns, runs
    from tests.core.test_session_runtime import _runtime_env
    pumped = []
    async def pump(**kwargs):
        pumped.append(kwargs)
        yield {"type": "done", "metadata": {}}
    def stop_resume():
        task_registry.mark_user_stopped("startup-race")
        task_registry.clear_user_stop("startup-race")
    row = turns.enqueue_turn("startup-race", "unpark", prompt="fixture")
    with _runtime_env(pump=pump) as env:
        env["registry"].stop_generation.side_effect = task_registry.stop_generation
        env["registry"].stop_invalidated.side_effect = task_registry.stop_invalidated
        env["registry"].is_user_stopped.side_effect = task_registry.is_user_stopped
        if stop_at == "recorder":
            env["recorder"].start.side_effect = lambda *a, **kw: stop_resume()
        else:
            module, name = (runs, "claim_run") if stop_at == "claim" else (turns, "start_turn")
            original = getattr(module, name)
            def racing_call(*a, **kw):
                value = original(*a, **kw)
                stop_resume()
                return value
            monkeypatch.setattr(module, name, racing_call)
        await session_runtime.SessionRuntime(row).execute()
    assert pumped == []
    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert session_runtime.get_live_runtime("startup-race") is None


async def test_parked_worker_obeys_absolute_deadline(worker_env, mock_db, monkeypatch):
    monkeypatch.setattr(pa, "_worker_still_working", lambda sid: True)
    outcome = await asyncio.wait_for(pa.dispatch_worker(
        parent_session_id="parent", profile_id="p1", prompt="fixture", max_wall_seconds=.03), 1)
    assert not outcome.ok
    assert outcome.error
    assert worker_env.tm.interrupts == [(outcome.session_id, "stop")]


async def test_abandonment_awaits_owned_shell_stop_and_skips_agents(monkeypatch):
    from app.mcp import builtin_tools
    stopped = AsyncMock(return_value={"running": False})
    monkeypatch.setattr(builtin_tools, "stop_background_shell_task", stopped)
    await pa._force_settle_background_work("worker", [
        {"task_id": "shell", "kind": "shell"}, {"task_id": "child", "kind": "agent"}])
    stopped.assert_awaited_once()
    assert stopped.call_args.args == ("shell",)
    assert stopped.call_args.kwargs["session_id"] == "worker"


async def test_abandonment_preserves_explicit_serving_monitor(monkeypatch):
    from app.mcp import builtin_tools
    stopped = AsyncMock()
    monkeypatch.setattr(builtin_tools, "stop_background_shell_task", stopped)
    task_registry.start_task("worker", "server", "shell", "fixture service")
    task_registry.mark_monitor_shell_task("worker", "server", serves=True)
    assert pa._active_worker_background_work("worker") == []
    await pa._force_settle_background_work("worker", [{"task_id": "server", "kind": "shell"}])
    stopped.assert_not_awaited()


async def test_abandonment_terminates_real_owned_process(monkeypatch):
    import sys
    from app.mcp import builtin_tools
    # A benign sleeper exercises termination; it has no files, network or models.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    shell = builtin_tools._BackgroundTask("owned-sleeper", "fixture sleeper", None, "worker")
    shell.process = process
    monkeypatch.setitem(builtin_tools._background_tasks, shell.task_id, shell)
    monkeypatch.setattr(builtin_tools, "_retire_stopped_shell", AsyncMock())
    task_registry.start_task("worker", shell.task_id, "shell", "fixture sleeper")
    try:
        await asyncio.wait_for(pa._force_settle_background_work("worker", [
            {"task_id": shell.task_id, "kind": "shell"}]), 15)
        assert process.returncode is not None
        assert task_registry.get_task("worker", shell.task_id)["status"] == "killed"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.parametrize("stopped", [False, True])
def test_inline_monitor_receipt_matches_registry(stopped):
    task_registry.start_task("s", "monitor", "shell", "fixture")
    task_registry.complete_task("s", "monitor", "completed")
    if stopped:
        task_registry.complete_task("s", "monitor", "killed", reason="user_stopped")
    task = SimpleNamespace(exit_code=0, lines=["tests failed", "EXIT:1"], task_id="monitor")
    status, _ = monitor_spawn.settle_inline_monitor_task(task, command="fixture", cwd=None, session_id="s")
    assert status == ("killed" if stopped else "error")
    assert task_registry.get_task("s", "monitor")["status"] == status


@pytest.mark.parametrize("lines,status", [(b"tests failed\nEXIT:1\n", "error"),
                                          (b"EXIT:1\nretry succeeded\nEXIT:0\n", "completed")])
async def test_reader_emits_correct_first_terminal_chip(monkeypatch, lines, status):
    from tests.core.test_monitor_spawn import _fake_proc
    from app.mcp import builtin_tools
    _fake_proc(monkeypatch, [lines])
    emitted = AsyncMock()
    monkeypatch.setattr(builtin_tools, "_emit_shell_settle_frame", emitted)
    shell = await builtin_tools._spawn_background("fixture", None, "s")
    try:
        await shell._reader_task
        emitted.assert_awaited_once_with("s", shell.task_id, status)
        assert task_registry.get_task("s", shell.task_id)["status"] == status
    finally:
        builtin_tools._background_tasks.pop(shell.task_id, None)
