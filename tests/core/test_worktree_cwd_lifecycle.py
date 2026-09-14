"""Windows session cwd locks and recovery after partial worktree cleanup."""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import git_operations as gitops
from app.core import checkout_manager, session_pool, session_runtime, turn_finalize
from app.core.config import settings
from app.db import checkouts as checkouts_db, database
from app.mcp import builtin_tools as bt
from tests.core.test_checkout_materialization import (
    RUNNER, _git, base_checkout, mock_db, project, test_db, workspace_root,
)


def make_checkout(project):
    return Path(checkout_manager.ensure_checkout(
        project, runner_id=RUNNER, branch="fix/cwd-lock", create_new_branch=True,
    ))


def record_cleanup_tip(project, checkout):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    row = checkouts_db.set_head_sha_for_path(
        str(checkout), sha, project_id=project["id"], runner_id=RUNNER,
    )
    assert row and row["head_sha"] == sha
    return sha


@pytest.mark.parametrize("obstruction", ["empty", "file", "unknown"])
def test_partial_cleanup_recovery(project, base_checkout, workspace_root, obstruction):
    checkout = make_checkout(project)
    if obstruction != "unknown":
        sha = record_cleanup_tip(project, checkout)
    _git(base_checkout, "worktree", "remove", "--force", str(checkout))
    checkout.mkdir()
    if obstruction == "file":
        (checkout / "keep.txt").write_text("do not delete", encoding="utf-8")
    result = checkout_manager.ensure_checkout(project, runner_id=RUNNER, branch="fix/cwd-lock")
    if obstruction == "empty":
        assert result == str(checkout)
        assert (checkout / ".git").is_file()
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == sha
    else:
        assert result is None
        if obstruction == "file":
            assert (checkout / "keep.txt").read_text() == "do not delete"


def test_empty_remnant_never_adopts_parent_head(project, base_checkout, workspace_root, monkeypatch):
    checkout = make_checkout(project)
    record_cleanup_tip(project, checkout)
    database.create_session(session_id="cwd-resync", profile_id="default", project_v2_id=project["id"])
    database.set_session_branch("cwd-resync", "fix/cwd-lock", project["id"])
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: RUNNER))
    _git(base_checkout, "worktree", "remove", "--force", str(checkout))
    checkout.mkdir()
    assert turn_finalize.resync_session_branch_from_head("cwd-resync") is None
    assert database.get_session("cwd-resync")["branch"] == "fix/cwd-lock"
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch="fix/cwd-lock")["path"] == str(checkout)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cwd deletion lock")
@pytest.mark.parametrize("partial_remnant", [False, True])
async def test_session_shutdown_releases_real_windows_cwd_lock(
    project, base_checkout, workspace_root, monkeypatch, partial_remnant,
):
    checkout = make_checkout(project)
    # This process is owned by this test session, not a process-name-wide kill.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", "print('ready', flush=True); input()",
        cwd=checkout, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    task = bt._BackgroundTask("cwd-lock-task", "hold cwd", str(checkout), session_id="cwd-lock-session")
    task.process = proc
    bt._background_tasks[task.task_id] = task
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(session_runtime, "active_executions", lambda: {})
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=AsyncMock()))
    monkeypatch.setattr(bt, "_settle_reaped_shell", AsyncMock())
    try:
        assert (await asyncio.wait_for(proc.stdout.readline(), 5)).strip() == b"ready"
        monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: RUNNER))
        op = SimpleNamespace(project_id=project["id"])
        if partial_remnant:
            # Reproduce the old race through the real cleanup producer: the
            # early release returns while the session's process still holds cwd.
            with monkeypatch.context() as race:
                race.setattr(gitops, "_release_session_cwd", AsyncMock(return_value=True))
                assert await gitops._cleanup_linked_worktree(
                    op, str(checkout), "fix/cwd-lock", "main", "cwd-lock-session",
                ) is False
            assert checkout.is_dir() and not (checkout / ".git").exists()
            assert await gitops._release_session_cwd("cwd-lock-session") is True
        else:
            assert await gitops._cleanup_linked_worktree(
                op, str(checkout), "fix/cwd-lock", "main", "cwd-lock-session",
            ) is True
            assert not checkout.exists()
        assert proc.returncode is not None
        # The SHA is written by _cleanup_linked_worktree, not by this fixture.
        row = checkouts_db.get_checkout(project["id"], RUNNER, branch="fix/cwd-lock")
        assert row["head_sha"]
        assert checkout_manager.ensure_checkout(
            project, runner_id=RUNNER, branch="fix/cwd-lock",
        ) == str(checkout)
        assert (checkout / ".git").is_file()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        bt._background_tasks.pop(task.task_id, None)


