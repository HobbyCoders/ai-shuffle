"""
U3 regression: the ERROR/teardown cleanup path must reap orphaned background
work, just like the pool-eviction force path.

V2 (Phase-1 deletion): ``LiveSessionStore.cleanup_session`` is deleted with the
live store — there is no in-memory message buffer to purge (render state lives
in the durable event log, busy truth in the runs table). The force/error
teardown duty moved WHOLESALE to
``resource_monitor._cleanup_evicted_sessions(session_ids, cleaned, reason)``:
for ``reason in ("lru_force", "dead")`` (hard-cap eviction of a busy session /
subprocess died) the pooled SDK client was just destroyed, so any work tracked
for the session can never complete. The pre-V2 leak this file pins:

  * orphaned "running" task_registry rows surfaced as PHANTOM active tasks — a
    later ``/live`` poll still returned them in ``activeTasks`` (computed from the
    registry, independent of pool membership), so the frontend taskbar showed a
    task that could never finish; and
  * Monitor shell subprocesses stranded (proc kept running, reader awaited
    readline() forever, ``_background_tasks`` entry leaked).

The force path must: reap running registry rows to "killed", clear the bucket,
and schedule the Monitor shell reap.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core import resource_monitor
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep the global task_registry isolated per test."""
    task_registry.reset_all()
    yield
    task_registry.reset_all()


def _patch_shell_reap():
    """Stub the Monitor shell reap so no real subprocess/loop work is scheduled.

    _cleanup_evicted_sessions lazy-imports ``reap_session_background_shells``
    from ``app.mcp.builtin_tools``, so that module is where the patch must land.
    """
    return patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock())


async def _force_cleanup(session_id: str) -> None:
    """Drive the force teardown path (reason='dead': SDK subprocess destroyed)."""
    await resource_monitor._cleanup_evicted_sessions([session_id], {}, "dead")


async def test_force_cleanup_clears_orphaned_running_tasks():
    """Phantom-task fix: after teardown the registry reports no active tasks."""
    sid = "u3-sess"
    task_registry.start_task(sid, "agent-1", "agent", "bg work")
    assert task_registry.get_active(sid), "precondition: a running task is tracked"

    with _patch_shell_reap():
        await _force_cleanup(sid)

    # The registry bucket is wiped — a subsequent /live poll computes
    # activeTasks straight from the registry, so this is what stops the phantom
    # "running" task from lingering on the taskbar.
    assert task_registry.get_active(sid) == []


async def test_force_cleanup_reaps_running_tasks_to_killed_before_clearing():
    """Each orphaned running row is flipped to 'killed' (force-path parity)."""
    sid = "u3-killed"
    task_registry.start_task(sid, "agent-1", "agent", "bg work")
    task_registry.start_task(sid, "shell-1", "shell", "tail -f")

    with _patch_shell_reap(), \
         patch.object(task_registry, "complete_task", wraps=task_registry.complete_task) as spy:
        await _force_cleanup(sid)

    killed = {c.args[1]: c.args[2] for c in spy.call_args_list}
    assert killed == {"agent-1": "killed", "shell-1": "killed"}
    # And the bucket is cleared afterward.
    assert task_registry.get_active(sid) == []


async def test_force_cleanup_schedules_monitor_shell_reap():
    """The session's Monitor shells are reaped on the error path too (U2 parity)."""
    sid = "u3-shells"

    with patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock()) as mock_reap:
        await _force_cleanup(sid)

    mock_reap.assert_called_once_with(sid)


async def test_force_cleanup_only_touches_the_named_session():
    """Isolation: tearing down one session leaves another's tasks intact."""
    task_registry.start_task("sA", "a1", "agent", "A work")
    task_registry.start_task("sB", "b1", "agent", "B work")

    with _patch_shell_reap():
        await _force_cleanup("sA")

    assert task_registry.get_active("sA") == []
    assert [t["id"] for t in task_registry.get_active("sB")] == ["b1"]


async def test_force_cleanup_no_work_is_noop_but_still_reaps_shells():
    """Negative control: no tasks/shells → no error, shell reap still scheduled
    (it self-noops when the session owns no shells), so error/teardown stays
    symmetric with the normal eviction path."""
    sid = "u3-empty"

    with patch("app.mcp.builtin_tools.reap_session_background_shells", new=MagicMock()) as mock_reap:
        await _force_cleanup(sid)

    assert task_registry.get_active(sid) == []
    mock_reap.assert_called_once_with(sid)


async def test_non_force_eviction_defers_teardown_of_parked_session():
    """Guard rail the force path deliberately bypasses: a normal ``idle``
    eviction of a session parked on active SDK background work must DEFER the
    registry teardown (the work can still complete on the pooled client), not
    reap it to killed."""
    sid = "u3-parked"
    task_registry.start_task(sid, "agent-1", "agent", "bg work")
    task_registry.mark_sdk_background_task(sid, "agent-1")
    assert task_registry.has_active_background_work(sid) is True

    with _patch_shell_reap() as _:
        await resource_monitor._cleanup_evicted_sessions([sid], {}, "idle")

    # Still tracked — nothing reaped, bucket intact.
    assert [t["id"] for t in task_registry.get_active(sid)] == ["agent-1"]
    assert task_registry.get_active(sid)[0]["status"] == "running"


async def test_non_force_eviction_defers_teardown_of_running_monitor_shell():
    """A routine idle eviction must NOT kill a live Monitor shell (2026-08-29
    incident: a CI-watch loop was reaped 3.5 minutes in and its task stranded
    "running" forever). The shell is an OS process independent of the pooled
    client, and its completion watcher can still wake the parent through a
    durable turn — but only while the registry keeps the task's OWN-lane mark.
    """
    sid = "u3-monitor"
    task_registry.start_task(sid, "sh-1", "shell", "gh pr checks loop")
    task_registry.mark_monitor_shell_task(sid, "sh-1")
    # Monitor shells deliberately do NOT pin the pool — only the new deferral
    # predicate protects them here.
    assert task_registry.has_active_background_work(sid) is False

    with _patch_shell_reap() as mock_reap:
        await resource_monitor._cleanup_evicted_sessions([sid], {}, "idle")

    mock_reap.assert_not_called()
    assert [t["id"] for t in task_registry.get_active(sid)] == ["sh-1"]
    # The OWN-lane mark survives, so the shell's eventual terminal frame still
    # passes is_own_background_task and wakes the parked parent.
    assert task_registry.is_monitor_shell_task(sid, "sh-1") is True


async def test_non_force_eviction_tears_down_once_monitor_shell_finished():
    """The deferral is scoped to RUNNING shells: once the Monitor completed,
    a normal eviction clears the bucket and schedules the (self-nooping) reap
    exactly as before."""
    sid = "u3-monitor-done"
    task_registry.start_task(sid, "sh-1", "shell", "loop")
    task_registry.mark_monitor_shell_task(sid, "sh-1")
    task_registry.complete_task(sid, "sh-1", "completed")

    with _patch_shell_reap() as mock_reap:
        await resource_monitor._cleanup_evicted_sessions([sid], {}, "idle")

    mock_reap.assert_called_once_with(sid)
    assert task_registry.get_active(sid) == []
    assert task_registry.is_monitor_shell_task(sid, "sh-1") is False
