"""
U3 regression: the ERROR/teardown cleanup path must reap orphaned background
work, just like the pool-eviction force path.

``LiveSessionStore.cleanup_session`` is the force/error teardown (connect
failure, dead origin socket, stuck-streaming disconnect, worktree deletion). It
runs WITHOUT the streaming/team/background-work guards of
``cleanup_ended_session`` — every caller has already decided the session is gone
(the SDK client is destroyed or never came up), so any work tracked for it can
never complete.

Before this fix it only popped ``_sessions`` + ``_session_locks`` and logged,
leaking on every error path:

  * orphaned "running" task_registry rows surfaced as PHANTOM active tasks — a
    later ``/live`` poll still returned them in ``activeTasks`` (computed from the
    registry, independent of store membership), so the frontend taskbar showed a
    task that could never finish; and
  * Monitor shell subprocesses stranded (proc kept running, reader awaited
    readline() forever, ``_background_tasks`` entry leaked).

cleanup_session now mirrors ``cleanup_ended_session``'s force path: reap running
registry rows to "killed", clear the bucket, and schedule the Monitor shell reap.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.live_session_store import LiveSessionStore
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep the global task_registry isolated per test."""
    task_registry.reset_all()
    yield
    task_registry.reset_all()


def _patch_shell_reap():
    """Stub the Monitor shell reap so no real subprocess/loop work is scheduled.

    cleanup_session lazy-imports ``reap_session_background_shells`` from
    ``app.mcp.builtin_tools``, so that module is where the patch must land.
    """
    return patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock())


async def _seed_session(store: LiveSessionStore, session_id: str) -> None:
    await store.start_streaming(session_id, initial_messages=[])


async def test_cleanup_session_clears_orphaned_running_tasks():
    """Phantom-task fix: after teardown the registry reports no active tasks."""
    store = LiveSessionStore()
    sid = "u3-sess"
    await _seed_session(store, sid)
    task_registry.start_task(sid, "agent-1", "agent", "bg work")
    assert task_registry.get_active(sid), "precondition: a running task is tracked"

    with _patch_shell_reap():
        await store.cleanup_session(sid)

    # The session is gone AND its registry bucket is wiped — a subsequent /live
    # poll computes activeTasks straight from the registry, so this is what stops
    # the phantom "running" task from lingering on the taskbar.
    assert store.has_session(sid) is False
    assert task_registry.get_active(sid) == []


async def test_cleanup_session_reaps_running_tasks_to_killed_before_clearing():
    """Each orphaned running row is flipped to 'killed' (force-path parity)."""
    store = LiveSessionStore()
    sid = "u3-killed"
    await _seed_session(store, sid)
    task_registry.start_task(sid, "agent-1", "agent", "bg work")
    task_registry.start_task(sid, "shell-1", "shell", "tail -f")

    with _patch_shell_reap(), \
         patch.object(task_registry, "complete_task", wraps=task_registry.complete_task) as spy:
        await store.cleanup_session(sid)

    killed = {c.args[1]: c.args[2] for c in spy.call_args_list}
    assert killed == {"agent-1": "killed", "shell-1": "killed"}
    # And the bucket is cleared afterward.
    assert task_registry.get_active(sid) == []


async def test_cleanup_session_schedules_monitor_shell_reap():
    """The session's Monitor shells are reaped on the error path too (U2 parity)."""
    store = LiveSessionStore()
    sid = "u3-shells"
    await _seed_session(store, sid)

    with patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock()) as mock_reap:
        await store.cleanup_session(sid)

    mock_reap.assert_called_once_with(sid)


async def test_cleanup_session_only_touches_the_named_session():
    """Isolation: tearing down one session leaves another's tasks intact."""
    store = LiveSessionStore()
    await _seed_session(store, "sA")
    await _seed_session(store, "sB")
    task_registry.start_task("sA", "a1", "agent", "A work")
    task_registry.start_task("sB", "b1", "agent", "B work")

    with _patch_shell_reap():
        await store.cleanup_session("sA")

    assert task_registry.get_active("sA") == []
    assert [t["id"] for t in task_registry.get_active("sB")] == ["b1"]
    assert store.has_session("sB") is True


async def test_cleanup_session_no_work_is_noop_but_still_removes_session():
    """Negative control: no tasks/shells → no error, session still removed."""
    store = LiveSessionStore()
    sid = "u3-empty"
    await _seed_session(store, sid)

    with patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock()) as mock_reap:
        await store.cleanup_session(sid)

    assert store.has_session(sid) is False
    assert task_registry.get_active(sid) == []
    # The shell reap is always scheduled (it self-noops when the session owns no
    # shells), so error/teardown stays symmetric with cleanup_ended_session.
    mock_reap.assert_called_once_with(sid)


async def test_cleanup_session_missing_session_does_not_raise():
    """Force-cleaning an unknown session is a safe no-op."""
    store = LiveSessionStore()
    with _patch_shell_reap():
        await store.cleanup_session("ghost")  # must not raise
    assert store.has_session("ghost") is False
