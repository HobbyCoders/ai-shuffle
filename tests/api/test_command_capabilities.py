"""Command discovery uses the selected runtime and authorizes before disk reads."""
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
async def test_api_native_list_and_detail_share_profile_sources(source_tree, monkeypatch, sources):
    home, project, child = source_tree
    put(home / ".fluid/commands/greet.md", "Hello $ARGUMENTS")
    session = {"id": "s", "profile_id": "p", "project_id": "mine", "api_user_id": "me", "model": "gpt-test"}
    monkeypatch.setattr(context.database, "get_session", lambda sid: session)
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {"config": {"setting_sources": sources}})
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda row: str(child))
    req = caller({"id": "me", "project_id": "mine", "profile_ids": ["p"]})
    listed = await commands.list_commands(session_id="s", http_request=req)
    allowed = sources is None or "user" in sources
    # /compact is the native engine's own compaction, listed on every native menu.
    assert {entry.name for entry in listed.commands} == ({"greet"} if allowed else set()) | {"compact"}
    if allowed:
        detail = await commands.get_command("greet", session_id="s", http_request=req)
        assert detail.content == "Hello $ARGUMENTS"
    else:
        with pytest.raises(HTTPException) as exc:
            await commands.get_command("greet", session_id="s", http_request=req)
        assert exc.value.status_code == 404


async def test_profile_cwd_is_used_before_session_creation(source_tree, monkeypatch):
    _, _, child = source_tree
    put(child / ".fluid/commands/here.md", "From profile cwd")
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {"config": {"cwd": str(child), "setting_sources": ["project"], "model": "gpt-test"}})
    req = caller()
    req.state.auth_token = "local-owner"
    result = await commands.list_commands(profile_id="p", http_request=req)
    assert [cmd.name for cmd in result.commands if cmd.name != "compact"] == ["here"]


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
        context.command_provider_kind(caller({"profile_ids": ["mine"]}), None, None, "other", "voice")
    assert exc.value.status_code == 403
    lookup.assert_not_called()


def test_scoped_caller_defaults_to_its_only_agent_and_may_use_any_assigned(monkeypatch):
    """``profile_ids`` (not the dead ``profile_id`` column) scopes commands."""
    seen = []
    monkeypatch.setattr(context.database, "get_profile",
                        lambda pid: seen.append(pid) or {"config": {}})
    context.command_setting_sources(caller({"profile_ids": ["only"]}), None, None)
    context.command_setting_sources(caller({"profile_ids": ["a", "b"]}), None, "b")
    assert seen == ["only", "b"]
    with pytest.raises(HTTPException) as exc:
        context.command_setting_sources(
            caller({"profile_id": "legacy", "profile_ids": ["a"]}), None, "legacy")
    assert exc.value.status_code == 403


def caller(user=None):
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer test-api-key')]})
    request.state.api_user = {'id': 'test-user', **user} if user else None
    return request


@pytest.mark.parametrize("kind", ["anthropic", "delegated"])
@pytest.mark.parametrize("session_mode,request_mode,allowed", [
    (None, "chat", False), (None, "code", True),
    ("chat", "code", False), ("code", "chat", True),
])
async def test_chat_catalog_and_details_match_runtime_scope_exclusion(
    source_tree, monkeypatch, kind, session_mode, request_mode, allowed,
):
    import json

    home, _, child = source_tree
    put(home / ".fluid/commands/direct.md", "Direct body")
    put(child / ".fluid/skills/guide/SKILL.md", "Skill body")
    put(home / ".fluid/plugins/cache/market/installed/1.0.0/commands/run.md", "Installed body")
    put(home / ".fluid/settings.json", json.dumps({"enabledPlugins": {"installed@market": True}}))
    put(home / ".claude/commands/claude-owned.md", "Claude body")
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {
        "config": {"setting_sources": ["user", "project", "local"], "cwd": str(child)},
    })
    session = {"id": "s", "profile_id": "p", "project_id": "mine", "mode": session_mode} if session_mode else None
    monkeypatch.setattr(context.database, "get_session", lambda sid: session)
    monkeypatch.setattr("app.core.project_resolver.resolve_session_cwd", lambda row: str(child))
    monkeypatch.setattr(commands, "command_provider_kind", lambda *a: kind)
    kwargs = {"http_request": caller(), "profile_id": "p", "mode": request_mode}
    if session:
        kwargs["session_id"] = "s"
    listed = await commands.list_commands(**kwargs)
    names = {cmd.name for cmd in listed.commands}
    file_names = {"direct", "guide", "installed:run"}
    if kind == "delegated":
        file_names.add("claude-owned")
    assert names & file_names == (file_names if allowed else set())
    assert "compact" in names  # Runtime built-ins survive filesystem isolation.
    for name in file_names:
        if allowed:
            assert (await commands.get_command(name, **kwargs)).content
        else:
            with pytest.raises(HTTPException) as error:
                await commands.get_command(name, **kwargs)
            assert error.value.status_code == 404
    assert (await commands.get_command("compact", **kwargs)).type == "sdk_builtin"


def test_chat_scope_exclusion_still_authorizes_profile(monkeypatch):
    lookup = Mock(side_effect=AssertionError("unauthorized profile loaded"))
    monkeypatch.setattr(context.database, "get_profile", lookup)
    with pytest.raises(HTTPException) as error:
        context.command_setting_sources(caller({"profile_ids": ["mine"]}), None, "other", "chat")
    assert error.value.status_code == 403
    lookup.assert_not_called()


