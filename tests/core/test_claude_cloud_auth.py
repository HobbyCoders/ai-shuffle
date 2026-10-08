"""The "Cloud provider or gateway" Claude mode (issue #1862).

Off by default, one named method at a time, only the owner's stored values,
never a host or profile value, and never the subscription token or the
Anthropic key alongside. All values here are fake; nothing calls a cloud.
"""

import json
import logging
import os
import stat
import sys
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

import pytest
from fluid.tools.child_env import (
    UNSET_ENV,
    child_env_with_overlay,
    claude_cli_env_with_overlay,
)

from app.core import claude_auth_mode, credential_gate
from app.core import claude_cloud_auth as cloud
from app.core.environment import inject_claude_code_auth
from app.db import database

FAKE = {
    "aws_id": "AKIAFAKEFAKEFAKE0000",
    "aws_secret": "fake-aws-secret-0000000000000000000000",
    "aws_session": "fake-aws-session-token-000",
    "bedrock_key": "fake-bedrock-api-key-000",
    "foundry_key": "fake-foundry-api-key-000",
    "gateway_token": "fake-gateway-token-000",
    "vertex_json": json.dumps({"type": "service_account", "private_key": "fake-private-key-000"}),
}

# Everything a host or a profile might carry for any method, plus the other
# Claude credentials. None of it may reach the CLI unless the owner stored it.
HOST = {
    "PATH": "/usr/bin", "HOME": "/home/agent", "LANG": "C.UTF-8",
    "AWS_REGION": "host-region-1", "AWS_DEFAULT_REGION": "host-region-2", "AWS_PROFILE": "host-profile",
    "AWS_ACCESS_KEY_ID": "AKIAHOSTHOSTHOST", "AWS_SECRET_ACCESS_KEY": "host-aws-secret",
    "AWS_SESSION_TOKEN": "host-aws-session", "AWS_BEARER_TOKEN_BEDROCK": "host-bedrock-key",
    "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLOUD_ML_REGION": "host-ml-region", "ANTHROPIC_VERTEX_PROJECT_ID": "host-project",
    "GOOGLE_APPLICATION_CREDENTIALS": "/host/creds.json", "VERTEX_REGION_CLAUDE_X": "host-vr",
    "ANTHROPIC_FOUNDRY_RESOURCE": "host-resource", "ANTHROPIC_FOUNDRY_API_KEY": "host-foundry-key",
    "ANTHROPIC_FOUNDRY_BASE_URL": "https://host-foundry.example",
    "ANTHROPIC_BASE_URL": "https://host-gateway.example", "ANTHROPIC_AUTH_TOKEN": "host-gateway-token",
    "ANTHROPIC_API_KEY": "sk-ant-host", "CLAUDE_CODE_OAUTH_TOKEN": "host-oauth",
}

ALL_CLOUD_NAMES = {n for m in cloud.METHODS.values() for n in m.allowlist}


@pytest.fixture
def store(mock_db, _api_unit_credentials, monkeypatch, tmp_path):
    files = tmp_path / "claude-cloud"
    monkeypatch.setattr(cloud, "_files_dir", lambda: files)
    return files


def _cli_env(profile=None, host=HOST):
    # The real host env is os.environ: the cloud branch reads it for the
    # VERTEX_REGION_* names it must also remove.
    with patch.dict(os.environ, host):
        overlay = inject_claude_code_auth(dict(profile or {}))
    return claude_cli_env_with_overlay(overlay, host)


def _unset(env):
    return set(filter(None, env.get(UNSET_ENV, "").split(",")))


# ─── off by default ───────────────────────────────────────────────────────

def test_cloud_is_never_the_default_mode(store):
    assert claude_auth_mode.ensure_mode_recorded(cli_signed_in=False) == "subscription"
    assert claude_auth_mode.default_mode(key_stored=False, cli_signed_in=None) == "subscription"
    assert claude_auth_mode.default_mode(key_stored=True, cli_signed_in=False) == "api_key"


