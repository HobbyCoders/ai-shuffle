"""The code-mode kernel child's OS-level sandbox (bubblewrap).

Two halves. The unit half needs no bwrap: it checks the argv the sandbox is
built from and the ``auto``/``required``/``off`` setting matrix with an
injected self-test. The integration half runs the REAL kernel under bwrap and
is skipped on a host where ``bwrap`` is missing or its self-test fails (no
unprivileged user namespaces, an AppArmor restriction, a container profile).

The integration probes that check the *OS* layer run a copy of the runner with
the in-process guards switched off (written to ``tmp_path``; tracked files are
never touched), so what refuses the socket / the file / the process table is
the sandbox and not the audit hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import shutil
import sys
from pathlib import Path

import pytest

from fluid.codemode import os_sandbox
from fluid.codemode import subprocess_kernel as sk
from fluid.codemode.kernel import HostReply
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel


@pytest.fixture(autouse=True)
def _fresh_sandbox_cache():
    os_sandbox.reset_cache()
    yield
    os_sandbox.reset_cache()


def _ok(_python):
    return os_sandbox.SelfTest(True)


def _fail(_python):
    return os_sandbox.SelfTest(False, "bwrap: No permissions to create new namespace")


def _flag_value(argv: list[str], flag: str) -> list[list[str]]:
    """Every ``flag a b`` triple's operands, in order."""
    out = []
    for i, item in enumerate(argv):
        if item == flag:
            out.append(argv[i + 1:i + 3])
    return out


# ─── mode parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("auto", "auto"), ("REQUIRED", "required"), (" off ", "off"),
     ("", "auto"), (None, "auto"), ("bogus", "auto")],
)
def test_normalize_mode(raw, expected) -> None:
    assert os_sandbox.normalize_mode(raw) == expected


def test_default_mode_reads_env_and_kernel_config_follows(monkeypatch) -> None:
    monkeypatch.setenv("CODE_MODE_OS_SANDBOX", "required")
    assert os_sandbox.default_mode() == "required"
    assert KernelConfig().os_sandbox == "required"
    monkeypatch.delenv("CODE_MODE_OS_SANDBOX")
    assert os_sandbox.default_mode() == "auto"
    assert KernelConfig().os_sandbox == "auto"


# ─── argv construction ───────────────────────────────────────────────────────


_LINKS = {"/bin": "usr/bin", "/lib": "usr/lib", "/lib64": "usr/lib64", "/sbin": "usr/sbin"}


def test_bwrap_argv_isolates_by_default() -> None:
    argv = os_sandbox.build_bwrap_argv(
        "/usr/bin/bwrap", ro_paths=["/opt/py", "/srv/fluid/codemode"], link_dirs=_LINKS,
    )
    assert argv[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-all", "--unshare-user", "--disable-userns",
                 "--die-with-parent", "--new-session"):
        assert flag in argv
    assert "--share-net" not in argv
    # Fresh /proc (own pid namespace), minimal /dev, private /tmp.
    assert _flag_value(argv, "--proc") == [["/proc", "--dev"]]
    assert argv[argv.index("--dev") + 1] == "/dev"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    binds = _flag_value(argv, "--ro-bind")
    assert ["/usr", "/usr"] in binds
    assert ["/opt/py", "/opt/py"] in binds
    assert ["/srv/fluid/codemode", "/srv/fluid/codemode"] in binds
    # Nothing writable, nothing of the host beyond the listed paths.
    assert "--bind" not in argv and "--dev-bind" not in argv
    assert not any(b[0] in ("/", "/home", "/etc", "/root") for b in binds)
    assert ["usr/lib", "/lib"] in _flag_value(argv, "--symlink")
    tries = [b[0] for b in _flag_value(argv, "--ro-bind-try")]
    assert "/etc/ld.so.cache" in tries and "/etc/resolv.conf" not in tries
    assert argv[argv.index("--chdir") + 1] == "/tmp"


