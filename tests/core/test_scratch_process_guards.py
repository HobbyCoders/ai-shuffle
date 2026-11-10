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
    def add(pid, args, parent=0, *, hidden=False, start="1234", cwd=None, uids=None, name="python"):
        base = proc / str(pid)
        base.mkdir()
        uid_text = " ".join(str(uid) for uid in (uids or [os.getuid()] * 4))
        (base / "status").write_text(f"Name:\t{name}\nUid:\t{uid_text}\nPPid:\t{parent}\n")
        (base / "comm").write_text(name + "\n")
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


# entrypoint.sh execs these flags with "$@" empty; handover appends "run".
LAUNCHERS = [
    ["/usr/local/bin/python3", "-I", "-B", "-u", "/app/docker/container_runtime.py"],
    ["/version/venv/bin/python", "/version/source/docker/container_runtime.py", "run"],
    ["/bin/sh", "-c", "launch-app"],
    ["python", "-m", "app.main"],
]


@pytest.mark.parametrize("args", LAUNCHERS)
def test_opaque_live_ancestor_does_not_disable_eligible_candidate(processes, caplog, args):
    _, add, root = processes
    add(880001, args, name=Path(args[0]).name, hidden=True)
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
    (["python", "/opt/app/not-supervisor.py", "run"], 880999),
    (["python", "-m", "app.main"], 880999),
])
def test_opaque_sibling_retains_regardless_of_launcher(processes, args, parent, caplog):
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


def test_incomplete_ancestor_chain_retains(processes):
    proc, add, root = processes
    add(880001, LAUNCHERS[0], hidden=True)
    add(880002, ["sh"], parent=880003)
    own = proc / str(os.getpid()) / "status"
    own.write_text(own.read_text().replace("880001", "880002"))
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("reused", [880001, 880002])
def test_reused_target_or_intermediate_pid_cannot_prove_ancestry(processes, monkeypatch, reused):
    proc, add, root = processes
    monkeypatch.delenv("SHUFFLE_AGENT_ISOLATION", raising=False)
    add(880001, LAUNCHERS[0], hidden=True)
    add(880002, ["sh"], parent=880001)
    own = proc / str(os.getpid()) / "status"
    own.write_text(own.read_text().replace("880001", "880002"))
    identity = sr.process_identity
    calls = 0
    def changing(pid, proc):
        nonlocal calls
        if int(pid) == reused:
            calls += 1
            if calls > 1:
                return "reused"
        return identity(pid, proc)
    monkeypatch.setattr(sr, "process_identity", changing)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
        assert calls >= 2
    finally:
        sweep.close()


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
@pytest.mark.parametrize("ancestor", [False, True])
def test_live_isolated_preview_from_other_checkout_allows_cleanup_and_protects_roots(
        processes, tmp_path, monkeypatch, caplog, bare, ancestor):
    proc, _, root = processes
    directory, _ = preview(processes, tmp_path, monkeypatch, bare=bare)
    if ancestor:
        own = proc / str(os.getpid()) / "status"
        own.write_text(own.read_text().replace("880001", "880002"))
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


@pytest.mark.parametrize("bare", [False, True])
def test_preview_exemption_works_without_the_scripts_package(processes, tmp_path, monkeypatch, bare):
    """The installed app ships ``app`` and ``fluid`` only. Importing the dev
    loop's status helper from ``scripts`` failed there, so one running dev
    instance disabled every scratch pass on the worker."""
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "scripts", None)  # ``import scripts...`` now raises ImportError
    with pytest.raises(ImportError):
        __import__("scripts.devloop.__main__")
    proc, _, root = processes
    preview(processes, tmp_path, monkeypatch, bare=bare)
    refs = sr._references(proc, time.monotonic() + 2, strict=True)
    assert refs is not None and refs.preview_exemptions == 1
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 1 and not path.exists()
        assert sweep.disabled_reason == "none"
    finally:
        sweep.close()


def test_devloop_status_path_is_the_one_the_reaper_reads(tmp_path, monkeypatch):
    from app.core import temp_scratch
    from scripts.devloop import __main__ as devloop

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert devloop.default_status_path(tmp_path) == temp_scratch.devloop_status_path(tmp_path)
    assert devloop.status_directory() == temp_scratch.devloop_status_directory() == tmp_path / "ai-shuffle-devloop"


def test_shipped_packages_never_import_scripts_at_runtime():
    """``scripts`` is a repository tool, absent from an installed app."""
    import ast

    root = Path(__file__).resolve().parents[2]
    # Dev-only launchers that run from a checkout, and test code.
    allowed = {"app/preview_run.py"}
    offenders = []
    for package in ("app", "fluid"):
        for path in (root / package).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            if relative in allowed or "/tests/" in relative or path.name in ("conftest.py",):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
                if any(name == "scripts" or name.startswith("scripts.") for name in names):
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == []


def test_process_check_has_its_own_budget_after_a_long_walk(processes, monkeypatch):
    """A walk that used the whole pass budget must not fail the process check
    (and discard the walk): the check gets its own bounded time."""
    _, _, root = processes
    path = session(root)
    deadlines = []
    inspect = ss._process_references

    def recording(deadline, diagnostics=None):
        deadlines.append(deadline - time.monotonic())
        return inspect(deadline, diagnostics)

    monkeypatch.setattr(ss, "_process_references", recording)
    sweep = ss.ScratchSweep(root)
    try:
        sweep._start(path)
        sweep.retained, sweep.reasons, sweep.process_notes = 0, ss.Counter(), set()
        sweep.preview_exemptions = 0
        # The pass deadline has already passed when the check begins.
        assert sweep._guard(sweep.job, time.monotonic() - 1) is True
        assert sweep.retained == 0
        assert ss.PROCESS_SCAN_SECONDS - 0.5 < deadlines[0] <= ss.PROCESS_SCAN_SECONDS
        # A pass with more time left than that keeps its own deadline.
        assert sweep._guard(sweep.job, time.monotonic() + 30) is True
        assert deadlines[1] > 29
    finally:
        sweep.close()


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
@pytest.mark.parametrize("ancestor", [False, True])
def test_devloop_status_checkout_is_protected_only_for_live_identity(
        processes, tmp_path, monkeypatch, fresh_identity, ancestor):
    proc, _, root = processes
    preview(processes, tmp_path, monkeypatch)
    if ancestor:
        own = proc / str(os.getpid()) / "status"
        own.write_text(own.read_text().replace("880001", "880002"))
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


@pytest.fixture(params=LAUNCHERS)
def split_ids(processes, monkeypatch, request):
    proc, add, root = processes
    uid, gid = os.getuid(), os.getgid()
    service = 64000 if uid != 64000 else 64001
    service_gid = 64000 if gid != 64000 else 64001
    monkeypatch.setenv("SHUFFLE_AGENT_ISOLATION", "on")
    monkeypatch.delenv("AGENT_ISOLATION_SERVICE_UID", raising=False)
    if service != 64000:
        monkeypatch.setenv("AGENT_ISOLATION_SERVICE_UID", str(service))
    monkeypatch.setattr(os, "getresuid", lambda: (uid, uid, service))
    monkeypatch.setattr(os, "getresgid", lambda: (gid, gid, service_gid))
    add(880001, request.param, name=Path(request.param[0]).name, hidden=True,
        uids=[uid, service, service, service])
    (proc / str(os.getpid()) / "status").write_text(
        f"Name: python\nUid: {uid} {uid} {service} {uid}\nPPid: 880001\n")
    return proc, add, root, service


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_split_service_postgres_and_forked_helpers_allow_eligible_candidate(split_ids, depth):
    _, add, root, service = split_ids
    parent = 880001
    for index in range(depth + 1):
        pid = 880010 + index
        add(pid, ["postgres", "-D", "/data/postgres/data"], parent=parent,
            hidden=True, uids=[service] * 4, name="postgres")
        parent = pid
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 1 and not path.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("bad", ["unknown_service", "agent_postgres", "other_parent", "uid_gap",
                                  "flag_unset", "same_saved_uid", "parent_cycle"])
def test_split_role_proof_never_exempts_unknown_or_agent_process(split_ids, monkeypatch, bad):
    _, add, root, service = split_ids
    kwargs = {"uids": [service] * 4, "name": "postgres", "parent": 880001, "hidden": True}
    if bad == "unknown_service":
        kwargs["name"] = "python"
    elif bad == "agent_postgres":
        kwargs["uids"] = [os.getuid()] * 4
    elif bad == "other_parent":
        add(880030, ["python", "/opt/app/not-supervisor.py"], hidden=False)
        kwargs["parent"] = 880030
    elif bad == "uid_gap":
        add(880030, ["postgres"], name="postgres", parent=880001, uids=[os.getuid()] * 4)
        kwargs["parent"] = 880030
    elif bad == "flag_unset":
        monkeypatch.delenv("SHUFFLE_AGENT_ISOLATION")
    elif bad == "same_saved_uid":
        monkeypatch.setattr(os, "getresuid", lambda: (os.getuid(),) * 3)
    elif bad == "parent_cycle":
        kwargs["parent"] = 880010
    add(880010, ["postgres"], **kwargs)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("name,pid", [("tini", "880001"), ("docker-init", "880001"),
                                      ("init", "880001"), ("systemd", "880001"), ("python", "1")])
def test_postgres_chain_ending_only_at_init_never_qualifies(split_ids, name, pid):
    proc, add, root, service = split_ids
    base = proc / "880001"
    status = base / "status"
    rows = status.read_text().splitlines()
    status.write_text("\n".join(f"Name: {name}" if row.startswith("Name:") else row for row in rows))
    if pid == "1":
        base.rename(proc / pid)
        own = proc / str(os.getpid()) / "status"
        own.write_text(own.read_text().replace("880001", pid))
    pg = add(880010, ["postgres"], parent=int(pid), hidden=True, uids=[service] * 4, name="postgres")
    assert not sr._managed_postgres(pg, proc, time.monotonic() + 2)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
    finally:
        sweep.close()