def test_default_install_cli_env_is_unchanged(store):
    """Regression: subscription mode, even with a cloud method saved, passes the
    host env exactly as before (only the two Anthropic names removed)."""
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("subscription")
    overlay = inject_claude_code_auth({})
    assert overlay == {UNSET_ENV: "ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN"}
    env = child_env_with_overlay(overlay, HOST)
    expected = {k: v for k, v in child_env_with_overlay({}, HOST).items()
                if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    assert env == expected
    assert FAKE["gateway_token"] not in json.dumps(env)


def test_api_key_mode_env_is_unchanged(store):
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-fake-ABCD")
    cloud.save("bedrock", {"region": "us-east-1", "bearer_token": FAKE["bedrock_key"]})
    claude_auth_mode.set_mode("api_key")
    overlay = inject_claude_code_auth({})
    assert overlay == {"ANTHROPIC_API_KEY": "sk-ant-api03-fake-ABCD",
                       UNSET_ENV: "ANTHROPIC_AUTH_TOKEN,CLAUDE_CODE_OAUTH_TOKEN"}


# ─── one method at a time, stored values only ─────────────────────────────

METHOD_CASES = {
    "bedrock_keys": ("bedrock", {"region": "eu-west-1", "access_key_id": FAKE["aws_id"],
                                 "secret_access_key": FAKE["aws_secret"], "session_token": FAKE["aws_session"]},
                     {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1",
                      "AWS_ACCESS_KEY_ID": FAKE["aws_id"], "AWS_SECRET_ACCESS_KEY": FAKE["aws_secret"],
                      "AWS_SESSION_TOKEN": FAKE["aws_session"]}),
    "bedrock_api_key": ("bedrock", {"region": "us-east-1", "bearer_token": FAKE["bedrock_key"]},
                        {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1",
                         "AWS_BEARER_TOKEN_BEDROCK": FAKE["bedrock_key"]}),
    "foundry": ("foundry", {"resource": "my-resource", "api_key": FAKE["foundry_key"]},
                {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "my-resource",
                 "ANTHROPIC_FOUNDRY_API_KEY": FAKE["foundry_key"]}),
    "foundry_url": ("foundry", {"base_url": "https://my-resource.services.ai.azure.com/anthropic",
                                "api_key": FAKE["foundry_key"]},
                    {"CLAUDE_CODE_USE_FOUNDRY": "1",
                     "ANTHROPIC_FOUNDRY_BASE_URL": "https://my-resource.services.ai.azure.com/anthropic",
                     "ANTHROPIC_FOUNDRY_API_KEY": FAKE["foundry_key"]}),
    "gateway": ("gateway", {"base_url": "https://gw.example/v1", "auth_token": FAKE["gateway_token"]},
                {"ANTHROPIC_BASE_URL": "https://gw.example/v1", "ANTHROPIC_AUTH_TOKEN": FAKE["gateway_token"]}),
}


@pytest.mark.parametrize("case", sorted(METHOD_CASES))
def test_each_method_passes_exactly_its_stored_allowlist(store, case):
    method, values, expected = METHOD_CASES[case]
    cloud.save(method, values)
    claude_auth_mode.set_mode("cloud")
    env = _cli_env(profile={k: "profile-" + k for k in HOST if k not in ("PATH", "HOME", "LANG")})
    passed = {k: v for k, v in env.items() if k in ALL_CLOUD_NAMES or k in HOST and k not in ("PATH", "HOME", "LANG")}
    assert passed == expected
    assert set(expected) <= set(cloud.METHODS[method].allowlist)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_REGION",
                 "VERTEX_REGION_CLAUDE_X"):
        assert name not in env
    assert "host-" not in json.dumps(env) and "profile-" not in json.dumps(env)
    assert env["PATH"] == "/usr/bin"


def test_vertex_writes_an_owner_only_credential_file(store):
    cloud.save("vertex", {"region": "global", "project_id": "my-project-123",
                          "credentials_json": FAKE["vertex_json"]})
    claude_auth_mode.set_mode("cloud")
    env = _cli_env()
    passed = {k: v for k, v in env.items() if k in ALL_CLOUD_NAMES}
    assert set(passed) == {"CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
                           "GOOGLE_APPLICATION_CREDENTIALS"}
    assert passed["CLOUD_ML_REGION"] == "global" and passed["ANTHROPIC_VERTEX_PROJECT_ID"] == "my-project-123"
    path = passed["GOOGLE_APPLICATION_CREDENTIALS"]
    assert path.startswith(str(store)) and open(path).read() == FAKE["vertex_json"]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Switching back removes the file; the saved settings stay.
    claude_auth_mode.set_mode("subscription")
    assert not os.path.exists(path)
    assert cloud.status()["ready"] is True


def test_cloud_mode_without_settings_or_worker_configuration_passes_no_credential(store):
    claude_auth_mode.set_mode("cloud")
    # Orphan cloud keys without a selector/endpoint are not a configuration.
    host = {name: value for name, value in HOST.items() if not (
        name.startswith("CLAUDE_CODE_USE_") or name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    )}
    env = _cli_env(profile={"AWS_REGION": "profile-region"}, host=host)
    assert not (set(env) & (ALL_CLOUD_NAMES | {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}))
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} <= _unset(inject_claude_code_auth({}))


def test_switching_back_restores_todays_behaviour(store):
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    assert inject_claude_code_auth({})["ANTHROPIC_AUTH_TOKEN"] == FAKE["gateway_token"]
    claude_auth_mode.set_mode("subscription")
    assert inject_claude_code_auth({}) == {UNSET_ENV: "ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN"}


