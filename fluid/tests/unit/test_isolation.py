"""The opt-in isolated execution profile (fluid.tools.isolation).

Three groups:

* default off: nothing about spawning changes unless ``[sandbox]`` asks;
* fail closed: a profile that cannot be enforced refuses, it never runs the
  command unconfined;
* enforcement: a confined Bash or Monitor command, or the real launcher,
  tries to break the policy (write outside the workspace, read a planted
  secret or another launch's handoff, read another process's environ, open a
  socket, use io_uring) and the kernel stops it. These need Landlock; the
  standalone seccomp tests need only seccomp. Both skip with the probe's
  reason where the kernel lacks it, except on GitHub Actions Linux, where
  they fail instead (see require_enforcement).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys

import pytest
from fluid.config.loader import _project_sandbox_layer
from fluid.config.model import HarnessConfig, SandboxConfig
from fluid.providers.base import ProviderCaps
from fluid.store.base import MemoryStore
from fluid.tools import isolation
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.builtins.monitor import handler as monitor_handler
from fluid.tools.registry import ToolCtx
from fluid.tools.shell import resolve_shell

LINUX = sys.platform.startswith("linux")
ABI, NO_LANDLOCK = isolation.landlock_abi()
# The strictest probe: network off also needs the seccomp filter.
NOT_ENFORCEABLE = isolation.backend().unavailable_reason(network=False)


def require_enforcement(reason: str | None, *, environ=os.environ, platform: str = sys.platform) -> None:
    """Skip an enforcement test where the profile cannot be enforced, with the
    probe's reason. On GitHub Actions Linux (an ubuntu VM with Landlock) fail
    instead: a skip there would only show as a count under ``-q``, and the
    sandbox would silently go untested."""
    if reason is None:
        return
    message = f"isolated profile not enforceable here: {reason}"
    if environ.get("GITHUB_ACTIONS") == "true" and platform.startswith("linux"):
        pytest.fail(f"{message}. GitHub Actions Linux runners must run the enforcement tests.",
                    pytrace=False)
    pytest.skip(message)


def _announce(request, what: str) -> None:
    # Under `pytest -q` passes are not listed, so on GitHub Actions each
    # enforcement test that got past its gate names itself as a run
    # annotation, written past pytest's output capture so it lands in the log.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        import platform

        capture = request.config.pluginmanager.getplugin("capturemanager")
        with capture.global_and_fixture_disabled():
            print(f"\n::notice title=isolation enforcement::{request.node.name} ran "
                  f"under {what} (kernel {platform.release()})", flush=True)


NO_SECCOMP = (None if LINUX and isolation.machine() and isolation.seccomp_available()
              else "seccomp filters (x86_64/aarch64 Linux) unavailable here")


@pytest.fixture
def _enforceable(request):
    require_enforcement(NOT_ENFORCEABLE)
    _announce(request, f"Landlock ABI {ABI}")


@pytest.fixture
def _seccomp_enforceable(request):
    require_enforcement(NO_SECCOMP)
    _announce(request, "seccomp")


needs_landlock = pytest.mark.usefixtures("_enforceable")
needs_seccomp = pytest.mark.usefixtures("_seccomp_enforceable")


@pytest.fixture(autouse=True)
def _git_probe_registry():
    # Every policy made here registers its roots with the prompt's git
    # probes; keep that from leaking into other tests in the process.
    from fluid.context import environment

    registry = getattr(environment, "_UNPROBED", set())
    saved = set(registry)
    yield
    registry.clear()
    registry.update(saved)


class _NoBackend:
    name = "none"

    def unavailable_reason(self, network: bool) -> str:
        return "test: no mechanism"

    def prefix(self, *args):  # pragma: no cover - must never be reached
        raise AssertionError("an unavailable backend must never build a command")


def _policy(tmp_path, **kwargs) -> isolation.IsolationPolicy:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kwargs.setdefault("home", str(tmp_path / "home"))
    return isolation.IsolationPolicy(workspace=str(workspace), **kwargs)


def _ctx(policy: isolation.IsolationPolicy | None, **extra) -> ToolCtx:
    if policy is not None:
        extra[isolation.EXTRA_KEY] = policy
    cwd = policy.workspace if policy is not None else os.getcwd()
    return ToolCtx(cwd=cwd, extra=extra)


async def _bash(policy, command: str, **input):
    return await bash_handler({"command": command, **input}, _ctx(policy))


async def _run_launch(launch: isolation.Launch, timeout: float = 60):
    """Start a :class:`isolation.Launch` the way Bash does; ``(returncode,
    output, acknowledged)``."""
    proc = await asyncio.create_subprocess_exec(
        *launch.argv, cwd=launch.cwd, env=launch.env, stdin=launch.stdin,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    launch.started()
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, out.decode(errors="replace"), launch.finish()


# --- default off ------------------------------------------------------------------


def test_profile_is_off_by_default():
    config = HarnessConfig.from_dict({})
    assert config.sandbox == SandboxConfig()
    assert config.sandbox.mode == "off"
    assert isolation.policy_from_config(config.sandbox, "/") is None
    assert isolation.policy_of(ToolCtx()) is None


def test_fluid_config_output_is_unchanged_when_the_profile_is_off():
    # Round 4 P1: `fluid config` prints to_dict(). With no [sandbox] section
    # (or mode = "off") the profile adds nothing to it, so the output is what
    # it was before the profile existed; turned on, it adds exactly one key.
    import json

    off = HarnessConfig.from_dict({}).to_dict()
    assert "sandbox" not in off and "sandbox" not in json.dumps(off)
    assert HarnessConfig.from_dict({"sandbox": {"mode": "off"}}).to_dict() == off
    assert HarnessConfig.from_dict(json.loads(json.dumps(off))).sandbox == SandboxConfig()
    on = HarnessConfig.from_dict({"sandbox": {"mode": "isolated", "network": True}}).to_dict()
    assert on.pop("sandbox")["network"] is True and on == off


async def test_bash_spawn_is_unchanged_when_off(tmp_path, monkeypatch):
    """With no policy Bash hands the plain shell argv and the usual
    environment to the spawn, exactly as before the profile existed."""
    from fluid.tools import agent_runner

    seen = {}
    real_wrap = agent_runner.wrap

    def record(argv, cwd, env):
        seen.update(argv=list(argv), env=dict(env))
        return real_wrap(argv, cwd, env)

    monkeypatch.setattr(agent_runner, "wrap", record)
    monkeypatch.setenv("FLUID_TEST_PASSTHROUGH", "kept")
    out = await bash_handler({"command": "echo plain"}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error and "plain" in out.text
    assert seen["argv"] == [*resolve_shell().argv_prefix, "echo plain"]
    assert seen["env"].get("FLUID_TEST_PASSTHROUGH") == "kept"


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_code_mode_kernel_lifecycle_is_mains_when_off(monkeypatch, failure):
    """A first kernel boot that fails or is cancelled leaves the kernel
    assigned and unstopped, exactly as on main: the profile does not touch
    code mode."""
    from fluid.codemode import session as session_mod

    made = []

    class _Kernel:
        def __init__(self, config):
            self.stopped = False
            made.append(self)

        async def start(self):
            if failure == "error":
                raise RuntimeError("boot failed")
            await asyncio.Event().wait()

        async def stop(self):  # pragma: no cover - must not be called
            self.stopped = True

    monkeypatch.setattr(session_mod, "SubprocessKernel", _Kernel)
    sess = object.__new__(session_mod.CodeModeSession)
    sess._kernel, sess._config, sess._skills = None, None, None
    sess._projection = lambda: type("P", (), {"sources": staticmethod(dict)})()

    async def no_skills():
        return {}

    sess._mountable_skills = no_skills
    task = asyncio.create_task(sess._ensure_kernel())
    if failure == "cancel":
        await asyncio.sleep(0.05)
        task.cancel()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    assert len(made) == 1 and sess._kernel is made[0] and not made[0].stopped


def test_hooks_and_the_kernel_carry_no_isolation_wiring():
    # The profile confines Bash and Monitor only; these spawners are main's.
    import inspect

    from fluid.codemode import subprocess_kernel
    from fluid.hooks import command

    assert "isolation" not in inspect.signature(command.CommandHook).parameters
    assert not hasattr(subprocess_kernel.KernelConfig(), "isolation")


# --- config ------------------------------------------------------------------------


def test_config_validation():
    sandbox = SandboxConfig.from_dict({"mode": "isolated", "network": True, "writable_roots": ["/srv/x"]})
    assert (sandbox.mode, sandbox.network, sandbox.writable_roots) == ("isolated", True, ("/srv/x",))
    assert SandboxConfig.from_dict(sandbox.to_dict()) == sandbox
    for bad in ({"mode": "strict"}, {"network": "off"}, {"writable_roots": "/srv"}, {"protect": [""]}):
        with pytest.raises(ValueError):
            SandboxConfig.from_dict(bad)


def test_root_lists_must_be_lists_of_strings():
    # P7: a TOML inline table iterates as its keys, so it must not pass as a list.
    for bad in ({"writable_roots": {"/shared": False}}, {"readable_roots": {}}, {"writable_roots": 5},
                {"env_allow": {"X": 1}}, {"protect": True}, {"readable_roots": ["/srv", 3]}):
        with pytest.raises(ValueError, match="list of non-empty strings"):
            SandboxConfig.from_dict(bad)
    assert SandboxConfig.from_dict({"writable_roots": []}).writable_roots == ()


def test_device_paths_cannot_be_granted(tmp_path):
    # P5: the device list is a guarantee, so no configured or per-spawn root reaches /dev.
    for key in ("writable_roots", "readable_roots"):
        for root in ("/dev", "/dev/shm", "/dev/null", "/dev/../dev/shm"):
            with pytest.raises(ValueError, match="/dev"):
                SandboxConfig.from_dict({key: [root]})
    policy = _policy(tmp_path, writable_roots=("/dev/shm",), backend_impl=_StubBackend())
    assert "device" in (policy.unavailable_reason() or "")
    with pytest.raises(isolation.IsolationUnavailable, match="device"):
        policy.launch(["true"], None, {})


def test_leading_slashes_are_folded_at_config_load():
    # P10: normpath keeps two leading slashes, which Linux reads as one.
    assert SandboxConfig.from_dict({"writable_roots": ["//srv/x"]}).writable_roots == ("/srv/x",)
    assert SandboxConfig.from_dict({"readable_roots": ["///opt"]}).readable_roots == ("/opt",)
    for key in ("writable_roots", "readable_roots"):
        for root in ("//dev/shm", "///dev", "//dev/../dev/null"):
            with pytest.raises(ValueError, match="/dev"):
                SandboxConfig.from_dict({key: [root]})


def test_writable_roots_must_be_directories(tmp_path):
    # P2: a file root could be the launcher's own interpreter.
    afile = tmp_path / "tool"
    afile.write_text("x")
    with pytest.raises(ValueError, match="must be directories"):
        SandboxConfig.from_dict({"writable_roots": [str(afile)]})
    assert SandboxConfig.from_dict({"readable_roots": [str(afile)]}).readable_roots == (str(afile),)
    for root in (str(afile), isolation.LAUNCH_PYTHON, isolation.launcher_script()):
        policy = _policy(tmp_path, writable_roots=(root,), backend_impl=_StubBackend())
        assert "must be a directory" in (policy.unavailable_reason() or ""), root
        with pytest.raises(isolation.IsolationUnavailable, match="must be a directory"):
            policy.launch(["true"], None, {})


def test_project_layer_can_only_turn_the_profile_on():
    loosen = {"sandbox": {"mode": "off", "network": True, "writable_roots": ["/"]}, "model": "m"}
    assert _project_sandbox_layer(loosen) == {"sandbox": {}, "model": "m"}
    tighten = {"sandbox": {"mode": "isolated", "readable_roots": ["/home"]}}
    assert _project_sandbox_layer(tighten) == {"sandbox": {"mode": "isolated"}}
    assert _project_sandbox_layer({"sandbox": "off"}) == {"sandbox": {}}
    assert _project_sandbox_layer({"model": "m"}) == {"model": "m"}


# --- policy ------------------------------------------------------------------------


def test_child_env_is_an_allowlist(tmp_path):
    policy = _policy(tmp_path, env_allow=("MY_TOOL_*",), backend_impl=_NoBackend())
    env = policy.child_env(
        {"PATH": "/usr/bin", "LC_ALL": "C", "AWS_SECRET_ACCESS_KEY": "s", "DATABASE_URL": "pg://x",
         "MY_TOOL_MODE": "fast", "HOME": "/home/real", "SSH_AUTH_SOCK": "/run/agent"},
        overlay={"FROM_SETTINGS": "1"},
    )
    assert env["PATH"] == "/usr/bin" and env["LC_ALL"] == "C" and env["MY_TOOL_MODE"] == "fast"
    assert env["FROM_SETTINGS"] == "1"
    for gone in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "SSH_AUTH_SOCK"):
        assert gone not in env
    assert env["HOME"].startswith(policy.private_dir()) and env["TMPDIR"].startswith(policy.private_dir())


def test_the_command_env_travels_as_data_and_the_launcher_starts_clean(tmp_path):
    # P1/P3: the launcher process gets only the environment Fluid builds; the
    # command's own (settings env included) goes over its standard input and
    # nothing is written to disk.
    side = os.path.dirname(isolation.launcher_script())
    before = sorted(os.listdir(side))
    policy = _policy(tmp_path, backend_impl=_StubBackend())
    launch = policy.launch(["true"], None, {"PATH": "/bin", "PYTHONPATH": "/ws"},
                           overlay={"LD_PRELOAD": "/ws/evil.so", "PYTHONEXECUTABLE": "/ws/python",
                                    "__PYVENV_LAUNCHER__": "/ws/python", "GCONV_PATH": "/ws"})
    try:
        assert launch.env == isolation.LAUNCHER_ENV == {}
        assert sorted(os.listdir(side)) == before  # no handoff file anywhere near the launcher
        assert not any(isinstance(a, str) and "PYTHONEXECUTABLE" in a for a in launch.argv)
        read, write = os.pipe()
        os.write(write, launch._payload)
        os.close(write)
        try:
            command_env = isolation.read_env_payload(read)
        finally:
            os.close(read)
        assert command_env["LD_PRELOAD"] == "/ws/evil.so" and command_env["PYTHONEXECUTABLE"] == "/ws/python"
        assert command_env["PATH"] == "/bin" and "PYTHONPATH" not in command_env
        assert command_env["HOME"].startswith(policy.private_dir())
    finally:
        assert launch.finish() is False  # nothing acknowledged
    # Round trip, including names and values that are not ASCII.
    env = {"A": "1", "B": "x=y", "C": "", "Ü": "\u00e9\udcff"}
    assert isolation.decode_env(isolation.encode_env(env)) == env


def test_the_launcher_reads_exactly_one_environment_block():
    read, write = os.pipe()
    os.write(write, isolation.env_payload({"A": "1"}) + b"rest")
    os.close(write)
    try:
        assert isolation.read_env_payload(read) == {"A": "1"}
        assert os.read(read, 10) == b"rest"
    finally:
        os.close(read)
    for broken in (b"\0\0", b"\0\0\0\x09A=1", (isolation.MAX_ENV_BYTES + 1).to_bytes(4, "big")):
        read, write = os.pipe()
        os.write(write, broken)
        os.close(write)
        try:
            with pytest.raises(ValueError):
                isolation.read_env_payload(read)
        finally:
            os.close(read)


def _plant_python(workspace):
    """A pyvenv.cfg and stdlib in the workspace whose ``encodings`` package
    (imported first by every interpreter start) records that it ran."""
    ver = "python%d.%d" % sys.version_info[:2]
    os.makedirs(os.path.join(workspace, "bin"))
    os.makedirs(os.path.join(workspace, "lib", ver, "encodings"))
    with open(os.path.join(workspace, "bin", "pyvenv.cfg"), "w") as handle:
        handle.write(f"home = {workspace}/bin\n")
    open(os.path.join(workspace, "lib", ver, "os.py"), "w").close()
    marker = os.path.join(workspace, "PLANTED-CODE-RAN")
    with open(os.path.join(workspace, "lib", ver, "encodings", "__init__.py"), "w") as handle:
        handle.write(f"import posix\nposix.write(posix.open({marker!r}, posix.O_WRONLY | posix.O_CREAT, 0o600), b'1')\n")
    return os.path.join(workspace, "bin", "python3"), marker


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
async def test_project_env_never_reaches_the_launchers_startup(tmp_path):
    # P1: PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ steer CPython's path setup
    # even under -I -S. From a project's settings env they must not reach the
    # launcher's own interpreter, which runs before anything is confined.
    policy = _policy(tmp_path, backend_impl=_ProbedLandlock())
    fake, marker = _plant_python(policy.workspace)
    launch = policy.launch(["true"], None, {}, overlay={"PYTHONEXECUTABLE": fake, "__PYVENV_LAUNCHER__": fake})
    code, out, confirmed = await _run_launch(launch)
    assert not os.path.exists(marker), out
    # Here the launcher either confined itself and ran `true`, or refused
    # (no Landlock): both mean its interpreter started from the real stdlib.
    assert (code == 0 and confirmed) or isolation.REFUSAL_PREFIX in out, out


def test_a_loader_dir_whose_real_target_is_writable_is_refused(tmp_path, monkeypatch):
    # P1: loader directories compare by their real targets, not their names.
    policy = _policy(tmp_path, backend_impl=_StubBackend())
    target = os.path.join(policy.workspace, "lib")
    os.makedirs(target)
    link = tmp_path / "linked-lib"
    link.symlink_to(target)
    monkeypatch.setattr(isolation, "_LOADER_TREES", (*isolation._LOADER_TREES, str(link)))
    reason = policy.unavailable_reason() or ""
    assert "cannot be writable" in reason and target in reason, reason


def test_a_root_holding_credentials_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    policy = isolation.IsolationPolicy(workspace=str(home), home=str(home), backend_impl=_StubBackend())
    reason = policy.unavailable_reason()
    assert reason and ".ssh" in reason
    with pytest.raises(isolation.IsolationUnavailable):
        policy.launch(["true"], str(home), {})
    root = isolation.IsolationPolicy(workspace="/", home=str(home), backend_impl=_StubBackend())
    assert "filesystem root" in (root.unavailable_reason() or "")


class _StubBackend:
    name = "stub"

    def unavailable_reason(self, network):
        return None

    def prefix(self, writable, readable, network, *, protect=()):
        return ["launcher", *[f"rw={p}" for p in writable], f"net={network}", "--"]


class _ProbedLandlock(isolation.LinuxLandlock):
    """The real launcher command, with the kernel probe passed (so the command
    line can be checked on machines without Landlock)."""

    def unavailable_reason(self, network):
        return None


_REFUSE = ("import sys; sys.stderr.write('fluid isolation refused: landlock_restrict_self failed: "
           "Operation not permitted\\n'); sys.exit(126)")


class _RefusingLauncher:
    """The probe passes, then the launcher refuses, as in a container whose
    seccomp profile allows the probe but denies landlock_restrict_self."""

    name = "refusing"

    def unavailable_reason(self, network):
        return None

    def prefix(self, writable, readable, network, *, protect=()):
        return [sys.executable, "-I", "-c", _REFUSE]


#: The real launcher's main(), with Landlock and seccomp stubbed out (after
#: an optional delay), so the handoff and acknowledgement that spawn sites
#: rely on can be checked on machines without Landlock.
_FAKE_LAUNCHER = (
    "import sys, time; sys.path.insert(0, sys.argv[1]); import isolation\n"
    "delay = float(sys.argv[2])\n"
    "def landlock(*args):\n    time.sleep(delay); return 3\n"
    "isolation.apply_landlock = landlock\n"
    "isolation.apply_seccomp = lambda *args, **kwargs: True\n"
    "sys.exit(isolation.main(sys.argv[3:]))\n"
)


class _FakeLauncher:
    name = "fake"

    def __init__(self, delay: float = 0.0, program: str | None = None) -> None:
        self.delay, self.program = delay, program

    def unavailable_reason(self, network):
        return None

    def prefix(self, writable, readable, network, *, protect=()):
        argv = [sys.executable, "-I", "-S", "-c", _FAKE_LAUNCHER, os.path.dirname(isolation.SCRIPT),
                str(self.delay)]
        for root in writable:
            argv += ["--rw", root]
        argv += ["--net", "on" if network else "off", "--"]
        # A program that does not exist, ahead of the command: exec fails
        # after the acknowledgement.
        return [*argv, self.program] if self.program else argv


# S1: nothing that runs before confinement may be writable by a confined command.


def _python_with_linked_stdlib(tmp_path, ctypes_target: str) -> str:
    """A copy of the launcher's interpreter whose stdlib is per-entry links to
    the real one, except ``ctypes`` (which the launcher imports before it
    confines itself), linked to ``ctypes_target``. The real stdlib directory
    and ``_ctypes`` stay where they are."""
    import shutil
    import sysconfig

    stdlib = sysconfig.get_path("stdlib")
    libdir = os.path.dirname(stdlib)
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    for entry in os.listdir(libdir):  # libpython, for an $ORIGIN/../lib rpath
        if entry != os.path.basename(stdlib):
            (prefix / "lib" / entry).symlink_to(os.path.join(libdir, entry))
    linked = prefix / "lib" / os.path.basename(stdlib)
    linked.mkdir()
    for entry in os.listdir(stdlib):
        if entry != "ctypes":
            (linked / entry).symlink_to(os.path.join(stdlib, entry))
    (linked / "ctypes").symlink_to(ctypes_target)
    python = prefix / "bin" / os.path.basename(isolation.LAUNCH_PYTHON)
    shutil.copy2(isolation.LAUNCH_PYTHON, python)
    found = subprocess.run([str(python), "-I", "-S", "-c", "import ctypes; print(ctypes.__file__)"],
                           env={}, capture_output=True, text=True, timeout=60)
    assert found.stdout.startswith(str(linked)), (found.stdout, found.stderr)
    return str(python)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the startup probe reads /proc/self/maps")
def test_a_stdlib_package_that_resolves_into_the_workspace_is_refused(tmp_path, monkeypatch):
    # Round 4 P2: the stdlib directory, _ctypes and the interpreter are all
    # outside the workspace, but the ctypes package the launcher imports
    # before confinement resolves into it. Only the launcher's dry run, which
    # reports what it actually loaded, can see that.
    import shutil
    import sysconfig

    real_ctypes = os.path.join(sysconfig.get_path("stdlib"), "ctypes")
    planted = tmp_path / "ws" / "ctypes"
    shutil.copytree(real_ctypes, planted, ignore=shutil.ignore_patterns("__pycache__"))
    control = tmp_path / "control"
    control.mkdir()
    # Control: the same layout with ctypes linked to the real package passes.
    monkeypatch.setattr(isolation, "LAUNCH_PYTHON", _python_with_linked_stdlib(control, real_ctypes))
    assert _policy(tmp_path, backend_impl=_ProbedLandlock()).unavailable_reason() is None
    monkeypatch.setattr(isolation, "LAUNCH_PYTHON", _python_with_linked_stdlib(tmp_path, str(planted)))
    policy = _policy(tmp_path, backend_impl=_ProbedLandlock())
    reason = policy.unavailable_reason()
    assert reason and "loads" in reason and f"{policy.workspace}/ctypes/" in reason, reason
    with pytest.raises(isolation.IsolationUnavailable, match="before it is confined"):
        policy.launch(["true"], None, {})


def test_the_startup_probe_reports_what_the_launcher_loads_and_fails_closed(tmp_path, monkeypatch):
    # The dry run sees the modules the launcher imports (and, on Linux, the
    # interpreter's mapped files); a probe that cannot run refuses the profile.
    import ctypes

    workspace = str(tmp_path)
    files, search = isolation.startup_files((workspace,))
    assert os.path.realpath(ctypes.__file__) in files and search
    if sys.platform.startswith("linux"):
        assert isolation.LAUNCH_PYTHON in files
    monkeypatch.setattr(isolation, "_probed", {})
    monkeypatch.setattr(isolation, "LAUNCH_PYTHON", str(tmp_path / "no-python"))
    with pytest.raises(isolation._Unverifiable, match="probe"):
        isolation.startup_files((workspace,))


def test_launcher_runs_from_a_private_copy_outside_every_root(tmp_path):
    # The workspace is the very directory that holds isolation.py (a source
    # checkout or editable install opened as the project).
    source_dir = os.path.dirname(isolation.SCRIPT)
    policy = isolation.IsolationPolicy(workspace=source_dir, home=str(tmp_path / "home"),
                                       backend_impl=_ProbedLandlock())
    launch = policy.launch(["true"], None, {})
    launch.finish()
    argv = launch.argv
    script = argv[3]
    assert argv[0] == isolation.LAUNCH_PYTHON == os.path.realpath(isolation.LAUNCH_PYTHON)
    assert argv[1:3] == ["-I", "-S"] and script == isolation.launcher_script()
    writable = [argv[i + 1] for i, a in enumerate(argv) if a == "--rw"]
    assert writable and not any(isolation._contains(root, script) for root in writable)
    with open(isolation.SCRIPT, "rb") as handle, open(script, "rb") as copy:
        assert copy.read() == handle.read()
    assert os.stat(script).st_mode & 0o777 == 0o400


def test_a_writable_interpreter_stdlib_or_launcher_is_refused(tmp_path):
    import sysconfig

    for root in (os.path.dirname(isolation.LAUNCH_PYTHON), sysconfig.get_path("stdlib"),
                 os.path.dirname(isolation.launcher_script())):
        policy = _policy(tmp_path, writable_roots=(root,), backend_impl=_StubBackend())
        reason = policy.unavailable_reason() or ""
        assert "cannot be writable" in reason, (root, reason)
        with pytest.raises(isolation.IsolationUnavailable):
            policy.launch(["true"], None, {})


# S2: the workspace never comes from a project file.


def test_project_cwd_cannot_move_the_isolated_workspace(tmp_path):
    from fluid.config.loader import load_config

    launch, elsewhere = tmp_path / "proj", tmp_path / "elsewhere"
    (launch / ".fluid").mkdir(parents=True)
    elsewhere.mkdir()
    project = launch / ".fluid" / "config.toml"
    project.write_text(f'cwd = "{elsewhere}"\n[sandbox]\nmode = "isolated"\n')
    config = load_config(launch, env={}, include_system=False, include_user=False)
    assert config.sandbox.mode == "isolated" and str(config.cwd) == str(launch)
    # With the profile off a project cwd applies exactly as before.
    project.write_text(f'cwd = "{elsewhere}"\n')
    config = load_config(launch, env={}, include_system=False, include_user=False)
    assert config.sandbox.mode == "off" and str(config.cwd) == str(elsewhere)


# S3: overlap is refused both ways, and the launcher checks what it opened.


def test_a_root_inside_a_credential_location_is_refused(tmp_path):
    home = tmp_path / "home"
    inner = home / ".aws" / "sub"
    inner.mkdir(parents=True)
    policy = _policy(tmp_path, home=str(home), readable_roots=(str(inner),), backend_impl=_StubBackend())
    assert "inside the credential location" in (policy.unavailable_reason() or "")
    with pytest.raises(isolation.IsolationUnavailable, match="credential location"):
        policy.launch(["true"], None, {})


@pytest.mark.skipif(not LINUX, reason="the launcher's O_PATH checks are Linux only")
def test_the_launcher_checks_the_object_it_opened(tmp_path):
    base = os.path.realpath(tmp_path)
    home, ws = os.path.join(base, "home"), os.path.join(base, "ws")
    ssh = os.path.join(home, ".ssh")
    os.makedirs(os.path.join(ssh, "keys"))
    os.makedirs(ws)
    locations, around = isolation.protected_identities([ssh])

    def problem(path):
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            return isolation.opened_problem(fd, path, locations, around)
        finally:
            os.close(fd)

    assert problem(ws) is None
    # A root whose name was swapped for a link after the parent's check.
    os.symlink(ssh, os.path.join(ws, "link"))
    assert "resolved to" in (problem(os.path.join(ws, "link", "keys")) or "")
    # Recognised by inode: inside, equal to, or above a protected location
    # (which is also how a bind mount of one of them is caught).
    for path in (os.path.join(ssh, "keys"), ssh, home):
        assert "protected credential location" in (problem(path) or ""), path


# S4 / S5 / S7: rules and filters.


def test_device_rules_name_single_nodes_without_ioctls():
    paths = [path for path, _ in isolation.DEVICE_RULES]
    assert "/dev" not in paths and "/dev/shm" not in paths
    allowed = isolation.FS_READ_FILE | isolation.FS_WRITE_FILE | isolation.FS_TRUNCATE
    assert all(access & ~allowed == 0 for _, access in isolation.DEVICE_RULES)


def test_seccomp_program_always_blocks_ownership_and_xattr_changes():
    for arch, spec in isolation.SECCOMP_ARCHES.items():
        prog = isolation.seccomp_program(arch, block_network=False, block_truncate=False)
        verdicts = {prog[i][3]: prog[i + 1][3] for i in range(4, len(prog) - 1) if prog[i][0] == 0x15}
        for nr in spec["metadata"]:
            assert verdicts[nr] == isolation.SECCOMP_RET_ERRNO | 1, (arch, nr)


@pytest.mark.parametrize("network", [False, True])
def test_seccomp_program_blocks_io_uring_in_both_network_modes(network):
    # P6: io_uring opcodes include setxattr and socket, behind the filter's back.
    for arch, spec in isolation.SECCOMP_ARCHES.items():
        prog = isolation.seccomp_program(arch, block_network=not network, block_truncate=False)
        verdicts = {prog[i][3]: prog[i + 1][3] for i in range(4, len(prog) - 1)
                    if prog[i][0] == 0x15 and prog[i + 1][0] == 0x06}
        for nr in spec["io_uring"]:
            assert verdicts.get(nr) == isolation.SECCOMP_RET_ERRNO | 13, (arch, nr, network)


def test_writable_roots_never_get_device_creation_or_ioctl_rights():
    # Round 4 P3: the device list is the only way to a device the profile
    # grants; a writable root cannot make new nodes or ioctl existing ones.
    banned = isolation.FS_MAKE_CHAR | isolation.FS_MAKE_BLOCK | isolation.FS_IOCTL_DEV
    for abi in range(isolation.MIN_ABI, isolation.MAX_ABI + 1):
        handled = isolation.fs_rights(abi)
        rules = isolation.ruleset_rules(["/w"], ["/r"], handled)
        writable = [access for path, access, kind in rules if kind == "rw"]
        assert writable and not any(access & banned for access in writable), abi
        assert all(access & isolation.FS_MAKE_REG and access & isolation.FS_MAKE_FIFO
                   for access in writable)
        # Handled but not granted: the kernel denies them, not just omits them.
        assert handled & isolation.FS_MAKE_CHAR and handled & isolation.FS_MAKE_BLOCK
        assert not any(access & banned for path, access, kind in rules if kind == "ro")


def test_newer_landlock_abis_are_handled_or_refused():
    assert isolation.fs_rights(9) & isolation.FS_RESOLVE_UNIX
    assert not isolation.fs_rights(8) & isolation.FS_RESOLVE_UNIX
    assert isolation.FS_RESOLVE_UNIX & isolation._FILE_RIGHTS
    assert isolation.net_rights(4, network=False) == 0b11
    assert isolation.net_rights(10, network=False) == 0b1111
    assert isolation.net_rights(12, network=True) == 0
    assert isolation._abi_verdict(isolation.MAX_ABI, 0) == (isolation.MAX_ABI, "")
    abi, reason = isolation._abi_verdict(isolation.MAX_ABI + 1, 0)
    assert abi == 0 and "newer" in reason


# S6 / P3: only the launcher's acknowledgement says "confined".


async def test_a_command_that_prints_the_refusal_marker_after_the_ack_ran(tmp_path):
    # What the command prints, or its exit status, never turns a run into
    # "not run".
    policy = _policy(tmp_path, backend_impl=_FakeLauncher())
    out = await _bash(policy, "echo \"$FLUID_TEST_MARK\"; echo 'fluid isolation refused: fake' >&2; exit 126")
    assert not out.text.startswith("Not run"), out.text
    assert "fluid isolation refused: fake" in out.text and "126" in out.text, out.text


async def test_bash_gets_its_settings_env_through_the_handoff(tmp_path):
    policy = _policy(tmp_path, backend_impl=_FakeLauncher())
    ctx = _ctx(policy, settings_env={"FLUID_HANDOFF_VALUE": "through stdin"})
    out = await bash_handler({"command": "echo \"$FLUID_HANDOFF_VALUE\"; cat; echo stdin-done"}, ctx)
    assert not out.is_error and "through stdin" in out.text and "stdin-done" in out.text, out.text


async def test_a_timeout_before_confinement_is_not_run(tmp_path):
    marker = tmp_path / "ran"
    policy = _policy(tmp_path, backend_impl=_FakeLauncher(delay=30))
    out = await _bash(policy, f"touch {marker}", timeout=1)
    assert out.is_error and out.text.startswith("Not run"), out.text
    assert not marker.exists()


async def test_an_exec_failure_after_the_ack_is_reported_as_ran(tmp_path):
    # Ack semantics: confinement applied, exec not yet attempted. A missing
    # shell after the ack is a run that failed (127), not "not run".
    policy = _policy(tmp_path, backend_impl=_FakeLauncher(program="/nonexistent/fluid-shell"))
    out = await _bash(policy, "echo hi")
    assert out.is_error and not out.text.startswith("Not run"), out.text
    assert "could not be started after confinement" in out.text and "127" in out.text, out.text
    assert "/nonexistent/fluid-shell" in out.text


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
async def test_bash_is_not_run_when_the_launcher_copy_is_unreadable(tmp_path, monkeypatch):
    # Python exits before it can print a refusal (an earlier confined command
    # used the chmod gap on the private copy, say); no acknowledgement.
    import shutil
    import tempfile

    holder = tempfile.mkdtemp(prefix="fluid-test-launcher-")
    copy = os.path.join(holder, "fluid_isolation_launcher.py")
    shutil.copyfile(isolation.SCRIPT, copy)
    monkeypatch.setattr(isolation, "_launcher_copy", copy)
    marker = tmp_path / "ran"
    try:
        policy = _policy(tmp_path, backend_impl=_ProbedLandlock())
        assert policy.unavailable_reason() is None  # the startup probe ran and is cached
        os.chmod(copy, 0)
        out = await _bash(policy, f"touch {marker}")
    finally:
        os.chmod(copy, 0o600)
        shutil.rmtree(holder, ignore_errors=True)
    assert out.is_error and out.text.startswith("Not run") and "confirmed confinement" in out.text, out.text
    assert not marker.exists()


async def test_monitor_reports_whether_the_launch_was_confirmed(tmp_path, monkeypatch):
    from fluid.cli.host import CliHost

    reports = []
    host = CliHost(config=None, store=None)

    async def record(worker, status, summary):
        reports.append((status, summary))

    monkeypatch.setattr(host, "_report", record)
    for backend_impl in (_RefusingLauncher(), _FakeLauncher(),
                         _FakeLauncher(program="/nonexistent/fluid-shell")):
        await host.monitor_spawn({"command": "echo monitored"},
                                 _ctx(_policy(tmp_path, backend_impl=backend_impl)))
        worker = list(host.workers.values())[-1]
        await worker.task
    (refused_status, refused), (ran_status, ran), (exec_status, exec_failed) = reports
    assert refused_status == "failed" and refused.startswith("Not run") and "restrict_self" in refused
    assert ran_status == "finished" and "monitored" in ran and not ran.startswith("Not run")
    assert exec_status == "failed" and not exec_failed.startswith("Not run")
    assert "could not be started after confinement" in exec_failed, exec_failed


async def test_cancelling_monitor_before_the_ack_reports_only_cancelled(tmp_path, monkeypatch):
    # Round 4 P5: cancel() kills a launcher still short of its ack; the
    # supervisor must not also publish "failed: not run" for the same task.
    from fluid.cli.host import CliHost

    reports = []
    host = CliHost(config=None, store=None)

    async def record(worker, status, summary):
        reports.append((worker.task_id, status))
        worker.status = status

    monkeypatch.setattr(host, "_report", record)
    await host.monitor_spawn({"command": "echo never"},
                             _ctx(_policy(tmp_path, backend_impl=_FakeLauncher(delay=60))))
    worker = list(host.workers.values())[-1]
    await asyncio.sleep(1)  # the launcher has the handoff and sits before its ack
    assert worker.process.returncode is None and worker.launch._report is None
    assert await host.cancel(worker.task_id)
    await asyncio.sleep(0)
    assert reports == [(worker.task_id, "cancelled")], reports


async def test_bash_reports_a_launcher_refusal_as_not_run(tmp_path):
    out = await _bash(_policy(tmp_path, backend_impl=_RefusingLauncher()), "echo hi")
    assert out.is_error and out.text.startswith("Not run") and "restrict_self" in out.text


_ENV_DIGEST = ("import hashlib, os; names = sorted(n for n in os.environ if n.startswith('FLUID_BIG_'));"
               "print(len(names), hashlib.sha256(''.join(n + os.environ[n] for n in names)"
               ".encode()).hexdigest())")


def _big_env() -> dict[str, str]:
    # About 1.2 MB, several times the default socket buffer (208 KiB), in
    # values under the kernel's 128 KiB per-string limit for execve.
    return {f"FLUID_BIG_{i}": f"{i:03d}" + "x" * 30_000 for i in range(40)}


def _digest(env: dict[str, str]) -> str:
    import hashlib

    names = sorted(env)
    return f"{len(names)} " + hashlib.sha256("".join(n + env[n] for n in names).encode()).hexdigest()


async def test_an_environment_larger_than_the_socket_buffer_round_trips(tmp_path):
    # Round 4 P6: the real launcher main() (Landlock and seccomp stubbed)
    # reads a block that cannot fit in the socket at once.
    big = _big_env()
    launch = _policy(tmp_path, backend_impl=_FakeLauncher()).launch(
        [sys.executable, "-I", "-S", "-c", _ENV_DIGEST], None, {}, overlay=big)
    code, out, acked = await _run_launch(launch)
    assert code == 0 and acked and _digest(big) in out, out


@needs_landlock
async def test_an_environment_larger_than_the_socket_buffer_reaches_a_confined_command(tmp_path):
    big = _big_env()
    launch = _policy(tmp_path).launch(
        [sys.executable, "-I", "-S", "-c", _ENV_DIGEST], None, {}, overlay=big)
    code, out, acked = await _run_launch(launch)
    assert code == 0 and acked and _digest(big) in out, out


def test_launch_builds_the_launcher_command(tmp_path):
    (tmp_path / "extra").mkdir()
    policy = _policy(tmp_path, writable_roots=(str(tmp_path / "extra"),), backend_impl=_StubBackend())
    launch = policy.launch(["sh", "-c", "true"], "/x", {"PATH": "/bin", "SECRET_TOKEN": "t"})
    try:
        argv = launch.argv
        assert argv[0] == "launcher" and argv[-3:] == ["sh", "-c", "true"]
        assert f"rw={policy.workspace}" in argv and f"rw={tmp_path / 'extra'}" in argv
        assert "net=False" in argv and launch.cwd == "/x" and launch.env == {}
        assert b"SECRET_TOKEN" not in launch._payload and b"PATH=/bin" in launch._payload
    finally:
        launch.finish()


# P4: one policy per engine, resolved before the build.


def test_a_host_none_does_not_turn_a_configured_profile_off(tmp_path):
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    on = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), "sandbox": {"mode": "isolated"}})
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider(),
                          ctx=ToolCtx(extra={isolation.EXTRA_KEY: None}))
    policy = isolation.policy_of(engine.ctx)
    assert isinstance(policy, isolation.IsolationPolicy)
    assert policy.workspace == os.path.realpath(tmp_path)
    # A policy the host injected before the build is the one used.
    injected = _policy(tmp_path, backend_impl=_StubBackend())
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider(),
                          ctx=ToolCtx(extra={isolation.EXTRA_KEY: injected}))
    assert isolation.policy_of(engine.ctx) is injected


def test_code_mode_is_refused_under_the_profile(tmp_path):
    # The profile confines Bash and Monitor only; a code-mode cell is
    # model-written Python in an unconfined kernel, so the two do not mix.
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    on = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), "code_mode": True,
                                       "sandbox": {"mode": "isolated"}})
    with pytest.raises(isolation.IsolationUnavailable, match="code mode"):
        build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider())


async def test_a_policy_changed_after_the_build_is_refused(tmp_path):
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    marker = tmp_path / "ran"
    off = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)})
    engine = build_engine(HarnessConfig.from_dict(off), MemoryStore(), provider=_FakeProvider())
    assert isolation.policy_of(engine.ctx) is None
    engine.ctx.extra[isolation.EXTRA_KEY] = _policy(tmp_path, backend_impl=_FakeLauncher())
    out = await bash_handler({"command": f"touch {marker}"}, engine.ctx)
    assert out.is_error and "changed after this engine was built" in out.text, out.text
    assert not marker.exists()
    on = deep_merge(off, {"sandbox": {"mode": "isolated"}})
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider())
    engine.ctx.extra.pop(isolation.EXTRA_KEY)  # removing it is a change too
    with pytest.raises(isolation.IsolationUnavailable, match="changed after"):
        isolation.policy_of(engine.ctx)


def test_seccomp_program_shape():
    prog = isolation.seccomp_program("x86_64", block_network=True, block_truncate=False)
    denied = {k for code, _jt, _jf, k in prog if code == 0x15} - {0xC000003E}
    assert {41, 425, 426, 427} <= denied and 76 not in denied
    on = isolation.seccomp_program("x86_64", block_network=False, block_truncate=False)
    assert {425, 426, 427} <= {k for code, _jt, _jf, k in on if code == 0x15} and 41 not in {
        k for code, _jt, _jf, k in on if code == 0x15}
    assert prog[-1] == (0x06, 0, 0, isolation.SECCOMP_RET_ALLOW)
    assert isolation.seccomp_program("aarch64", block_network=False, block_truncate=True)[-3][3] == 45


# --- fail closed ---------------------------------------------------------------------


async def test_bash_refuses_when_isolation_is_unavailable(tmp_path):
    policy = _policy(tmp_path, backend_impl=_NoBackend())
    marker = tmp_path / "ran"
    out = await _bash(policy, f"touch {marker}")
    assert out.is_error and "Not run" in out.text and "test: no mechanism" in out.text
    assert not marker.exists()


async def test_monitor_refuses_when_isolation_is_unavailable_or_the_spawner_is_unaware(tmp_path):
    calls = []

    async def aware(input, ctx):  # pragma: no cover - must never be reached
        calls.append(input)

    aware.applies_isolation = True

    async def unaware(input, ctx):  # pragma: no cover - must never be reached
        calls.append(input)

    unavailable = _policy(tmp_path, backend_impl=_NoBackend())
    out = await monitor_handler({"command": "make ci"}, _ctx(unavailable, monitor_spawn=aware))
    assert out.is_error and "test: no mechanism" in out.text
    available = _policy(tmp_path, backend_impl=_StubBackend())
    out = await monitor_handler({"command": "make ci"}, _ctx(available, monitor_spawn=unaware))
    assert out.is_error and "does not apply it" in out.text
    assert calls == []


def test_delegated_provider_is_refused_under_the_profile(tmp_path):
    from fluid.config.loader import build_engine

    config = HarnessConfig.from_dict({
        "cwd": str(tmp_path), "default_provider": "d",
        "providers": {"d": {"kind": "delegated_claude"}},
        "sandbox": {"mode": "isolated"},
    })
    with pytest.raises(isolation.IsolationUnavailable, match="delegated_claude"):
        build_engine(config, MemoryStore())


class _FakeProvider:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request):  # pragma: no cover - never streamed here
        if False:
            yield None


def test_loader_carries_the_policy_only_when_asked(tmp_path):
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    base = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)})
    engine = build_engine(HarnessConfig.from_dict(base), MemoryStore(), provider=_FakeProvider())
    assert isolation.EXTRA_KEY not in engine.ctx.extra

    (tmp_path / "x").mkdir()
    on = deep_merge(base, {"sandbox": {"mode": "isolated", "writable_roots": [str(tmp_path / "x")]}})
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider())
    policy = engine.ctx.extra[isolation.EXTRA_KEY]
    assert isinstance(policy, isolation.IsolationPolicy)
    assert policy.workspace == os.path.realpath(tmp_path) and not policy.network


def test_fluids_own_git_probes_skip_the_profiles_writable_roots(tmp_path):
    # P4: `git status` runs programs .git/config names (core.fsmonitor), and a
    # confined command can write that file; Fluid's prompt probes run unconfined.
    from fluid.context import environment

    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    # Off: main's probe, unchanged (and here it runs the configured program).
    assert environment.git_context_parts(str(repo)) is not None
    assert marker.exists()
    marker.unlink()
    from fluid.cli.config import resolved_prompt

    config = HarnessConfig.from_dict({"cwd": str(repo), "sandbox": {"mode": "isolated"}})
    text = resolved_prompt(config).text
    assert not marker.exists() and environment.git_probes_skipped(str(repo / "sub"))
    assert environment.git_context_parts(str(repo)) is None and not marker.exists()
    assert "Git repository" not in text or "yes" not in text.split("Git repository", 1)[1][:20]


@pytest.mark.skipif(not (shutil.which("rg") and shutil.which("git")), reason="needs rg and git")
async def test_glob_spawns_no_git_in_the_profiles_writable_roots(tmp_path, monkeypatch):
    # Round 4 P4: Glob's link probe lists files with `git ls-files`, which
    # reads the repository's config, unconfined. In a registered root it uses
    # the filesystem walk instead, and still finds the symlinked file.
    from fluid.context import environment
    from fluid.tools.builtins import _util
    from fluid.tools.builtins.glob import handler as glob_handler

    spawned: list[str] = []
    real_popen = _util.subprocess.Popen

    def spy(argv, *args, **kwargs):
        spawned.append(os.path.basename(argv[0]))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(_util.subprocess, "Popen", spy)
    for name in ("plain", "ws"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "real.txt").write_text("x")
        (repo / "link.txt").symlink_to(repo / "real.txt")
    # Unregistered: the probe runs git (this is what the spy can see).
    out = await glob_handler({"pattern": "*.txt"}, ToolCtx(cwd=str(tmp_path / "plain")))
    assert "link.txt" in out.text and "git" in spawned
    spawned.clear()
    environment.skip_git_probes(str(tmp_path / "ws"))
    out = await glob_handler({"pattern": "*.txt"}, ToolCtx(cwd=str(tmp_path / "ws")))
    assert not out.is_error and "link.txt" in out.text and "real.txt" in out.text
    assert spawned and "git" not in spawned  # rg ran; git did not


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
def test_launcher_refuses_when_landlock_is_unusable(tmp_path):
    # Without Landlock the kernel refuses; with it, a reviewed-ABI ceiling of
    # 0 makes this kernel's ABI "too new". Either way nothing may run.
    marker = tmp_path / "ran"
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
             "isolation.MAX_ABI = 0\n"
             "sys.exit(isolation.main(sys.argv[2:]))\n")
    ours, theirs = socket.socketpair()
    with ours, theirs:
        ours.sendall(isolation.env_payload({"PATH": "/usr/bin:/bin"}))
        proc = subprocess.run(
            [sys.executable, "-I", "-c", probe, os.path.dirname(isolation.SCRIPT),
             "--rw", str(tmp_path / "ws"), "--net", "on", "--", "touch", str(marker)],
            stdin=theirs, capture_output=True, text=True, timeout=30, check=False)
        theirs.close()
        ours.settimeout(5)
        assert ours.recv(16) == b""  # refused before confinement: no acknowledgement
    assert proc.returncode == isolation.REFUSED, proc.stderr
    assert isolation.REFUSAL_PREFIX in proc.stderr and not marker.exists()
    assert ("newer than" in proc.stderr) == bool(ABI), proc.stderr


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
def test_launcher_rejects_a_malformed_policy(tmp_path):
    for args in (["--rw", str(tmp_path), "--", "true"], ["--net", "off", "--"], ["--bogus", "x"]):
        proc = subprocess.run([sys.executable, "-I", "-S", isolation.SCRIPT, *args],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                              check=False)
        assert proc.returncode == isolation.REFUSED, (args, proc.stderr)


def test_non_linux_platforms_have_no_backend(tmp_path):
    policy = _policy(tmp_path, backend_impl=isolation.backend("darwin"))
    with pytest.raises(isolation.IsolationUnavailable, match="darwin"):
        policy.check()


# --- enforcement (from inside the child) ------------------------------------------------


def test_enforcement_tests_fail_rather_than_skip_on_github_actions_linux():
    reason = "test: Landlock is not supported by this kernel"
    ci = {"GITHUB_ACTIONS": "true"}
    with pytest.raises(pytest.fail.Exception, match="Landlock is not supported") as failed:
        require_enforcement(reason, environ=ci, platform="linux")
    assert not isinstance(failed.value, pytest.skip.Exception)
    # Elsewhere, including GitHub's Windows and macOS runners, the skip stays visible.
    for environ, platform in ((ci, "win32"), (ci, "darwin"), ({}, "linux"),
                              ({"GITHUB_ACTIONS": "false"}, "linux")):
        with pytest.raises(pytest.skip.Exception, match="Landlock is not supported"):
            require_enforcement(reason, environ=environ, platform=platform)
    require_enforcement(None, environ=ci, platform="linux")  # enforceable: runs


@needs_seccomp
def test_seccomp_blocks_new_sockets_in_a_child():
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
        "isolation.apply_seccomp(isolation.machine(), block_network=True, block_truncate=False)\n"
        "import socket\n"
        "try:\n    socket.socket()\nexcept PermissionError:\n    print('blocked')\n"
        "else:\n    print('OPEN')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", probe, os.path.dirname(isolation.SCRIPT)],
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.stdout.strip() == "blocked", proc.stderr


_IO_URING = (
    "import ctypes, os, sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
    "if sys.argv[2] != 'confined':\n"
    "    isolation.apply_seccomp(isolation.machine(), block_network=False, block_truncate=False)\n"
    "libc = ctypes.CDLL(None, use_errno=True); libc.syscall.restype = ctypes.c_long\n"
    "params = ctypes.create_string_buffer(120)\n"
    "fd = libc.syscall(425, 1, params)\n"  # io_uring_setup: 425 on x86_64 and aarch64
    "err = ctypes.get_errno()\n"
    "print('io_uring', 'blocked' if fd < 0 and err == 13 else f'OPEN fd={fd} errno={err}')\n"
)


@needs_seccomp
def test_seccomp_blocks_io_uring_with_the_network_on():
    # P6: setxattr and socket are io_uring opcodes too.
    proc = subprocess.run([sys.executable, "-I", "-c", _IO_URING, os.path.dirname(isolation.SCRIPT), "filter"],
                          capture_output=True, text=True, timeout=30, check=False)
    assert "io_uring blocked" in proc.stdout.split("\n"), (proc.stdout, proc.stderr)


@needs_seccomp
def test_seccomp_blocks_ownership_and_xattr_changes_in_a_child(tmp_path):
    target = tmp_path / "owned"
    target.write_text("x")
    probe = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
        "isolation.apply_seccomp(isolation.machine(), block_network=False, block_truncate=False)\n"
        "path = sys.argv[2]\n"
        "def attempt(name, call):\n"
        "    try:\n        call()\n"
        "    except PermissionError:\n        print(name, 'blocked')\n"
        "    except OSError as exc:\n        print(name, 'error', exc.errno)\n"
        "    else:\n        print(name, 'ALLOWED')\n"
        "attempt('chown', lambda: os.chown(path, os.getuid(), os.getgid()))\n"
        "attempt('lchown', lambda: os.lchown(path, os.getuid(), os.getgid()))\n"
        "attempt('setxattr', lambda: os.setxattr(path, 'user.fluid', b'1'))\n"
        "attempt('removexattr', lambda: os.removexattr(path, 'user.fluid'))\n"
        "os.chmod(path, 0o600); print('chmod still works')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", probe, os.path.dirname(isolation.SCRIPT), str(target)],
                          capture_output=True, text=True, timeout=30, check=False)
    lines = proc.stdout.split("\n")
    for name in ("chown", "lchown", "setxattr", "removexattr"):
        assert f"{name} blocked" in lines, (name, proc.stdout, proc.stderr)
    assert "chmod still works" in lines, proc.stderr


_SOCKETPAIRS = (
    "import os, socket, sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
    "isolation.apply_seccomp(isolation.machine(), block_network=True, block_truncate=False)\n"
    "def attempt(name, kind):\n"
    "    try:\n        a, b = socket.socketpair(socket.AF_UNIX, kind)\n"
    "    except PermissionError:\n        print(name, 'blocked')\n"
    "    else:\n        a.send(b'x'); print(name, 'works' if b.recv(1) == b'x' else 'BROKEN')\n"
    "attempt('stream', socket.SOCK_STREAM)\n"
    "attempt('stream-cloexec-nonblock', socket.SOCK_STREAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)\n"
    "attempt('seqpacket', socket.SOCK_SEQPACKET)\n"
    "attempt('dgram', socket.SOCK_DGRAM)\n"
    "attempt('dgram-cloexec', socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)\n"
    "import asyncio; asyncio.run(asyncio.sleep(0)); print('asyncio works')\n"
)


@needs_seccomp
def test_seccomp_allows_stream_socketpairs_only_with_the_network_off():
    # P2: a datagram pair can sendto() any pathname datagram socket, so with
    # the network off only AF_UNIX stream and seqpacket pairs are allowed.
    proc = subprocess.run([sys.executable, "-I", "-c", _SOCKETPAIRS, os.path.dirname(isolation.SCRIPT)],
                          capture_output=True, text=True, timeout=30, check=False)
    lines = proc.stdout.split("\n")
    for name, verdict in (("stream", "works"), ("stream-cloexec-nonblock", "works"),
                          ("seqpacket", "works"), ("dgram", "blocked"), ("dgram-cloexec", "blocked")):
        assert f"{name} {verdict}" in lines, (name, proc.stdout, proc.stderr)
    assert "asyncio works" in lines, proc.stderr


@needs_landlock
async def test_network_off_blocks_a_datagram_socketpair_to_an_outside_listener(tmp_path):
    # P2, end to end: a pathname datagram listener outside the workspace.
    path = str(tmp_path / "outside.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(path)
    listener.setblocking(False)
    probe = (f"{sys.executable} -I -c \"import socket\n"
             f"try:\n a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
             f"except PermissionError:\n print('socketpair blocked')\n"
             f"else:\n a.sendto(b'escaped', {path!r}); print('SENT')\n"
             f"s, t = socket.socketpair(); s.send(b'y'); print('stream', t.recv(1).decode())\"")
    try:
        out = await _bash(_policy(tmp_path, network=False), probe)
        with pytest.raises(BlockingIOError):
            listener.recv(64)
    finally:
        listener.close()
    assert "socketpair blocked" in out.text and "SENT" not in out.text, out.text
    assert "stream y" in out.text, out.text


@needs_landlock
async def test_child_can_write_the_workspace_but_nothing_else(tmp_path):
    policy = _policy(tmp_path)
    inside = os.path.join(policy.workspace, "made")
    outside = tmp_path / "escaped"
    out = await _bash(policy, f"echo ok > {inside} && cat {inside}")
    assert not out.is_error and "ok" in out.text, out.text
    out = await _bash(policy, f"echo x > {outside}; echo x > /usr/fluid-escape; mkdir {tmp_path}/d")
    assert not outside.exists() and not (tmp_path / "d").exists()
    assert out.text.count("Permission denied") >= 2, out.text


@needs_landlock
async def test_child_cannot_read_a_planted_secret_or_the_real_home(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    policy = _policy(tmp_path)
    out = await _bash(policy, f"cat {secret}; ls {os.path.expanduser('~')}; echo HOME=$HOME")
    assert "TOPSECRET" not in out.text and "Permission denied" in out.text, out.text
    assert f"HOME={policy.private_dir()}" in out.text


@needs_landlock
async def test_child_cannot_read_another_process_environ(tmp_path):
    # A peer with the canary in its exec-time environment, which is what
    # /proc/<pid>/environ exposes (a later setenv in this process would not be).
    peer = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                            env={"PATH": os.environ.get("PATH", ""), "FLUID_PEER_CANARY": "canary-value"})
    try:
        environ = f"/proc/{peer.pid}/environ"
        for _ in range(100):  # until the exec has replaced the forked image
            with open(environ, "rb") as handle:
                if b"canary-value" in handle.read():
                    break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the peer's environ never showed the canary, so the test proves nothing")
        out = await _bash(_policy(tmp_path), f"cat {environ}; echo \"status=$?\"")
        assert "canary-value" not in out.text, out.text
        assert "status=0" not in out.text and "status=" in out.text, out.text
    finally:
        peer.kill()
        peer.wait()


@needs_landlock
async def test_child_env_carries_no_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_TEST_API_TOKEN_X", "leak")
    monkeypatch.setenv("SOME_RANDOM_HOST_VAR", "leak")
    out = await _bash(_policy(tmp_path), "env")
    assert "leak" not in out.text, out.text


def _listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _connect_probe(port: int) -> str:
    return (f"{sys.executable} -I -c \"import socket\n"
            f"try:\n socket.create_connection(('127.0.0.1', {port}), timeout=5); print('CONNECTED')\n"
            f"except OSError as e:\n print('refused', e.errno)\"")


@needs_landlock
async def test_network_off_blocks_connections(tmp_path):
    server = _listener()
    try:
        out = await _bash(_policy(tmp_path, network=False), _connect_probe(server.getsockname()[1]))
    finally:
        server.close()
    assert "CONNECTED" not in out.text and "refused" in out.text, out.text


@needs_landlock
async def test_network_on_allows_connections(tmp_path):
    server = _listener()
    try:
        out = await _bash(_policy(tmp_path, network=True), _connect_probe(server.getsockname()[1]))
    finally:
        server.close()
    assert "CONNECTED" in out.text, out.text


@needs_landlock
async def test_monitor_command_runs_confined(tmp_path, monkeypatch):
    from fluid.cli.host import CliHost

    host = CliHost(config=None, store=None)
    monkeypatch.setattr(host, "_supervise", lambda worker: asyncio.sleep(0))
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    policy = _policy(tmp_path)
    await host.monitor_spawn({"command": f"cat {secret}; echo x > {tmp_path}/escaped; echo end"},
                             _ctx(policy))
    (worker,) = host.workers.values()
    out = (await worker.process.stdout.read()).decode()
    await worker.process.wait()
    assert worker.launch.finish(), out  # the launcher acked before the command ran
    assert "TOPSECRET" not in out and "end" in out, out
    assert not (tmp_path / "escaped").exists()


# --- the launcher boundary, end to end (real launcher process) ----------------------------


@needs_landlock
def test_the_launcher_refuses_a_file_as_a_writable_root(tmp_path):
    # P2 at the launcher: even a command line that skipped the policy's checks
    # cannot grant write access to a single file (such as an interpreter).
    target = tmp_path / "python-like"
    target.write_text("x")
    ours, theirs = socket.socketpair()
    with ours, theirs:
        ours.sendall(isolation.env_payload({"PATH": "/usr/bin:/bin"}))
        proc = subprocess.run(
            [isolation.LAUNCH_PYTHON, "-I", "-S", isolation.launcher_script(), "--rw", str(target),
             "--net", "on", "--", "sh", "-c", f"echo changed > {target}"],
            stdin=theirs, capture_output=True, text=True, timeout=60, check=False, env={})
        theirs.close()
        ours.settimeout(5)
        assert ours.recv(16) == b"", proc.stderr  # never confined, never acknowledged
    assert proc.returncode == isolation.REFUSED and "not a directory" in proc.stderr, proc.stderr
    assert target.read_text() == "x"


@needs_landlock
async def test_a_pending_handoff_is_not_readable_by_another_confined_command(tmp_path):
    # P3, round 4 P6: the first launcher is held (behind a FIFO) before it
    # reads its standard input, with the environment block already sent into
    # the socket. A confined command searching the readable temporary
    # directory and every process's environ and command line finds nothing,
    # and its own stdin is /dev/null rather than a handoff socket.
    import tempfile

    canary = "canary-2xyz"
    policy = _policy(tmp_path, readable_roots=(tempfile.gettempdir(),))
    first = policy.launch(["true"], None, {}, overlay={"FLUID_HANDOFF_CANARY": canary})
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    held = await asyncio.create_subprocess_exec(
        "sh", "-c", 'read go < "$0"; exec "$@"', str(gate), *first.argv,
        env=first.env, stdin=first.stdin,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    first.started()
    try:
        await asyncio.wait_for(first._sender, 10)  # the block sits in the socket
        assert held.returncode is None
        out = await _bash(policy, (
            f"if grep -rqs \"canary-$((1+1))xyz\" {tempfile.gettempdir()} /proc/[0-9]*/environ "
            "/proc/[0-9]*/cmdline; then echo FOUND; else echo clean; fi; cat; "
            "readlink /proc/self/fd/0"))
    finally:
        await asyncio.to_thread(gate.write_text, "go\n")
        output, _ = await asyncio.wait_for(held.communicate(), 60)
    assert "clean" in out.text and "FOUND" not in out.text, out.text
    assert "/dev/null" in out.text, out.text
    # Released, the held launcher consumed the very block that was pending.
    assert held.returncode == 0 and first.finish(), output


@needs_landlock
async def test_io_uring_is_blocked_in_a_confined_command_with_the_network_on(tmp_path):
    # P6 end to end: the launcher's filter, not one this test installs.
    policy = _policy(tmp_path, network=True)
    script = os.path.join(policy.workspace, "uring.py")
    with open(script, "w") as handle:
        handle.write(
            "import ctypes\n"
            "libc = ctypes.CDLL(None, use_errno=True); libc.syscall.restype = ctypes.c_long\n"
            "fd = libc.syscall(425, 1, ctypes.create_string_buffer(120))\n"
            "err = ctypes.get_errno()\n"
            "print('io_uring', 'blocked' if fd < 0 and err == 13 else f'OPEN fd={fd} errno={err}')\n")
    out = await _bash(policy, f"{sys.executable} -I -S {script}")
    assert "io_uring blocked" in out.text, out.text


@needs_landlock
async def test_a_confined_command_cannot_make_device_nodes_in_the_workspace(tmp_path):
    # Round 4 P3 end to end. Landlock's mknod hook runs before the kernel's
    # CAP_MKNOD check, so its EACCES (rather than EPERM, or success for an
    # overlayfs whiteout 0:0) shows the profile, not a missing capability,
    # refused it. A FIFO is not a device and is still allowed.
    policy = _policy(tmp_path)
    script = os.path.join(policy.workspace, "nodes.py")
    with open(script, "w") as handle:
        handle.write(
            "import errno, os, stat\n"
            "for name, mode in (('chr', stat.S_IFCHR), ('blk', stat.S_IFBLK), ('fifo', stat.S_IFIFO)):\n"
            "    try:\n"
            "        os.mknod(name, mode | 0o600, os.makedev(0, 0))\n"
            "        print(name, 'made')\n"
            "    except OSError as exc:\n"
            "        print(name, errno.errorcode[exc.errno])\n")
    out = await _bash(policy, f"cd {policy.workspace} && {sys.executable} -I -S {script}")
    assert "chr EACCES" in out.text and "blk EACCES" in out.text, out.text
    assert "fifo made" in out.text, out.text


@needs_landlock
async def test_an_exec_failure_after_real_confinement_is_reported(tmp_path):
    # Ack semantics at the real launcher: confined, then exec failed.
    launch = _policy(tmp_path).launch(["/nonexistent/fluid-program"], None, {})
    code, out, acked = await _run_launch(launch)
    assert acked and code == 127, out
    assert "/nonexistent/fluid-program" in (launch.exec_error or ""), (launch.exec_error, out)
