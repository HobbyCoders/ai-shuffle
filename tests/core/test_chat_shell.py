"""Isolated folder shell: fail-closed availability, approval gating, and real
sandbox escape attempts.

The escape tests need Linux, bubblewrap and permission to create user
namespaces. They skip elsewhere, except that CHAT_SHELL_REQUIRE_SANDBOX=1 (set
by the CI step that installs bubblewrap) turns a missing sandbox into a failure.
"""
import asyncio
import errno
import os
import socket
import struct
import sys
import time
from unittest.mock import AsyncMock

import pytest

from app.core import chat_access, chat_files, chat_shell
from app.core.chat_access import AccessDenied, Broker, Grant


@pytest.fixture(autouse=True)
def fresh_self_test():
    chat_shell.invalidate()
    yield
    chat_shell.invalidate()


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("local", "worker")))
    return Broker()


def folder_grant(broker, tmp_path, writable=True):
    """A folder grant whose root is any open descriptor (runs on every OS)."""
    root = os.open(tmp_path / f"root-{len(broker.grants)}", os.O_RDWR | os.O_CREAT)
    grant = Grant(f"g{len(broker.grants)}", "chat", ("local", "worker"), "Files", "folder", "/folder",
                  time.time() + 600, writable=writable, root=root)
    broker.grants[grant.id] = grant
    return grant


async def pending(broker):
    async with asyncio.timeout(3):
        while not broker.actions:
            await asyncio.sleep(0.001)
    return next(iter(broker.actions.values()))


# ── fail closed ──────────────────────────────────────────────────────────────


