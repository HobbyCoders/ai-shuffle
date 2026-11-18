"""The opt-in isolated execution profile (fluid.tools.isolation).

Three groups:

* default off: nothing about spawning changes unless ``[sandbox]`` asks;
* fail closed: a profile that cannot be enforced refuses, it never runs the
  command unconfined;
* enforcement: a confined child tries to break the policy (write outside the
  workspace, read a planted secret, read another process's environ, open a
  socket) and the kernel stops it. These need Landlock, and skip with the
  probe's reason where the kernel lacks it, except on GitHub Actions Linux,
  where they fail instead (see require_enforcement). The seccomp network test needs
  only seccomp, so it runs on more machines.
"""

from __future__ import annotations

import asyncio
import os
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


@pytest.fixture
def _enforceable(request):
    require_enforcement(NOT_ENFORCEABLE)
    # Under `pytest -q` passes are not listed, so on GitHub Actions each
    # enforcement test that got past the gate names itself and the kernel's
    # Landlock ABI as a run annotation, written past pytest's output capture
    # so it lands in the job log.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        import platform

        capture = request.config.pluginmanager.getplugin("capturemanager")
        with capture.global_and_fixture_disabled():
            print(f"\n::notice title=isolation enforcement::{request.node.name} ran "
                  f"under Landlock ABI {ABI} (kernel {platform.release()})", flush=True)


needs_landlock = pytest.mark.usefixtures("_enforceable")
needs_seccomp = pytest.mark.skipif(
    not (LINUX and isolation.machine() and isolation.seccomp_available()),
    reason="seccomp filters (x86_64/aarch64 Linux) unavailable here",
)


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


async def _bash(policy, command: str):
    return await bash_handler({"command": command}, _ctx(policy))


# --- default off ------------------------------------------------------------------


def test_profile_is_off_by_default():
    config = HarnessConfig.from_dict({})
    assert config.sandbox == SandboxConfig()
    assert config.sandbox.mode == "off"
    assert isolation.policy_from_config(config.sandbox, "/") is None
    assert isolation.policy_of(ToolCtx()) is None


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
    plain = _policy(tmp_path, backend_impl=_StubBackend())
    with pytest.raises(isolation.IsolationUnavailable, match="device"):
        plain.launch(["true"], None, {}, readable=("/dev",))


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
    # P1: the launcher process gets only the environment Fluid builds; the
    # command's own (settings env included) is a file it installs after confinement.
    policy = _policy(tmp_path, backend_impl=_StubBackend())
    launch = policy.launch(["true"], None, {"PATH": "/bin", "PYTHONPATH": "/ws"},
                           overlay={"LD_PRELOAD": "/ws/evil.so", "PYTHONEXECUTABLE": "/ws/python",
                                    "__PYVENV_LAUNCHER__": "/ws/python", "GCONV_PATH": "/ws"})
    try:
        assert launch.env == isolation.LAUNCHER_ENV == {}
        command_env = isolation.read_env_file(launch.env_path)
        assert command_env["LD_PRELOAD"] == "/ws/evil.so" and command_env["PYTHONEXECUTABLE"] == "/ws/python"
        assert command_env["PATH"] == "/bin" and "PYTHONPATH" not in command_env
        assert command_env["HOME"].startswith(policy.private_dir())
        side = os.path.dirname(isolation.launcher_script())
        assert os.path.dirname(launch.env_path) == os.path.dirname(launch.ack_path) == side
        assert os.stat(launch.env_path).st_mode & 0o777 == 0o600
    finally:
        assert launch.finish() is False  # nothing acknowledged
    assert not os.path.exists(launch.env_path) and not os.path.exists(launch.ack_path)
    # Round trip, including names and values that are not ASCII.
    env = {"A": "1", "B": "x=y", "C": "", "Ü": "\u00e9\udcff"}
    assert isolation.decode_env(isolation.encode_env(env)) == env


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
def test_project_env_never_reaches_the_launchers_startup(tmp_path):
    # P1: PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ steer CPython's path setup
    # even under -I -S. From a project's settings env they must not reach the
    # launcher's own interpreter, which runs before anything is confined.
    policy = _policy(tmp_path, backend_impl=_ProbedLandlock())
    fake, marker = _plant_python(policy.workspace)
    launch = policy.launch(["true"], None, {}, overlay={"PYTHONEXECUTABLE": fake, "__PYVENV_LAUNCHER__": fake})
    try:
        proc = subprocess.run(launch.argv, env=launch.env, capture_output=True, text=True, timeout=60,
                              check=False)
    finally:
        confirmed = launch.finish()
    assert not os.path.exists(marker), proc.stderr
    # Here the launcher either confined itself and ran `true`, or refused
    # (no Landlock): both mean its interpreter started from the real stdlib.
    assert (proc.returncode == 0 and confirmed) or isolation.REFUSAL_PREFIX in proc.stderr, proc.stderr


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

    def prefix(self, writable, readable, network, *, protect=(), env_file, ack_file):
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

    def prefix(self, writable, readable, network, *, protect=(), env_file, ack_file):
        return [sys.executable, "-I", "-c", _REFUSE]