async def test_cleanup_retains_checkout_when_owned_shell_survives(monkeypatch):
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(session_runtime, "active_executions", lambda: {})
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=AsyncMock()))
    stop = AsyncMock(side_effect=RuntimeError("shell survived"))
    monkeypatch.setattr(bt, "stop_session_background_shells", stop)
    assert await gitops._release_session_cwd("locked") is False
    stop.assert_awaited_once_with("locked", require_stopped=True)




@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_recovery_rejects_directory_links(tmp_path, monkeypatch, kind):
    candidate = tmp_path / "remnant"
    candidate.mkdir()
    row = {"path": str(candidate), "head_sha": "a" * 40}
    monkeypatch.setattr(Path, "is_symlink", lambda self: kind == "symlink")
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("Windows junction reparse tag")
        import stat
        monkeypatch.setattr(checkout_manager.os, "lstat", lambda path: SimpleNamespace(
            st_reparse_tag=stat.IO_REPARSE_TAG_MOUNT_POINT,
        ))
    assert checkout_manager._is_empty_cleanup_remnant(str(candidate), row) is False




@pytest.mark.parametrize("failure", [False, True])
async def test_legacy_cleanup_awaits_owned_shell_shutdown(monkeypatch, failure):
    from app.core import worktree_manager as wm
    monkeypatch.setattr(wm.database, "get_sessions_for_worktree", lambda wid: [{"id": "legacy"}])
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(remove=AsyncMock()))
    from app.core import streaming_status
    monkeypatch.setattr(streaming_status, "has_active_run", lambda sid: False)
    finished = []
    async def stop(sid, *, require_stopped=False):
        assert sid == "legacy" and require_stopped
        await asyncio.sleep(0)
        if failure:
            raise RuntimeError("still locked")
        finished.append(sid)
    monkeypatch.setattr(bt, "stop_session_background_shells", stop)
    if failure:
        with pytest.raises(wm.WorktreeError, match="retaining checkout"):
            await wm.WorktreeManager().terminate_worktree_sessions("legacy-wt")
    else:
        await wm.WorktreeManager().terminate_worktree_sessions("legacy-wt")
        assert finished == ["legacy"]


async def test_strict_shell_reap_keeps_survivor_for_retry(monkeypatch):
    from app.core import process_tree
    proc = SimpleNamespace(returncode=None)
    task = bt._BackgroundTask("surviving-task", "hold cwd", "/tmp", session_id="survivor")
    task.process = proc
    bt._background_tasks[task.task_id] = task
    monkeypatch.setattr(process_tree, "kill_process_tree", AsyncMock(return_value=[123]))
    try:
        with pytest.raises(RuntimeError, match="live processes"):
            await bt.stop_session_background_shells("survivor", require_stopped=True)
        assert bt._background_tasks[task.task_id] is task
        proc.returncode = 0
        monkeypatch.setattr(process_tree, "pid_alive", lambda pid: True)
        with pytest.raises(RuntimeError, match="live processes"):
            await bt.stop_session_background_shells("survivor", require_stopped=True)
        assert await bt.stop_session_background_shells("survivor") == 0
        monkeypatch.setattr(process_tree, "pid_alive", lambda pid: False)
        monkeypatch.setattr(bt, "_settle_reaped_shell", AsyncMock())
        assert await bt.stop_session_background_shells("survivor", require_stopped=True) == 1
    finally:
        bt._background_tasks.pop(task.task_id, None)
