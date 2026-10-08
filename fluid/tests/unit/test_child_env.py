"""Agent subprocesses inherit the host environment minus host secrets."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fluid.engine.delegated import DelegatedLoop
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.child_env import (
    PASSTHROUGH_ENV,
    UNSET_ENV,
    child_env_with_overlay,
    scrub_child_env,
)
from fluid.tools.registry import ToolCtx

HOST = {
    "PATH": "/usr/bin", "HOME": "/home/appuser", "LANG": "C.UTF-8", "TERM": "xterm",
    "HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "localhost", "PGID": "100",
    "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN": "life", "ADMIN_PASSWORD": "pw",
    "DATABASE_URL": "postgres://u:p@h/db", "PGPASSWORD": "pg", "POSTGRES_PASSWORD": "pg",
    "SECRET_KEY": "sk", "CLUSTER_SECRET": "cs", "WEBHOOK_SECRET": "ws",
    "SOME_SERVICE_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "aws",
    "ANTHROPIC_API_KEY": "ant", "FAL_KEY": "fal", "GH_TOKEN": "gh",
}


def test_scrub_keeps_tool_basics_and_drops_host_secrets() -> None:
    env = scrub_child_env(HOST)
    for kept in ("PATH", "HOME", "LANG", "TERM", "HTTPS_PROXY", "NO_PROXY", "PGID",
                 "ANTHROPIC_API_KEY", "FAL_KEY", "GH_TOKEN"):
        assert env[kept] == HOST[kept]
    for dropped in ("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "ADMIN_PASSWORD", "DATABASE_URL",
                    "PGPASSWORD", "POSTGRES_PASSWORD", "SECRET_KEY", "CLUSTER_SECRET",
                    "WEBHOOK_SECRET", "SOME_SERVICE_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        assert dropped not in env


def test_database_and_auth_secrets_cannot_use_passthrough() -> None:
    names = ("DSN", "app_dsn", "DATABASE_URL", "PGPASSWORD", "SESSION_SECRET",
             "JWT_SECRET", "AUTH_SECRET", "SHUFFLE_DESKTOP_ATTACH_TOKEN")
    source = {name: "private-sentinel" for name in names}
    source[PASSTHROUGH_ENV] = ",".join(names) + ",NPM_TOKEN"
    source[UNSET_ENV] = "ADMIN_PASSWORD"
    source["NPM_TOKEN"] = "deploy-token"
    env = scrub_child_env(source)
    assert not set(names) & env.keys()
    assert PASSTHROUGH_ENV not in env and UNSET_ENV not in env
    assert env["NPM_TOKEN"] == "deploy-token"
    assert source["DSN"] == "private-sentinel"


def test_extra_secret_shapes_are_scrubbed() -> None:
    names = ("APP_COOKIE", "APP_SIGNING_KEY", "APP_ENCRYPTION_KEY", "APP_JWT_KEY")
    assert scrub_child_env({name: "private-sentinel" for name in names}) == {}


def test_scrub_is_case_insensitive_and_honours_passthrough() -> None:
    env = scrub_child_env({"admin_password": "x", "NPM_TOKEN": "n", PASSTHROUGH_ENV: "npm_token"})
    assert "admin_password" not in env
    assert env["NPM_TOKEN"] == "n"


def test_delegated_child_env_scrubs_inherited_but_keeps_handover(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "life")
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.env = {"OPENROUTER_API_KEY": "or", "AIH_GATEWAY_TOKEN": "gw"}
    env = loop._child_env()
    assert "ADMIN_PASSWORD" not in env
    assert "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN" not in env
    # What the caller hands over on purpose is never scrubbed.
    assert env["OPENROUTER_API_KEY"] == "or"
    assert env["AIH_GATEWAY_TOKEN"] == "gw"


@pytest.mark.parametrize("shell", ["bash", pytest.param("powershell", marks=pytest.mark.skipif(
    os.name != "nt", reason="Windows PowerShell"))])
async def test_real_shell_child_drops_dsn_and_auth_secrets(monkeypatch, tmp_path, shell) -> None:
    from fluid.tools.builtins import bash

    names = ("DSN", "APP_DSN", "SESSION_SECRET", "SHUFFLE_DESKTOP_ATTACH_TOKEN")
    for name in names:
        monkeypatch.setenv(name, "private-sentinel")
    script = tmp_path / "env_probe.py"
    script.write_text("import os\nassert not set(" + repr(names) + ") & os.environ.keys()\n",
                      encoding="utf-8")
    command = f'"{sys.executable}" "{script}"'
    if shell == "powershell":
        monkeypatch.setattr(bash, "resolve_shell", lambda: SimpleNamespace(
            argv_prefix=("powershell.exe", "-NoProfile", "-NonInteractive", "-Command")))
        command = "& " + command
    out = await bash_handler({"command": command}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error, out.text


async def test_bash_child_does_not_see_host_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "life-token")
    ctx = ToolCtx(cwd=str(tmp_path))
    ctx.extra["settings_env"] = {"DEPLOY_TOKEN": "handed-over"}
    out = await bash_handler({"command": "env"}, ctx)
    assert "hunter2" not in out.text
    assert "life-token" not in out.text
    assert "DEPLOY_TOKEN=handed-over" in out.text


def test_delegated_child_drops_inherited_names_the_caller_unsets(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-token")
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.env = {UNSET_ENV: "ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN", "FOO": "bar"}
    env = loop._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert UNSET_ENV not in env
    assert env["FOO"] == "bar"


CLOUD_ENV = {
    "AWS_ACCESS_KEY_ID": "fake-aws-id", "AWS_SECRET_ACCESS_KEY": "fake-aws-secret",
    "AWS_SESSION_TOKEN": "fake-aws-session", "AWS_BEARER_TOKEN_BEDROCK": "fake-bedrock-token",
    "AWS_PROFILE": "fake-profile", "AWS_REGION": "us-east-1",
    "GOOGLE_APPLICATION_CREDENTIALS": "/fake/google.json", "CLOUD_ML_REGION": "global",
    "ANTHROPIC_VERTEX_PROJECT_ID": "fake-project", "CLAUDE_CODE_USE_VERTEX": "1",
    "AZURE_CLIENT_SECRET": "fake-entra-secret", "ANTHROPIC_FOUNDRY_API_KEY": "fake-foundry-key",
    "ANTHROPIC_FOUNDRY_RESOURCE": "fake-resource", "CLAUDE_CODE_USE_FOUNDRY": "1",
    "ANTHROPIC_BASE_URL": "https://fake-gateway.invalid", "ANTHROPIC_AUTH_TOKEN": "fake-gateway-token",
    "ANTHROPIC_API_KEY": "fake-gateway-api-key", "ANTHROPIC_CUSTOM_HEADERS": "fake: header",
}


def test_cloud_auth_cannot_use_passthrough_or_tool_overlays():
    source = {**CLOUD_ENV, PASSTHROUGH_ENV: ",".join(CLOUD_ENV), "FAL_KEY": "fake-fal"}
    assert not set(scrub_child_env(source)) & CLOUD_ENV.keys()
    assert not set(child_env_with_overlay(CLOUD_ENV, source)) & CLOUD_ENV.keys()
    assert child_env_with_overlay(CLOUD_ENV, source)["FAL_KEY"] == "fake-fal"


async def test_real_bash_drops_cloud_auth_even_from_settings_overlay(monkeypatch, tmp_path):
    for name, value in CLOUD_ENV.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "cloud_env_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(CLOUD_ENV)) + ") & os.environ.keys()\nprint('clean')\n")
    ctx = ToolCtx(cwd=str(tmp_path), extra={"settings_env": dict(CLOUD_ENV)})
    out = await bash_handler({"command": f'"{sys.executable}" "{script}"'}, ctx)
    assert not out.is_error and out.text.strip() == "clean"


async def test_real_cli_mcp_trampoline_masks_before_launch_and_unsets_for_server(tmp_path):
    import asyncio

    from fluid.tools.mcp.bundled_server import isolated_cli_server_entry

    script = tmp_path / "mcp_env_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(CLOUD_ENV)) + ") & os.environ.keys()\n"
                      "assert os.environ['MCP_SERVICE_TOKEN'] == 'fake-mcp-token'\nprint('clean')\n")
    original = {"type": "stdio", "command": sys.executable, "args": [str(script)],
                "env": {**CLOUD_ENV, "MCP_SERVICE_TOKEN": "fake-mcp-token"}}
    config = isolated_cli_server_entry(original, CLOUD_ENV)
    inherited = {**os.environ, **CLOUD_ENV, **config["env"]}
    # The trampoline itself never sees a cloud credential value.
    assert all(inherited[name] == "" for name in CLOUD_ENV)
    assert original["env"]["ANTHROPIC_AUTH_TOKEN"] == "fake-gateway-token"
    proc = await asyncio.create_subprocess_exec(config["command"], *config["args"],
                                                env=inherited, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode()
    assert stdout.strip() == b"clean"


@pytest.mark.parametrize("surface", ["prompt_git", "runtime_git", "inline_command"])
def test_context_children_run_without_cloud_credentials(monkeypatch, tmp_path, surface):
    import subprocess

    from fluid.context import environment, instructions, runtime_facts

    for name, value in CLOUD_ENV.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "context_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(CLOUD_ENV)) + ") & os.environ.keys()\nprint('clean')\n")
    original = subprocess.run
    def launch(command, *args, **kwargs):
        kwargs.pop("shell", None)
        return original([sys.executable, str(script)], *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", launch)
    if surface == "prompt_git":
        assert environment._run_git(str(tmp_path), "status") == "clean"
    elif surface == "runtime_git":
        assert runtime_facts._receipt_path(tmp_path) == tmp_path / "clean"
    else:
        assert instructions._run_inline_bash("fake-command", str(tmp_path), 5) == "clean"


async def test_native_grep_child_runs_without_cloud_credentials(monkeypatch, tmp_path):
    import asyncio

    from fluid.tools.builtins.grep import _ripgrep

    for name, value in CLOUD_ENV.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "grep_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(CLOUD_ENV)) + ") & os.environ.keys()\nprint('clean.txt:1:clean')\n")
    original = asyncio.create_subprocess_exec
    async def launch(*args, **kwargs):
        return await original(sys.executable, str(script), **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await _ripgrep("fake-rg", "clean", tmp_path, None, False, 5)
    assert not result.is_error
    assert "clean.txt:1:clean" in result.text


@pytest.mark.parametrize("surface", ["listing", "links"])
def test_file_discovery_children_scrub_cloud_credentials(monkeypatch, tmp_path, surface):
    from unittest.mock import Mock

    from fluid.tools.builtins import _util

    for name, value in CLOUD_ENV.items():
        monkeypatch.setenv(name, value)
    popen = Mock(return_value=Mock())
    monkeypatch.setattr(_util.subprocess, "Popen", popen)
    monkeypatch.setattr(_util.shutil, "which", lambda name: "fake-git")
    if surface == "listing":
        _util._rg_listing("fake-rg", tmp_path, _util.WalkState())
    else:
        _util._LinkProbe(tmp_path)
    assert popen.call_count
    for call in popen.call_args_list:
        assert not set(call.kwargs["env"]) & CLOUD_ENV.keys()


async def test_fluid_hook_child_runs_without_cloud_credentials(monkeypatch, tmp_path):
    from fluid.hooks.bus import PRE_TOOL_USE
    from fluid.hooks.command import CommandHook

    for name, value in CLOUD_ENV.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "hook_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(CLOUD_ENV)) + ") & os.environ.keys()\nprint('clean')\n")
    hook = CommandHook(event=PRE_TOOL_USE, command=f'"{sys.executable}" "{script}"', cwd=str(tmp_path), timeout=5)
    code, stdout, stderr = await hook._run(b"{}")
    assert code == 0, stderr
    assert stdout.strip() == "clean"


def test_unset_never_removes_what_the_caller_hands_over() -> None:
    env = child_env_with_overlay(
        {UNSET_ENV: "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY": "user-key"},
        {"ANTHROPIC_API_KEY": "host-key", "PATH": "/bin"},
    )
    assert env["ANTHROPIC_API_KEY"] == "user-key"
    assert env["PATH"] == "/bin"


def test_delegated_child_env_disables_the_clis_background_tasks(monkeypatch) -> None:
    """The loop cannot deliver the between-turn wakes a background job ends in."""
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "0")
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.env = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}
    assert loop._child_env()["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_delegated_child_env_turns_off_the_clis_bash_first_steer(monkeypatch) -> None:
    """Bypass mode must not push the model's reads and edits onto Bash."""
    monkeypatch.setenv("CLAUDE_CODE_THRIFTY_SONIC", "1")
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.env = {"CLAUDE_CODE_THRIFTY_SONIC": "1"}
    assert loop._child_env()["CLAUDE_CODE_THRIFTY_SONIC"] == "0"


def test_delegated_child_env_applies_every_forced_override_last(monkeypatch) -> None:
    """One table drives the child env, the --settings env block and the
    DelegatedSurface record, so they cannot drift apart."""
    from fluid.engine.delegated_proto import FORCED_CHILD_ENV

    assert FORCED_CHILD_ENV == {
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "CLAUDE_CODE_THRIFTY_SONIC": "0",
    }
    for name in FORCED_CHILD_ENV:
        monkeypatch.setenv(name, "host")
    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.env = {name: "overlay" for name in FORCED_CHILD_ENV}
    env = loop._child_env()
    assert {name: env[name] for name in FORCED_CHILD_ENV} == FORCED_CHILD_ENV