def test_bwrap_argv_binds_after_private_tmp_and_skips_covered_paths() -> None:
    argv = os_sandbox.build_bwrap_argv(
        "bwrap",
        ro_paths=["/tmp/pytest-1/variant", "/usr/lib/python3.12", "/tmp/pytest-1/variant"],
        link_dirs={},
    )
    binds = _flag_value(argv, "--ro-bind")
    # A runner under /tmp must be bound AFTER the tmpfs or the tmpfs hides it.
    assert argv.index("--tmpfs") < argv.index("/tmp/pytest-1/variant")
    assert binds.count(["/tmp/pytest-1/variant", "/tmp/pytest-1/variant"]) == 1
    # Already under the /usr bind: not bound twice.
    assert ["/usr/lib/python3.12", "/usr/lib/python3.12"] not in binds


def test_bwrap_argv_network_and_userns_switches() -> None:
    argv = os_sandbox.build_bwrap_argv(
        "bwrap", ro_paths=[], allow_network=True, disable_userns=False, link_dirs={},
    )
    assert "--share-net" in argv
    assert "--disable-userns" not in argv and "--unshare-user" not in argv
    tries = [b[0] for b in _flag_value(argv, "--ro-bind-try")]
    assert "/etc/resolv.conf" in tries


def test_sandbox_env_is_minimal_and_has_no_home() -> None:
    env = os_sandbox.sandbox_env()
    assert set(env) == {"PATH", "LANG", "TMPDIR"}


def test_python_roots_skip_usr_and_flag_too_broad(monkeypatch) -> None:
    roots = os_sandbox.python_roots(sys.executable)
    assert all(not r.startswith("/usr/") and r != "/usr" for r in roots)
    assert os_sandbox._too_broad("/")
    home = os.path.realpath(os.path.expanduser("~"))
    assert os_sandbox._too_broad(home)
    assert os_sandbox._too_broad(os.path.dirname(home))
    assert not os_sandbox._too_broad(os.path.join(home, ".local", "python"))


# ─── the setting matrix ──────────────────────────────────────────────────────


def _plan(mode, tester, **kw):
    return os_sandbox.plan_spawn(
        mode, python="/usr/bin/python3", runner="/srv/codemode/_runner.py",
        base_env={"PATH": "/x", "FLUID_PLAIN": "1"}, tester=tester, **kw,
    )


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")


def test_off_never_probes_and_spawns_plain(linux) -> None:
    def boom(_):
        raise AssertionError("self-test must not run when off")
    plan = _plan("off", boom)
    assert plan.argv == ["/usr/bin/python3", "-I", "-S", "/srv/codemode/_runner.py"]
    assert plan.env == {"PATH": "/x", "FLUID_PLAIN": "1"} and not plan.sandboxed


def test_auto_with_passing_selftest_sandboxes(linux) -> None:
    plan = _plan("auto", _ok)
    assert plan.sandboxed
    assert "--unshare-all" in plan.argv
    assert plan.argv[-4:] == [os.path.realpath("/usr/bin/python3"), "-I", "-S",
                              "/srv/codemode/_runner.py"]
    assert plan.env == os_sandbox.sandbox_env()
    assert ["/srv/codemode", "/srv/codemode"] in _flag_value(plan.argv, "--ro-bind")


def test_auto_with_failing_selftest_falls_back_and_logs_once(linux, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="fluid.codemode.os_sandbox")
    first = _plan("auto", _fail)
    second = _plan("auto", _fail)
    assert not first.sandboxed and not second.sandboxed
    assert first.argv[0] == "/usr/bin/python3"
    assert "No permissions" in first.reason
    lines = [r for r in caplog.records if "OS sandbox unavailable" in r.getMessage()]
    assert len(lines) == 1 and "No permissions" in lines[0].getMessage()


def test_required_with_failing_selftest_refuses(linux) -> None:
    with pytest.raises(os_sandbox.OsSandboxUnavailable) as err:
        _plan("required", _fail)
    assert "required" in str(err.value) and "No permissions" in str(err.value)


def test_required_with_passing_selftest_sandboxes(linux) -> None:
    assert _plan("required", _ok).sandboxed


