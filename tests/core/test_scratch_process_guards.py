"""Opaque process cases: run the actual reference scanner beside disposable scratch."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from app.core import scratch_reaper as sr
from app.core import session_scratch as ss


def session(root, key="s"):
    path = root / "project" / "scratch" / key
    path.mkdir(parents=True)
    (path / "payload").write_bytes(b"data")
    os.utime(path / "payload", (1, 1))
    os.utime(path, (1, 1))
    return path


@pytest.fixture
def processes(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Linux-style proc fixture")
    proc = tmp_path / "proc"
    proc.mkdir()
    opaque = set()
    original = os.scandir
    def scandir(path):
        if Path(path).name == "fd" and Path(path).parent.name in opaque:
            raise PermissionError("non-dumpable")
        return original(path)
    monkeypatch.setattr(sr.os, "scandir", scandir)
    def add(pid, args, parent=0, *, hidden=False, start="1234", cwd=None):
        base = proc / str(pid)
        base.mkdir()
        uid = os.getuid()
        (base / "status").write_text(f"Name:\tpython\nUid:\t{uid} {uid} {uid} {uid}\nPPid:\t{parent}\n")
        (base / "comm").write_text("python\n")
        (base / "stat").write_text(f"{pid} (python) S " + "0 " * 18 + start)
        (base / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in args) + b"\0")
        (base / "fd").mkdir()
        (base / "cwd").symlink_to(cwd or tmp_path, target_is_directory=True)
        (base / "root").symlink_to(Path("/"), target_is_directory=True)
        if hidden:
            opaque.add(str(pid))
        return base
    add(os.getpid(), ["python", "probe.py"], parent=880001)
    root = tmp_path / "projects"
    monkeypatch.setattr(ss, "_session_rows", lambda key: [{"id": key, "is_active": False, "open_turn": False}])
    def inspect(deadline, diagnostics=None):
        return sr._references(proc, deadline, strict=True,
                              on_disabled=diagnostics.append if diagnostics is not None else None)
    monkeypatch.setattr(ss, "_process_references", inspect)
    return proc, add, root


def test_exact_supervisor_ancestor_does_not_disable_eligible_candidate(processes, caplog):
    _, add, root = processes
    add(880001, ["python", "/opt/app/docker/container_runtime.py", "run"], hidden=True)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 1 and not path.exists()
        finally:
            sweep.close()
    messages = [r.message for r in caplog.records if "Session scratch pass:" in r.message]
    assert len(messages) == 1
    assert "removed=1 removed_bytes=4 retained=0" in messages[0]
    assert "disabled=none" in messages[0]


@pytest.mark.parametrize("args,parent", [
    (["python", "/opt/app/docker/container_runtime.py", "run"], 880999),
    (["python", "/opt/app/not-supervisor.py", "run"], 880001),
    (["python", "-m", "app.main"], 880001),
])
def test_opaque_sibling_wrong_launcher_or_unreceipted_app_retains(processes, args, parent, caplog):
    proc, add, root = processes
    (proc / str(os.getpid()) / "status").write_text(
        f"Name: python\nUid: {os.getuid()}\nPPid: {parent}\n")
    add(880001, args, hidden=True)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 0 and path.exists()
        finally:
            sweep.close()
    assert "pid=880001 name=python" in caplog.text
    assert "retained=1" in caplog.text


def preview(processes, tmp_path, monkeypatch, *, bare=False):
    _, add, _ = processes
    temp = tmp_path / "temp"
    temp.mkdir()
    directory = temp / "ai-shuffle-preview-fixture"
    directory.mkdir(mode=0o700)
    for name in ("home", "data", "workspace"):
        (directory / name).mkdir()
    (directory / ".branch-preview").write_text(str(directory / "data"))
    checkout = tmp_path / "another-worktree"
    checkout.mkdir()
    add(880001, ["python", "-m", "scripts.devloop", "up"], cwd=checkout)
    marker = {"version": 1, "pid": 880001, "start": "1234", "uid": os.getuid(), "created": time.time()}
    (directory / sr.MARKER).write_text(json.dumps(marker))
    activity = directory / "activity.json"
    activity.write_text(json.dumps({"pid": 880002, "identity": "1234", "updated": time.monotonic()}))
    args = ["python", "-m", "app.main"] if bare else ["python", "-m", "scripts.devloop.child", str(activity)]
    child = add(880002, args, parent=880001, hidden=True)
    monkeypatch.setattr(sr.tempfile, "gettempdir", lambda: str(temp))
    return directory, child


@pytest.mark.parametrize("bare", [False, True])
def test_live_isolated_preview_from_other_checkout_allows_cleanup_and_protects_roots(
        processes, tmp_path, monkeypatch, caplog, bare):
    proc, _, root = processes
    directory, _ = preview(processes, tmp_path, monkeypatch, bare=bare)
    refs = sr._references(proc, time.monotonic() + 2, strict=True)
    assert refs is not None and refs.preview_exemptions == 1
    assert ss._in_use(directory / "home" / ".fluid" / "projects" / "p" / "scratch" / "s", refs)
    assert ss._in_use(directory.parent, refs)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 1 and not path.exists()
        finally:
            sweep.close()
    assert "preview_exemptions=1" in caplog.text


@pytest.mark.parametrize("broken", ["owner_start", "child_start", "stale_activity", "missing_marker", "symlink_home", "forged_uid", "forged_launcher"])
def test_stale_reused_or_unsafe_preview_receipts_retain(processes, tmp_path, monkeypatch, broken):
    proc, _, root = processes
    directory, child = preview(processes, tmp_path, monkeypatch)
    if broken == "owner_start":
        receipt = json.loads((directory / sr.MARKER).read_text())
        receipt["start"] = "reused-pid"
        (directory / sr.MARKER).write_text(json.dumps(receipt))
    elif broken == "child_start":
        (child / "stat").write_text("880002 (python) S " + "0 " * 18 + "99999")
    elif broken == "stale_activity":
        (directory / "activity.json").write_text(json.dumps({"pid": 880002, "identity": "1234", "updated": 0}))
    elif broken == "missing_marker":
        (directory / ".branch-preview").unlink()
    elif broken == "forged_uid":
        receipt = json.loads((directory / sr.MARKER).read_text())
        receipt["uid"] += 1
        (directory / sr.MARKER).write_text(json.dumps(receipt))
    elif broken == "forged_launcher":
        (proc / "880001" / "cmdline").write_bytes(b"python\0unknown.py\0")
    else:
        (directory / "home").rmdir()
        (directory / "home").symlink_to(tmp_path, target_is_directory=True)
    assert sr._references(proc, time.monotonic() + 2, strict=True) is None
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
    finally:
        sweep.close()




def test_without_supervisor_no_exemption_is_needed(processes):
    _, _, root = processes
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 1 and not path.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("workers_arg", [["--workers", "2"], ["--workers=2"]])
def test_paired_preview_scopes_are_proven_from_exact_launcher(processes, tmp_path, monkeypatch, workers_arg):
    proc, _, root = processes
    directory, _ = preview(processes, tmp_path, monkeypatch, bare=True)
    (directory / ".branch-preview").unlink()
    for name in ("home", "data", "workspace"):
        (directory / name).rmdir()
    for worker in ("worker-a", "worker-b"):
        scope = directory / worker
        scope.mkdir()
        for name in ("home", "data", "workspace"):
            (scope / name).mkdir()
        (scope / ".branch-preview").write_text(str(scope / "data"))
    (proc / "880001" / "cmdline").write_bytes(
        b"\0".join(os.fsencode(x) for x in ["python", "-m", "scripts.devloop", "up", *workers_arg]) + b"\0")
    refs = sr._references(proc, time.monotonic() + 2, strict=True)
    assert refs is not None and refs.preview_exemptions == 1
    assert ss._in_use(directory / "worker-b" / "home" / "nested", refs)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 1 and not path.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("fresh_identity", [True, False])
def test_devloop_status_checkout_is_protected_only_for_live_identity(
        processes, tmp_path, monkeypatch, fresh_identity):
    proc, _, root = processes
    preview(processes, tmp_path, monkeypatch)
    path = session(root)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"pid": 880001, "pid_identity": "1234", "checkout": str(path),
                                 "app": {"pid": 880002, "pid_identity": "1234" if fresh_identity else "old"}}))
    (proc / "880001" / "cmdline").write_bytes(b"\0".join(os.fsencode(x) for x in
        ["python", "-m", "scripts.devloop", "up", "--status", str(status)]) + b"\0")
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == (0 if fresh_identity else 1)
        assert path.exists() is fresh_identity
    finally:
        sweep.close()


class Denied:
    def __init__(self, pid):
        self.pid = pid
    def name(self):
        return "protected-test"
    def cwd(self):
        raise psutil.AccessDenied(self.pid)
    cmdline = open_files = cwd


def test_windows_denied_process_does_not_turn_complete_table_into_unknown(monkeypatch):
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([Denied(123)]))
    notes = []
    assert ss._psutil_references(time.monotonic() + 2, notes, windows=True) == set()
    assert "pid=123 name=protected-test" in notes[0]
    assert ss._psutil_references(time.monotonic() - 1, notes, windows=True) is None
    assert ss._psutil_references(time.monotonic() + 2, notes, windows=False) is None


@pytest.mark.skipif(os.name != "nt", reason="real Windows handle sharing semantics")
@pytest.mark.parametrize("share_delete", [False, True])
def test_windows_opaque_handle_holder_survives_and_sibling_drains(tmp_path, monkeypatch, share_delete):
    root = tmp_path / "projects"
    held = session(root, "held")
    good = session(root, "good")
    script = """
import ctypes, sys
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
handle = kernel.CreateFileW(sys.argv[1], 0x80000000, int(sys.argv[2]), None, 3, 0x80, None)
if handle == wintypes.HANDLE(-1).value:
    raise ctypes.WinError(ctypes.get_last_error())
print('ready', flush=True)
sys.stdin.read(1)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(held / "payload"), "7" if share_delete else "3"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp_path)
    try:
        assert child.stdout.readline().strip() == "ready"
        real = psutil.Process(os.getpid())
        monkeypatch.setattr(psutil, "process_iter", lambda: iter([Denied(child.pid), real]))
        monkeypatch.setattr(ss, "_session_rows", lambda key: [{"id": key, "is_active": False, "open_turn": False}])
        sweep = ss.ScratchSweep(root)
        try:
            assert sweep.step() == 1
            assert (held / "payload").read_bytes() == b"data"
            assert not good.exists()
            assert sweep.retained == 1
        finally:
            sweep.close()
    finally:
        child.communicate("x", timeout=5)
