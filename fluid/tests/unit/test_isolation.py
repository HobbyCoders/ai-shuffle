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
    assert not any(k.startswith(isolation.HELD_PREFIX) for k in seen["env"])


# --- config ------------------------------------------------------------------------


def test_config_validation():
    sandbox = SandboxConfig.from_dict({"mode": "isolated", "network": True, "writable_roots": ["/srv/x"]})
    assert (sandbox.mode, sandbox.network, sandbox.writable_roots) == ("isolated", True, ("/srv/x",))
    assert SandboxConfig.from_dict(sandbox.to_dict()) == sandbox
    for bad in ({"mode": "strict"}, {"network": "off"}, {"writable_roots": "/srv"}, {"protect": [""]}):
        with pytest.raises(ValueError):
            SandboxConfig.from_dict(bad)


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


def test_loader_variables_are_held_until_the_launcher_is_confined():
    held = isolation.hold_loader_env({"LD_PRELOAD": "/ws/evil.so", "GCONV_PATH": "/ws", "PATH": "/bin"})
    assert "LD_PRELOAD" not in held and "GCONV_PATH" not in held
    assert held[isolation.HELD_PREFIX + "LD_PRELOAD"] == "/ws/evil.so"
    # A variable smuggled under the hold prefix is dropped, never promoted.
    assert isolation.HELD_PREFIX + "X" not in isolation.hold_loader_env({isolation.HELD_PREFIX + "X": "1"})
    assert isolation.release_loader_env(held) == {"LD_PRELOAD": "/ws/evil.so", "GCONV_PATH": "/ws",
                                                  "PATH": "/bin"}


def test_a_root_holding_credentials_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    policy = isolation.IsolationPolicy(workspace=str(home), home=str(home), backend_impl=_StubBackend())
    reason = policy.unavailable_reason()
    assert reason and ".ssh" in reason
    with pytest.raises(isolation.IsolationUnavailable):
        policy.wrap(["true"], str(home), {})
    root = isolation.IsolationPolicy(workspace="/", home=str(home), backend_impl=_StubBackend())
    assert "filesystem root" in (root.unavailable_reason() or "")


class _StubBackend:
    name = "stub"

    def unavailable_reason(self, network):
        return None

    def prefix(self, writable, readable, network, protect=()):
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

    def prefix(self, writable, readable, network, protect=()):
        return [sys.executable, "-I", "-c", _REFUSE]


# S1: nothing that runs before confinement may be writable by a confined command.


def test_launcher_runs_from_a_private_copy_outside_every_root(tmp_path):
    # The workspace is the very directory that holds isolation.py (a source
    # checkout or editable install opened as the project).
    source_dir = os.path.dirname(isolation.SCRIPT)
    policy = isolation.IsolationPolicy(workspace=source_dir, home=str(tmp_path / "home"),
                                       backend_impl=_ProbedLandlock())
    argv, _cwd, _env = policy.wrap(["true"], None, {})
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
            policy.wrap(["true"], None, {})


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
        plain.wrap(["true"], None, {}, readable=(str(inner),))


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


def test_launcher_failure_needs_both_the_status_and_the_message():
    assert isolation.launcher_failure(126, "fluid isolation refused: x\n") == "fluid isolation refused: x"
    assert isolation.launcher_failure(126, "sh: ./tool: Permission denied") is None
    assert isolation.launcher_failure(1, "fluid isolation refused: x") is None


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
    finally:
        await kernel.stop()


# S8: Task and Swarm workers inherit the effective policy.


async def test_a_worker_inherits_the_parent_policy_with_its_own_private_dir(tmp_path):
    from fluid.cli.config import host_of, make_engine
    from fluid.cli.host import Worker
    from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge

    from tests.unit.test_tool_loop import ScriptedProvider

    store = MemoryStore()
    meta = await store.create_session()
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    engine = make_engine(config, store, provider=ScriptedProvider([]), session_id=meta.session_id)
    # Config leaves the profile off; the host opted in on the parent's ctx.
    policy = _policy(tmp_path, backend_impl=_StubBackend())
    engine.ctx.extra[isolation.EXTRA_KEY] = policy
    host = host_of(engine)
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


def test_wrap_builds_the_launcher_command(tmp_path):
    policy = _policy(tmp_path, writable_roots=(str(tmp_path / "extra"),), backend_impl=_StubBackend())
    argv, cwd, env = policy.wrap(["sh", "-c", "true"], "/x", {"PATH": "/bin", "SECRET_TOKEN": "t"})
    assert argv[0] == "launcher" and argv[-3:] == ["sh", "-c", "true"]
    assert f"rw={policy.workspace}" in argv and f"rw={tmp_path / 'extra'}" in argv
    assert "net=False" in argv and cwd == "/x" and "SECRET_TOKEN" not in env


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
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe, os.path.dirname(isolation.SCRIPT),
         "--rw", str(tmp_path), "--net", "on", "--", "touch", str(marker)],
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == isolation.REFUSED, proc.stderr
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