@pytest.mark.parametrize("case", sorted(METHOD_CASES) + ["vertex"])
async def test_saved_method_is_private_to_the_delegated_spawn(store, monkeypatch, tmp_path, case):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolRegistry

    from app.core import environment
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    if case == "vertex":
        method, values = "vertex", {"region": "global", "project_id": "fake-project",
                                    "credentials_json": FAKE["vertex_json"]}
    else:
        method, values, _ = METHOD_CASES[case]
    cloud.save(method, values)
    claude_auth_mode.set_mode("cloud")
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    shared = environment._build_env_with_ai_tools(HOST, None)
    assert not set(shared) & ALL_CLOUD_NAMES
    options = SessionOptions(model="opus", cwd=str(tmp_path), env=shared,
                             extra={"fluid_store": MemoryStore(), "fluid_registry": ToolRegistry()})
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    try:
        assert isinstance(adapter._engine, DelegatedLoop)
        from unittest.mock import AsyncMock

        captured = {}
        async def spawn(*argv, **kwargs):
            captured.update(kwargs["env"])
            raise RuntimeError("captured-claude-spawn")
        monkeypatch.setattr(adapter._engine, "_version_guard", AsyncMock())
        monkeypatch.setattr(adapter._engine, "_build_argv", lambda: ["claude", "--fake-in-test"])
        monkeypatch.setattr("fluid.engine.delegated.asyncio.create_subprocess_exec", spawn)
        with pytest.raises(RuntimeError, match="captured-claude-spawn"):
            await adapter._engine._ensure_process()
        expected = cloud.env_for_cli()
        assert {name: captured[name] for name in expected} == expected
        assert not set(options.env) & ALL_CLOUD_NAMES
        assert not set(child_env_with_overlay(adapter._engine.env, HOST)) & ALL_CLOUD_NAMES
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("method,env", [
    ("bedrock", {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1", "AWS_PROFILE": "fake-sso"}),
    ("bedrock", {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ACCESS_KEY_ID": FAKE["aws_id"],
                 "AWS_SECRET_ACCESS_KEY": FAKE["aws_secret"], "AWS_SESSION_TOKEN": FAKE["aws_session"]}),
    ("vertex", {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "global",
                "ANTHROPIC_VERTEX_PROJECT_ID": "fake-project", "CLOUDSDK_CONFIG": "/fake/adc"}),
    ("vertex", {"CLAUDE_CODE_USE_VERTEX": "1", "GOOGLE_APPLICATION_CREDENTIALS": "/fake/key.json",
                "VERTEX_REGION_CLAUDE_SONNET_4_6": "global"}),
    ("foundry", {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "fake-resource",
                 "AZURE_CLIENT_ID": "fake-client", "AZURE_TENANT_ID": "fake-tenant",
                 "AZURE_CLIENT_SECRET": "fake-entra-secret"}),
    ("foundry", {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "fake-resource",
                 "ANTHROPIC_FOUNDRY_API_KEY": FAKE["foundry_key"]}),
    ("gateway", {"ANTHROPIC_BASE_URL": "https://fake-gateway.invalid",
                 "ANTHROPIC_AUTH_TOKEN": FAKE["gateway_token"]}),
    ("gateway", {"ANTHROPIC_BASE_URL": "https://fake-gateway.invalid",
                 "ANTHROPIC_API_KEY": "fake-gateway-api-key", "ANTHROPIC_CUSTOM_HEADERS": "fake: header"}),
])
@pytest.mark.real_agent_gate
def test_worker_environment_methods_are_cli_only_and_report_cloud(store, method, env):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.tools.child_env import scrub_child_env

    from app.core import environment, generation_options
    from app.core.terminals import shell_environment

    claude_auth_mode.set_mode("subscription")
    # Only obvious fakes. This test never reads an ADC/AWS/Entra file or account.
    with patch.dict(os.environ, {"PATH": "/usr/bin", **env, "PRIVATE_SERVICE_TOKEN": "fake-unrelated"}, clear=True):
        assert claude_auth_mode.current_mode() == "cloud"
        assert cloud.status()["method"] == method and cloud.status()["ready"]
        assert "worker environment" in cloud.status()["method_label"]
        assert credential_gate.claude_credential_ready({"installed": True, "signed_in": False}) == (True, None)
        assert claude_auth_mode.summary({"installed": True, "signed_in": False})["mode"] == "cloud"
        loop = DelegatedLoop.__new__(DelegatedLoop)
        loop.env = inject_claude_code_auth({})
        actual = loop._child_env()
        assert {name: actual[name] for name in env} == env
        assert "PRIVATE_SERVICE_TOKEN" not in actual
        for child in (scrub_child_env(), child_env_with_overlay(env),
                      environment._build_env_with_ai_tools(env, None),
                      generation_options._resolve_env({"env": env}),
                      shell_environment("bash", "/bin/bash")):
            assert not set(child) & set(env)
        claude_auth_mode.set_mode("off")
        assert claude_auth_mode.current_mode() == "off"
        assert not set(inject_claude_code_auth({})) & set(env)


async def test_native_loop_never_prepares_or_receives_cloud_auth(store, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolRegistry

    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    class FakeProvider:
        name = "fake"
        caps = ProviderCaps()

    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    monkeypatch.setattr(ha, "_build_provider_client", AsyncMock(return_value=(FakeProvider(), None)))
    monkeypatch.setattr(cloud, "env_for_cli", lambda: pytest.fail("NativeLoop requested CLI auth"))
    options = SessionOptions(model="fake-model", cwd=str(tmp_path), env=dict(HOST),
                             extra={"fluid_provider": "openrouter", "fluid_store": MemoryStore(),
                                    "fluid_registry": ToolRegistry()})
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    try:
        assert isinstance(adapter._engine, NativeLoop)
        assert not set(adapter._engine.ctx.extra.get("settings_env", {})) & ALL_CLOUD_NAMES
    finally:
        await adapter.disconnect()


async def test_real_monitor_shell_has_no_cloud_auth(store, monkeypatch, tmp_path):
    import asyncio
    import sys

    from app.core import process_ledger
    from app.mcp import builtin_tools

    for name, value in HOST.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(process_ledger, "record_process", lambda *args, **kwargs: None)
    script = tmp_path / "monitor_env_probe.py"
    script.write_text("import os\nassert not set(" + repr(tuple(ALL_CLOUD_NAMES)) + ") & os.environ.keys()\nprint('clean')\n")
    task = await builtin_tools._spawn_background(f'"{sys.executable}" "{script}"', str(tmp_path), None)
    try:
        await asyncio.wait_for(task._reader_task, 10)
        assert task.exit_code == 0
        assert "clean" in task.lines
    finally:
        builtin_tools._background_tasks.pop(task.task_id, None)


async def test_node_plugin_process_drops_cloud_overlay_and_keeps_its_own_key(store, monkeypatch):
    from app.mcp.node_worker import NodeWorker

    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("captured-before-start")
    monkeypatch.setattr("app.mcp.node_worker.asyncio.create_subprocess_exec", spawn)
    with patch.dict(os.environ, HOST):
        worker = NodeWorker("fake-worker.mjs", env={**HOST, "FAL_KEY": "fake-fal-key"})
        with pytest.raises(RuntimeError, match="captured-before-start"):
            await worker._start()
    assert not set(captured) & ALL_CLOUD_NAMES
    assert captured["FAL_KEY"] == "fake-fal-key"


async def test_imessage_child_does_not_inherit_cloud_credentials(store, monkeypatch):
    from app.core.connectors.imessage import IMessageRpcClient

    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("captured-before-start")
    monkeypatch.setattr("app.core.connectors.imessage.asyncio.create_subprocess_exec", spawn)
    with patch.dict(os.environ, HOST), pytest.raises(RuntimeError, match="captured-before-start"):
        await IMessageRpcClient("fake-imsg").start()
    assert not set(captured) & ALL_CLOUD_NAMES


def test_selected_settings_method_wins_over_worker_environment_without_fallback(store):
    cloud.save("gateway", {"base_url": "https://saved.invalid", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "fake-sso"}):
        assert cloud.env_for_cli() == {"ANTHROPIC_BASE_URL": "https://saved.invalid",
                                      "ANTHROPIC_AUTH_TOKEN": FAKE["gateway_token"]}
        database.delete_system_setting(cloud.SECRETS_SETTING)
        assert cloud.env_for_cli() == {}  # never fall back to the AWS account


def test_app_owned_subprocess_launches_never_default_to_inheritance():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    missing = []
    checked = 0
    for area in ("app", "fluid/fluid"):
        for path in (root / area).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                wrapped = bool(node.args) and isinstance(node.args[0], ast.Attribute) and ast.unparse(node.args[0]) == "subprocess.run"
                subprocess_call = name.startswith("subprocess.") and name.split(".")[-1] in {
                    "run", "Popen", "call", "check_output", "check_call",
                }
                if wrapped or subprocess_call or name.endswith((".create_subprocess_exec", ".create_subprocess_shell")):
                    checked += 1
                    if not any(keyword.arg == "env" for keyword in node.keywords):
                        missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert checked > 0
    assert missing == [], "App-owned launches must supply a scrubbed environment: " + ", ".join(missing)


@pytest.mark.parametrize("explicit", [False, True])
def test_git_service_children_scrub_inheritance_and_explicit_maps(store, monkeypatch, explicit):
    from unittest.mock import Mock

    from app.core.git_service import GitService

    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr("app.core.git_service.subprocess.run", run)
    with patch.dict(os.environ, HOST):
        GitService()._run_git("fake-repo", ["status"], env={**HOST, "GIT_ASKPASS": "fake-askpass"} if explicit else None)
    assert not set(run.call_args.kwargs["env"]) & ALL_CLOUD_NAMES
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    if explicit:
        assert run.call_args.kwargs["env"]["GIT_ASKPASS"] == "fake-askpass"


def test_damaged_selected_settings_never_fall_back_to_worker_env(store):
    claude_auth_mode.set_mode("cloud")
    database.set_system_setting(cloud.CONFIG_SETTING, "damaged-not-json")
    with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "fake-sso"}):
        assert not cloud.status()["ready"]
        assert cloud.env_for_cli() == {}


def _mcp_module_cache():
    return {name: module for name, module in sys.modules.copy().items()
            if name == "mcp" or name.startswith("mcp.")}


@contextmanager
def _preserve_mcp_modules():
    original = _mcp_module_cache()
    try:
        yield
    finally:
        for name in _mcp_module_cache().keys() - original.keys():
            sys.modules.pop(name, None)
        sys.modules.update(original)


@pytest.mark.parametrize("cache_state", ["cold", "parent_present", "parent_absent"])
@pytest.mark.parametrize("fail_inside", [False, True])
def test_mcp_module_cache_restored_after_reimport(cache_state, fail_inside):
    # Preserve even the regression's setup imports, not just its inner reimport.
    incoming = _mcp_module_cache()
    with _preserve_mcp_modules():
        import importlib
        from types import ModuleType

        stdio = parent = None
        if cache_state == "cold":
            for name in _mcp_module_cache():
                sys.modules.pop(name)
        else:
            stdio = importlib.import_module("mcp.client.stdio")
            parent = importlib.import_module("mcp")
            if cache_state == "parent_absent":
                sys.modules.pop("mcp")
                assert "mcp" not in sys.modules
                assert importlib.import_module("mcp.client.stdio") is stdio

        original = _mcp_module_cache()
        expected = pytest.raises(RuntimeError, match="injected failure") if fail_inside else nullcontext()
        with expected, _preserve_mcp_modules():
            sys.modules.pop("mcp", None)
            recreated = importlib.import_module("mcp")
            assert recreated is not parent
            loaded_stdio = importlib.import_module("mcp.client.stdio")
            if stdio is not None:
                assert loaded_stdio is stdio
            # Cover both a removed original child and an introduced descendant.
            sys.modules.pop("mcp.client.stdio")
            sys.modules["mcp._regression_only"] = ModuleType("mcp._regression_only")
            if fail_inside:
                raise RuntimeError("injected failure")

        restored = _mcp_module_cache()
        assert restored.keys() == original.keys()
        assert all(restored[name] is module for name, module in original.items())

    restored = _mcp_module_cache()
    assert restored.keys() == incoming.keys()
    assert all(restored[name] is module for name, module in incoming.items())


@pytest.mark.parametrize("reimport_parent", [False, True])
def test_app_mcp_browser_plugin_and_connector_envs_do_not_inherit_cloud_auth(store, monkeypatch, tmp_path, reimport_parent):
    with _preserve_mcp_modules():
        import importlib
        from unittest.mock import Mock

        from fluid.tools.mcp.client import McpClient

        from app.core import mcp_config, native_chrome, plugin_install
        from app.core.connectors.package_proxy import worker_environment

        # A retried parent import can leave cached children without corresponding
        # parent attributes. Patch the module the transport imports, not traversal
        # through mcp.client attributes (which pytest's string resolver requires).
        stdio = importlib.import_module("mcp.client.stdio")
        if reimport_parent:
            # The cached SDK children may already outlive an absent parent in CI.
            sys.modules.pop("mcp", None)
            assert "mcp" not in sys.modules
            parent = importlib.import_module("mcp")
            assert not hasattr(parent, "client")
            assert importlib.import_module("mcp.client.stdio") is stdio
        with patch.dict(os.environ, HOST):
            fake_stdio = Mock(return_value=object())
            monkeypatch.setattr(stdio, "stdio_client", fake_stdio)
            client = McpClient("fake-mcp", {"command": "fake", "env": {**HOST, "MCP_SERVICE_TOKEN": "fake-mcp-token"}})
            client._open_transport()
            env = fake_stdio.call_args.args[0].env
            assert not set(env) & ALL_CLOUD_NAMES
            assert env["MCP_SERVICE_TOKEN"] == "fake-mcp-token"
            assert not set(native_chrome._egress_env()) & ALL_CLOUD_NAMES
            assert not set(worker_environment()) & ALL_CLOUD_NAMES
            browser = mcp_config._build_browser_server_env(HOST)
            assert not set(browser) & ALL_CLOUD_NAMES
            run = Mock(return_value=Mock(returncode=0))
            monkeypatch.setattr(plugin_install.subprocess, "run", run)
            plugin_install._clone_plugin_source(None, {"source": "url", "url": "https://github.com/fake/plugin.git"}, tmp_path)
            assert not set(run.call_args.kwargs["env"]) & ALL_CLOUD_NAMES


@pytest.mark.parametrize("method", tuple(cloud.CLI_ENV_ALLOWLISTS))
def test_every_worker_method_allowlist_name_reaches_only_cli(store, method):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.tools.child_env import scrub_child_env

    env = {name: "fake-allowlisted-value" for name in cloud.CLI_ENV_ALLOWLISTS[method]}
    env.update(dict(cloud.METHODS[method].selector))
    with patch.dict(os.environ, env, clear=True):
        overlay = inject_claude_code_auth({})
        loop = DelegatedLoop.__new__(DelegatedLoop)
        loop.env = overlay
        actual = loop._child_env()
        assert {name: actual[name] for name in env} == env
        assert not set(scrub_child_env()) & set(env)
        assert not set(child_env_with_overlay(env)) & set(env)


def test_conflicting_environment_selectors_fail_closed(store):
    with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1"}):
        assert not cloud.status()["ready"]
        with pytest.raises(cloud.CloudConfigError):
            cloud.environment_env_for_cli()
        from app.core.environment import ClaudeCloudSetupError
        with pytest.raises(ClaudeCloudSetupError):
            inject_claude_code_auth({})


# ─── storage ──────────────────────────────────────────────────────────────

def test_secrets_are_stored_encrypted_and_never_returned(store, caplog):
    caplog.set_level(logging.DEBUG)
    out = cloud.save("bedrock", {"region": "us-east-1", "access_key_id": FAKE["aws_id"],
                                 "secret_access_key": FAKE["aws_secret"]})
    raw = database.get_system_setting(cloud.SECRETS_SETTING)
    assert raw.startswith("enc:") and FAKE["aws_secret"] not in raw
    assert FAKE["aws_secret"] not in database.get_system_setting(cloud.CONFIG_SETTING)
    for payload in (out, cloud.public_config(), cloud.status(), claude_auth_mode.summary({})):
        text = json.dumps(payload)
        assert FAKE["aws_secret"] not in text and FAKE["aws_id"] not in text
    assert out["secrets"]["secret_access_key"] == {"set": True}
    assert out["secrets"]["bearer_token"] == {"set": False}
    assert out["values"] == {"region": "us-east-1"}
    claude_auth_mode.set_mode("cloud")
    inject_claude_code_auth({})
    assert FAKE["aws_secret"] not in caplog.text and FAKE["aws_id"] not in caplog.text


def test_empty_secret_keeps_the_stored_one_and_other_alternative_drops_it(store):
    cloud.save("bedrock", {"region": "us-east-1", "access_key_id": FAKE["aws_id"],
                           "secret_access_key": FAKE["aws_secret"]})
    cloud.save("bedrock", {"region": "us-west-2", "access_key_id": "", "secret_access_key": None})
    cfg = cloud.stored()
    assert cfg["values"] == {"region": "us-west-2"}
    assert cfg["secrets"]["secret_access_key"] == FAKE["aws_secret"]
    cloud.save("bedrock", {"region": "us-west-2", "bearer_token": FAKE["bedrock_key"]})
    assert cloud.stored()["secrets"] == {"bearer_token": FAKE["bedrock_key"]}
    with pytest.raises(cloud.CloudConfigError):
        cloud.save("bedrock", {"region": "us-west-2", "bearer_token": "x", "access_key_id": "y",
                               "secret_access_key": "z"})


def test_choosing_another_method_drops_the_previous_secrets(store):
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    with pytest.raises(cloud.CloudConfigError, match="API key"):
        cloud.save("foundry", {"resource": "res"})  # the gateway token does not carry over
    cloud.save("foundry", {"resource": "res", "api_key": FAKE["foundry_key"]})
    assert cloud.stored()["secrets"] == {"api_key": FAKE["foundry_key"]}


def test_clear_deletes_settings_secrets_and_files(store):
    cloud.save("vertex", {"region": "global", "project_id": "p-123456", "credentials_json": FAKE["vertex_json"]})
    claude_auth_mode.set_mode("cloud")
    path = inject_claude_code_auth({})["GOOGLE_APPLICATION_CREDENTIALS"]
    assert os.path.exists(path)
    assert cloud.clear() is True
    assert database.get_system_setting(cloud.CONFIG_SETTING) is None
    assert database.get_system_setting(cloud.SECRETS_SETTING) is None
    assert not os.path.exists(path)
    assert cloud.status() == {"method": None, "method_label": None, "configured": False, "ready": False,
                              "missing": ["a cloud provider or gateway"], "storage_locked": False}


def test_save_refuses_when_credential_storage_is_locked(store, monkeypatch):
    from app.core import encryption

    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: False)
    with pytest.raises(cloud.CloudStorageLocked):
        cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    assert database.get_system_setting(cloud.SECRETS_SETTING) is None


