"""Billing browser viewers never receive authentication-bearing page URLs."""

from unittest.mock import AsyncMock

import pytest

from app.core.browser_stream import BrowserStreamHub, _Viewer


@pytest.mark.asyncio
async def test_billing_viewer_redacts_urls_without_changing_other_viewers(monkeypatch):
    sent = []

    async def send(socket, message):
        sent.append(message)

    monkeypatch.setattr("app.core.browser_stream.safe_ws_send", send)
    hub = BrowserStreamHub("openai-billing:provider")
    private = _Viewer(AsyncMock(), redact_urls=True)
    regular = _Viewer(AsyncMock())
    message = {"type": "page", "url": "https://auth.openai.com/callback?code=test-secret", "loading": False}
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
    await hub.send_to_viewer(viewer, frame)
    sender.assert_awaited_once_with(viewer.websocket, frame)
