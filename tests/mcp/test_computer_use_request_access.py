"""request_access grants only on a server-side allow decision.

The permission callback (or the headless auto-approver) records one approval
per allowed call; the tool spends it. Bypass mode is itself the decision. A
call that reaches the tool with neither is refused and grants nothing. No real
display or input device is touched.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, access, activity, scaling, safety

SID = "cu-approval"


def payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(activity, "_sessions", {})
    monkeypatch.setattr(activity, "_last_stop", None)
    access.revoke(SID)
    monkeypatch.setitem(sys.modules, "pynput.mouse", SimpleNamespace(
        Controller=MagicMock, Button=SimpleNamespace(left="left", right="right", middle="middle")))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(
        Controller=MagicMock, Key=SimpleNamespace()))
    monkeypatch.setattr(sdk, "computer_use_available", lambda: (True, "ok"))
    monkeypatch.setattr("app.mcp.computer_use.overlay.ensure_started", lambda: None)
    monkeypatch.setattr(safety, "ensure_started", lambda: True)
    monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)

    class Capture:  # display enumeration only; nothing is captured
        monitors = [{"left": 0, "top": 0, "width": 200, "height": 100}] * 2

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))
    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server",
                        lambda cfg, tools: {t.name: t.handler for t in tools})

    def build(permission_mode):
        return sdk.create_computer_use_server(
            enabled=True, permission_mode=permission_mode, session_id=SID)

    yield build
    access.revoke(SID)


async def test_approved_call_grants_and_the_approval_is_one_shot(server):
    tools = server("default")
    access.approve_request(SID)
    result = payload(await tools["request_access"]({"apps": ["Notepad"], "reason": "r"}))
    assert result["granted_apps"] == ["Notepad"]
    again = await tools["request_access"]({"apps": ["Calculator"], "reason": "r"})
    assert again["isError"]
    assert access.info(SID)["apps"] == ["Notepad"]


async def test_direct_call_without_a_decision_is_refused(server):
    tools = server("default")
    result = await tools["request_access"](
        {"apps": ["Notepad"], "reason": "r", "clipboardWrite": True})
    assert result["isError"]
    assert "not approved" in payload(result)["error"]
    assert not access.is_granted(SID)


async def test_approval_is_keyed_to_the_servers_session(server):
    tools = server("default")
    access.approve_request("another-session")
    try:
        assert (await tools["request_access"]({"apps": ["Notepad"], "reason": "r"}))["isError"]
    finally:
        access.revoke("another-session")


async def test_bypass_mode_needs_no_record(server):
    result = payload(await server("bypassPermissions")["request_access"](
        {"apps": ["Notepad"], "reason": "r"}))
    assert result["granted_apps"] == ["Notepad"]


async def test_live_permission_mode_wins_over_the_build_time_mode(server, monkeypatch):
    """The mode can change between turns without rebuilding the server."""
    from app.core import session_pool
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m", permission_mode="bypassPermissions")
    state = SimpleNamespace(adapter=adapter)
    pool = SimpleNamespace(peek=lambda sid: state if sid == SID else None)
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    assert payload(await server("default")["request_access"](
        {"apps": ["A"], "reason": "r"}))["success"]
    adapter._options.permission_mode = "default"
    assert (await server("bypassPermissions")["request_access"](
        {"apps": ["B"], "reason": "r"}))["isError"]


def test_unspent_approval_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: now[0])
    access.approve_request(SID)
    now[0] += access._APPROVAL_TTL_S + 1
    assert not access.consume_request_approval(SID)


# ─── where the runtime decides: both approver paths record the approval ─────


async def test_headless_delegated_auto_approval_records(monkeypatch):
    """No interactive approver + a mode that approves on its own: the adapter's
    allow is recorded for the app session the tool server is bound to."""
    from unittest.mock import AsyncMock

    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import PermissionRequestEvent, SessionOptions

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m", permission_mode="acceptEdits")
    adapter._options.extra["app_session_id"] = SID
    adapter._engine = SimpleNamespace(_registry_spec=lambda name: SimpleNamespace(
        name="mcp__computer-use__request_access"))
    adapter.respond_to_permission = AsyncMock()
    try:
        await adapter._answer_without_approver(PermissionRequestEvent(
            tool_use_id="t", input={},
            name="mcp__fluid_bundled__mcp__computer-use__request_access"))
        assert access.consume_request_approval(SID)
        # Ask mode denies: nothing is recorded.
        adapter._options.permission_mode = "default"
        await adapter._answer_without_approver(PermissionRequestEvent(
            tool_use_id="t2", input={},
            name="mcp__fluid_bundled__mcp__computer-use__request_access"))
        assert not access.consume_request_approval(SID)
    finally:
        access.revoke(SID)