# ─── validation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://gw.example", "https://gw.example:8443/anthropic/", "http://localhost:4000",
    "http://127.0.0.1:4000/v1", "http://[::1]:4000",
])
def test_base_url_accepted(url):
    assert cloud.validate_base_url(url) == url


@pytest.mark.parametrize("url,reason", [
    ("http://gw.example", "https"),
    ("http://10.0.0.5:4000", "https"),
    ("ftp://gw.example", "https"),
    ("https://user:pass@gw.example", "user name or password"),
    ("https://token@gw.example", "user name or password"),
    ("https://gw.example/?key=x", "query"),
    ("https://gw.example/?", "query"),
    ("https://gw.example/#frag", "fragment"),
    ("https:///path", "host"),
    ("https://gw.example/a b", "spaces"),
    ("", "Enter"),
])
def test_base_url_rejected(url, reason):
    with pytest.raises(cloud.CloudConfigError, match=reason):
        cloud.validate_base_url(url)


def test_other_field_validation(store):
    with pytest.raises(cloud.CloudConfigError, match="resource name only"):
        cloud.save("foundry", {"resource": "https://res.services.ai.azure.com", "api_key": "k"})
    with pytest.raises(cloud.CloudConfigError, match="not both"):
        cloud.save("foundry", {"resource": "res", "base_url": "https://res.example", "api_key": "k"})
    with pytest.raises(cloud.CloudConfigError, match="JSON"):
        cloud.save("vertex", {"region": "global", "project_id": "p-123456", "credentials_json": "not json"})
    with pytest.raises(cloud.CloudConfigError, match="region"):
        cloud.save("bedrock", {"region": "US EAST", "bearer_token": "k"})
    with pytest.raises(cloud.CloudConfigError, match="one line"):
        cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": "a\nb"})
    with pytest.raises(cloud.CloudConfigError, match="does not use"):
        cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": "t", "region": "x"})
    with pytest.raises(cloud.CloudConfigError, match="Unknown method"):
        cloud.save("mantle", {})
    with pytest.raises(cloud.CloudConfigError, match="must be text"):
        cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": 123})
    with pytest.raises(cloud.CloudConfigError, match="needs: AWS region"):
        cloud.save("bedrock", {"bearer_token": "k"})
    assert database.get_system_setting(cloud.CONFIG_SETTING) is None