def test_allow_fs_skips_the_sandbox_in_auto_but_not_in_required(linux) -> None:
    def boom(_):
        raise AssertionError("auto + allow_fs must not probe")
    plan = _plan("auto", boom, allow_fs=True)
    assert not plan.sandboxed and "allow_fs" in plan.reason
    assert _plan("required", _ok, allow_fs=True).sandboxed


def test_allow_network_shares_net(linux) -> None:
    assert "--share-net" in _plan("auto", _ok, allow_network=True).argv
    assert "--share-net" not in _plan("auto", _ok).argv


def test_non_linux_is_plain_in_auto_and_refused_in_required(monkeypatch) -> None:
    monkeypatch.setattr(os_sandbox.sys, "platform", "darwin")
    assert not _plan("auto", _ok).sandboxed
    with pytest.raises(os_sandbox.OsSandboxUnavailable):
        _plan("required", _ok)


def test_selftest_reports_missing_bwrap_and_caches(monkeypatch) -> None:
    monkeypatch.setattr(os_sandbox, "find_bwrap", lambda: None)
    result = os_sandbox.self_test(sys.executable)
    assert not result.ok and "not installed" in result.reason

    calls = []

    def fake_probe(bwrap, python, disable_userns):
        calls.append(disable_userns)
        return os_sandbox.SelfTest(True, "", disable_userns)

    monkeypatch.setattr(os_sandbox, "_probe_once", fake_probe)
    monkeypatch.setattr(os_sandbox, "_supports_disable_userns", lambda b: True)
    assert os_sandbox.self_test(sys.executable, "/fake/bwrap").ok
    assert os_sandbox.self_test(sys.executable, "/fake/bwrap").ok
    assert calls == [True]  # probed once, then cached


def test_selftest_retries_without_disable_userns(monkeypatch) -> None:
    def fake_probe(bwrap, python, disable_userns):
        return os_sandbox.SelfTest(not disable_userns, "userns refused", disable_userns)

    monkeypatch.setattr(os_sandbox, "_probe_once", fake_probe)
    monkeypatch.setattr(os_sandbox, "_supports_disable_userns", lambda b: True)
    result = os_sandbox.self_test(sys.executable, "/fake/bwrap")
    assert result.ok and result.disable_userns is False


# ─── kernel wiring (no bwrap needed) ─────────────────────────────────────────


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools: {name}")


async def test_required_mode_without_sandbox_gives_every_cell_a_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox, "self_test", _fail)
    k = SubprocessKernel(KernelConfig(os_sandbox="required"))
    await k.start()  # does not raise: the refusal belongs to the cell
    try:
        assert k._proc is None
        await k.set_vfs({"T": "def T():\n    pass\n"})
        for _ in range(2):
            r = await k.exec("1 + 1", timeout=5, on_host_call=_no_tools)
            assert r.error is not None
            assert "code_mode_os_sandbox=required" in r.error
            assert "No permissions" in r.error
    finally:
        await k.stop()


