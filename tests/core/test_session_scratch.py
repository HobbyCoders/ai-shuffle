"""Disposable scratch only: safety guards, resumable backlog and disk pacing."""
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fluid.state import scratch_dir, scratch_prompt
from fluid.tools.registry import ToolCtx

from app.core import scratch_reaper
from app.core import session_scratch as ss


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    rows = [{"id": "s", "is_active": False, "open_turn": False}]
    monkeypatch.setattr(ss, "_session_rows", lambda key: rows)
    monkeypatch.setattr(ss, "_process_references", lambda deadline, **kwargs: set())
    sweeper = ss.ScratchSweep(root)
    yield root, rows, sweeper
    sweeper.close()


def folder(root, name="s", count=1):
    # Drive the same path producer as native/delegated harness reminders.
    ctx = ToolCtx(session_id=name, cwd=str(root), extra={"harness_state_root": root / "project"})
    path = scratch_dir(ctx)
    assert str(path) in scratch_prompt(ctx)
    path.mkdir(parents=True)
    for i in range(count):
        (path / str(i)).write_bytes(b"data")
    age(path)
    return path


def age(path, stamp=1):
    for parent, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            os.utime(Path(parent) / name, (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))


def test_settled_scratch_removed_durable_siblings_survive_and_can_recreate(world):
    root, _, sweep = world
    path = folder(root)
    for name in ("ledger", "notes", "task-reports"):
        sibling = path.parent.parent / name
        sibling.mkdir()
        (sibling / "s.md").write_text("durable")
    assert sweep.step() == 1
    assert not path.exists()
    assert all((path.parent.parent / name / "s.md").read_text() == "durable"
               for name in ("ledger", "notes", "task-reports"))
    folder(root)  # Unsettle/handoff needs no retained scratch to recreate it.


@pytest.mark.parametrize("field,value", [("is_active", True), ("is_active", None), ("open_turn", True)])
def test_live_or_unknown_session_is_retained(world, field, value):
    root, rows, sweep = world
    path = folder(root)
    rows[0][field] = value
    assert sweep.step() == 0 and path.exists()


def test_session_reactivated_after_scan_is_retained(world, monkeypatch):
    root, rows, sweep = world
    path = folder(root)
    def refs(deadline, **kwargs):
        rows[0]["is_active"] = True
        return set()
    monkeypatch.setattr(ss, "_process_references", refs)
    assert sweep.step() == 0 and (path / "0").exists()


@pytest.mark.parametrize("kind", ["cwd", "file", "command", "unknown"])
def test_process_use_and_unknown_inspection_keep_files(world, monkeypatch, kind):
    root, _, sweep = world
    path = folder(root)
    refs = {str(path)} if kind == "cwd" else {str(path / "0")}
    if kind == "command":
        refs = {f"shell --input={path}/0"}
    if kind == "unknown":
        refs = None
    monkeypatch.setattr(ss, "_process_references", lambda deadline, **kwargs: refs)
    assert sweep.step() == 0 and path.exists()


@pytest.mark.parametrize("target", ["file", "directory"])
def test_recent_nested_change_keeps_entire_folder(world, target):
    root, _, sweep = world
    path = folder(root)
    nested = path / "nested"
    nested.mkdir()
    (nested / "new").write_text("recent")
    if target == "directory":
        os.utime(nested / "new", (1, 1))
    else:
        os.utime(nested, (1, 1))
    os.utime(path, (1, 1))
    assert sweep.step() == 0 and (path / "0").exists()


def test_missing_session_requires_seven_days(world):
    root, rows, sweep = world
    path = folder(root)
    rows.clear()
    age(path, time.time() - 2 * 86400)
    assert sweep.step() == 0 and path.exists()
    age(path, time.time() - 8 * 86400)
    assert sweep.step() == 1


