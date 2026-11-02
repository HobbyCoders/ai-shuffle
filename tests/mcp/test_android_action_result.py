"""Keep native action status truthful at the agent-facing tool boundary."""

from unittest.mock import AsyncMock, patch

import pytest

from app.mcp import builtin_tools, inproc
from tests.mcp.tool_helpers import tool_result_json


def _android_action():
    captured = {}
    real_create = inproc.create_tool_server

    def capture(name, version, tools):
        captured.update({tool.name: tool for tool in tools})
        return real_create(name=name, version=version, tools=tools)

    with patch.object(inproc, 'create_tool_server', side_effect=capture):
        builtin_tools.create_builtin_tools_server(session_id='android-action-test')
    return captured['AndroidAction'].handler


@pytest.mark.asyncio
@pytest.mark.parametrize(('outcome', 'success'), [
    ({'command_id': 'one', 'result': {'ok': True, 'status': 'launched_confirmation'}}, True),
    ({'command_id': 'one', 'result': {'ok': False, 'error': 'denied'}}, False),
    ({'command_id': 'one', 'result': {'success': False}}, False),
    ({'command_id': 'one', 'error': 'command_expired_or_cancelled', 'outcome_unknown': True}, False),
    ({'command_id': 'one', 'error': 'device_timeout', 'outcome_unknown': True}, False),
])
async def test_action_preserves_actual_device_outcome(monkeypatch, outcome, success):
    from app.services import mobile_tools
    execute = AsyncMock(return_value=outcome)
    monkeypatch.setattr(mobile_tools, 'execute', execute)
    args = {'device_id': 'phone', 'name': 'open_url', 'arguments': {'url': 'https://example.com'}}
    result = tool_result_json(await _android_action()(args))
    assert result == {**outcome, 'success': success}
    execute.assert_awaited_once_with('android-action-test', 'phone', 'open_url', args['arguments'], 25)


@pytest.mark.asyncio
async def test_service_failure_warns_that_side_effect_may_have_run(monkeypatch):
    from app.services import mobile_tools
    monkeypatch.setattr(mobile_tools, 'execute', AsyncMock(side_effect=RuntimeError('offline')))
    result = tool_result_json(await _android_action()({
        'device_id': 'phone', 'name': 'open_url', 'arguments': {'url': 'https://example.com'},
    }))
    assert result['success'] is False
    assert result['outcome_unknown'] is True
    assert 'Verify device state before retrying' in result['message']