@pytest.mark.parametrize("kind", ["anthropic", "delegated"])
@pytest.mark.parametrize("stored_mode,token,allowed", [
    ("chat", "local-owner", True), (None, "preview:fixture", False),
    ("chat", "preview:fixture", False), (None, "local-owner", True),
])
async def test_saved_catalog_uses_current_http_caller_runtime_lane(source_tree, monkeypatch, kind, stored_mode, token, allowed):
    from app.core.session_modes import runtime_session_mode, caller_of
    from app.api.chat_query import _request_query_principal

    home, _, child = source_tree
    put(home / ".fluid/commands/direct.md", "Direct body $1")
    put(child / ".fluid/skills/guide/SKILL.md", "Skill body")
    row = {"id": "owner-chat", "profile_id": "p", "mode": stored_mode}
    req = caller()
    req.state.auth_token = token
    monkeypatch.setattr(context.database, "get_session", lambda sid: row)
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {
        "config": {"setting_sources": ["user", "project", "local"]},
    })
    monkeypatch.setattr("app.core.project_resolver.resolve_session_cwd", lambda row: str(child))
    monkeypatch.setattr("app.core.session_orchestration._principal", lambda sid: None)
    # Opposite stored caller: the current HTTP send's caller must win.
    monkeypatch.setattr("app.db.turns.latest_caller", lambda sid: "preview" if allowed else "authenticated")
    monkeypatch.setattr(commands, "command_provider_kind", lambda *a: kind)
    effective = runtime_session_mode(row, caller=caller_of(_request_query_principal(req)))
    assert effective == ("code" if allowed else "chat")
    kwargs = {"session_id": row["id"], "http_request": req, "mode": "chat" if allowed else "code"}
    listed = await commands.list_commands(**kwargs)
    assert {cmd.name for cmd in listed.commands} & {"direct", "guide"} == ({"direct", "guide"} if allowed else set())
    if allowed:
        assert (await commands.get_command("direct", **kwargs)).content == "Direct body $1"
    else:
        with pytest.raises(HTTPException) as error:
            await commands.get_command("direct", **kwargs)
        assert error.value.status_code == 404


@pytest.mark.parametrize("kind", ["anthropic", "delegated"])
@pytest.mark.parametrize("token,project_id,mode,allowed", [
    ("local-owner", None, "chat", False), ("local-owner", None, "code", True),
    ("preview:fixture", None, "code", True), ("preview:fixture", None, "chat", False),
    ("local-owner", "mine", "chat", False), ("preview:fixture", "mine", "code", True),
    ("api", None, "code", True), ("api", None, "chat", False),
])
async def test_unsaved_catalog_preserves_explicit_request_mode(source_tree, monkeypatch, kind, token, project_id, mode, allowed):
    home, _, child = source_tree
    put(home / ".fluid/commands/direct.md", "Fresh direct body")
    req = caller({"project_id": "mine", "profile_ids": ["p"]} if token == "api" else None)
    req.state.auth_token = token
    monkeypatch.setattr(context.database, "get_profile", lambda pid: {"config": {"cwd": str(child), "setting_sources": ["user"]}})
    monkeypatch.setattr(context.database, "get_project", lambda pid: {"id": "mine", "working_dir": str(child)})
    monkeypatch.setattr(context, "get_project_working_dir", lambda row: str(child))
    monkeypatch.setattr(commands, "command_provider_kind", lambda *a: kind)
    kwargs = {"project_id": project_id, "profile_id": "p", "http_request": req, "mode": mode}
    listed = await commands.list_commands(**kwargs)
    assert ("direct" in {cmd.name for cmd in listed.commands}) == allowed
    if allowed:
        assert (await commands.get_command("direct", **kwargs)).content == "Fresh direct body"
    else:
        with pytest.raises(HTTPException) as error:
            await commands.get_command("direct", **kwargs)
        assert error.value.status_code == 404


@pytest.mark.parametrize('kind', ['anthropic','openai_responses','openrouter','voice','unavailable'])
def test_native_menu_uses_fluid_files_without_cli_builtins(monkeypatch, tmp_path, kind):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path / 'home')
    (tmp_path / '.git').mkdir()
    command_dir = tmp_path / '.fluid' / 'commands'
    command_dir.mkdir(parents=True)
    (command_dir / 'native.md').write_text('Native command', encoding='utf-8')
    result = slash_commands.get_all_commands(str(tmp_path), provider_kind=kind)
    # The native engine runs a bare /compact itself (#1831); no other CLI built-in.
    native = kind not in ('voice', 'unavailable')
    assert {cmd['name'] for cmd in result} == ({'native', 'compact'} if native else set())
    bare = slash_commands.get_all_commands(str(tmp_path), provider_kind=kind, setting_sources=[])
    assert [cmd['name'] for cmd in bare] == (['compact'] if native else [])


def test_delegated_menu_keeps_prompt_commands_and_omits_terminal_settings():
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


@pytest.mark.parametrize('endpoint', ['list','detail'])
async def test_session_command_endpoints_deny_other_principal_before_path_scan(monkeypatch, endpoint):
    user={'id':'me','project_id':'mine'}
    monkeypatch.setattr(context.database,'get_session',lambda sid:{'id':sid,'project_id':'theirs','api_user_id':'other'})
    resolve=Mock(side_effect=AssertionError('unauthorized path resolved'))
    monkeypatch.setattr('app.core.project_resolver.resolve_existing_cwd',resolve)
    with pytest.raises(HTTPException) as exc:
        if endpoint=='list': await commands.list_commands(session_id='foreign',http_request=caller(user))
        else: await commands.get_command('review',session_id='foreign',http_request=caller(user))
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
