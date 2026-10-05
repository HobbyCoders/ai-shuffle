"""The WS permission callback decides request_access but records nothing.

``mcp__computer-use__request_access`` grants only on a server-side record of
an allow decision (see ``app.mcp.computer_use.access``). That record is made
once, when fluid's gate lets the allowed call through (HarnessAdapter
``_on_tool_allowed``), whatever allowed it. The callback answers the prompt —
approved, denied, or bypass mode without a prompt — and must not record one
itself, or an allowed call would leave a second, unspent approval behind.

The session's first request is the exception to "bypass without a prompt":
it always goes to a person, past saved rules, and the person's allow is
recorded as the first-use confirmation the tool requires.
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
        previous = qe.run_sync
        async def read_binding(fn, *args, **kwargs):
            from app.db import database
            if fn == database.get_session:
                return {"permission_mode": permission_mode}
            return await previous(fn, *args, **kwargs)
        stack.enter_context(patch.object(qe, "run_sync", side_effect=read_binding))
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
def _clean(monkeypatch):
    access.revoke(SID)
    # A person shares this desktop unless a test says otherwise.
    monkeypatch.setattr("app.api.computer_use.is_dedicated_desktop", lambda: False)
    yield
    access.revoke(SID)


async def test_dedicated_desktop_bypass_allows_the_first_use_without_prompting(tmp_path, monkeypatch):
    """Only agents use a dedicated desktop: nobody to protect, nobody to ask."""
    monkeypatch.setattr("app.api.computer_use.is_dedicated_desktop", lambda: True)
    callback = await _callback(tmp_path, "bypassPermissions")
    prompt = AsyncMock(side_effect=AssertionError("dedicated bypass must not prompt"))
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback(TOOL, {"apps": ["Notepad"]},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert type(result).__name__ == "PermissionResultAllow"
    assert not access.consume_first_use_confirmation(SID)


async def test_dedicated_desktop_first_use_outside_bypass_may_use_saved_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.computer_use.is_dedicated_desktop", lambda: True)
    callback = await _callback(tmp_path, "default")
    prompt = AsyncMock(return_value=PermissionResultAllow())
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        await callback(TOOL, {"apps": ["Notepad"]},
                       SimpleNamespace(tool_use_id="t", agent_id=None))
    prompt.assert_awaited_once()
    assert prompt.await_args.args[0]["skip_saved_rules"] is False


@pytest.mark.parametrize("answer,allowed", [
    (PermissionResultAllow(), True),
    (PermissionResultDeny(message="no"), False),
])
async def test_prompt_answer_decides_without_recording(tmp_path, answer, allowed):
    """The gate approval is not recorded here; a person's allow of the first
    request is recorded as the session's first-use confirmation."""
    callback = await _callback(tmp_path, "default")
    with patch("app.core.provider_agent_approvals.route_permission",
               new=AsyncMock(return_value=answer)):
        result = await callback(TOOL, {"apps": ["Notepad"], "reason": "r"},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert (type(result).__name__ == "PermissionResultAllow") is allowed
    assert not access.consume_request_approval(SID)
    assert access.consume_first_use_confirmation(SID) is allowed


@pytest.mark.parametrize("mode", ["default", "bypassPermissions"])
async def test_first_use_prompt_skips_saved_rules(tmp_path, mode):
    """The session's first request goes to a person in every mode; a saved
    "always allow" rule cannot answer it."""
    callback = await _callback(tmp_path, mode)
    prompt = AsyncMock(return_value=PermissionResultAllow())
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback(TOOL, {"apps": ["Notepad"]},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert type(result).__name__ == "PermissionResultAllow"
    prompt.assert_awaited_once()
    assert prompt.await_args.args[0]["skip_saved_rules"] is True


async def test_bypass_after_the_first_grant_allows_without_prompting(tmp_path):
    callback = await _callback(tmp_path, "bypassPermissions")
    access.grant(SID, ["Notepad"])
    prompt = AsyncMock(side_effect=AssertionError("bypass must not prompt again"))
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback(TOOL, {"apps": ["Calculator"]},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
    assert type(result).__name__ == "PermissionResultAllow"
    assert not access.consume_request_approval(SID)
    assert not access.consume_first_use_confirmation(SID)


async def test_later_requests_outside_bypass_may_use_saved_rules(tmp_path):
    callback = await _callback(tmp_path, "default")
    access.grant(SID, ["Notepad"])
    prompt = AsyncMock(return_value=PermissionResultAllow())
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        await callback(TOOL, {"apps": ["Calculator"]},
                       SimpleNamespace(tool_use_id="t", agent_id=None))
    assert prompt.await_args.args[0]["skip_saved_rules"] is False


async def test_other_computer_use_actions_record_nothing(tmp_path):
    callback = await _callback(tmp_path, "default")
    await callback("mcp__computer-use__left_click", {"coordinate": [1, 1]},
                   SimpleNamespace(tool_use_id="t", agent_id=None))
    assert not access.consume_request_approval(SID)
