"""Command discovery uses the selected runtime and authorizes before disk reads."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.api import command_context as context
from app.api import commands
from app.core import slash_commands


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / "home"
    project = tmp_path / "project"
    child = project / "nested"
    (project / ".git").mkdir(parents=True)
    child.mkdir()
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("app.core.plugin_models._get_claude_dir", lambda: home / ".claude")
    return home, project, child


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_native_sources_precedence_and_real_runtime_catalogue(source_tree):
    from fluid.context.instructions import discover_commands, discover_skills

    home, project, child = source_tree
    put(home / ".fluid/commands/review.md", "User review")
    put(project / ".fluid/commands/review.md", "Outer review")
    put(child / ".agents/commands/review.md", "Agents review")
    put(child / ".fluid/commands/review.md", "Inner review")
    put(child / ".claude/commands/legacy.md", "Must not load")
    put(home / ".fluid/skills/folder/SKILL.md", "---\nname: named-skill\n---\nSkill body")
    put(child / ".fluid/skills/nearest/SKILL.md", "---\nname: named-skill\n---\nInner skill")
    cwd = str(child)
    actual = slash_commands.get_all_commands(cwd, provider_kind="native")
    runtime = {cmd.name for cmd in discover_commands(cwd)} | {skill.name for skill in discover_skills(cwd)}
    assert {cmd["name"] for cmd in actual} == runtime == {"review", "named-skill"}
    assert slash_commands.get_command_by_name(cwd, "review", provider_kind="native").content == "Inner review"
    assert slash_commands.get_command_by_name(cwd, "named-skill", provider_kind="native").content == "Inner skill"
    assert slash_commands.get_command_by_name(cwd, "review", provider_kind="native", setting_sources=["user"]).content == "User review"
    assert slash_commands.get_all_commands(cwd, provider_kind="native", setting_sources=[]) == []
    assert slash_commands.get_all_commands(cwd, provider_kind="native", setting_sources=["local"]) == []


@pytest.mark.parametrize("kind,root", [("native", ".fluid"), ("delegated", ".claude")])
def test_plugin_enablement_uses_selected_stack_and_same_lookup(source_tree, kind, root):
    import json

    home, project, child = source_tree
    install = home / root / "plugins/cache/market/demo/1.10.0"
    put(install / "commands/action.md", "Plugin $ARGUMENTS")
    put(install / "skills/folder/SKILL.md", "---\nname: plugin-skill\n---\nSkill $ARGUMENTS")
    put(home / root / "settings.json", json.dumps({"enabledPlugins": {"demo@market": True}}))
    put(project / root / "settings.json", json.dumps({"enabledPlugins": {"demo@market": False}}))
    put(child / root / "settings.local.json", json.dumps({"enabledPlugins": {"demo@market": True}}))
    cwd = str(child)
    for sources, enabled in [(None, True), (["user"], True), (["user", "project"], False), ([], False)]:
        commands_ = slash_commands.get_all_commands(cwd, provider_kind=kind, setting_sources=sources)
        names = {cmd["name"] for cmd in commands_ if cmd["type"] != "sdk_builtin"}
        assert names == ({"demo:action", "demo:plugin-skill"} if enabled else set())
        command = slash_commands.get_command_by_name(cwd, "demo:action", provider_kind=kind, setting_sources=sources)
        assert (command is not None) == enabled
        if enabled:
            assert command.expand_prompt("argument") == "Plugin argument"


@pytest.mark.parametrize("sources", [None, [], ["user"], ["project", "local"]])
async def test_api_native_list_detail_execute_share_profile_sources(source_tree, monkeypatch, sources):
    home, project, child = source_tree
    put(home / ".fluid/commands/greet.md", "Hello $ARGUMENTS")
    session = {"id": "s", "profile_id": "p", "project_id": "mine", "api_user_id": "me", "model": "gpt-test"}
    monkeypatch.setattr(context.database, "get_session", lambda sid: session)
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {"config": {"setting_sources": sources}})
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda row: str(child))
    req = caller({"id": "me", "project_id": "mine", "profile_id": "p"})
    listed = await commands.list_commands(session_id="s", http_request=req)
    allowed = sources is None or "user" in sources
    assert {entry.name for entry in listed.commands} == ({"greet"} if allowed else set())
    if allowed:
        detail = await commands.get_command("greet", session_id="s", http_request=req)
        executed = await commands.execute_command(commands.ExecuteCommandRequest(command="/greet world", session_id="s"), req)
        assert detail.content == "Hello $ARGUMENTS"
        assert executed.expanded_prompt == "Hello world"
    else:
        with pytest.raises(HTTPException) as exc:
            await commands.get_command("greet", session_id="s", http_request=req)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await commands.execute_command(commands.ExecuteCommandRequest(command="/greet world", session_id="s"), req)
        assert exc.value.status_code == 404


async def test_profile_cwd_is_used_before_session_creation(source_tree, monkeypatch):
    _, _, child = source_tree
    put(child / ".fluid/commands/here.md", "From profile cwd")
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {"config": {"cwd": str(child), "setting_sources": ["project"], "model": "gpt-test"}})
    result = await commands.list_commands(profile_id="p", http_request=caller())
    assert [cmd.name for cmd in result.commands] == ["here"]


def test_skill_invocation_metadata_and_arguments(source_tree):
    _, _, child = source_tree
    put(child / ".fluid/skills/hidden/SKILL.md", "---\nuser-invocable: false\n---\nNot a slash command")
    put(child / ".fluid/skills/explicit/SKILL.md", "---\ndisable-model-invocation: true\n---\nRun this skill")
    put(child / ".fluid/commands/malformed.md", "---\n- not a mapping\n---\nStill usable body")
    entries = slash_commands.get_all_commands(str(child), provider_kind="native")
    assert {entry["name"] for entry in entries} == {"explicit", "malformed"}
    expanded = slash_commands.expand_native_command(str(child), "/explicit requested task")
    assert "Run this skill" in expanded
    assert "Arguments: requested task" in expanded
    assert str(child / ".fluid/skills/explicit/SKILL.md") in expanded
    assert slash_commands.get_command_by_name(str(child), "hidden", provider_kind="native") is None


def test_scoped_profile_cannot_override_sources_even_in_voice(monkeypatch):
    lookup = Mock(side_effect=AssertionError("unauthorized profile loaded"))
    monkeypatch.setattr(context.database, "get_profile", lookup)
    with pytest.raises(HTTPException) as exc:
        context.command_provider_kind(caller({"profile_id": "mine"}), None, None, "other", "voice")
    assert exc.value.status_code == 403
    lookup.assert_not_called()


def caller(user=None):
    request = Request({'type':'http','headers':[]})
    request.state.api_user = user
    return request


@pytest.mark.parametrize('kind', ['anthropic','openai_responses','openrouter','voice','unavailable'])
def test_native_menu_uses_fluid_files_without_cli_builtins(monkeypatch, tmp_path, kind):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path / 'home')
    (tmp_path / '.git').mkdir()
    command_dir = tmp_path / '.fluid' / 'commands'
    command_dir.mkdir(parents=True)
    (command_dir / 'native.md').write_text('Native command', encoding='utf-8')
    result = slash_commands.get_all_commands(str(tmp_path), provider_kind=kind)
    assert {cmd['name'] for cmd in result} == (set() if kind in ('voice', 'unavailable') else {'native'})
    assert not slash_commands.get_all_commands(str(tmp_path), provider_kind=kind, setting_sources=[])


def test_delegated_menu_keeps_prompt_commands_and_omits_terminal_settings(monkeypatch):
    monkeypatch.setattr(slash_commands,'discover_commands',lambda cwd: [])
    monkeypatch.setattr(slash_commands,'discover_plugin_commands',lambda: [])
    monkeypatch.setattr(slash_commands,'discover_skills',lambda cwd: [])
    names = {c['name'] for c in slash_commands.get_all_commands('/fixture')}
    assert {'compact','context','cost','review','init','security-review','pr-comments'} <= names
    assert not names & {'model','plan','memory','doctor','export','sandbox','bug','status'}


@pytest.mark.parametrize('model,provider,expected', [
    ('sonnet',None,'delegated'), ('gpt-test',None,'openai_responses'),
    ('prov:gateway:claude-test','gateway','openrouter'),
    ('claude-test','direct','anthropic'),
    ('sonnet','gateway','delegated'),
])
def test_menu_route_matches_adapter_model_and_provider_binding(monkeypatch, model, provider, expected):
    from app.db import model_providers
    rows = {
        'gateway': {'id':'gateway','provider_type':'openrouter','models_json':[{'id':'claude-test'}]},
        'direct': {'id':'direct','provider_type':'anthropic','auth_mode':'api_key','models_json':[{'id':'claude-test'}]},
    }
    monkeypatch.delenv('AIH_FLUID_PROVIDER',raising=False)
    monkeypatch.delenv('AIH_OMNI_PROVIDER',raising=False)
    monkeypatch.setattr(context.database,'get_model_provider',lambda pid,**kw: rows.get(pid))
    monkeypatch.setattr(model_providers,'get_model_provider',lambda pid,**kw: rows.get(pid))
    monkeypatch.setattr(context.database,'get_profile',lambda pid: {'config':{}})
    assert context.command_provider_kind(caller(), {'provider_id':provider}, model,None,'code') == expected


@pytest.mark.parametrize('endpoint', ['list','detail','execute'])
async def test_session_command_endpoints_deny_other_principal_before_path_scan(monkeypatch, endpoint):
    user={'id':'me','project_id':'mine'}
    monkeypatch.setattr(context.database,'get_session',lambda sid:{'id':sid,'project_id':'theirs','api_user_id':'other'})
    resolve=Mock(side_effect=AssertionError('unauthorized path resolved'))
    monkeypatch.setattr('app.core.project_resolver.resolve_existing_cwd',resolve)
    with pytest.raises(HTTPException) as exc:
        if endpoint=='list': await commands.list_commands(session_id='foreign',http_request=caller(user))
        elif endpoint=='detail': await commands.get_command('review',session_id='foreign',http_request=caller(user))
        else: await commands.execute_command(commands.ExecuteCommandRequest(command='/rewind',session_id='foreign'),caller(user))
    assert exc.value.status_code == 403
    resolve.assert_not_called()


@pytest.mark.parametrize('worktree_id,project_id', [('foreign-tree','mine'),(None,'theirs')])
async def test_foreign_project_and_worktree_rejected_before_scan(monkeypatch, worktree_id, project_id):
    monkeypatch.setattr(context.database,'get_worktree',lambda wid:{'repository_id':'repo'})
    monkeypatch.setattr(context.database,'get_git_repository',lambda rid:{'project_id':'theirs'})
    scan=Mock(side_effect=AssertionError('unauthorized scan'))
    monkeypatch.setattr(commands,'get_all_commands',scan)
    with pytest.raises(HTTPException) as exc:
        await commands.list_commands(project_id=project_id,worktree_id=worktree_id,http_request=caller({'id':'me','project_id':'mine'}))
    assert exc.value.status_code == 403
    scan.assert_not_called()


async def test_custom_execution_uses_authorized_session_checkout(monkeypatch,tmp_path):
    checkout=tmp_path/'session-checkout'; command_dir=checkout/'.claude'/'commands'; command_dir.mkdir(parents=True)
    (command_dir/'greet.md').write_text('Hello $ARGUMENTS from this checkout',encoding='utf-8')
    monkeypatch.setattr(context.database,'get_session',lambda sid:{'id':sid,'project_id':'mine','api_user_id':'me'})
    monkeypatch.setattr('app.core.project_resolver.resolve_existing_cwd',lambda row:str(checkout))
    result=await commands.execute_command(commands.ExecuteCommandRequest(command='/greet world',session_id='s'),caller({'id':'me','project_id':'mine'}))
    assert result.expanded_prompt == 'Hello world from this checkout'


def test_scoped_projectless_discovery_uses_assigned_project(monkeypatch,tmp_path):
    project={'id':'mine','working_dir':str(tmp_path)}
    monkeypatch.setattr(context.database,'get_project',lambda pid:project if pid=='mine' else None)
    monkeypatch.setattr(context,'get_project_working_dir',lambda row:str(tmp_path))
    assert context.command_context(caller({'id':'me','project_id':'mine'}),None,None,None) == (str(tmp_path),None)


async def test_other_api_principal_same_project_is_rejected(monkeypatch):
    monkeypatch.setattr(context.database,'get_session',lambda sid:{'id':sid,'project_id':'shared','api_user_id':'other'})
    with pytest.raises(HTTPException) as exc:
        await commands.list_commands(session_id='s',http_request=caller({'id':'me','project_id':'shared'}))
    assert exc.value.status_code == 403


def test_v2_only_session_checks_origin_before_checkout_resolution(monkeypatch):
    monkeypatch.setattr(context.database,'get_session',lambda sid:{'id':sid,'project_v2_id':'foreign-uuid'})
    def deny_origin(**kw):
        assert kw['project_v2_id']=='foreign-uuid'
        raise HTTPException(403,'foreign origin')
    monkeypatch.setattr(context,'require_origin_access',deny_origin)
    resolve=Mock(side_effect=AssertionError('unauthorized checkout resolution'))
    monkeypatch.setattr('app.core.project_resolver.resolve_existing_cwd',resolve)
    with pytest.raises(HTTPException):
        context.command_context(caller({'id':'me','project_id':'mine'}),None,None,'s')
    resolve.assert_not_called()
