"""The opt-in isolated execution profile (fluid.tools.isolation).

Three groups:

* default off: nothing about spawning changes unless ``[sandbox]`` asks;
* fail closed: a profile that cannot be enforced refuses, it never runs the
  command unconfined;
* enforcement: a confined child tries to break the policy (write outside the
  workspace, read a planted secret, read another process's environ, open a
  socket) and the kernel stops it. These need Landlock, and skip with the
  probe's reason where the kernel lacks it. The seccomp network test needs
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
from fluid.tools import isolation
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.builtins.monitor import handler as monitor_handler
from fluid.tools.registry import ToolCtx
from fluid.tools.shell import resolve_shell
from fluid.providers.base import ProviderCaps
from fluid.store.base import MemoryStore

LINUX = sys.platform.startswith("linux")
ABI, NO_LANDLOCK = isolation.landlock_abi()
needs_landlock = pytest.mark.skipif(not ABI, reason=f"isolated profile not enforceable here: {NO_LANDLOCK}")
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

    def prefix(self, writable, readable, network):
        return ["launcher", *[f"rw={p}" for p in writable], f"net={network}", "--"]


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


@pytest.mark.skipif(not LINUX or bool(ABI), reason="needs a Linux kernel without Landlock")
def test_launcher_refuses_without_landlock(tmp_path):
    marker = tmp_path / "ran"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", isolation.SCRIPT, "--rw", str(tmp_path), "--net", "on", "--",
         "touch", str(marker)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == isolation.REFUSED
    assert isolation.REFUSAL_PREFIX in proc.stderr and not marker.exists()


@pytest.mark.skipif(not LINUX, reason="the launcher is Linux only")
def test_launcher_rejects_a_malformed_policy(tmp_path):
    for args in (["--rw", str(tmp_path), "--", "true"], ["--net", "off", "--"], ["--bogus", "x"]):
        proc = subprocess.run([sys.executable, "-I", "-S", isolation.SCRIPT, *args],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == isolation.REFUSED, (args, proc.stderr)


def test_non_linux_platforms_have_no_backend(tmp_path):
    policy = _policy(tmp_path, backend_impl=isolation.backend("darwin"))
    with pytest.raises(isolation.IsolationUnavailable, match="darwin"):
        policy.check()


# --- enforcement (from inside the child) ------------------------------------------------


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
                          capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "blocked", proc.stderr


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
async def test_child_cannot_read_another_process_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_PARENT_CANARY", "canary-value")
    out = await _bash(_policy(tmp_path), f"cat /proc/{os.getpid()}/environ; echo done")
    assert "canary-value" not in out.text and "done" in out.text, out.text


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