def test_read_only_owned_subdirectory_is_repaired(world):
    root, _, sweep = world
    path = folder(root)
    nested = path / "readonly"
    nested.mkdir()
    (nested / "payload").write_text("temporary")
    age(path)
    nested.chmod(0o500)
    path.chmod(0o500)
    try:
        assert sweep.step() == 1
        assert not path.exists()
    finally:
        if path.exists():
            path.chmod(0o700)
        if nested.exists():
            nested.chmod(0o700)


def test_file_budget_resumes_large_tree_without_stalling(world, monkeypatch):
    root, _, sweep = world
    path = folder(root, count=19)
    monkeypatch.setattr(ss, "MAX_SLICE_FILES", 2)
    for _ in range(20):
        sweep.step()
        assert sweep.slice_files <= 2
        if not path.exists():
            break
    assert not path.exists()
    assert ss.SLICE_PAUSE_SECONDS >= 30


def test_byte_budget_stops_and_resumes(world, monkeypatch):
    root, _, sweep = world
    path = folder(root, count=4)
    monkeypatch.setattr(ss, "MAX_SLICE_BYTES", 4)
    sweep.step()
    assert sweep.slice_bytes == 4 and path.exists()
    for _ in range(8):
        sweep.step()
    assert not path.exists()


def test_scan_budget_resumes_instead_of_skipping_large_tree(world, monkeypatch):
    root, _, sweep = world
    path = folder(root, count=30)
    monkeypatch.setattr(ss, "MAX_SCAN_ENTRIES", 3)
    sweep.step()
    assert len(list(path.iterdir())) == 30
    for _ in range(20):
        sweep.step()
        if not path.exists():
            break
    assert not path.exists()


def test_one_failed_folder_does_not_starve_siblings(world, monkeypatch):
    root, _, sweep = world
    bad = folder(root, "bad")
    good = folder(root, "good")
    original = ss._remove_entry
    def unlink(root, path, info, identity):
        if path.parent == bad:
            raise PermissionError("foreign owner")
        return original(root, path, info, identity)
    monkeypatch.setattr(ss, "_remove_entry", unlink)
    assert sweep.step() == 1
    assert bad.exists() and not good.exists()


def test_symlinks_are_not_followed(world, tmp_path, requires_symlinks):
    root, _, sweep = world
    path = folder(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("durable")
    (path / "link").symlink_to(outside, target_is_directory=True)
    (path.parent / "linked-session").symlink_to(outside, target_is_directory=True)
    age(path)
    assert sweep.step() == 1
    assert (outside / "keep").read_text() == "durable"
    assert (path.parent / "linked-session").is_symlink()


def test_known_devloop_child_protects_all_preview_temp_trees(tmp_path):
    base = tmp_path / "123"
    base.mkdir()
    (base / "cmdline").write_bytes(b"python\0-m\0scripts.devloop.child\0" + os.fsencode(tmp_path / "activity.json") + b"\0")
    protected = set()
    assert scratch_reaper._is_application_process(base, {"Name": "python"}, tmp_path, protected)
    assert protected == {"ai-shuffle-preview-"}


def test_reparse_attribute_is_recognized_on_every_platform():
    assert ss._linked(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))