#: Speaks the launcher's protocol (the command env from the file, the ack
#: after "confinement", then exec) without confining anything, so the way
#: spawn sites read the ack can be checked on machines without Landlock.
_FAKE_LAUNCHER = r"""
import os, sys, time
env_file, ack_file, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
command = sys.argv[sys.argv.index("--") + 1:]
with open(env_file, "rb") as handle:
    entries = [e.partition(b"=") for e in handle.read().split(b"\0") if e]
env = {os.fsdecode(k): os.fsdecode(v) for k, _, v in entries}
time.sleep(delay)
fd = os.open(ack_file, os.O_WRONLY)
os.write(fd, b"\x01")
os.close(fd)
os.execvpe(command[0], command, env)
"""


class _FakeLauncher:
    name = "fake"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def unavailable_reason(self, network):
        return None

    def prefix(self, writable, readable, network, *, protect=(), env_file, ack_file):
        return [sys.executable, "-I", "-S", "-c", _FAKE_LAUNCHER, env_file, ack_file, str(self.delay), "--"]


# S1: nothing that runs before confinement may be writable by a confined command.


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
    # A spawn's own read-only roots pass the same check.
    plain = _policy(tmp_path, home=str(home), backend_impl=_StubBackend())
    assert plain.unavailable_reason() is None
    with pytest.raises(isolation.IsolationUnavailable, match="credential location"):
        plain.launch(["true"], None, {}, readable=(str(inner),))


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


# S6: a launcher that refuses after the probe passed is not a command exit.


async def test_a_command_that_prints_the_refusal_marker_after_the_ack_ran(tmp_path):
    # P3: only the launcher's ack says "confined and started"; what the
    # command prints, or its exit status, never turns a run into "not run".
    policy = _policy(tmp_path, backend_impl=_FakeLauncher())
    out = await _bash(policy, "echo \"$FLUID_TEST_MARK\"; echo 'fluid isolation refused: fake' >&2; exit 126")
    assert not out.text.startswith("Not run"), out.text
    assert "fluid isolation refused: fake" in out.text and "126" in out.text, out.text


async def test_a_hook_runs_only_after_the_ack(tmp_path):
    from fluid.hooks.command import CommandHook

    marker = tmp_path / "hook-ran"
    policy = _policy(tmp_path, backend_impl=_FakeLauncher())
    ok = CommandHook(event="PreToolUse", command=f"touch {marker}", cwd=str(tmp_path), isolation=policy)
    decision = await ok("PreToolUse", {"name": "Edit", "input": {}})
    assert marker.exists() and (decision is None or decision.action != "deny"), decision
    # A hook that ran and exited 126 with the marker is an ordinary exit status.
    mimic = CommandHook(event="PreToolUse", command="echo 'fluid isolation refused: x' >&2; exit 126",
                        cwd=str(tmp_path), isolation=policy)
    decision = await mimic("PreToolUse", {"name": "Edit", "input": {}})
    assert decision is not None and decision.action != "deny", decision
    assert any("exited with code 126" in w for w in decision.warnings), decision