# ─── credential gate ──────────────────────────────────────────────────────

@pytest.mark.real_agent_gate
def test_gate_refuses_then_allows(store):
    claude_auth_mode.set_mode("cloud")
    signed_in = {"installed": True, "signed_in": True}
    assert credential_gate.claude_credential_ready(signed_in) == (False, credential_gate.CLOUD_NOT_SET_MESSAGE)
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-fake-ABCD")
    assert credential_gate.claude_credential_ready(signed_in)[0] is False  # no fallback to the key
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    assert credential_gate.claude_credential_ready({"installed": True, "signed_in": False}) == (True, None)


@pytest.mark.real_agent_gate
def test_gate_names_what_is_missing(store, monkeypatch):
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    database.delete_system_setting(cloud.SECRETS_SETTING)
    ready, reason = credential_gate.claude_credential_ready({})
    assert not ready and "LLM gateway" in reason and "Auth token is missing" in reason
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    from app.core import encryption

    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: False)
    assert credential_gate.claude_credential_ready({}) == (False, credential_gate.CLOUD_LOCKED_MESSAGE)


def test_billing_and_registry_treat_cloud_as_api(store):
    from app.core import model_registry, usage_attribution

    claude_auth_mode.set_mode("cloud")
    assert usage_attribution.claude_billing() == "api"
    assert model_registry._claude_payload()["auth_kind"] is None
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    assert model_registry._claude_payload() == {"auth_kind": "api", "plan_type": None, "enabled": True}


