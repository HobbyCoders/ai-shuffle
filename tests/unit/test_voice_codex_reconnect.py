"""Subscription session renewal through the real adapter, with fake HTTP/RTC only."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import voice_codex, voice_prompts, voice_stream
from app.core.voice_codex_transport import CodexPeer, incoming_event
from tests.unit.test_voice_codex import FakePeer
from tests.unit.test_voice_stream_reconnect import FakeBrowser, FakeProgress, until


class FakeRTC(FakePeer):
    def __init__(self, expires_in=None):
        super().__init__()
        self.expires_in = expires_in
        self.connectionState = "connected"
        self.audio = []
        self.close_gate = None
        self.close_entered = asyncio.Event()
        self.close = AsyncMock(side_effect=self.end_local)

    def event(self, payload):
        self.handlers["message"](json.dumps(payload))

    async def setRemoteDescription(self, answer):
        session = {"id": "synthetic-id", "instructions": "private-upstream-metadata"}
        if self.expires_in is not None:
            session["expires_at"] = time.time() + self.expires_in
        self.event({"type": "session.started", "session": session})

    def end_remote(self):
        self.readyState = "closed"
        self.handlers["close"]()

    async def end_local(self):
        self.close_entered.set()
        if self.close_gate:
            await self.close_gate.wait()
        self.end_remote()


@pytest.fixture
def codex_call(monkeypatch):
    from app.core import http_client, turn_recorder, voice_bridge, voice_greeting, voice_progress
    from app.core.provider import fluid_credentials

    monkeypatch.setattr("app.core.voice_dependencies.availability", lambda: (True, None))
    monkeypatch.setattr(voice_stream.database, "update_session", lambda *a, **kw: None)
    monkeypatch.setattr(voice_stream.database, "set_session_active", lambda *a, **kw: None)
    monkeypatch.setattr(voice_progress, "VoiceProgress", FakeProgress)
    monkeypatch.setattr(voice_greeting, "caller_first_name", lambda *args: None)
    monkeypatch.setattr(voice_stream, "RECONNECT_DELAYS", (0, 0, 0))
    monkeypatch.setattr(voice_stream, "connect", AsyncMock(side_effect=AssertionError("no API-key socket")))
    h = SimpleNamespace(script=[], adapters=[], history=[], post_hook=None)
    monkeypatch.setattr(voice_bridge, "build_context_seed", lambda sid: "\n".join(h.history))

    async def record(sid, user, assistant, **kwargs):
        h.history.extend(text for text in (user, assistant) if text)

    h.record = AsyncMock(side_effect=record)
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", h.record)

    def make_peer(adapter):
        rtc = h.script.pop(0)
        h.adapters.append(adapter)
        return rtc, rtc.audio.append, lambda sdp: sdp

    monkeypatch.setattr(CodexPeer, "make_peer", make_peer)
    h.load = AsyncMock(side_effect=lambda provider: (f"fake-token-{h.load.await_count}", "fake-account"))
    monkeypatch.setattr(fluid_credentials, "_load_codex_access_token", h.load)

    async def post(*args, **kwargs):
        if h.post_hook:
            return await h.post_hook()
        return SimpleNamespace(status_code=201, text="synthetic-answer")

    h.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    monkeypatch.setattr(http_client, "get_http_client", lambda: h.client)
    h.browser = FakeBrowser()
    h.call = voice_codex.CodexVoiceStream(h.browser, {"id": "codex-renew"}, {
        "model": "gpt-live-1-codex", "voice": "cove", "provider_id": "sub",
        "options": {"opening": "Test opening."}}, "", 0)
    monkeypatch.setitem(voice_stream.calls, h.call.session_id, h.call)
    return h


@pytest.mark.parametrize("expiry", [1234567890, 1234567890.5, True, "123", None, float("inf"), float("nan")])
def test_adapter_preserves_only_a_finite_reported_expiry(expiry):
    event = incoming_event({"type": "session.started", "session": {
        "id": "private-upstream-metadata", "expires_at": expiry}})
    expected = {"type": "session.started"}
    if type(expiry) in (int, float) and expiry == expiry and expiry != float("inf"):
        expected["session"] = {"expires_at": expiry}
    assert event == expected


@pytest.mark.parametrize("reason, expected", [(None, "closed"), ("expired", "expired"),
                                             ("connection_lost", "connection_lost"),
                                             ("private-upstream-metadata", "other")])
def test_closed_reason_is_allowlisted(reason, expected):
    assert incoming_event({"type": "session.closed", "session": {"reason": reason}}) == {
        "type": "session.closed", "reason": expected}


@pytest.mark.asyncio
async def test_reported_expiry_renews_silently_with_produced_history_and_no_greeting(codex_call, monkeypatch):
    h = codex_call
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 1.9)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 0.5)
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 0)
    first, second = FakeRTC(expires_in=2.5), FakeRTC()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    assert first.sent  # A fresh call was asked to greet, whatever the wording.
    first.event({"type": "input_transcript.added", "item": {"text": "Change the header colour."}})
    first.event({"type": "output_transcript.added", "item": {"text": "I will change it."}})
    await until(lambda: h.call.pending["assistant"])
    await until(lambda: len(h.adapters) == 2 and h.call.connected)

    resumed = h.client.post.await_args_list[1].kwargs["json"]["session"]["initial_items"]
    text = resumed[0]["content"][0]["text"]
    assert "Change the header colour." in text and "I will change it." in text
    assert voice_stream.RESUME_NOTE in text
    assert h.record.await_count == 1  # Captions were stored before rebuilding the seed.
    assert second.sent == []  # No greeting or other instruction on the new peer.
    assert all(note not in json.dumps(second.sent) for note in (
        voice_prompts.GREETING_NEW_CALL, voice_prompts.GREETING_RETURNING_CALL))
    assert h.browser.events("status") == []
    assert first.close.await_count == 1 and h.call.reconnects == 1 and not h.call.failures
    assert h.call.expires_at > time.time() + voice_codex.CODEX_RENEW_S
    assert not h.call.heard_this_session and not h.call.deaf.is_set()
    assert h.load.await_count == 2
    assert [c.kwargs["headers"]["Authorization"] for c in h.client.post.await_args_list] == [
        "Bearer fake-token-1", "Bearer fake-token-2"]
    assert h.adapters[0] is not h.adapters[1] and h.call.api_key == ""
    await h.call.close()
    await asyncio.wait_for(run, 3)
    assert h.browser.events("error") == []


@pytest.mark.asyncio
async def test_reported_renewal_waits_for_quiet(codex_call, monkeypatch):
    call = codex_call.call
    call.expires_at = time.time() + 60
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 60)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 1)
    call.settling = ["pending-handoff"]
    renew = asyncio.create_task(call.renew_when_due())
    await asyncio.sleep(0.3)
    assert not renew.done()
    call.settling = None
    await asyncio.wait_for(renew, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["channel", "peer", "session"])
async def test_old_provider_end_is_renewed_without_failure(codex_call, monkeypatch, signal):
    h = codex_call
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 0)
    first, second = FakeRTC(), FakeRTC()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    assert h.call.expires_at > time.time() + voice_codex.CODEX_RENEW_S
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    if signal == "channel":
        first.end_remote()
    elif signal == "peer":
        first.connectionState = "failed"
        await first.handlers["connectionstatechange"]()
    else:
        first.event({"type": "session.closed", "reason": "expired"})
    await until(lambda: len(h.adapters) == 2 and h.call.connected)
    assert not h.call.failures and second.sent == []
    assert h.browser.events("status") == [{"type": "status", "state": "reconnecting"},
                                          {"type": "status", "state": "live"}]
    await h.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["early-close", "old-error", "old-rejected-close"])
async def test_failures_are_counted_but_never_start_an_automatic_retry(codex_call, signal):
    h = codex_call
    first = FakeRTC()
    h.script += [first, FakeRTC()]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    if signal == "early-close":
        first.end_remote()
    else:
        h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
        first.event({"type": "error", "error": {"message": "private-upstream-metadata"}}
                    if signal == "old-error" else {"type": "session.closed", "reason": "rejected"})
    await asyncio.wait_for(run, 3)
    assert len(h.call.failures) == 1 and h.call.reconnects == 0
    assert h.client.post.await_count == 1 and h.load.await_count == 1
    assert "private-upstream-metadata" not in str(h.browser.send_json.await_args_list)


@pytest.mark.asyncio
async def test_notes_during_old_peer_close_are_held_and_stale_handoffs_become_plain_context(codex_call):
    h = codex_call
    first, second = FakeRTC(), FakeRTC()
    first.close_gate = asyncio.Event()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    h.call.delegations.add("old-handoff")
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.end_remote()
    # A send racing the channel close is held even before run() chooses renewal.
    await h.call.send({"type": "session.commentary.append", "delegation_id": "old-handoff", "content": "Result."})
    await asyncio.wait_for(first.close_entered.wait(), 3)
    await h.call.send({"type": "session.thinking.append", "content": "Still working."})
    await h.call.send({"type": "session.input_audio.append", "audio": "AAAA"})
    first.close_gate.set()
    await until(lambda: len(h.adapters) == 2 and h.call.connected and len(second.sent) == 2)
    assert [e["type"] for e in second.sent] == ["session.context.append"] * 2
    assert [e["content"][0]["text"] for e in second.sent] == ["Result.", "Still working."]
    assert not h.call.held and not second.audio
    await h.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["old-close", "new-post"])
async def test_hangup_during_renewal_leaves_no_new_live_peer(codex_call, phase):
    h = codex_call
    first, second = FakeRTC(), FakeRTC()
    gate, entered = asyncio.Event(), asyncio.Event()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    if phase == "old-close":
        first.close_gate = gate
        entered = first.close_entered
    else:
        async def wait_post():
            entered.set()
            await gate.wait()
            return SimpleNamespace(status_code=201, text="synthetic-answer")
        h.post_hook = wait_post
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.end_remote()
    await asyncio.wait_for(entered.wait(), 3)
    await voice_stream.stop(h.call.session_id)
    gate.set()
    await asyncio.wait_for(run, 3)
    assert second.sent == [] and h.browser.events("error") == []
    assert h.call.closed and h.call.session_id not in voice_stream.calls
    if phase == "old-close":
        assert len(h.adapters) == 1 and h.load.await_count == 1
    else:
        assert h.adapters[1].closed and second.close.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", [False, True])
async def test_401_uses_shared_login_renewal_and_fresh_credentials(codex_call, monkeypatch, renewal):
    from app.core import codex_token_refresh
    h = codex_call
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", refresh)
    h.script += [FakeRTC(), FakeRTC(), FakeRTC()]
    first = h.script[0]
    refused_call = 2 if renewal else 1

    async def refuse_once():
        return SimpleNamespace(status_code=401 if h.client.post.await_count == refused_call else 201,
                               text="private-upstream-metadata")

    h.post_hook = refuse_once
    run = asyncio.create_task(h.call.run())
    if renewal:
        await until(lambda: h.browser.events("ready"))
        h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
        first.end_remote()
        await until(lambda: h.client.post.await_count == 3 and h.call.connected)
        await h.call.close()
    await asyncio.wait_for(run, 3)
    refresh.assert_awaited_once_with("sub", stale_access_token=f"fake-token-{refused_call}")
    assert h.load.await_count == (3 if renewal else 1)
    assert "private-upstream-metadata" not in str(h.browser.send_json.await_args_list)


@pytest.mark.asyncio
async def test_failed_expiry_renewal_is_bounded(codex_call):
    h = codex_call
    first = FakeRTC()
    h.script += [first, FakeRTC(), FakeRTC(), FakeRTC()]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))

    async def refused():
        return SimpleNamespace(status_code=503, text="private-upstream-metadata")

    h.post_hook = refused
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.end_remote()
    await asyncio.wait_for(run, 3)
    assert h.client.post.await_count == 1 + len(voice_stream.RECONNECT_DELAYS)
    assert all(adapter.closed for adapter in h.adapters)
    assert h.browser.events("error") == [{"type": "error", "message": voice_stream.DROPPED_MESSAGE}]


@pytest.mark.asyncio
@pytest.mark.parametrize("over_minimum", [False, True])
async def test_explicit_expiry_uses_shared_minimum_age(codex_call, monkeypatch, over_minimum):
    h = codex_call
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 0)
    first, second = FakeRTC(), FakeRTC()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    age = voice_stream.RENEW_MIN_AGE_S + (1 if over_minimum else -1)
    h.call.session_started_at = time.monotonic() - age
    first.event({"type": "session.closed", "reason": "expired"})
    if over_minimum:
        await until(lambda: len(h.adapters) == 2 and h.call.connected)
        assert not h.call.failures and second.sent == []
        await h.call.close()
    await asyncio.wait_for(run, 3)
    assert h.client.post.await_count == (2 if over_minimum else 1)
    assert len(h.call.failures) == (0 if over_minimum else 1)


@pytest.mark.asyncio
async def test_no_reported_expiry_renews_proactively_at_a_quiet_moment(codex_call, monkeypatch):
    h = codex_call
    monkeypatch.setattr(voice_codex, "CODEX_RENEW_S", 0.4)
    monkeypatch.setattr(voice_codex, "RENEW_LEAD_S", 1)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 1)
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 0.1)
    first, second = FakeRTC(), FakeRTC()
    h.script += [first, second]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    h.call.settling = ["pending-handoff"]
    await asyncio.sleep(0.6)
    assert h.client.post.await_count == 1  # Due, but not a quiet moment yet.
    monkeypatch.setattr(voice_codex, "CODEX_RENEW_S", 50 * 60)
    h.call.settling = None
    await until(lambda: len(h.adapters) == 2 and h.call.connected)
    assert first.close.await_count == 1 and not h.call.failures
    assert h.browser.events("status") == []
    assert second.sent == []
    assert all(note not in json.dumps(second.sent) for note in (
        voice_prompts.GREETING_NEW_CALL, voice_prompts.GREETING_RETURNING_CALL))
    await h.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_temporary_webrtc_disconnect_does_not_end_an_old_session(codex_call):
    h = codex_call
    first = FakeRTC()
    h.script += [first, FakeRTC()]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.connectionState = "disconnected"
    await first.handlers["connectionstatechange"]()
    assert not h.adapters[0].ended
    first.connectionState = "connected"
    first.event({"type": "input_transcript.added", "item": {"text": "Still connected."}})
    await until(lambda: h.call.pending["user"] == "Still connected.")
    assert h.client.post.await_count == 1 and h.call.connected and not h.call.failures
    await h.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 401])
async def test_definite_refusal_during_renewal_is_not_retried(codex_call, monkeypatch, status):
    from app.core import codex_token_refresh
    h = codex_call
    refresh = AsyncMock(return_value=False)
    monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", refresh)
    first = FakeRTC()
    h.script += [first, FakeRTC(), FakeRTC()]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))

    async def refused():
        return SimpleNamespace(status_code=status, text="private-upstream-metadata")

    h.post_hook = refused
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.end_remote()
    await asyncio.wait_for(run, 3)
    assert h.client.post.await_count == 2 and h.load.await_count == 2
    assert all(adapter.closed for adapter in h.adapters)
    assert h.browser.events("error")
    assert "private-upstream-metadata" not in str(h.browser.send_json.await_args_list)
    if status == 401:
        refresh.assert_awaited_once_with("sub", stale_access_token="fake-token-2")
    else:
        refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_hangup_during_401_rotation_still_saves_the_new_token(codex_call, monkeypatch):
    from app.core import codex_token_refresh
    h = codex_call
    entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {"refresh_token": "fake-old-refresh"}

    async def rotate(*args, **kwargs):
        entered.set()  # A rotating refresh request has returned its new token.
        await release.wait()  # Its persistence must survive hangup.
        state["refresh_token"] = "fake-new-refresh"
        saved.set()
        return True

    monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", rotate)
    first = FakeRTC()
    h.script += [first, FakeRTC()]
    run = asyncio.create_task(h.call.run())
    await until(lambda: h.browser.events("ready"))

    async def refused():
        return SimpleNamespace(status_code=401, text="private-upstream-metadata")

    h.post_hook = refused
    h.call.session_started_at -= voice_codex.CODEX_EXPIRY_CLOSE_S
    first.end_remote()
    await asyncio.wait_for(entered.wait(), 3)
    await voice_stream.stop(h.call.session_id)
    await asyncio.wait_for(run, 3)
    assert state["refresh_token"] == "fake-old-refresh"
    assert voice_stream._login_renewals  # The detached rotation is still owned.
    release.set()
    await asyncio.wait_for(saved.wait(), 3)
    assert state["refresh_token"] == "fake-new-refresh"
    await until(lambda: not voice_stream._login_renewals)
    assert h.browser.events("error") == []