async def test_off_mode_runs_the_plain_child() -> None:
    k = SubprocessKernel(KernelConfig(os_sandbox="off"))
    await k.start()
    try:
        assert not k.sandboxed
        r = await k.exec("40 + 2", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "42"
    finally:
        await k.stop()


def test_death_note_maps_bwrap_signal_exit_codes() -> None:
    class _Proc:
        returncode = 128 + 9

    k = SubprocessKernel(KernelConfig(os_sandbox="off"))
    k.sandboxed = True
    assert "SIGKILL" in k._death_note(_Proc())
    k.sandboxed = False
    assert "status 137" in k._death_note(_Proc())


# ─── integration: the real kernel under bwrap ────────────────────────────────


def _sandbox_usable() -> tuple[bool, str]:
    if sys.platform != "linux":
        return False, "bubblewrap sandbox is Linux-only"
    if shutil.which("bwrap") is None:
        return False, "bwrap is not on PATH"
    result = os_sandbox.self_test(sys.executable)
    os_sandbox.reset_cache()
    return result.ok, result.reason or "ok"


_USABLE, _WHY = _sandbox_usable()
needs_bwrap = pytest.mark.skipif(not _USABLE, reason=f"OS sandbox unavailable: {_WHY}")


def _echo_stub(name: str) -> dict:
    return {
        name: (
            f"def {name}(**kwargs) -> str:\n"
            f'    """Echo/record tool."""\n'
            f"    return __host_call__({name!r}, kwargs)\n"
        )
    }


def _unguarded_runner(tmp_path: Path) -> str:
    """A runner copy with every in-process guard off, so only the OS layer is left."""
    real_dir = Path(sk._RUNNER).parent
    dest = tmp_path / "unguarded"
    dest.mkdir()
    for name in ("_audit_guard.py", "truncate.py", "vfs.py"):
        shutil.copy(real_dir / name, dest / name)
    src = (real_dir / "_runner.py").read_text()
    for old, new in (
        ("    _audit_guard.install(\n", "    (lambda **_k: None)(\n"),
        ("        _guard_network()\n", "        pass\n"),
        ("        _guard_filesystem()  # must precede", "        pass  # must precede"),
    ):
        assert old in src, f"anchor not found: {old!r}"
        src = src.replace(old, new, 1)
    runner = dest / "_runner.py"
    runner.write_text(src)
    return str(runner)


def _descendants(pid: int) -> set[int]:
    """Every live process whose parent chain reaches ``pid`` (host view)."""
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        parents[int(entry)] = int(stat.rsplit(")", 1)[1].split()[1])
    out: set[int] = set()
    frontier = {pid}
    while frontier:
        nxt = {c for c, p in parents.items() if p in frontier and c not in out}
        out |= nxt
        frontier = nxt
    return out


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def _wait_gone(pids: set[int], seconds: float = 5.0) -> set[int]:
    for _ in range(int(seconds * 20)):
        left = {p for p in pids if _alive(p)}
        if not left:
            return left
        await asyncio.sleep(0.05)
    return {p for p in pids if _alive(p)}


_REAL_IMPORT = "json.__builtins__['__import__']"


@needs_bwrap
async def test_os_layer_blocks_network_files_and_processes(tmp_path, tmp_path_factory, monkeypatch) -> None:
    secret_dir = tmp_path_factory.mktemp("outside")
    secret = secret_dir / "secret.txt"
    secret.write_text("host-secret")
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setattr(sk, "_RUNNER", _unguarded_runner(tmp_path))
    k = SubprocessKernel(KernelConfig(os_sandbox="required"))
    await k.start()
    try:
        assert k.sandboxed
        # Sanity: the in-process guards really are off in this runner.
        r = await k.exec(f"{_REAL_IMPORT}('os').getpid()", timeout=5, on_host_call=_no_tools)
        assert r.error is None, r.error

        r = await k.exec(
            f"socket = {_REAL_IMPORT}('socket')\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(2)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 53))\n"
            "    out = 'connected'\n"
            "except OSError as exc:\n"
            "    out = type(exc).__name__ + ':' + str(exc.errno)\n"
            "out",
            timeout=10, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert "connected" not in r.value_repr and "OSError" in r.value_repr

        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            "def probe(p):\n"
            "    try:\n"
            "        with open(p) as fh:\n"
            "            return fh.read()\n"
            "    except OSError as exc:\n"
            "        return 'blocked:' + type(exc).__name__\n"
            f"(probe({str(secret)!r}), probe('/etc/passwd'), "
            f"os.path.exists({os.path.join(real_home, '.ssh')!r}), "
            f"os.path.exists({real_home!r} + '/.bashrc'))",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert "host-secret" not in r.value_repr
        assert r.value_repr.count("blocked:") == 2
        assert r.value_repr.endswith("False, False)")

        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            "pids = sorted(int(p) for p in os.listdir('/proc') if p.isdigit())\n"
            "try:\n"
            f"    os.kill({os.getpid()}, 0)\n"
            "    seen = 'host pid visible'\n"
            "except ProcessLookupError:\n"
            "    seen = 'host pid invisible'\n"
            "(pids, seen)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "([1, 2], 'host pid invisible')"

        # Writes land nowhere on the host: the runner dir is read-only, /tmp
        # is private.
        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            "def attempt(p):\n"
            "    try:\n"
            "        with open(p, 'w') as fh:\n"
            "            fh.write('x')\n"
            "        return 'wrote'\n"
            "    except OSError as exc:\n"
            "        return type(exc).__name__\n"
            f"(attempt({str(tmp_path / 'unguarded' / 'planted.txt')!r}), "
            f"attempt({str(secret_dir / 'planted.txt')!r}))",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert not (tmp_path / "unguarded" / "planted.txt").exists()
        assert not (secret_dir / "planted.txt").exists()
    finally:
        await k.stop()


@needs_bwrap
async def test_sandboxed_kernel_keeps_host_calls_refs_checkpoint_and_guard() -> None:
    big = "R" * 12000
    calls: list[tuple[str, dict]] = []

    async def host(name: str, kwargs: dict) -> HostReply:
        calls.append((name, kwargs))
        return HostReply(result=big)

    k = SubprocessKernel(KernelConfig(os_sandbox="required"))
    await k.start()
    try:
        assert k.sandboxed
        await k.set_vfs(_echo_stub("Fetch"))
        r = await k.exec(
            "from tools import Fetch\nkept = {'n': 3}\ndoc = Fetch(url='x')\n"
            "(len(doc), type(doc).__name__)",
            timeout=8, on_host_call=host,
        )
        assert r.error is None, r.error
        assert r.value_repr == "(12000, 'ResultRef')"
        assert calls == [("Fetch", {"url": "x"})]
        # The in-process layer still runs inside the sandbox.
        r = await k.exec(f"{_REAL_IMPORT}('os').system('true')", timeout=5, on_host_call=host)
        assert r.error is not None and "os.system is blocked" in r.error
        # Kill only the outer bwrap, as an eviction would: --die-with-parent
        # must take the sandboxed child with it, and the checkpoint restores.
        proc = k._proc
        inner = _descendants(proc.pid)
        assert inner, "bwrap has no sandboxed descendants"
        proc.kill()
        await proc.wait()
        assert await _wait_gone(inner) == set()
        r = await k.exec("(kept['n'], len(doc), doc[:2])", timeout=8, on_host_call=host)
        assert r.error is None, r.error
        assert r.value_repr == "(3, 12000, 'RR')"
        assert "kernel restarted" in r.notes
        assert len(calls) == 1
        assert k.sandboxed
    finally:
        await k.stop()


@needs_bwrap
async def test_sandboxed_timeout_kill_reaps_the_whole_sandbox() -> None:
    k = SubprocessKernel(KernelConfig(os_sandbox="required"))
    await k.start()
    try:
        await k.exec("keeper = 7", timeout=5, on_host_call=_no_tools)
        old_pid = k._proc.pid
        old = _descendants(old_pid) | {old_pid}
        r = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "exceeded 1.0s" in r.error
        assert await _wait_gone(old) == set()
        assert k._proc is not None and k._proc.pid != old_pid and k.sandboxed
        r = await k.exec("keeper", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "7"
    finally:
        await k.stop()
    assert await _wait_gone({old_pid}) == set()


@needs_bwrap
async def test_sandboxed_memory_cap_still_applies() -> None:
    k = SubprocessKernel(KernelConfig(os_sandbox="required", mem_limit_bytes=256 * 1024 * 1024))
    await k.start()
    try:
        assert k.sandboxed
        r = await k.exec(
            "blob = bytearray(2 * 1024 * 1024 * 1024)\nlen(blob)",
            timeout=15, on_host_call=_no_tools,
        )
        assert r.error is not None and "MemoryError" in r.error
    finally:
        await k.stop()


@needs_bwrap
async def test_sandboxed_child_env_is_minimal(monkeypatch) -> None:
    monkeypatch.setenv("FLUID_PLAIN_SETTING", "visible-outside")
    k = SubprocessKernel(KernelConfig(os_sandbox="required"))
    await k.start()
    try:
        r = await k.exec(
            f"sorted({_REAL_IMPORT}('os').environ)", timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        # bwrap itself adds PWD (the --chdir target); nothing of the host's.
        assert r.value_repr == "['LANG', 'PATH', 'PWD', 'TMPDIR']"
    finally:
        await k.stop()