def test_preconditions_refuse_non_linux(monkeypatch):
    monkeypatch.setattr(chat_shell.sys, "platform", "darwin")
    assert chat_shell._preconditions() == (None, "the isolated shell is Linux-only")


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX")
def test_preconditions_refuse_root_worker(monkeypatch):
    monkeypatch.setattr(chat_shell.sys, "platform", "linux")
    monkeypatch.setattr(chat_shell.os, "geteuid", lambda: 0)
    assert chat_shell._preconditions() == (None, "the worker runs as root")


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX")
def test_preconditions_refuse_an_architecture_without_a_filter(monkeypatch):
    monkeypatch.setattr(chat_shell.sys, "platform", "linux")
    monkeypatch.setattr(chat_shell.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(chat_shell.platform, "machine", lambda: "riscv64")
    assert chat_shell._preconditions() == (None, "no seccomp filter for riscv64")


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX")
@pytest.mark.parametrize("help_text, reason", [
    (None, "bubblewrap (bwrap) is not installed"),
    ("--bind --ro-bind --size --disable-userns --seccomp", "bwrap lacks --bind-fd, --ro-bind-fd"),
    ("--bind-fd --ro-bind-fd --size --disable-userns", "bwrap lacks --seccomp"),
])
def test_preconditions_refuse_missing_or_old_bwrap(monkeypatch, help_text, reason):
    monkeypatch.setattr(chat_shell.sys, "platform", "linux")
    monkeypatch.setattr(chat_shell.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(chat_shell.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(chat_shell, "_bwrap", lambda: None if help_text is None else "/usr/bin/bwrap")

    class Done:
        stdout, stderr = help_text or "", ""
    monkeypatch.setattr(chat_shell.subprocess, "run", lambda *a, **k: Done())
    assert chat_shell._preconditions() == (None, reason)


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX")
@pytest.mark.parametrize("data_dir, refused", [
    ("/opt/python/var/data", True), ("/opt/python", True), ("/opt/python-data", False), ("/data", False)])
def test_preconditions_refuse_a_python_prefix_holding_the_data_dir(monkeypatch, data_dir, refused):
    """The prefix is bound read-only into the sandbox; the worker's private data
    must never come with it (the same rule as for folder grants)."""
    from app.core.config import settings
    monkeypatch.setattr(chat_shell.sys, "platform", "linux")
    monkeypatch.setattr(chat_shell.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(chat_shell.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(chat_shell, "_bwrap", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(chat_shell, "_python_roots", lambda: ["/opt/python"])
    monkeypatch.setattr(chat_shell.os.path, "realpath", lambda path: str(path))
    monkeypatch.setattr(type(settings), "effective_data_dir", property(lambda self: data_dir))

    class Done:
        stdout, stderr = " ".join(chat_shell.REQUIRED_OPTIONS), ""
    monkeypatch.setattr(chat_shell.subprocess, "run", lambda *a, **k: Done())
    expected = (None, "the Python prefix contains the worker's data directory") if refused else ("/usr/bin/bwrap", "")
    assert chat_shell._preconditions() == expected


def run_filter(program: bytes, arch: int, nr: int, args=(0, 0)) -> int:
    """Evaluate the classic BPF seccomp program on one syscall."""
    data = struct.pack("=iIQ6Q", nr, arch, 0, *args, 0, 0, 0, 0)
    code = [struct.unpack_from("=HBBI", program, i) for i in range(0, len(program), 8)]
    pc, acc = 0, 0
    while True:
        op, jt, jf, k = code[pc]
        if op == 0x20:
            acc = struct.unpack_from("=I", data, k)[0]
        elif op == 0x54:
            acc &= k
        elif op in (0x15, 0x35):
            pc += jt if (acc == k if op == 0x15 else acc >= k) else jf
        elif op == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected opcode {op:#x}")
        pc += 1


@pytest.mark.parametrize("machine", sorted(chat_shell._SECCOMP_ARCH))
def test_seccomp_filter_blocks_unix_sockets_io_uring_and_foreign_abis(machine):
    program = chat_shell.seccomp_filter(machine)
    arch, nr = chat_shell._SECCOMP_ARCH[machine]
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    for family, verdict in ((2, allow), (10, allow), (1, deny),  # Linux AF_INET, AF_INET6, AF_UNIX
                            (16, deny), (17, deny), (40, deny), (0x100000001, deny)):  # netlink, packet, vsock
        assert run_filter(program, arch, nr["socket"], (family, 1)) == verdict, family
    for kind, verdict in ((1, allow), (1 | 0o4000 | 0o2000000, allow), (2, deny), (5, deny)):
        assert run_filter(program, arch, nr["socketpair"], (1, kind)) == verdict, kind
    for name in ("io_uring_setup", "keyctl", "add_key", "request_key"):
        assert run_filter(program, arch, nr[name]) == deny, name
    assert run_filter(program, arch, 0) == allow
    assert run_filter(program, 0x40000003, 102) == 0x80000000  # i386 socketcall
    if machine == "x86_64":
        assert run_filter(program, arch, 0x40000000 | 41, (1, 1)) == deny  # x32 socket


def test_self_test_failure_is_retried_after_ttl_and_pass_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(chat_shell, "_preconditions", lambda: ("/usr/bin/bwrap", ""))
    results = iter([(False, "no userns"), (True, ""), (False, "never asked")])
    monkeypatch.setattr(chat_shell, "_probe", lambda bwrap: calls.append(bwrap) or next(results))
    assert chat_shell.self_test() == (False, "no userns")
    assert chat_shell.self_test() == (False, "no userns")  # cached failure
    assert len(calls) == 1
    monkeypatch.setattr(chat_shell, "_FAILURE_TTL", 0.0)
    assert chat_shell.self_test() == (True, "")
    assert chat_shell.self_test() == (True, "")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unavailable_shell_refuses_before_any_approval(broker, monkeypatch, tmp_path):
    monkeypatch.setattr(chat_shell, "self_test", lambda: (False, "bubblewrap (bwrap) is not installed"))
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "default"})
    run = AsyncMock()
    monkeypatch.setattr(chat_shell, "run", run)
    grant = folder_grant(broker, tmp_path)
    with pytest.raises(AccessDenied, match="cannot run an isolated shell .*not installed"):
        await broker.run("chat", grant.id, "command", {"command": "ls"})
    assert not broker.actions and not run.called
    broker.revoke("chat", grant.id)


@pytest.mark.asyncio
async def test_service_grant_cannot_run_commands(broker):
    grant = Grant("svc", "chat", ("local", "worker"), "API", "service", "https://x", time.time() + 600)
    broker.grants[grant.id] = grant
    with pytest.raises(AccessDenied, match="machine grant or a folder grant"):
        await broker.run("chat", grant.id, "command", {"command": "ls"})


# ── approval gating, exactly like the SSH command path ───────────────────────


@pytest.fixture
def fake_sandbox(monkeypatch):
    seen = []

    async def run(fd, writable, command):
        os.fstat(fd)  # open while the command runs
        seen.append((fd, writable, command))
        return {"exit_code": 0, "stdout": "ran", "stderr": "", "truncated": False}
    monkeypatch.setattr(chat_shell, "ensure_available", AsyncMock())
    monkeypatch.setattr(chat_shell, "ensure_folder", AsyncMock())
    monkeypatch.setattr(chat_shell, "run", run)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_folder_command_waits_for_one_shot_approval(broker, fake_sandbox, monkeypatch, tmp_path, allow):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "default"})
    grant = folder_grant(broker, tmp_path, writable=False)
    task = asyncio.create_task(broker.run("chat", grant.id, "command", {"command": "ls -la"}))
    action = await pending(broker)
    assert (action.operation, action.arguments) == ("command", {"command": "ls -la"})
    assert not fake_sandbox
    await broker.decide("chat", action.id, allow)
    if allow:
        assert (await asyncio.wait_for(task, 3))["stdout"] == "ran"
        [(fd, writable, command)] = fake_sandbox
        # A private duplicate, closed afterwards; never the grant's own descriptor.
        assert fd != grant.root and writable is False and command == "ls -la"
        with pytest.raises(OSError):
            os.fstat(fd)
    else:
        with pytest.raises(AccessDenied, match="denied"):
            await asyncio.wait_for(task, 3)
        assert not fake_sandbox
    os.fstat(grant.root)
    broker.revoke("chat", grant.id)


@pytest.mark.asyncio
async def test_folder_command_in_bypass_runs_without_card(broker, fake_sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "bypassPermissions"})
    grant = folder_grant(broker, tmp_path)
    result = await broker.run("chat", grant.id, "command", {"command": "true"})
    assert result["exit_code"] == 0 and fake_sandbox[0][1] is True and not broker.actions
    broker.revoke("chat", grant.id)


@pytest.mark.asyncio
async def test_revocation_stops_running_folder_command(broker, monkeypatch, tmp_path):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "bypassPermissions"})
    monkeypatch.setattr(chat_shell, "ensure_available", AsyncMock())
    monkeypatch.setattr(chat_shell, "ensure_folder", AsyncMock())
    started, stopped, held = asyncio.Event(), asyncio.Event(), []

    async def run(fd, writable, command):
        held.append(fd)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(chat_shell, "run", run)
    grant = folder_grant(broker, tmp_path)
    task = asyncio.create_task(broker.run("chat", grant.id, "command", {"command": "sleep 100"}))
    await asyncio.wait_for(started.wait(), 3)
    broker.revoke("chat", grant.id)
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 3)
    assert stopped.is_set()
    with pytest.raises(OSError):
        os.fstat(held[0])


