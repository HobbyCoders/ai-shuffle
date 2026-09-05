"""Billing browser viewers never receive authentication-bearing page URLs."""

from unittest.mock import AsyncMock

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
    monkeypatch, payload, method
):
    import json
    from unittest.mock import Mock

    from fastapi import WebSocketDisconnect

    from app.api import browser_panel as panel

    socket = AsyncMock()
    socket.receive_text.side_effect = [json.dumps(payload), WebSocketDisconnect()]
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(panel, "safe_ws_send", sender)
    monkeypatch.setattr("app.core.browser_stream.safe_ws_send", sender)
    hub = BrowserStreamHub("openai-billing:provider")
    hub.page_url = "https://example.test/callback?code=test-private"
    hub.dispatch_input = AsyncMock()
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

    await panel.serve_browser_panel(socket, "openai-billing:provider", login_only=True)

    attach.assert_awaited_once_with("openai-billing:provider", socket, redact_urls=True)
    detach.assert_awaited_once_with("openai-billing:provider", viewer)
    broker.assert_not_called()
    messages = [call.args[1] for call in sender.await_args_list]
    assert messages[0] == {"type": "hello", "protocol": 1}
    assert [message["type"] for message in messages] == ["hello", "status", "page"]
    assert messages[1]["url"] is None
    assert messages[2]["url"] is None
    if method is None:
        hub.dispatch_input.assert_not_awaited()
        socket.close.assert_awaited_once_with(code=4400, reason="Protocol violation")
    else:
        hub.dispatch_input.assert_awaited_once()
        assert hub.dispatch_input.await_args.args[0]["method"] == method
        socket.close.assert_not_awaited()