async def test_a_hook_gate_denies_when_the_launcher_times_out_before_confinement(tmp_path):
    from fluid.hooks.command import CommandHook

    marker = tmp_path / "hook-ran"
    policy = _policy(tmp_path, backend_impl=_FakeLauncher(delay=30))
    pre = CommandHook(event="PreToolUse", command=f"touch {marker}", cwd=str(tmp_path), timeout=1,
                      isolation=policy)
    decision = await pre("PreToolUse", {"name": "Edit", "input": {}})
    assert decision is not None and decision.action == "deny", decision
    assert not marker.exists()


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
async def test_a_hook_gate_denies_when_the_launcher_copy_is_unreadable(tmp_path, monkeypatch):
    # P3: Python exits before it can print the refusal (an earlier confined
    # command used the chmod gap on the private copy, say); no ack, so deny.
    import shutil
    import tempfile

    from fluid.hooks.command import CommandHook

    holder = tempfile.mkdtemp(prefix="fluid-test-launcher-")
    copy = os.path.join(holder, "fluid_isolation_launcher.py")
    shutil.copyfile(isolation.SCRIPT, copy)
    os.chmod(copy, 0)
    monkeypatch.setattr(isolation, "_launcher_copy", copy)
    marker = tmp_path / "hook-ran"
    try:
        policy = _policy(tmp_path, backend_impl=_ProbedLandlock())
        assert policy.unavailable_reason() is None
        pre = CommandHook(event="PreToolUse", command=f"touch {marker}", cwd=str(tmp_path), isolation=policy)
        decision = await pre("PreToolUse", {"name": "Edit", "input": {}})
    finally:
        os.chmod(copy, 0o600)
        shutil.rmtree(holder, ignore_errors=True)
    assert decision is not None and decision.action == "deny", decision
    # Denied for the missing ack, not because Python's exit status 2 happens
    # to mean "block" in the hook protocol.
    assert "not run" in (decision.note or "") and "confirmed confinement" in decision.note, decision
    assert not marker.exists()


async def test_monitor_reports_whether_the_launch_was_confirmed(tmp_path, monkeypatch):
    from fluid.cli.host import CliHost

    reports = []
    host = CliHost(config=None, store=None)

    async def record(worker, status, summary):
        reports.append((status, summary))

    monkeypatch.setattr(host, "_report", record)
    for backend_impl in (_RefusingLauncher(), _FakeLauncher()):
        await host.monitor_spawn({"command": "echo monitored"},
                                 _ctx(_policy(tmp_path, backend_impl=backend_impl)))
        worker = list(host.workers.values())[-1]
        await worker.task
    (refused_status, refused), (ran_status, ran) = reports
    assert refused_status == "failed" and refused.startswith("Not run") and "restrict_self" in refused
    assert ran_status == "finished" and "monitored" in ran and not ran.startswith("Not run")


async def test_code_mode_kernel_starts_once_the_launcher_acks(tmp_path):
    from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

    kernel = SubprocessKernel(KernelConfig(isolation=_policy(tmp_path, backend_impl=_FakeLauncher())))
    await kernel.start()
    try:
        async def no_tools(*_args, **_kwargs):
            raise AssertionError("no tool calls expected")

        result = await kernel.exec("6 * 7", timeout=10, on_host_call=no_tools)
        assert result.error is None and result.value_repr == "42", result.error
    finally:
        await kernel.stop()


async def test_a_hook_whose_launcher_refuses_denies_the_tool(tmp_path):
    from fluid.hooks.command import CommandHook

    marker = tmp_path / "hook-ran"
    policy = _policy(tmp_path, backend_impl=_RefusingLauncher())
    pre = CommandHook(event="PreToolUse", command=f"touch {marker}", cwd=str(tmp_path), isolation=policy)
    decision = await pre("PreToolUse", {"name": "Edit", "input": {}})
    assert decision is not None and decision.action == "deny", decision
    assert "restrict_self" in (decision.note or "")
    post = CommandHook(event="PostToolUse", command=f"touch {marker}", cwd=str(tmp_path), isolation=policy)
    decision = await post("PostToolUse", {"name": "Edit", "input": {}})
    assert decision is not None and decision.action != "deny"
    assert any("restrict_self" in w for w in decision.warnings)
    assert not marker.exists()


async def test_bash_reports_a_launcher_refusal_as_not_run(tmp_path):
    out = await _bash(_policy(tmp_path, backend_impl=_RefusingLauncher()), "echo hi")
    assert out.is_error and out.text.startswith("Not run") and "restrict_self" in out.text


async def test_code_mode_kernel_reports_a_launcher_refusal(tmp_path):
    from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

    kernel = SubprocessKernel(KernelConfig(isolation=_policy(tmp_path, backend_impl=_RefusingLauncher())))
    try:
        with pytest.raises(isolation.IsolationUnavailable, match="restrict_self"):
            await kernel.start()
        assert kernel._proc.returncode is not None  # nothing left running
    finally:
        await kernel.stop()


