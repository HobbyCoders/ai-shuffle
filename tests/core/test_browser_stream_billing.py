"""Billing browser viewers never receive authentication-bearing page URLs."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.browser_stream import BrowserStreamHub, _Viewer


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["page", "status"])
async def test_billing_viewer_redacts_urls_without_changing_other_viewers(
    monkeypatch, message_type
):
    sent = []

    async def send(socket, message):
        sent.append(message)

    monkeypatch.setattr("app.core.browser_stream.safe_ws_send", send)
    hub = BrowserStreamHub("openai-billing:provider")
    private = _Viewer(AsyncMock(), redact_urls=True)
    regular = _Viewer(AsyncMock())
    message = {
        "type": message_type,
        "url": "https://auth.openai.com/callback?code=test-secret",
        "loading": False,
    }
    assert await hub.send_to_viewer(private, message)
    assert sent[-1]["url"] is None
    assert await hub.send_to_viewer(regular, message)
    assert sent[-1]["url"] == message["url"]


@pytest.mark.asyncio
async def test_redaction_preserves_live_frames(monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr("app.core.browser_stream.safe_ws_send", sender)
    hub = BrowserStreamHub("openai-billing:provider")
    viewer = _Viewer(AsyncMock(), redact_urls=True)
    frame = {"type": "frame", "data": "test-frame", "metadata": {"deviceWidth": 390}}
    hub._fan_out_frame(frame["data"], frame["metadata"])
    await hub.send_to_viewer(viewer, hub._last_frame)
    sender.assert_awaited_once_with(viewer.websocket, frame)


@pytest.fixture
def login_panel(monkeypatch):
    from app.api import browser_panel as panel

    socket = AsyncMock()
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(panel, "safe_ws_send", sender)
    monkeypatch.setattr("app.core.browser_stream.safe_ws_send", sender)
    hub = BrowserStreamHub("openai-billing:provider")
    hub.page_url = "https://example.test/callback?code=test-private"
    viewer = _Viewer(socket, redact_urls=True)
    attach = AsyncMock(return_value=(hub, viewer))
    detach = AsyncMock()
    monkeypatch.setattr(panel.browser_stream, "attach_viewer", attach)
    monkeypatch.setattr(panel.browser_stream, "detach_viewer", detach)
    monkeypatch.setattr(
        panel,
        "_initial_status",
        AsyncMock(
            return_value={"type": "status", "state": "live", "url": hub.page_url}
        ),
    )
    broker = Mock(
        side_effect=AssertionError("Login must not allocate a workspace broker")
    )
    monkeypatch.setattr(panel.browser_control, "get", broker)
    yield SimpleNamespace(
        panel=panel, socket=socket, sender=sender, hub=hub, viewer=viewer,
        attach=attach, detach=detach,
    )
    broker.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "method"),
    [
        ({"type": "input", "kind": "text", "text": "hello"}, "Input.insertText"),
        ({"type": "command", "command": "reload"}, "Page.reload"),
        (
            {"type": "viewport", "width": 390, "height": 844},
            "Emulation.setDeviceMetricsOverride",
        ),
        ({"type": "navigate", "url": "https://example.test"}, None),
        ({"type": "inspect", "x": 1, "y": 1}, None),
        ({"type": "workspace-command", "op": "takeover", "args": {}}, None),
    ],
)
async def test_login_protocol_is_redacted_and_independent_of_workspace(
    login_panel, payload, method
):
    from fastapi import WebSocketDisconnect

    env = login_panel
    env.socket.receive_text.side_effect = [json.dumps(payload), WebSocketDisconnect()]
    login_input = AsyncMock()

    await env.panel.serve_browser_panel(
        env.socket, "openai-billing:provider", login_only=True, login_input=login_input,
    )

    env.attach.assert_awaited_once_with(
        "openai-billing:provider", env.socket, redact_urls=True,
    )
    env.detach.assert_awaited_once_with("openai-billing:provider", env.viewer)
    messages = [call.args[1] for call in env.sender.await_args_list]
    assert messages[0] == {"type": "hello", "protocol": 1}
    assert [message["type"] for message in messages] == ["hello", "status", "page"]
    assert messages[1]["url"] is None
    assert messages[2]["url"] is None
    if method is None:
        login_input.assert_not_awaited()
        env.socket.close.assert_awaited_once_with(code=4400, reason="Protocol violation")
    else:
        login_input.assert_awaited_once_with(
            env.panel.browser_stream.translate_input(payload, bounds=env.hub.last_bounds()),
        )
        assert login_input.await_args.args[0]["method"] == method
        env.socket.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["unavailable", "failing", "absent"])
@pytest.mark.parametrize("payload", [
    {"type": "viewport", "width": 390, "height": 844},
    {"type": "input", "kind": "text", "text": "hello"},
])
async def test_login_input_failure_reports_generic_error_and_closes(
    login_panel, owner, payload,
):
    from fastapi import WebSocketDisconnect

    from app.core.openai_billing_browser import BillingBrowserError

    env = login_panel
    env.socket.receive_text.side_effect = [json.dumps(payload), WebSocketDisconnect()]
    options = {}
    if owner != "absent":
        failure = (
            BillingBrowserError("billing_browser_unavailable")
            if owner == "unavailable"
            else RuntimeError("https://auth.openai.com/callback?code=test-private")
        )
        options["login_input"] = AsyncMock(side_effect=failure)

    await env.panel.serve_browser_panel(
        env.socket, "openai-billing:provider", login_only=True, **options,
    )

    messages = [call.args[1] for call in env.sender.await_args_list]
    assert [message["type"] for message in messages] == ["hello", "status", "page", "error"]
    assert messages[-1] == {
        "type": "error",
        "message": "The sign-in browser is unavailable. Close this dialog and try again.",
    }
    assert "test-private" not in json.dumps(messages)
    env.socket.close.assert_awaited_once_with(code=1011, reason="Sign-in browser unavailable")
    env.detach.assert_awaited_once_with("openai-billing:provider", env.viewer)
    if owner != "absent":
        options["login_input"].assert_awaited_once()
