"""A cancelled voice attempt must not cancel a rotating subscription login."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core import codex_token_refresh, voice_stream, xai_token_refresh
from app.core.voice_codex import CodexVoiceStream
from app.core.voice_grok import SuperGrokVoiceStream
from tests.unit.test_voice_stream_reconnect import until


@pytest.fixture(params=["ChatGPT", "SuperGrok"])
def subscription(request, monkeypatch):
    label = request.param
    cls, model, module = ((CodexVoiceStream, "gpt-live-1-codex", codex_token_refresh)
                          if label == "ChatGPT" else
                          (SuperGrokVoiceStream, "grok-voice-supergrok", xai_token_refresh))
    call = cls(AsyncMock(), {"id": "refresh-cancel"}, {
        "model": model, "voice": "cove" if label == "ChatGPT" else "eve", "provider_id": "sub"}, "", 0)
    return call, module, label


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["timeout", "cancelled-waiter"])
async def test_cancelled_attempt_keeps_rotated_login_until_it_is_saved(subscription, monkeypatch, cancel):
    call, module, _ = subscription
    entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {"refresh_token": "fake-old-refresh"}

    async def rotate(provider_id, *, stale_access_token):
        assert provider_id == "sub" and stale_access_token == "fake-stale-access"
        entered.set()  # The provider has issued a replacement refresh token.
        await release.wait()  # Persistence has not completed yet.
        state["refresh_token"] = "fake-rotated-refresh"
        saved.set()
        return True

    monkeypatch.setattr(module, "refresh_on_401_once", rotate)

    async def attempt():
        async with asyncio.timeout(0.1 if cancel == "timeout" else None):
            return await call.renew_after_401("fake-stale-access")

    task = asyncio.create_task(attempt())
    await asyncio.wait_for(entered.wait(), 1)
    if cancel == "cancelled-waiter":
        task.cancel()
    with pytest.raises(TimeoutError if cancel == "timeout" else asyncio.CancelledError):
        await task
    assert voice_stream._login_renewals
    assert state["refresh_token"] == "fake-old-refresh"
    release.set()
    await asyncio.wait_for(saved.wait(), 1)
    assert state["refresh_token"] == "fake-rotated-refresh"
    await until(lambda: not voice_stream._login_renewals)


@pytest.mark.asyncio
async def test_refresh_exception_is_logged_without_sensitive_details(subscription, monkeypatch, caplog):
    call, module, label = subscription
    monkeypatch.setattr(module, "refresh_on_401_once", AsyncMock(side_effect=RuntimeError("synthetic-private-value")))
    result = await call.renew_after_401("fake-stale-access")
    assert result is False if label == "ChatGPT" else "refused the login" in result
    assert f"{label} voice: login renewal after 401 failed" in caplog.text
    assert "synthetic-private-value" not in caplog.text
    await until(lambda: not voice_stream._login_renewals)