class _MiniLoop:
    """The minimal ``loop`` surface ``CodeModeSession.run_python`` reads."""

    def __init__(self, registry) -> None:
        from fluid.permissions.engine import PermissionEngine

        self.registry = registry
        self.ctx = ToolCtx()
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")

    async def _emit(self, event) -> None:
        pass


async def test_a_refused_kernel_restart_is_a_not_run_result_and_keeps_the_checkpoint(tmp_path):
    # P6: the restart path reports a refusal the way the first start does.
    from fluid.codemode.session import CodeModeSession
    from fluid.codemode.subprocess_kernel import KernelConfig
    from fluid.tools.registry import ToolRegistry

    policy = _policy(tmp_path, backend_impl=_FakeLauncher())
    registry = ToolRegistry()
    session = CodeModeSession(registry, config=KernelConfig(isolation=policy))
    loop = _MiniLoop(registry)

    async def run(code):
        return await session.run_python(sid="s", turn_id="t", input={"code": code, "timeout": 20}, loop=loop)

    try:
        first = await run("x = 41")
        assert not first.is_error, first.text
        await session._kernel._kill()  # the kernel dies between cells
        policy.backend = _RefusingLauncher()
        refused = await run("x + 1")
        assert refused.is_error and refused.text.startswith("Not run") and "restrict_self" in refused.text
        policy.backend = _FakeLauncher()
        again = await run("x + 1")
        assert not again.is_error and "42" in again.text, again.text
    finally:
        await session.aclose()


# S8: Task and Swarm workers inherit the effective policy.


async def test_a_worker_inherits_the_parent_policy_with_its_own_private_dir(tmp_path):
    from fluid.cli.host import CliHost, Worker
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    from tests.unit.test_tool_loop import ScriptedProvider

    store = MemoryStore()
    meta = await store.create_session()
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    # Config leaves the profile off; the host opts in on the ctx before the build.
    policy = _policy(tmp_path, backend_impl=_StubBackend())
    provider = ScriptedProvider([])
    engine = build_engine(config, store, provider=provider, session_id=meta.session_id,
                          ctx=ToolCtx(extra={isolation.EXTRA_KEY: policy}))
    assert isolation.policy_of(engine.ctx) is policy
    host = CliHost(config, store, provider=provider)
    host.attach(engine)
    child = None
    try:
        child = await host._build_child(None, Worker(task_id="t", kind="task", description="d"))
        inherited = isolation.policy_of(child.ctx)
        assert inherited is not None and inherited is not policy
        assert inherited.workspace == policy.workspace and inherited.network == policy.network
        assert inherited.private_dir() != policy.private_dir()
    finally:
        if child is not None:
            await child.aclose()
        await host.aclose()
        await engine.aclose()


# S9 (introduced by this change): a cancelled first start must not orphan the kernel.


async def test_a_cancelled_kernel_start_stops_the_process(monkeypatch):
    from fluid.codemode import session as session_mod

    made = []

    class _SlowKernel:
        def __init__(self, config):
            self.stopped = False
            made.append(self)

        async def start(self):
            await asyncio.Event().wait()

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(session_mod, "SubprocessKernel", _SlowKernel)
    sess = object.__new__(session_mod.CodeModeSession)
    sess._kernel, sess._config, sess._skills = None, None, None
    sess._projection = lambda: type("P", (), {"sources": staticmethod(dict)})()

    async def no_skills():
        return {}

    sess._mountable_skills = no_skills
    task = asyncio.create_task(sess._ensure_kernel())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made and made[0].stopped and sess._kernel is None


def test_launch_builds_the_launcher_command(tmp_path):
    policy = _policy(tmp_path, writable_roots=(str(tmp_path / "extra"),), backend_impl=_StubBackend())
    launch = policy.launch(["sh", "-c", "true"], "/x", {"PATH": "/bin", "SECRET_TOKEN": "t"})
    try:
        argv = launch.argv
        assert argv[0] == "launcher" and argv[-3:] == ["sh", "-c", "true"]
        assert f"rw={policy.workspace}" in argv and f"rw={tmp_path / 'extra'}" in argv
        assert "net=False" in argv and launch.cwd == "/x" and launch.env == {}
        assert "SECRET_TOKEN" not in isolation.read_env_file(launch.env_path)
    finally:
        launch.finish()


# P4: one policy per engine, resolved before the build.