def test_pooled_cli_respawns_when_the_cloud_account_changes():
    from app.core.session_pool import provider_env_requires_reconnect

    assert provider_env_requires_reconnect({}, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"})
    assert provider_env_requires_reconnect({"AWS_REGION": "us-east-1"}, {"AWS_REGION": "eu-west-1"})
    assert provider_env_requires_reconnect({"GOOGLE_APPLICATION_CREDENTIALS": "/a/vertex-1.json"},
                                           {"GOOGLE_APPLICATION_CREDENTIALS": "/a/vertex-2.json"})
    assert not provider_env_requires_reconnect({"FOO": "1"}, {"FOO": "2"})


# ─── security review fixes (PR #1960) ─────────────────────────────────────

def _save_vertex(key=FAKE["vertex_json"]):
    cloud.save("vertex", {"region": "global", "project_id": "my-project-123", "credentials_json": key})


def test_a_failed_cloud_setup_fails_the_run_and_never_falls_back_to_host_credentials(store, monkeypatch, caplog):
    """Finding 1: the removal marker is set before the cloud step, and its
    failure ends the run with a value-free error, logged at warning."""
    from app.core.environment import ClaudeCloudSetupError

    caplog.set_level(logging.DEBUG)
    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")

    def broken():
        raise OSError("disk full at /secret/" + FAKE["gateway_token"])

    monkeypatch.setattr(cloud, "env_for_cli", broken)
    env = {"ANTHROPIC_API_KEY": "profile-key", "AWS_BEARER_TOKEN_BEDROCK": "profile-bedrock"}
    with patch.dict(os.environ, HOST), pytest.raises(ClaudeCloudSetupError) as err:
        inject_claude_code_auth(env)
    # Even a caller that swallowed the error hands the CLI no host credential.
    child = child_env_with_overlay(env, HOST)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
                 "AWS_ACCESS_KEY_ID", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert name not in child
    assert "host-" not in json.dumps(child) and "profile-" not in json.dumps(child)
    assert FAKE["gateway_token"] not in str(err.value)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "refusing the run" in r.getMessage()]
    assert warnings
    assert FAKE["gateway_token"] not in caplog.text