@pytest.mark.skipif(os.name != "nt", reason="real Windows junction")
def test_windows_junction_never_recurses(world, tmp_path):
    root, _, sweep = world
    path = folder(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("durable")
    junction = path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    os.utime(path, (1, 1))
    try:
        assert sweep.step() == 0
        assert (outside / "keep").read_text() == "durable"
    finally:
        junction.rmdir()  # remove the link only, never its target


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux procfs")
def test_actual_cwd_fd_and_cmdline_are_observed(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / str(os.getpid())).symlink_to(Path("/proc") / str(os.getpid()))
    with (tmp_path / "held").open("w"):
        refs = scratch_reaper._references(proc, time.monotonic() + 5, strict=True)
    assert str(tmp_path / "held") in refs
    assert str(Path.cwd()) in refs
    assert any(str(Path(sys.executable).name) in ref for ref in refs)


def test_usage_categories_skip_links(world, monkeypatch):
    root, _, _ = world
    folder(root)
    from app.core import worktree_reclaim
    monkeypatch.setattr(worktree_reclaim, "_local_sources", lambda: [{"path": str(root / "repo")}])
    checkout = root / "repo" / ".fluid" / "worktrees" / "task"
    checkout.mkdir(parents=True)
    (checkout / "data").write_bytes(b"checkout")
    usage = ss.StorageUsage(root)
    try:
        result = usage.step()
        assert result == {"scratch": 4, "checkouts": 8}
    finally:
        usage.close()


def test_database_settle_producer_and_open_turn_guard(mock_db, tmp_path, monkeypatch):
    from app.db import database, sessions, turns
    from app.db.base import get_db

    sid = "scratch-produced-session"
    database.create_session(sid, None, is_active=True, derive_provider_from_profile=False)
    root = tmp_path / "projects"
    root.mkdir()
    path = folder(root, sid)
    monkeypatch.setattr(ss, "_process_references", lambda deadline, **kwargs: set())
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
        sessions.set_session_active(sid, False)
        turns.record_adhoc_turn(sid, "test", outcome="completed")
        with get_db() as conn:
            conn.execute("UPDATE turns SET ended_at = NULL WHERE session_id = %s", (sid,))
        assert sweep.step() == 0 and path.exists()
        with get_db() as conn:
            conn.execute("UPDATE turns SET ended_at = NOW() WHERE session_id = %s", (sid,))
        assert sweep.step() == 1 and not path.exists()
    finally:
        sweep.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow descriptor operations")
def test_raced_parent_symlink_cannot_redirect_unlink(tmp_path, monkeypatch):
    root = tmp_path / "root"
    parent = root / "parent"
    parent.mkdir(parents=True)
    payload = parent / "data"
    payload.write_text("original")
    info = payload.lstat()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("keep")
    original = os.open
    swapped = False
    def open_path(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "parent" and not swapped:
            swapped = True
            parent.rename(root / "saved")
            parent.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(ss.os, "open", open_path)
    with pytest.raises(OSError):
        ss._remove_entry(root, payload, info, ss._identity(root.lstat()))
    assert (outside / "data").read_text() == "keep"
    assert (root / "saved" / "data").read_text() == "original"


def test_changes_during_resumable_scan_abort_before_any_unlink(world, monkeypatch):
    root, _, sweep = world
    path = folder(root, count=5)
    monkeypatch.setattr(ss, "MAX_SCAN_ENTRIES", 1)
    sweep.step()
    (path / "0").write_text("changed after observation")
    for _ in range(15):
        sweep.step()
        if sweep.candidates is None:
            break
    assert len(list(path.iterdir())) == 5


@pytest.mark.asyncio
async def test_maintenance_paces_slices_and_warns_without_size_eviction(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import notification_service

    waits = []
    closed = []
    class Sweep:
        candidates = True
        def __init__(self, root):
            pass
        def step(self):
            return 0
        def close(self):
            closed.append("sweep")
    class Usage(Sweep):
        def step(self):
            return {"scratch": 101 * 1024**3, "checkouts": 0}
    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError
    notify = AsyncMock()
    monkeypatch.setattr(ss, "ScratchSweep", Sweep)
    monkeypatch.setattr(ss, "StorageUsage", Usage)
    monkeypatch.setattr(ss.asyncio, "sleep", sleep)
    monkeypatch.setattr(notification_service, "emit_notification", notify)
    with pytest.raises(asyncio.CancelledError):
        await ss.maintain_session_scratch()
    assert waits == [60, ss.SLICE_PAUSE_SECONDS]
    assert len(closed) == 2
    notify.assert_awaited_once()
    assert notify.call_args.kwargs["category"] == "agent_storage"


def test_windows_reference_case_and_separator_normalization(monkeypatch):
    import ntpath
    monkeypatch.setattr(ss.os.path, "normcase", ntpath.normcase)
    assert ss._in_use(r"C:\State\projects\p\scratch\s", {"python --input=c:/state/projects/p/scratch/s/input"})
