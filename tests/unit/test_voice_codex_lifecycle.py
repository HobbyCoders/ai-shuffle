"""Startup races use fake peers only; never open a provider session."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import voice_stream
from app.core.voice_codex import CodexVoiceStream
from app.core.voice_codex_transport import CodexPeer


class FakeConnection:
    def __init__(self):
        self.handlers = {}
        self.connectionState = "new"
        self.closed = False
        self.localDescription = type("SDP", (), {"sdp": "synthetic-offer"})()

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
        return register

    def createDataChannel(self, name):
        return self

    async def createOffer(self):
        return self.localDescription

    async def setLocalDescription(self, offer):
        pass

    async def setRemoteDescription(self, answer):
        assert not self.closed, "closed peer must never connect"

    async def close(self):
        self.closed = True


def make_call(monkeypatch):
    call = CodexVoiceStream(AsyncMock(), {"id": "startup-race"}, {
        "model": "gpt-live-1-codex", "voice": "cove", "provider_id": "sub"}, "fake-token", 5)
    call.upstream = CodexPeer()
    pc = FakeConnection()
    call.upstream.make_peer = Mock(return_value=(pc, lambda audio: None, lambda sdp: sdp))
    monkeypatch.setitem(voice_stream.calls, call.session_id, call)
    return call, pc


async def until_entered(entered, task):
    """Wait for the fake to be reached, or for the task to end without reaching it.

    No wall-clock deadline: a stalled CI runner (busy CPU, cold lazy imports on
    the event loop) outlasted the old 1s wait and timed out a correct run (#794).
    """
    waiter = asyncio.ensure_future(entered.wait())
    try:
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    assert entered.is_set(), f"task ended before reaching the fake: {task!r}"


async def hang_up(call, action):
    if action == "hangup":
        await voice_stream.stop(call.session_id)
    else:
        # The real run() sets this after teardown. No full run is needed here.
        call.finished.set()
        await voice_stream.take_over(call.session_id)
        await asyncio.gather(*voice_stream._closing)


@pytest.mark.asyncio
async def test_peer_closed_before_open_never_creates_a_connection():
    peer = CodexPeer()
    peer.make_peer = Mock(side_effect=AssertionError("must not create a peer"))
    client = AsyncMock()
    await peer.close()
    with pytest.raises(ValueError, match="closed"):
        await peer.open(client, "fake-token", None, {})
    peer.make_peer.assert_not_called()
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_always_closes_a_connection_it_holds():
    peer = CodexPeer()
    await peer.close()
    pc = FakeConnection()
    peer.pc = pc
    await peer.close()
    assert pc.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hangup", "resume"])
async def test_close_while_loading_token_never_opens_peer(monkeypatch, action):
    from app.core.provider import fluid_credentials
    monkeypatch.setattr("app.core.voice_dependencies.availability", lambda: (True, None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def load(provider):
        entered.set()
        await release.wait()
        return "fake-token", None

    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", load)
    call, pc = make_call(monkeypatch)
    task = asyncio.create_task(call.start_live([]))
    await until_entered(entered, task)
    await hang_up(call, action)
    release.set()
    with pytest.raises(ValueError, match="could not start"):
        await task
    call.upstream.make_peer.assert_not_called()
    assert call.api_key == ""


@pytest.mark.asyncio
async def test_close_while_loading_waits_out_a_slow_cold_start(monkeypatch):
    """#794: a cold start that blocks the loop past 1s must not fail the race test."""
    import time

    from app.core.provider import fluid_credentials

    monkeypatch.setattr("app.core.voice_dependencies.availability", lambda: (True, None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def load(provider):
        # A runner stall (lazy imports, a busy CPU) longer than the old 1s
        # wait, then one more await before the fake is reached. CI saw the
        # Event already set when that wait had timed out.
        time.sleep(1.2)  # noqa: ASYNC251 — blocking the loop is the point
        await asyncio.sleep(0)
        entered.set()
        await release.wait()
        return "fake-token", None

    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", load)
    call, _ = make_call(monkeypatch)
    task = asyncio.create_task(call.start_live([]))
    await until_entered(entered, task)
    await hang_up(call, "hangup")
    release.set()
    with pytest.raises(ValueError, match="could not start"):
        await task
    call.upstream.make_peer.assert_not_called()
    assert call.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hangup", "resume"])
async def test_close_during_post_leaves_no_open_connection(monkeypatch, action):
    call, pc = make_call(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def post(*args, **kwargs):
        entered.set()
        await release.wait()
        return type("Response", (), {"status_code": 201, "text": "synthetic-answer"})()

    client = AsyncMock()
    client.post.side_effect = post
    task = asyncio.create_task(call.upstream.open(client, "fake-token", None, {}))
    await until_entered(entered, task)
    await hang_up(call, action)
    release.set()
    with pytest.raises(ValueError, match="closed"):
        await task
    await call.upstream.close()
    assert pc.closed
    assert call.upstream.closed


@pytest.mark.asyncio
async def test_closed_call_start_raises_before_loading_credentials(monkeypatch):
    from app.core.provider import fluid_credentials
    load = AsyncMock(side_effect=AssertionError("closed call must not load credentials"))
    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", load)
    call, pc = make_call(monkeypatch)
    await voice_stream.stop(call.session_id)
    with pytest.raises(ValueError, match="closed"):
        await call.start_live([])
    load.assert_not_awaited()
    call.upstream.make_peer.assert_not_called()
    assert call.api_key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hangup", "resume"])
async def test_close_during_remote_description_closes_late_connection(monkeypatch, action):
    call, pc = make_call(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def set_remote(answer):
        entered.set()
        await release.wait()
        # Model a native connect completing after the first close returned.
        pc.closed = False

    pc.setRemoteDescription = set_remote
    pc.close = AsyncMock(wraps=pc.close)
    client = AsyncMock()
    client.post.return_value = type("Response", (), {"status_code": 201, "text": "synthetic-answer"})()
    task = asyncio.create_task(call.upstream.open(client, "fake-token", None, {}))
    await until_entered(entered, task)
    await hang_up(call, action)
    assert pc.closed
    release.set()
    with pytest.raises(ValueError, match="closed"):
        await task
    assert pc.closed
    assert pc.close.await_count >= 2
    assert call.upstream.closed