def test_generation_defers_auth_to_the_cli_boundary(store, monkeypatch):
    """Generation options may feed NativeLoop; they must not prepare CLI auth."""
    from app.core import generation_options

    cloud.save("gateway", {"base_url": "https://gw.example", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    monkeypatch.setattr(cloud, "env_for_cli", lambda: pytest.fail("auth prepared before runtime selection"))
    env = generation_options._resolve_env({"env": HOST})
    assert not set(env) & ALL_CLOUD_NAMES
    assert env["CLAUDECODE"] == ""


def test_cloud_mode_removes_every_cloud_and_anthropic_prefix_but_keeps_proxy_and_ca(store):
    """Finding 3: removal by prefix; NODE_TLS_REJECT_UNAUTHORIZED always goes;
    the host's proxy and CA settings stay; the app's own markers stay."""
    host = dict(HOST, **{
        "AWS_ENDPOINT_URL": "https://host-endpoint", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "https://host-br",
        "AWS_CA_BUNDLE": "/host/ca.pem", "ANTHROPIC_CUSTOM_HEADERS": "x-host: 1",
        "ANTHROPIC_MODEL": "host-model", "ANTHROPIC_SMALL_FAST_MODEL": "host-small",
        "AZURE_CLIENT_SECRET": "host-azure", "GOOGLE_CLOUD_PROJECT": "host-gcp",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1", "CLOUD_ML_PROJECT": "host-ml",
        "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        "HTTPS_PROXY": "http://proxy.corp:3128", "NO_PROXY": "localhost", "NODE_EXTRA_CA_CERTS": "/corp/ca.pem",
    })
    cloud.save("gateway", {"base_url": "https://gw.example/v1", "auth_token": FAKE["gateway_token"]})
    claude_auth_mode.set_mode("cloud")
    profile = {"ANTHROPIC_CUSTOM_HEADERS": "x-profile: 1", "AWS_ENDPOINT_URL": "https://profile-endpoint",
               "CLAUDECODE": "", "AI_SHUFFLE_MANAGED_BY": "app"}
    env = _cli_env(profile=profile, host=host)
    for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_CA_BUNDLE",
                 "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                 "AZURE_CLIENT_SECRET", "GOOGLE_CLOUD_PROJECT", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                 "CLOUD_ML_PROJECT", "NODE_TLS_REJECT_UNAUTHORIZED"):
        assert name not in env, name
    assert "host-" not in json.dumps(env) and "profile-" not in json.dumps(env)
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == FAKE["gateway_token"]
    assert env["HTTPS_PROXY"] == "http://proxy.corp:3128" and env["NO_PROXY"] == "localhost"
    assert env["NODE_EXTRA_CA_CERTS"] == "/corp/ca.pem"
    assert env["CLAUDECODE"] == "" and env["AI_SHUFFLE_MANAGED_BY"] == "app"


@pytest.mark.parametrize("kind", ["external_account", "authorized_user", "impersonated_service_account", None])
def test_vertex_accepts_only_a_service_account_key(store, kind):
    """Finding 4: other Google credential types are refused without echoing them."""
    marker = "PASTED-CONTENT-MARKER-000"
    body = {"private_key": marker, "credential_source": {"file": "/etc/" + marker}}
    if kind:
        body["type"] = kind
    with pytest.raises(cloud.CloudConfigError, match="service account") as err:
        _save_vertex(json.dumps(body))
    assert marker not in str(err.value)
    assert not kind or kind not in str(err.value)
    assert database.get_system_setting(cloud.CONFIG_SETTING) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX links and modes")
def test_vertex_key_file_replaces_a_planted_link_instead_of_following_it(store, tmp_path):
    """Finding 5: a link at the destination is replaced, its target untouched."""
    _save_vertex()
    claude_auth_mode.set_mode("cloud")
    path = inject_claude_code_auth({})["GOOGLE_APPLICATION_CREDENTIALS"]
    os.unlink(path)
    target = tmp_path / "elsewhere.json"
    target.write_text("attacker-owned")
    os.symlink(target, path)
    assert inject_claude_code_auth({})["GOOGLE_APPLICATION_CREDENTIALS"] == path
    assert not os.path.islink(path) and open(path).read() == FAKE["vertex_json"]
    assert target.read_text() == "attacker-owned"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in store.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX links and modes")
def test_vertex_key_file_is_never_written_through_a_linked_folder(store, tmp_path):
    """Finding 5: a link planted as the key file's folder fails the run;
    nothing is written into the link's target and its mode is unchanged."""
    from app.core.environment import ClaudeCloudSetupError

    _save_vertex()
    claude_auth_mode.set_mode("cloud")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    os.chmod(elsewhere, 0o755)
    if store.exists():
        for p in store.iterdir():
            p.unlink()
        store.rmdir()
    os.symlink(elsewhere, store)
    with pytest.raises(ClaudeCloudSetupError):
        inject_claude_code_auth({})
    assert list(elsewhere.iterdir()) == []
    assert stat.S_IMODE(os.stat(elsewhere).st_mode) == 0o755
    assert os.path.islink(store)


def test_vertex_runs_starting_at_once_all_succeed(store):
    """Finding 5: concurrent writers each use their own temp file."""
    import threading

    keys = [json.dumps({"type": "service_account", "private_key": f"fake-key-{i % 2}"}) for i in range(16)]
    barrier = threading.Barrier(len(keys))
    errors = []

    def run(key):
        barrier.wait()
        try:
            for _ in range(20):
                cloud._materialize_vertex(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [p.name for p in store.iterdir() if p.name.endswith(".tmp")] == []


def test_a_key_file_that_cannot_be_written_fails_the_run(store, monkeypatch):
    """Finding 5 into finding 1: a write error is the fail-closed error, never a fallback."""
    from app.core.environment import ClaudeCloudSetupError

    _save_vertex()
    claude_auth_mode.set_mode("cloud")

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cloud.tempfile, "mkstemp", no_space)
    env = {}
    with patch.dict(os.environ, HOST), pytest.raises(ClaudeCloudSetupError):
        inject_claude_code_auth(env)
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"} <= _unset(env)
    assert not list(store.glob("vertex-*.json"))


def test_a_save_interrupted_between_its_writes_never_mixes_two_saves(store, monkeypatch):
    """Finding 6a: new secrets pair only with the config saved alongside them."""
    cloud.save("gateway", {"base_url": "https://old.example", "auth_token": "fake-old-token"})
    real = database.set_system_setting

    def fail_config(key, value, *a, **k):
        if key == cloud.CONFIG_SETTING:
            raise RuntimeError("database went away")
        return real(key, value, *a, **k)

    monkeypatch.setattr(database, "set_system_setting", fail_config)
    with pytest.raises(RuntimeError):
        cloud.save("gateway", {"base_url": "https://new.example", "auth_token": "fake-new-token"})
    monkeypatch.setattr(database, "set_system_setting", real)
    cfg = cloud.stored()
    assert cfg["values"] == {"base_url": "https://old.example"}
    assert cfg["secrets"] == {}  # neither the new token with the old address, nor the old token
    assert cloud.status()["ready"] is False and cloud.status()["missing"] == ["Auth token"]
    claude_auth_mode.set_mode("cloud")
    assert "ANTHROPIC_AUTH_TOKEN" not in inject_claude_code_auth({})
    cloud.save("gateway", {"base_url": "https://new.example", "auth_token": "fake-new-token"})
    assert cloud.status()["ready"] is True


@pytest.mark.parametrize("change", ["cleared", "other_method", "incomplete"])
def test_a_config_changed_underneath_leaves_no_vertex_key_file(store, change):
    """Finding 2: whatever changed the stored config (another code path, a
    restore, an older sync), the next run removes the leftover key file."""
    _save_vertex()
    claude_auth_mode.set_mode("cloud")
    assert os.path.exists(inject_claude_code_auth({})["GOOGLE_APPLICATION_CREDENTIALS"])
    if change == "cleared":
        database.delete_system_setting(cloud.CONFIG_SETTING)
        database.delete_system_setting(cloud.SECRETS_SETTING)
    elif change == "other_method":
        database.set_system_setting(cloud.CONFIG_SETTING, json.dumps(
            {"method": "gateway", "values": {"base_url": "https://gw.example"}, "save_id": "x"}))
    else:
        database.delete_system_setting(cloud.SECRETS_SETTING)
    env = inject_claude_code_auth({})
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert not list(store.glob("vertex-*.json"))


def test_cloud_settings_stay_on_this_worker():
    """Finding 2: the mode is per worker (its key carries the instance id and
    each worker reads only its own), so the method and secrets are not synced."""
    from app.network import engine, policy

    for key in (cloud.CONFIG_SETTING, cloud.SECRETS_SETTING):
        assert policy.is_shared_setting_key(key) is False
        row = {"key": key, "value": "enc:fake", "updated_at": "2026-10-06T00:00:00"}
        assert engine.serialize_row(policy.collection("system_settings"), row) is None
    assert claude_auth_mode.setting_key().startswith("claude_auth_mode:")