def test_a_host_none_does_not_turn_a_configured_profile_off(tmp_path):
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge

    on = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), "code_mode": True,
                                       "sandbox": {"mode": "isolated"},
                                       "hooks": [{"event": "PreToolUse", "command": "true"}]})
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider(),
                          ctx=ToolCtx(extra={isolation.EXTRA_KEY: None}))
    policy = isolation.policy_of(engine.ctx)
    assert isinstance(policy, isolation.IsolationPolicy)
    assert engine._codemode._config.isolation is policy
    hooks = [h for hs in engine.hooks._hooks.values() for h in hs if hasattr(h, "isolation")]
    assert hooks and all(h.isolation is policy for h in hooks)


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


async def test_code_mode_kernel_refuses_to_start(tmp_path):
    from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

    kernel = SubprocessKernel(KernelConfig(isolation=_policy(tmp_path, backend_impl=_NoBackend())))
    with pytest.raises(isolation.IsolationUnavailable):
        await kernel.start()
    assert kernel._proc is None


async def test_command_hook_gate_denies_when_isolation_is_unavailable(tmp_path):
    from fluid.hooks.command import CommandHook

    marker = tmp_path / "hook-ran"
    hook = CommandHook(event="PreToolUse", command=f"touch {marker}", cwd=str(tmp_path),
                       isolation=_policy(tmp_path, backend_impl=_NoBackend()))
    decision = await hook("PreToolUse", {"name": "Edit", "input": {}})
    assert decision is not None and decision.action == "deny"
    assert not marker.exists()


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

    base = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), "code_mode": True})
    engine = build_engine(HarnessConfig.from_dict(base), MemoryStore(), provider=_FakeProvider())
    assert isolation.EXTRA_KEY not in engine.ctx.extra
    assert engine._codemode._config.isolation is None

    on = deep_merge(base, {"sandbox": {"mode": "isolated", "writable_roots": [str(tmp_path / "x")]}})
    engine = build_engine(HarnessConfig.from_dict(on), MemoryStore(), provider=_FakeProvider())
    policy = engine.ctx.extra[isolation.EXTRA_KEY]
    assert isinstance(policy, isolation.IsolationPolicy)
    assert policy.workspace == os.path.realpath(tmp_path) and not policy.network
    assert engine._codemode._config.isolation is policy


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
def test_launcher_refuses_when_landlock_is_unusable(tmp_path):
    # Without Landlock the kernel refuses; with it, a reviewed-ABI ceiling of
    # 0 makes this kernel's ABI "too new". Either way nothing may run.
    marker = tmp_path / "ran"
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import isolation\n"
             "isolation.MAX_ABI = 0\n"
             "sys.exit(isolation.main(sys.argv[2:]))\n")
    env_file, ack_file = tmp_path / "env", tmp_path / "ack"
    env_file.write_bytes(b"PATH=/usr/bin:/bin\0")
    ack_file.write_bytes(b"")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe, os.path.dirname(isolation.SCRIPT),
         "--env", str(env_file), "--ack", str(ack_file),
         "--rw", str(tmp_path / "ws"), "--net", "on", "--", "touch", str(marker)],
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == isolation.REFUSED, proc.stderr
    assert ack_file.read_bytes() == b""  # refused before confinement: no ack
    assert isolation.REFUSAL_PREFIX in proc.stderr and not marker.exists()
    assert ("newer than" in proc.stderr) == bool(ABI), proc.stderr


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
def test_launcher_rejects_a_malformed_policy(tmp_path):
    for args in (["--rw", str(tmp_path), "--", "true"], ["--net", "off", "--"], ["--bogus", "x"]):
        proc = subprocess.run([sys.executable, "-I", "-S", isolation.SCRIPT, *args],
                              capture_output=True, text=True, timeout=30, check=False)
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


@needs_landlock
async def test_code_mode_kernel_runs_confined(tmp_path):
    from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

    kernel = SubprocessKernel(KernelConfig(isolation=_policy(tmp_path)))
    await kernel.start()
    try:
        # Checked from outside the interpreter: the kernel's process carries
        # the no-new-privileges bit and the seccomp filter the launcher set.
        with open(f"/proc/{kernel._proc.pid}/status") as handle:
            status = dict(line.split(":", 1) for line in handle if ":" in line)
        assert status["NoNewPrivs"].strip() == "1"
        assert status["Seccomp"].strip() == "2"

        async def no_tools(*_args, **_kwargs):
            raise AssertionError("no tool calls expected")

        result = await kernel.exec("6 * 7", timeout=10, on_host_call=no_tools)
        assert result.error is None and result.value_repr == "42", result.error
    finally:
        await kernel.stop()
