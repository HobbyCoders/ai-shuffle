"""Command discovery uses the selected runtime and authorizes before disk reads."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.api import command_context as context
from app.api import commands
from app.core import slash_commands


def caller(user=None):
    request = Request({'type':'http','headers':[]})
    request.state.api_user = user
    return request


@pytest.mark.parametrize('kind', ['anthropic','openai_responses','openrouter','voice','unavailable'])
def test_native_menu_does_not_advertise_unwired_slash_paths(monkeypatch, kind):
    monkeypatch.setattr(slash_commands,'discover_commands',lambda cwd: [])
    monkeypatch.setattr(slash_commands,'discover_plugin_commands',lambda: [{'name':'plugin:review','type':'plugin'}])
    monkeypatch.setattr(slash_commands,'discover_skills',lambda cwd: [{'name':'my-skill','type':'skill'}])
    result = slash_commands.get_all_commands('/fixture',provider_kind=kind)
    assert result == []
    # The same discovered entries remain usable on the delegated dispatcher.
    delegated = slash_commands.get_all_commands('/fixture', provider_kind='delegated')
    assert {'plugin:review','my-skill'} <= {c['name'] for c in delegated}


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
