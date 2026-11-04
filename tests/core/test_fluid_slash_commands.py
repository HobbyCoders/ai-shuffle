"""Fluid file/installed-package producers shared by the native and CLI catalogs."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.slash_commands import (
    expand_delegated_command, expand_native_command, get_all_commands, get_command_by_name,
)


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def fluid_home(tmp_path, monkeypatch):
    from app.core.plugin_service import PluginService

    home, cwd = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    (cwd / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    for module in ("app.core.plugin_service", "app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: home / ".fluid")
    monkeypatch.setattr("app.core.plugin_models._get_claude_dir", lambda: home / ".claude")
    # Only network publication is stubbed; installation, settings and discovery are real.
    monkeypatch.setattr("app.network.plugins_sync.request_install", lambda *a, **kw: None)
    return PluginService(), cwd


@pytest.fixture
def installed_plugin(fluid_home):
    svc, cwd = fluid_home
    market = svc.MARKETPLACES_DIR / "journey"
    package = market / "installed"
    write(package / ".claude-plugin" / "plugin.json", json.dumps({"name": "installed", "version": "1.0.0"}))
    write(package / "commands" / "run.md", "---\ndescription: Installed command\nmodel: other-model\n---\n"
          "Installed $1 / $2. $ARGUMENTS. ${CLAUDE_PLUGIN_ROOT} ${CLAUDE_PLUGIN_DATA}")
    write(package / "skills" / "guide" / "SKILL.md", "---\ndescription: Installed skill\n---\nInstalled guide")
    write(package / "skills" / "hidden" / "SKILL.md", "---\nuser-invocable: false\n---\nHidden skill")
    write(market / ".claude-plugin" / "marketplace.json", json.dumps({
        "name": "journey", "plugins": [{"name": "installed", "source": "./installed"}],
    }))
    svc._write_json_file(svc.MARKETPLACES_FILE, {"journey": {"installLocation": str(market)}})
    # The real agent install producer must leave it off, pending owner review.
    plugin = svc.install_plugin("installed@journey", requested_by={"session_id": "test", "agent_name": "Test"})
    assert plugin.id == "installed@journey"
    assert not svc.get_enabled_plugins().get(plugin.id)
    return svc, cwd


@pytest.mark.parametrize("kind,expand", [("native", expand_native_command), ("delegated", expand_delegated_command)])
@pytest.mark.parametrize("state", ["pending", "enabled", "disabled"])
def test_installed_commands_and_skills_follow_owner_enablement(installed_plugin, kind, expand, state):
    svc, cwd = installed_plugin
    if state != "pending":
        assert svc.enable_plugin("installed@journey")
        assert all(r.get("approval") != "pending" for r in svc.get_installation_records("installed@journey"))
    if state == "disabled":
        assert svc.disable_plugin("installed@journey")
    entries = {c["name"]: c for c in get_all_commands(str(cwd), provider_kind=kind)}
    assert "installed:hidden" not in entries
    for name in ("installed:run", "installed:guide"):
        cmd = get_command_by_name(str(cwd), name, provider_kind=kind)
        assert (name in entries) == (cmd is not None) == (state == "enabled")
        prompt = f'/{name} "two words" release'
        result = expand(str(cwd), prompt)
        if state != "enabled":
            assert result == prompt
        elif name.endswith(":run"):
            assert cmd.model is None
            assert entries[name]["source"] == "installed@journey"
            assert 'Installed two words / release. "two words" release.' in result
            assert str(svc.CACHE_DIR / "journey" / "installed" / "1.0.0") in result
            assert "${CLAUDE_PLUGIN_" not in result
        else:
            assert result.startswith("# Skill: installed:guide\n# From:")
            assert 'Installed guide\n\nArguments: "two words" release' in result


@pytest.mark.parametrize("kind,expand", [("native", expand_native_command), ("delegated", expand_delegated_command)])
@pytest.mark.parametrize("sources", [[], ["local"], ["user"], ["project"], ["user", "project", "local"]])
def test_fluid_scopes_apply_to_listing_details_and_expansion(installed_plugin, kind, expand, sources):
    svc, cwd = installed_plugin
    assert svc.enable_plugin("installed@journey")
    for scope, root in (("user", svc.FLUID_DIR), ("project", cwd / ".fluid")):
        write(root / "commands" / f"{scope}-command.md", f"{scope} command $1")
        write(root / "skills" / f"{scope}-skill" / "SKILL.md", f"{scope} skill $1")
        write(root / "skills" / f"{scope}-hidden" / "SKILL.md", "---\nuser-invocable: false\n---\nHidden")
    entries = {c["name"] for c in get_all_commands(str(cwd), provider_kind=kind, setting_sources=sources)}
    for name, scope in (("user-command", "user"), ("user-skill", "user"),
                        ("project-command", "project"), ("project-skill", "project"),
                        ("installed:run", "user"), ("installed:guide", "user")):
        cmd = get_command_by_name(str(cwd), name, provider_kind=kind, setting_sources=sources)
        assert (name in entries) == (cmd is not None) == (scope in sources)
        prompt = f"/{name} arg"
        assert (expand(str(cwd), prompt, setting_sources=sources) != prompt) == (scope in sources)
    assert not {"user-hidden", "project-hidden", "installed:hidden"} & entries


@pytest.mark.parametrize("kind,expand", [("native", expand_native_command), ("delegated", expand_delegated_command)])
def test_fluid_precedence_and_inline_code_never_executes(fluid_home, kind, expand):
    svc, cwd = fluid_home
    sentinel = cwd / "must-not-exist"
    write(svc.FLUID_DIR / "commands" / "same.md", "User $1")
    write(cwd / ".agents" / "commands" / "same.md", "Agents $1")
    write(cwd / ".fluid" / "commands" / "same.md", f"---\nallowed-tools: Bash\n---\nProject $1 !`touch {sentinel}`")
    write(cwd / ".fluid" / "skills" / "same" / "SKILL.md", "Command wins over skill")
    cmd = get_command_by_name(str(cwd), "same", provider_kind=kind)
    assert cmd.source == "project"
    assert cmd.type == "custom"
    expanded = expand(str(cwd), '/same "two words"')
    assert expanded == f"Project two words !`touch {sentinel}`"
    assert not sentinel.exists()
    # Delegated Claude commands (files, skills, installed plugins) keep their
    # CLI execution path on a collision; native never consults provider homes.
    write(Path.home() / ".claude" / "commands" / "same.md", "Claude owns this")
    cmd = get_command_by_name(str(cwd), "same", provider_kind=kind)
    assert cmd.content.startswith("Claude" if kind == "delegated" else "Project")
    assert expand(str(cwd), "/same arg") == ("/same arg" if kind == "delegated" else f"Project arg !`touch {sentinel}`")


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_adapter_submits_direct_and_installed_fluid_commands(installed_plugin, monkeypatch, delegated):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fluid.events.ops import UserTurn

    svc, cwd = installed_plugin
    assert svc.enable_plugin("installed@journey")
    write(svc.FLUID_DIR / "commands" / "direct.md", "Direct $1 / $2")
    write(svc.FLUID_DIR / "skills" / "direct-skill" / "SKILL.md", "Skill $1 / $2")
    adapter = HarnessAdapter()
    adapter._engine = SimpleNamespace(submit=AsyncMock())
    adapter._is_delegated = delegated
    adapter._options = SessionOptions(model="sonnet" if delegated else "fake-ai", cwd=str(cwd))
    monkeypatch.setattr(adapter, "reconcile_user_questions", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_task_roster", AsyncMock())
    monkeypatch.setattr(adapter, "_turn_context_note", lambda query="": "context-canary")
    expand = expand_delegated_command if delegated else expand_native_command
    for name in ("direct", "direct-skill", "installed:run", "installed:guide"):
        prompt = f'/{name} "two words" release'
        await adapter.send_query(prompt)
        op = adapter._engine.submit.await_args.args[0]
        assert isinstance(op, UserTurn)
        assert op.content == expand(str(cwd), prompt) + "\n\ncontext-canary"
        assert op.content != prompt + "\n\ncontext-canary"
    for prompt in ("/compact", "/context", "/unknown command"):
        await adapter.send_query(prompt)
        op = adapter._engine.submit.await_args.args[0]
        expected = prompt if delegated or prompt == "/compact" else prompt + "\n\ncontext-canary"
        assert op.content == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "delegated"])
async def test_command_detail_endpoint_uses_same_catalog(installed_plugin, monkeypatch, kind):
    from app.api import commands
    from fastapi import HTTPException

    svc, cwd = installed_plugin
    assert svc.enable_plugin("installed@journey")
    write(svc.FLUID_DIR / "commands" / "direct.md", "Direct $1")
    write(svc.FLUID_DIR / "skills" / "direct-skill" / "SKILL.md", "Direct skill $1")
    monkeypatch.setattr(commands, "command_context", lambda *a: (str(cwd), None))
    monkeypatch.setattr(commands, "command_provider_kind", lambda *a: kind)
    monkeypatch.setattr(commands, "command_setting_sources", lambda *a: ["user"])
    for name in ("direct", "direct-skill", "installed:run", "installed:guide"):
        detail = await commands.get_command(name)
        assert detail.content == get_command_by_name(str(cwd), name, provider_kind=kind).content
    monkeypatch.setattr(commands, "command_setting_sources", lambda *a: [])
    with pytest.raises(HTTPException) as error:
        await commands.get_command("direct")
    assert error.value.status_code == 404
    for excluded_runtime in ("voice", "unavailable"):
        assert get_all_commands(str(cwd), provider_kind=excluded_runtime) == []


@pytest.mark.parametrize("kind", ["native", "delegated"])
@pytest.mark.parametrize("state", ["pending", "approved", "disabled", "edited"])
def test_shared_catalog_preserves_authored_content_approval(fluid_home, kind, state):
    from fluid.context.plugin_authoring import save_authored_plugin

    svc, cwd = fluid_home
    saved = save_authored_plugin({
        "name": "authored-check", "version": "0.1.0", "description": "Approval check",
        "commands": [{"name": "run", "body": "Reviewed $1"}],
        "skills": [{"name": "guide", "body": "Reviewed guide $1", "description": "A guide"}],
    }, home=svc.PLUGINS_DIR)
    if state != "pending":
        assert svc.enable_plugin(saved.id, content_hash=saved.content_hash)
    if state == "disabled":
        assert svc.disable_plugin(saved.id)
    elif state == "pending":
        # An enabled flag alone still cannot approve an authored draft.
        svc._write_json_file(svc.SETTINGS_FILE, {"enabledPlugins": {saved.id: True}})
    elif state == "edited":
        write(saved.path / "commands" / "run.md", "Changed after owner review")
    expand = expand_delegated_command if kind == "delegated" else expand_native_command
    for name in ("authored-check:run", "authored-check:guide"):
        cmd = get_command_by_name(str(cwd), name, provider_kind=kind)
        assert (cmd is not None) == (state == "approved")
        prompt = f"/{name} arg"
        assert (expand(str(cwd), prompt) != prompt) == (state == "approved")


@pytest.mark.parametrize("owner", ["command", "skill", "plugin"])
def test_claude_owned_collision_keeps_cli_execution(installed_plugin, owner):
    svc, cwd = installed_plugin
    assert svc.enable_plugin("installed@journey")
    claude = Path.home() / ".claude"
    if owner == "plugin":
        name = "installed:run"
        write(claude / "plugins" / "cache" / "market" / "installed" / "1.0.0" / "commands" / "run.md", "Claude body")
        write(claude / "settings.json", json.dumps({"enabledPlugins": {"installed@market": True}}))
    else:
        name = "direct"
        write(svc.FLUID_DIR / "commands" / "direct.md", "Fluid body $1")
        target = claude / "commands" / "direct.md" if owner == "command" else claude / "skills" / "direct" / "SKILL.md"
        write(target, "Claude body")
    entries = [c for c in get_all_commands(str(cwd)) if c["name"] == name]
    assert len(entries) == 1
    assert get_command_by_name(str(cwd), name).content == "Claude body"
    assert expand_delegated_command(str(cwd), f"/{name} arg") == f"/{name} arg"


@pytest.mark.asyncio
@pytest.mark.parametrize("file_kind", ["command", "skill"])
@pytest.mark.parametrize("name,detail_type", [("compact", "sdk_builtin"), ("resume", "interactive"), ("doctor", None)])
async def test_fluid_files_cannot_capture_cli_reserved_names(fluid_home, monkeypatch, file_kind, name, detail_type):
    from app.api import commands
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fastapi import HTTPException

    svc, cwd = fluid_home
    path = (svc.FLUID_DIR / "commands" / f"{name}.md" if file_kind == "command"
            else svc.FLUID_DIR / "skills" / name / "SKILL.md")
    write(path, "Fluid reserved-name body $ARGUMENTS")
    entries = [cmd for cmd in get_all_commands(str(cwd)) if cmd["name"] == name]
    assert [cmd["type"] for cmd in entries] == (["sdk_builtin"] if detail_type == "sdk_builtin" else [])
    assert get_command_by_name(str(cwd), name) is None
    monkeypatch.setattr(commands, "command_context", lambda *a: (str(cwd), None))
    monkeypatch.setattr(commands, "command_provider_kind", lambda *a: "delegated")
    monkeypatch.setattr(commands, "command_setting_sources", lambda *a: ["user"])
    if detail_type:
        detail = await commands.get_command(name)
        assert detail.type == detail_type and detail.content is None
    else:
        with pytest.raises(HTTPException) as error:
            await commands.get_command(name)
        assert error.value.status_code == 404
    prompt = f"/{name} keep this argument"
    adapter = HarnessAdapter()
    adapter._engine = SimpleNamespace(submit=AsyncMock())
    adapter._is_delegated = True
    adapter._options = SessionOptions(model="sonnet", cwd=str(cwd))
    monkeypatch.setattr(adapter, "reconcile_user_questions", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_task_roster", AsyncMock())
    monkeypatch.setattr(adapter, "_turn_context_note", lambda query="": "must-not-append")
    await adapter.send_query(prompt)
    assert adapter._engine.submit.await_args.args[0].content == prompt
    # Only the delegated Fluid additions are reserved, not native files or
    # Claude's existing custom-file precedence over its own built-ins.
    assert "Fluid reserved-name body" in expand_native_command(str(cwd), prompt)
    write(Path.home() / ".claude" / "commands" / f"{name}.md", "Claude owned")
    assert get_command_by_name(str(cwd), name).content == "Claude owned"
    assert expand_delegated_command(str(cwd), prompt) == prompt