def test_foreign_uid_ancestor_is_not_a_postgres_chain_by_itself(split_ids):
    proc, _, root, service = split_ids
    status = proc / "880001" / "status"
    rows = status.read_text().splitlines()
    status.write_text("\n".join(f"Uid: {service} {service} {service} {service}"
                                if row.startswith("Uid:") else row for row in rows))
    assert not sr._managed_postgres(status.parent, proc, time.monotonic() + 2)
    path = session(root)
    sweep = ss.ScratchSweep(root)
    try:
        assert sweep.step() == 0 and path.exists()
    finally:
        sweep.close()


def test_managed_postgres_argv_still_protects_candidate(split_ids, caplog):
    _, add, root, service = split_ids
    path = session(root)
    add(880010, ["postgres", "-D", str(path)], parent=880001,
        hidden=True, uids=[service] * 4, name="postgres")
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 0 and path.exists()
        finally:
            sweep.close()
    assert "process_reference pid=880010 name=postgres" in caplog.text
    assert str(path) not in caplog.text


def test_postmaster_restart_uses_fresh_ancestry_not_old_pid(split_ids):
    proc, add, root, service = split_ids
    add(880010, ["postgres"], parent=880001, hidden=True, uids=[service] * 4, name="postgres")
    add(880011, ["postgres"], parent=880010, hidden=True, uids=[service] * 4, name="postgres")
    sweep = ss.ScratchSweep(root)
    first = session(root, "first")
    try:
        assert sweep.step() == 1 and not first.exists()
        (proc / "880010").rename(proc / "exited-postmaster")
        add(880020, ["postgres"], parent=880001, hidden=True, uids=[service] * 4, name="postgres")
        status = proc / "880011" / "status"
        status.write_text(status.read_text().replace("880010", "880020"))
        second = session(root, "second")
        assert sweep.step() == 1 and not second.exists()
        # Surviving helpers reparented to init have no longer proven role.
        status.write_text(status.read_text().replace("880020", "1"))
        third = session(root, "third")
        assert sweep.step() == 0 and third.exists()
    finally:
        sweep.close()


@pytest.mark.parametrize("field", ["cwd", "fd", "cmdline"])
def test_readable_holder_is_named_without_its_private_path(processes, caplog, field):
    _, add, root = processes
    path = session(root)
    args = ["python", "task.py"] + ([str(path / "payload")] if field == "cmdline" else [])
    base = add(880099, args, cwd=path if field == "cwd" else None)
    if field == "fd":
        (base / "fd" / "10").symlink_to(path / "payload")
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 0 and path.exists()
        finally:
            sweep.close()
    assert "process_reference pid=880099 name=python" in caplog.text
    assert str(path) not in caplog.text


@pytest.mark.parametrize("reverse", [False, True])
def test_holder_details_are_capped_and_names_sanitized(processes, caplog, monkeypatch, reverse):
    from contextlib import nullcontext
    proc, add, root = processes
    scan = os.scandir
    def ordered(path):
        if Path(path) == proc:
            with scan(path) as entries:
                return nullcontext(iter(sorted(entries, key=lambda entry: entry.name, reverse=reverse)))
        return scan(path)
    monkeypatch.setattr(sr.os, "scandir", ordered)
    paths = []
    for index in range(12):
        # Equal-width keys cannot prefix-match another holder's cwd. Reference
        # matching is conservative; held-1 would also match held-10/held-11.
        path = session(root, f"held-{index:02d}")
        paths.append(path)
        base = add(881000 + index, ["python"], cwd=path)
        (base / "comm").write_text("long/name\n" + "x" * 1000)
    sweep = ss.ScratchSweep(root)
    with caplog.at_level("INFO", logger=ss.__name__):
        try:
            assert sweep.step() == 0 and all(path.exists() for path in paths)
        finally:
            sweep.close()
    named = [key for key in sweep.reasons if key.startswith("process_reference pid=")]
    assert len(named) == 8
    assert sweep.reasons["process_reference additional_holders"] == 4
    assert all(len(key.split("name=", 1)[1]) == 32 for key in named)
    assert all(str(path) not in caplog.text for path in paths)
    assert "long/name" not in caplog.text


def test_psutil_readable_reference_keeps_owner(monkeypatch, tmp_path):
    from types import SimpleNamespace
    process = SimpleNamespace(pid=124, name=lambda: "python", cwd=lambda: str(tmp_path),
                              open_files=list, cmdline=lambda: ["python"])
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([Denied(123), process]))
    refs = ss._psutil_references(time.monotonic() + 2, [], windows=True)
    assert ss._in_use(tmp_path, refs)
    assert refs.holder(tmp_path) == "pid=124 name=python"


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
