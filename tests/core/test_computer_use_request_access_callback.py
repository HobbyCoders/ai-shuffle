"""The WS permission callback decides request_access but records nothing.

``mcp__computer-use__request_access`` grants only on a server-side record of
an allow decision (see ``app.mcp.computer_use.access``). That record is made
once, when fluid's gate lets the allowed call through (HarnessAdapter
``_on_tool_allowed``), whatever allowed it. The callback answers the prompt —
approved, denied, or bypass mode without a prompt — and must not record one
itself, or an allowed call would leave a second, unspent approval behind.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_engine as qe
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.mcp.computer_use import access
from tests.core.engine_harness import StubPool, collect
from tests.core.test_busy_write_guard_engine import _chat_patches

SID = "cu-callback"
TOOL = "mcp__computer-use__request_access"


async def _callback(tmp_path, permission_mode):
    """Build a chat turn far enough to wire its can_use_tool callback."""
    with ExitStack() as stack:
        options = _chat_patches(stack, StubPool())
        options.cwd = str(tmp_path / "missing-cwd-stops-before-connect")
        stack.enter_context(patch.object(qe, "memory_leg_for_options", return_value="native"))
        stack.enter_context(patch("app.core.hook_service.attach_hooks_to_options", return_value=None))
        await collect(qe._stream_to_websocket_impl(
            "hi", SID, "claude-code", overrides={"permission_mode": permission_mode},
            broadcast_func=AsyncMock(),
        ))
    assert callable(options.can_use_tool)
    return options.can_use_tool


@pytest.fixture(autouse=True)
def _clean():
    access.revoke(SID)
    yield
    access.revoke(SID)


@pytest.mark.parametrize("answer,allowed", [
    (PermissionResultAllow(), True),
    (PermissionResultDeny(message="no"), False),
])
async def test_prompt_answer_decides_without_recording(tmp_path, answer, allowed):
    callback = await _callback(tmp_path, "default")
    with patch("app.core.provider_agent_approvals.route_permission",
               new=AsyncMock(return_value=answer)):
        result = await callback(TOOL, {"apps": ["Notepad"], "reason": "r"},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert (type(result).__name__ == "PermissionResultAllow") is allowed
    assert not access.consume_request_approval(SID)


async def test_bypass_mode_allows_without_prompting_or_recording(tmp_path):
    callback = await _callback(tmp_path, "bypassPermissions")
    prompt = AsyncMock(side_effect=AssertionError("bypass must not prompt"))
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback(TOOL, {"apps": ["Notepad"]},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert type(result).__name__ == "PermissionResultAllow"
    assert not access.consume_request_approval(SID)


async def test_other_computer_use_actions_record_nothing(tmp_path):
    callback = await _callback(tmp_path, "default")
    await callback("mcp__computer-use__left_click", {"coordinate": [1, 1]},
                   SimpleNamespace(tool_use_id="t", agent_id=None))
    assert not access.consume_request_approval(SID)