# ── argv and mount parsing ───────────────────────────────────────────────────


def test_argv_isolates_and_binds_only_the_descriptor(monkeypatch):
    monkeypatch.setattr(chat_shell, "_python_roots", lambda: ["/opt/python"])
    for writable, option in ((True, "--bind-fd"), (False, "--ro-bind-fd")):
        argv = chat_shell.build_argv("/usr/bin/bwrap", 9, writable, "echo hi", 11)
        assert argv[:4] == ["/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns"]
        at = argv.index("--seccomp")
        assert argv[at + 1] == "11" and at < argv.index("--")
        assert not {"--share-net", "--bind", "--dev-bind", "--bind-try"} & set(argv)
        at = argv.index(option)
        assert argv[at + 1:at + 3] == ["9", "/work"]
        assert {"--die-with-parent", "--new-session"} <= set(argv)
        at = argv.index("--tmpfs")
        assert argv[at - 2:at] == ["--size", str(chat_shell.TMP_BYTES)]
        at = argv.index("--")
        assert argv[at + 2:at + 4] == ["-I", "-S"] and argv[-1] == "echo hi"
        sources = {argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--ro-bind-try")}
        assert sources <= {"/usr", "/opt/python", *chat_shell._SYSTEM_FILES, *chat_shell._SYSTEM_LINK_DIRS}


def test_mount_points_inside_the_folder_are_refused():
    info = ("22 1 0:21 / / rw - ext4 /dev/root rw\n"
            "30 22 0:30 / /data/shared rw - ext4 /dev/sdb rw\n"
            "31 22 0:31 / /data/my\\040folder/inner rw - tmpfs tmpfs rw\n")
    assert chat_shell.mounts_below("/data/shared", info) is False  # the folder itself may be a mount
    assert chat_shell.mounts_below("/data", info) is True
    assert chat_shell.mounts_below("/data/my folder", info) is True
    assert chat_shell.mounts_below("/data/my", info) is False
    assert chat_shell.mounts_below("/data/gone (deleted)", info) is True
    assert chat_shell.mounts_below("relative", info) is True


# ── process handling (Linux, a stand-in for bwrap) ───────────────────────────

linux = pytest.mark.skipif(sys.platform != "linux", reason="descriptor passing and process groups are POSIX")


@pytest.fixture
def stand_in(monkeypatch, tmp_path):
    """Run a Python script in place of bwrap, keeping every other step."""
    monkeypatch.setattr(chat_shell, "ensure_available", AsyncMock())
    monkeypatch.setattr(chat_shell, "nested_mounts", lambda fd: False)
    monkeypatch.setattr(chat_shell, "_bwrap", lambda: "/stand-in")
    monkeypatch.setattr(chat_shell, "build_argv",
                        lambda bwrap, fd, writable, command, seccomp_fd: [sys.executable, "-c", command])
    fd = os.open(tmp_path, os.O_RDONLY)
    yield fd
    os.close(fd)


@linux
@pytest.mark.asyncio
async def test_output_is_bounded(stand_in):
    result = await chat_shell.run(stand_in, True, "import sys; sys.stdout.write('x' * 200000); "
                                                  "sys.stderr.write('e'); sys.exit(3)")
    assert result["exit_code"] == 3 and result["stderr"] == "e" and result["truncated"]
    assert len(result["stdout"]) == chat_files.LIMIT


@linux
@pytest.mark.asyncio
async def test_timeout_kills_the_process_group(stand_in, monkeypatch, tmp_path):
    monkeypatch.setattr(chat_shell, "TIMEOUT_SECONDS", 1)
    marker = tmp_path / "pid"
    script = ("import subprocess, time; child = subprocess.Popen(['sleep', '30']); "
              f"open({str(marker)!r}, 'w').write(str(child.pid)); time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(AccessDenied, match="exceeded 1 seconds"):
        await chat_shell.run(stand_in, True, script)
    assert time.monotonic() - started < 10
    pid = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("a process of the timed-out command survived")
    assert chat_shell._running == 0


@linux
@pytest.mark.asyncio
async def test_bwrap_refusal_invalidates_the_cached_pass(stand_in):
    chat_shell._result = (True, "", time.monotonic())
    with pytest.raises(AccessDenied, match="could not start"):
        await chat_shell.run(stand_in, True, "import sys; "
                             "sys.stderr.write('bwrap: No permissions to create new namespace'); sys.exit(1)")
    assert chat_shell._result is None


@linux
@pytest.mark.asyncio
async def test_concurrency_is_bounded(stand_in, monkeypatch):
    monkeypatch.setattr(chat_shell, "_running", chat_shell.MAX_RUNNING)
    with pytest.raises(AccessDenied, match="Too many"):
        await chat_shell.run(stand_in, True, "pass")


# ── hard links and setuid bits (Linux, no bwrap needed) ─────────────────────


@pytest.fixture
def linked(tmp_path):
    """A folder, a file outside it, and an open descriptor for the folder."""
    folder = tmp_path / "grant"
    (folder / "sub").mkdir(parents=True)
    (tmp_path / "secret").write_text("host secret")
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    yield folder, tmp_path / "secret", fd
    os.close(fd)


@linux
@pytest.mark.parametrize("where", ["top.txt", "sub/deep.txt"])
def test_hard_link_to_a_file_outside_the_folder_is_refused(linked, where):
    folder, secret, fd = linked
    os.link(secret, folder / where)
    with pytest.raises(AccessDenied, match=rf"1 file\(s\) hard-linked to files outside it \(first: '{where}'\)"):
        chat_shell.check_folder(fd)


@linux
def test_hard_links_wholly_inside_the_folder_are_allowed(linked):
    folder, _secret, fd = linked
    (folder / "a").write_text("a")
    os.link(folder / "a", folder / "sub" / "b")
    os.link(folder / "a", folder / "c")
    assert chat_shell.check_folder(fd) == frozenset()
    os.link(folder / "a", folder.parent / "d")  # a third link, outside
    with pytest.raises(AccessDenied, match="hard-linked to files outside"):
        chat_shell.check_folder(fd)


@linux
@pytest.mark.parametrize("bound, value", [("SCAN_ENTRIES", 3), ("SCAN_SECONDS", -1.0), ("SCAN_DEPTH", 1)])
def test_hard_link_check_refuses_past_its_bounds(linked, monkeypatch, bound, value):
    folder, _secret, fd = linked
    for name in "abcd":
        (folder / "sub" / name).write_text(name)
    monkeypatch.setattr(chat_shell, bound, value)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(AccessDenied, match="too large|nests deeper"):
        chat_shell.check_folder(fd)
    assert set(os.listdir("/proc/self/fd")) == before


@linux
@pytest.mark.skipif(sys.platform == "linux" and os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_directory_fails_the_check_closed(linked):
    folder, _secret, fd = linked
    (folder / "sub").chmod(0)
    try:
        with pytest.raises(AccessDenied, match="could not check this folder"):
            chat_shell.check_folder(fd)
    finally:
        (folder / "sub").chmod(0o755)


@linux
def test_walk_closes_every_descriptor(linked):
    folder, _secret, fd = linked
    for depth in range(5):
        (folder / "sub" / ("d" * (depth + 1))).mkdir()
    before = set(os.listdir("/proc/self/fd"))
    chat_shell.check_folder(fd)
    assert set(os.listdir("/proc/self/fd")) == before


@linux
def test_only_new_setid_bits_are_cleared(linked):
    folder, _secret, fd = linked
    old, new, plain = folder / "old", folder / "sub" / "new", folder / "plain"
    for path in (old, new, plain):
        path.write_text("#!/bin/sh\n")
    old.chmod(0o4755)
    before = chat_shell.check_folder(fd)
    new.chmod(0o6755)
    plain.chmod(0o700)
    chat_shell.strip_new_setid(fd, before)
    assert (old.stat().st_mode & 0o7777, new.stat().st_mode & 0o7777) == (0o4755, 0o755)
    assert plain.stat().st_mode & 0o7777 == 0o700


@linux
@pytest.mark.asyncio
async def test_hard_link_is_refused_before_any_approval(broker, linked, monkeypatch):
    folder, secret, fd = linked
    os.link(secret, folder / "alias")
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "default"})
    monkeypatch.setattr(chat_shell, "ensure_available", AsyncMock())
    run = AsyncMock()
    monkeypatch.setattr(chat_shell, "run", run)
    grant = Grant("g", "chat", ("local", "worker"), "Files", "folder", str(folder), time.time() + 600,
                  writable=True, root=os.dup(fd))
    broker.grants[grant.id] = grant
    with pytest.raises(AccessDenied, match="hard-linked"):
        await broker.run("chat", grant.id, "command", {"command": "cat alias"})
    assert not broker.actions and not run.called
    broker.revoke("chat", grant.id)


@linux
@pytest.mark.asyncio
async def test_run_rechecks_links_after_approval_and_strips_setid(stand_in, tmp_path):
    marker, target = tmp_path / "ran", tmp_path / "tool"
    target.write_text("x")
    command = (f"import os; open({str(marker)!r}, 'w').close(); "
               f"os.chmod({str(target)!r}, 0o4755)")
    result = await chat_shell.run(stand_in, True, command)
    assert result["exit_code"] == 0 and marker.exists()
    assert target.stat().st_mode & 0o7777 == 0o755
    marker.unlink()
    os.link(target, tmp_path.parent / f"{tmp_path.name}-outside")
    with pytest.raises(AccessDenied, match=r"outside it \(first: 'tool'\)"):
        await chat_shell.run(stand_in, True, command)
    assert not marker.exists() and chat_shell._running == 0


# ── the real sandbox ─────────────────────────────────────────────────────────


def _sandbox_ready():
    if sys.platform != "linux" or not chat_files.available():
        return False, "Linux with openat2 required"
    return chat_shell.self_test()


_ready, _why = _sandbox_ready()
chat_shell.invalidate()
if os.environ.get("CHAT_SHELL_REQUIRE_SANDBOX") == "1" and not _ready:
    raise RuntimeError(f"CHAT_SHELL_REQUIRE_SANDBOX=1 but the isolated shell is unavailable: {_why}")
sandbox = pytest.mark.skipif(not _ready, reason=f"isolated shell unavailable here: {_why}")
PYTHON = os.path.realpath(sys.executable)


@pytest.fixture
def grant_dir(tmp_path):
    folder = tmp_path / "grant"
    folder.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("host secret")
    fd = chat_files.open_root(str(folder))
    yield folder, fd
    os.close(fd)


async def shell(fd, command, writable=True):
    return await chat_shell.run(fd, writable, command)


def surviving(marker: bytes) -> list[str]:
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as stream:
                if marker in stream.read():
                    found.append(pid)
        except OSError:
            pass
    return found


@sandbox
@pytest.mark.asyncio
async def test_real_writes_land_only_in_the_folder(grant_dir, tmp_path):
    folder, fd = grant_dir
    result = await shell(fd, "pwd; echo hello > made.txt; mkdir sub; echo deep > sub/f; cat made.txt")
    assert result["exit_code"] == 0 and result["stdout"] == "/work\nhello\n"
    assert (folder / "made.txt").read_text() == "hello\n" and (folder / "sub" / "f").read_text() == "deep\n"
    for target in ("/usr/escape", "/proc/escape", f"{PYTHON}.escape"):
        result = await shell(fd, f"echo x > {target}")
        assert result["exit_code"] != 0, target
    # Anything else outside /work lands on the sandbox's own throwaway root, if anywhere.
    await shell(fd, f"echo x > /escape; echo x > /etc/escape; echo x > ../escape; echo x > {tmp_path}/escape")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["grant", "outside"]
    assert sorted(p.name for p in folder.iterdir()) == ["made.txt", "sub"]
    assert not os.path.exists("/escape") and not os.path.exists(f"{PYTHON}.escape")


@sandbox
@pytest.mark.asyncio
async def test_real_read_only_grant_cannot_write(grant_dir):
    folder, fd = grant_dir
    (folder / "keep.txt").write_text("keep")
    result = await shell(fd, "cat keep.txt; echo no > keep.txt; touch new.txt; rm keep.txt", writable=False)
    assert result["stdout"] == "keep" and result["exit_code"] != 0
    assert sorted(p.name for p in folder.iterdir()) == ["keep.txt"]
    assert (folder / "keep.txt").read_text() == "keep"


@sandbox
@pytest.mark.asyncio
async def test_real_symlinks_resolve_inside_the_sandbox(grant_dir, tmp_path):
    folder, fd = grant_dir
    outside = tmp_path / "outside"
    (folder / "link").symlink_to(outside, target_is_directory=True)
    (folder / "file").symlink_to(outside / "secret")
    result = await shell(fd, f"cat link/secret; cat file; cat {outside}/secret; ln -s / root; ls root/; "
                             "cat /proc/1/root/etc/passwd; cat /proc/self/root/etc/passwd; cat ../etc/passwd")
    assert "host secret" not in result["stdout"] and "root:" not in result["stdout"]
    # A link planted inside resolves inside too; the host file it names is untouched.
    await shell(fd, f"ln -s {outside}/secret planted; echo changed > planted")
    assert (outside / "secret").read_text() == "host secret"


@sandbox
@pytest.mark.asyncio
async def test_real_hard_link_to_a_host_file_is_refused(grant_dir, tmp_path):
    folder, fd = grant_dir
    secret = tmp_path / "outside" / "secret"
    os.link(secret, folder / "alias")
    for writable in (True, False):
        with pytest.raises(AccessDenied, match="hard-linked to files outside it"):
            await shell(fd, "cat alias; echo changed > alias", writable=writable)
    assert secret.read_text() == "host secret"
    # Links the sandbox makes stay inside /work (another mount gives EXDEV),
    # and links wholly inside the folder do not block the next command.
    (folder / "alias").unlink()
    result = await shell(fd, "echo a > a; ln a b; ln a /tmp/c; ln /usr/bin/env env")
    assert result["exit_code"] != 0 and sorted(p.name for p in folder.iterdir()) == ["a", "b"]
    assert (await shell(fd, "cat b"))["stdout"] == "a\n"


@sandbox
@pytest.mark.asyncio
async def test_real_setuid_bits_do_not_reach_the_host(grant_dir):
    folder, fd = grant_dir
    result = await shell(fd, "cp /bin/sh tool; chmod 6755 tool; stat -c %a tool")
    assert result["stdout"] == "6755\n"
    assert (folder / "tool").stat().st_mode & 0o7777 == 0o755


@sandbox
@pytest.mark.asyncio
async def test_real_root_holds_only_system_files(grant_dir):
    _, fd = grant_dir
    result = await shell(fd, "ls -A /; ls -A /etc")
    names = set(result["stdout"].split())
    assert {"usr", "work", "proc", "dev", "tmp"} <= names
    assert not names & {"home", "data", "app", "root", "var", "srv", "workspace", "passwd", "shadow",
                        "hostname", "resolv.conf", "hosts", "ssh"}
    home = os.path.realpath(os.path.expanduser("~"))
    result = await shell(fd, f"ls -A {home} 2>/dev/null")
    assert not set(result["stdout"].split()) & {".ssh", ".config", ".claude", ".fluid", ".gitconfig"}


@sandbox
@pytest.mark.asyncio
async def test_real_proc_shows_only_the_sandbox(grant_dir):
    _, fd = grant_dir
    result = await shell(fd, "ls /proc | grep -c '^[0-9]'; cat /proc/[0-9]*/cmdline | tr '\\0' ' '")
    count, _, cmdlines = result["stdout"].partition("\n")
    assert int(count) <= 6 and "pytest" not in cmdlines


@sandbox
@pytest.mark.asyncio
async def test_real_no_descriptor_leaks_into_the_command(grant_dir):
    """bwrap's --bind-fd source would let a command walk out through '..'."""
    folder, fd = grant_dir
    result = await shell(fd, "ls /proc/$$/fd; readlink /proc/$$/fd/*")
    lines = result["stdout"].splitlines()
    assert lines[:3] == ["0", "1", "2"] and len(lines) == 6, result["stdout"]
    assert str(folder) not in result["stdout"]


@sandbox
@pytest.mark.asyncio
async def test_real_no_network_even_to_worker_loopback(grant_dir):
    folder, fd = grant_dir
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    (folder / "probe.py").write_text(
        "import socket\n"
        f"for host in (('127.0.0.1', {port}), ('1.1.1.1', 53)):\n"
        "    s = socket.socket()\n"
        "    s.settimeout(3)\n"
        "    try:\n"
        "        s.connect(host)\n"
        "        print('connected', host)\n"
        "    except OSError as e:\n"
        "        print('blocked', type(e).__name__)\n")
    try:
        result = await shell(fd, f"{PYTHON} -I -S probe.py; wc -l < /proc/net/route; grep -cv ' lo$' /proc/net/ipv6_route")
    finally:
        listener.close()
    out = result["stdout"].splitlines()
    assert [line.split()[0] for line in out[:2]] == ["blocked", "blocked"], result
    # No IPv4 route at all, and every IPv6 route is on loopback.
    assert out[2:] == ["1", "0"], result


@sandbox
@pytest.mark.asyncio
async def test_real_no_unix_socket_reaches_a_host_listener(grant_dir):
    """A listening socket inside the folder (a /tmp grant can hold the worker's
    own PostgreSQL socket) is a route out that the network namespace does not
    close. Neither a stream connect nor a datagram sent by path may reach it."""
    folder, fd = grant_dir
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.bind(str(folder / "stream.sock"))
    stream.listen(1)
    stream.setblocking(False)
    datagram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    datagram.bind(str(folder / "dgram.sock"))
    datagram.setblocking(False)
    (folder / "probe.py").write_text(
        "import socket\n"
        "def attempt(make, act):\n"
        "    try:\n"
        "        act(make())\n"
        "        print('reached')\n"
        "    except OSError as e:\n"
        "        print('blocked', type(e).__name__)\n"
        "attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), lambda s: s.connect('stream.sock'))\n"
        "attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM), lambda s: s.sendto(b'x', 'dgram.sock'))\n"
        "attempt(lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)[0], lambda s: s.sendto(b'x', 'dgram.sock'))\n"
        "attempt(lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)[0], lambda s: None)\n"
        "a, b = socket.socketpair()\n"  # stream pairs stay usable (asyncio's self-pipe needs one)
        "a.sendall(b'ok'); print('pair', b.recv(2).decode())\n")
    try:
        result = await shell(fd, f"{PYTHON} -I -S probe.py")
        with pytest.raises(BlockingIOError):
            stream.accept()
        with pytest.raises(BlockingIOError):
            datagram.recv(16)
    finally:
        stream.close()
        datagram.close()
    out = result["stdout"].splitlines()
    assert [line.split()[0] for line in out[:4]] == ["blocked"] * 4, result
    assert out[4:] == ["pair ok"], result


@sandbox
@pytest.mark.asyncio
async def test_real_limits_apply(grant_dir):
    folder, fd = grant_dir
    (folder / "limits.py").write_text(
        "import resource as r\n"
        "print(*[r.getrlimit(getattr(r, 'RLIMIT_' + k))[0] for k in ('CPU', 'AS', 'FSIZE', 'NOFILE', 'NPROC')])\n")
    (folder / "forks.py").write_text(
        "import os, time\n"
        "pids = []\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(5)\n"
        "            os._exit(0)\n"
        "        pids.append(pid)\n"
        "except OSError:\n"
        "    pass\n"
        "print(len(pids))\n"
        "for pid in pids:\n"
        "    os.kill(pid, 9)\n")
    result = await shell(fd, f"{PYTHON} -I -S limits.py; head -c 70000000 /dev/zero > big; "
                             f"{PYTHON} -I -S forks.py")
    lines = result["stdout"].splitlines()
    assert lines[0].split() == [str(v) for v in (chat_shell.CPU_SECONDS, chat_shell.MEMORY_BYTES,
                                                 chat_shell.FILE_BYTES, chat_shell.OPEN_FILES,
                                                 chat_shell.PROCESSES)], result
    assert (folder / "big").stat().st_size <= chat_shell.FILE_BYTES
    # The process cap holds: a fork loop stops well short of a fork bomb.
    assert int(lines[-1]) < chat_shell.PROCESSES, result


@sandbox
@pytest.mark.asyncio
async def test_real_cannot_nest_namespaces_or_keep_background_work(grant_dir):
    _, fd = grant_dir
    result = await shell(fd, "unshare -U true && echo nested; (sleep 47.25 &); echo done")
    assert "nested" not in result["stdout"] and "done" in result["stdout"]
    await asyncio.sleep(0.5)
    assert not surviving(b"47.25")


@sandbox
@pytest.mark.asyncio
async def test_real_timeout_stops_the_sandbox(grant_dir, monkeypatch):
    _, fd = grant_dir
    monkeypatch.setattr(chat_shell, "TIMEOUT_SECONDS", 2)
    with pytest.raises(AccessDenied, match="exceeded 2 seconds"):
        await shell(fd, "sleep 58.5")
    await asyncio.sleep(0.5)
    assert not surviving(b"58.5")


@sandbox
@pytest.mark.asyncio
async def test_real_broker_path_with_approval(broker, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "default"})
    folder = tmp_path / "grant"
    folder.mkdir()
    grant = await broker.create("chat", chat_access.GrantInput(kind="folder", label="Files", path=str(folder),
                                                               writable=True))
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "echo hi > hi.txt; ls"}))
    action = await pending(broker)
    assert not (folder / "hi.txt").exists()
    await broker.decide("chat", action.id, True)
    result = await asyncio.wait_for(task, 30)
    assert result["stdout"] == "hi.txt\n" and (folder / "hi.txt").read_text() == "hi\n"
    broker.revoke("chat", grant["id"])
