"""GPT Live provider disconnects: reconnect, renewal and a plain-language end."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError

from app.core import voice_stream

SEED = "Conversation so far:\nUser: Change the header colour."


class FakeProvider:
    """A GPT Live socket: answers session.start, then plays scripted frames."""

    def __init__(self, frames=(), expires_in=None):
        self.sent = []
        self.closed = False
        self.inbox: asyncio.Queue = asyncio.Queue()
        session = {"id": "live_test"}
        if expires_in is not None:
            session["expires_at"] = int(time.time() + expires_in)
        self.inbox.put_nowait(json.dumps({"type": "session.started", "session": session}))
        for frame in frames:
            self.inbox.put_nowait(frame)

    def drop(self):
        """The provider vanishes without a close frame."""
        self.inbox.put_nowait(ConnectionClosedError(None, None))

    async def send(self, message):
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(json.loads(message))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            self.closed = True
            raise item
        if item is None:
            raise ConnectionClosedError(None, None)
        return item

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        while True:
            item = await self.inbox.get()
            if item is None:
                return  # Closed by us: iteration ends like ConnectionClosedOK.
            if isinstance(item, BaseException):
                self.closed = True
                raise item
            yield item

    async def close(self):
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(None)

    def started(self):
        return [e for e in self.sent if e["type"] == "session.start"]


class FakeBrowser:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.send_json = AsyncMock()
        self.send_bytes = AsyncMock()

    async def receive(self):
        return await self.inbox.get()

    async def close(self):
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    def events(self, kind):
        return [c.args[0] for c in self.send_json.await_args_list if c.args[0]["type"] == kind]


class FakeProgress:
    active_count = 0

    def __init__(self, session_id, send, **_transport):
        self.send = send

    async def open(self):
        pass

    async def run(self):
        await asyncio.Future()

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    from app.core import turn_recorder, voice_bridge, voice_progress
    monkeypatch.setattr(voice_stream.database, "update_session", lambda *a, **kw: None)
    monkeypatch.setattr(voice_stream.database, "set_session_active", lambda *a, **kw: None)
    monkeypatch.setattr(voice_bridge, "build_context_seed", lambda sid: SEED)
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", AsyncMock())
    monkeypatch.setattr(voice_progress, "VoiceProgress", FakeProgress)
    monkeypatch.setattr(voice_stream, "RECONNECT_DELAYS", (0, 0, 0))
    attempts = []
    script = []

    async def connect(url, **kwargs):
        attempts.append(kwargs["additional_headers"]["Authorization"])
        step = script.pop(0) if script else OSError("network unreachable")
        if callable(step):
            step = await step()
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(voice_stream, "connect", connect)
    browser = FakeBrowser()
    call = voice_stream.VoiceStream(browser, {"id": "s1"}, {"model": "gpt-live-1", "voice": "marin"}, "sk-test", 60)
    voice_stream.calls["s1"] = call
    yield SimpleNamespace(call=call, browser=browser, script=script, attempts=attempts)
    voice_stream.calls.pop("s1", None)


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_abrupt_provider_close_reconnects_and_carries_the_call_on(harness):
    first = FakeProvider([json.dumps({"type": "session.output_transcript.delta", "delta": "Sure, the header"})])
    second = FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    harness.call.delegations.add("d-old")  # A handoff the old session opened.
    first.drop()
    await until(lambda: second.started() and harness.call.connected)

    resumed = second.started()[0]["session"]["input"][0]["content"][0]["text"]
    assert resumed.startswith(SEED) and voice_stream.RESUME_NOTE in resumed
    assert not [e for e in second.sent if e["type"] == "session.instructions.append"]  # No second greeting.
    assert harness.attempts == ["Bearer sk-test"] * 2
    assert harness.browser.events("status") == [{"type": "status", "state": "reconnecting"},
                                                {"type": "status", "state": "live"}]
    # The old session's handoff result reaches the new session as plain commentary.
    await harness.call.send({"type": "session.commentary.append", "delegation_id": "d-old", "content": "Done."})
    assert second.sent[-1] == {"type": "session.commentary.append", "delegation_id": None, "content": "Done."}
    # Microphone audio flows to the new session.
    await harness.browser.inbox.put({"type": "websocket.receive", "bytes": bytes(4)})
    await until(lambda: any(e["type"] == "session.input_audio.append" for e in second.sent))

    await harness.call.close()
    await asyncio.wait_for(run, 3)
    assert harness.browser.events("error") == []
    assert harness.call.api_key == ""


@pytest.mark.asyncio
async def test_results_sent_while_reconnecting_wait_for_the_new_session(harness):
    first = FakeProvider()
    second = FakeProvider()
    gate = asyncio.Event()

    async def slow_second():
        await gate.wait()
        return second

    harness.script += [first, slow_second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    await until(lambda: harness.call.restoring)
    await harness.call.send({"type": "session.commentary.append", "delegation_id": None, "content": "Result."})
    await harness.call.send({"type": "session.input_audio.append", "audio": "AAAA"})
    gate.set()
    await until(lambda: harness.call.connected and second.started())
    kinds = [e["type"] for e in second.sent]
    assert kinds == ["session.start", "session.commentary.append"]  # Audio is not replayed.
    await harness.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_exhausted_reconnects_end_the_call_with_a_plain_message(harness):
    first = FakeProvider()
    harness.script += [first]  # Every later attempt fails to connect.
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    await asyncio.wait_for(run, 3)

    assert len(harness.attempts) == 1 + len(voice_stream.RECONNECT_DELAYS)
    assert harness.browser.events("error") == [{"type": "error", "message": voice_stream.DROPPED_MESSAGE}]
    assert "no close frame" not in voice_stream.DROPPED_MESSAGE
    assert harness.call.closed and "s1" not in voice_stream.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["backoff", "connect"])
async def test_hang_up_during_reconnect_stops_without_a_new_session(harness, monkeypatch, where):
    first = FakeProvider()
    entered = asyncio.Event()
    late = FakeProvider()

    async def hanging_connect():
        entered.set()
        await asyncio.sleep(30)
        return late

    harness.script += [first, hanging_connect]
    if where == "backoff":
        monkeypatch.setattr(voice_stream, "RECONNECT_DELAYS", (30, 30, 30))
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    if where == "backoff":
        await until(lambda: harness.call.restoring)
    else:
        await asyncio.wait_for(entered.wait(), 3)
    await voice_stream.stop("s1")  # The hang-up endpoint's path.
    await asyncio.wait_for(run, 3)

    assert harness.browser.events("error") == []
    assert late.sent == []
    assert len(harness.attempts) == (1 if where == "backoff" else 2)


@pytest.mark.asyncio
async def test_session_is_renewed_before_the_expiry_the_provider_reports(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 1.9)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 0.5)
    first = FakeProvider(expires_in=2)
    second = FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: second.started() and harness.call.connected)

    assert first.sent[-1] == {"type": "session.close"} and first.closed
    assert harness.browser.events("status") == []  # A planned renewal is not shown as a reconnect.
    assert voice_stream.RESUME_NOTE in second.started()[0]["session"]["input"][0]["content"][0]["text"]
    await harness.call.close()
    await asyncio.wait_for(run, 3)
    assert harness.browser.events("error") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,reconnects", [("expired", True), ("connection_lost", True), ("content", False)])
async def test_provider_session_closed_reconnects_only_for_connection_reasons(harness, reason, reconnects):
    first = FakeProvider([json.dumps({"type": "session.closed", "reason": reason})])
    second = FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    if reconnects:
        await until(lambda: second.started() and harness.call.connected)
        await harness.call.close()
        await asyncio.wait_for(run, 3)
    else:
        await asyncio.wait_for(run, 3)
        assert second.sent == []
        assert harness.browser.events("ended") == [{"type": "ended", "reason": "provider-closed"}]


@pytest.mark.asyncio
async def test_drop_and_give_up_are_logged_with_the_close_details(harness, caplog):
    caplog.set_level("INFO", logger="app.core.voice_stream")
    first = FakeProvider()
    harness.script += [first]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    await asyncio.wait_for(run, 3)

    text = caplog.text
    # The provider leg's drop names the WebSocket close state, not just a class.
    assert "GPT Live connection dropped" in text and "ConnectionClosedError: no close frame received or sent" in text
    assert "reconnect attempt 1 failed after" in text and "s: OSError: network unreachable" in text
    assert "Voice audio stream failed for s1 after" in text
    assert "Every reconnect attempt failed. Last failure: OSError: network unreachable" in text
    assert harness.call.finished.is_set()


@pytest.mark.asyncio
async def test_browser_close_code_is_logged_and_ends_the_call_quietly(harness, caplog):
    caplog.set_level("INFO", logger="app.core.voice_stream")
    harness.script += [FakeProvider()]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    await harness.browser.inbox.put({"type": "websocket.disconnect", "code": 1006})
    await asyncio.wait_for(run, 3)

    assert "the browser closed the audio connection (code 1006)" in caplog.text
    assert harness.browser.events("error") == []
    assert len(harness.attempts) == 1  # The browser leg is not the provider's: no provider reconnect.
    assert "s1" not in voice_stream.calls


@pytest.mark.asyncio
async def test_error_for_a_browser_that_is_already_gone_does_not_escape(harness):
    from uvicorn.protocols.utils import ClientDisconnected
    first = FakeProvider()
    harness.script += [first]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    harness.browser.send_json.side_effect = ClientDisconnected()
    first.drop()
    await asyncio.wait_for(run, 3)  # Ends cleanly instead of raising.
    assert harness.call.closed and harness.call.finished.is_set()


@pytest.mark.asyncio
async def test_take_over_ends_the_leftover_call_and_waits_for_its_cleanup(harness):
    from app.core import turn_recorder
    first = FakeProvider([json.dumps({"type": "session.input_transcript.delta", "delta": "Keep this."})])
    harness.script += [first]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.call.pending["user"])

    await voice_stream.take_over("s1")

    assert run.done() and harness.call.closed and "s1" not in voice_stream.calls
    assert first.closed
    # The leftover call stored the caller's last words before the new one starts.
    turn_recorder.record_voice_exchange.assert_awaited()
    assert turn_recorder.record_voice_exchange.await_args.args[1] == "Keep this."


@pytest.mark.asyncio
async def test_take_over_without_a_leftover_call_is_a_no_op():
    await voice_stream.take_over("nobody")


def test_reconnect_window_is_about_twenty_seconds_with_backoff():
    waits, starts = 0.0, []
    for attempt, delay in enumerate(voice_stream.RECONNECT_DELAYS, 1):
        if attempt > 1 and waits + delay > voice_stream.RECONNECT_WINDOW_S:
            break
        waits += delay
        starts.append(waits)
    assert starts == [0.5, 1.5, 3.5, 7.5, 11.5, 15.5, 19.5]
    assert list(voice_stream.RECONNECT_DELAYS) == sorted(voice_stream.RECONNECT_DELAYS)  # Backoff, never shorter.
    assert voice_stream.RECONNECT_WINDOW_S + voice_stream.RECONNECT_ATTEMPT_S <= 30


@pytest.mark.asyncio
async def test_no_reconnect_attempt_starts_after_the_window(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "RECONNECT_DELAYS", (0.1,) * 10)
    monkeypatch.setattr(voice_stream, "RECONNECT_WINDOW_S", 0.25)
    first = FakeProvider()
    harness.script += [first]  # Every later attempt fails to connect.
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    await asyncio.wait_for(run, 3)

    assert len(harness.attempts) == 1 + 2  # Attempts at about 0.1 s and 0.2 s; 0.3 s is past the window.
    assert harness.browser.events("status") == [{"type": "status", "state": "reconnecting"}]
    assert harness.browser.events("error") == [{"type": "error", "message": voice_stream.DROPPED_MESSAGE}]


@pytest.mark.asyncio
async def test_failed_renewal_attempt_shows_reconnecting(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 1.9)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 0.5)
    first = FakeProvider(expires_in=2)
    second = FakeProvider()
    harness.script += [first, OSError("network unreachable"), second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: second.started() and harness.call.connected)
    assert harness.browser.events("status") == [{"type": "status", "state": "reconnecting"},
                                                {"type": "status", "state": "live"}]
    await harness.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_a_provider_that_stops_hearing_the_caller_is_replaced(harness, monkeypatch):
    """#1824: GPT Live heard the caller, then transcribed nothing for many
    seconds of speech. The call says it is reconnecting and gets a new session."""
    monkeypatch.setattr(voice_stream, "PROVIDER_DEAF_VOICED_S", 1.0)
    first = FakeProvider([json.dumps({"type": "session.input_transcript.delta", "delta": "Hello there"})])
    second = FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.call.heard_this_session)
    speech = b"\x10\x00" * 12_000  # 0.5 s of speech-level audio
    for _ in range(3):
        await harness.browser.inbox.put({"type": "websocket.receive", "bytes": speech})
    await until(lambda: second.started() and harness.call.connected)

    assert harness.browser.events("status") == [{"type": "status", "state": "reconnecting"},
                                                {"type": "status", "state": "live"}]
    assert not harness.call.deaf.is_set() and not harness.call.heard_this_session
    # A second deaf spell inside the cooldown does not churn sessions.
    for _ in range(3):
        await harness.browser.inbox.put({"type": "websocket.receive", "bytes": speech})
    harness.call.heard_this_session = True
    await harness.browser.inbox.put({"type": "websocket.receive", "bytes": speech})
    await until(lambda: harness.call.mic_frames == 7)
    assert not harness.call.deaf.is_set() and len(harness.attempts) == 2
    await harness.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_zero_minutes_runs_the_call_without_a_time_budget(harness, monkeypatch):
    call = voice_stream.VoiceStream(harness.browser, {"id": "s1"}, {"model": "gpt-live-1", "voice": "marin"},
                                    "sk-test", 0)
    voice_stream.calls["s1"] = call
    budgets = []
    real_timeout = asyncio.timeout
    monkeypatch.setattr(voice_stream.asyncio, "timeout", lambda delay: budgets.append(delay) or real_timeout(delay))
    harness.script += [FakeProvider()]
    run = asyncio.create_task(call.run())
    await until(lambda: harness.browser.events("ready"))

    assert call.max_seconds is None and budgets[0] is None  # run() opens no call budget.
    state = voice_stream.state("s1")
    assert (state["max_call_minutes"], state["remaining_seconds"]) == (0, None)
    await call.close()
    await asyncio.wait_for(run, 3)
    assert harness.browser.events("error") == []


def test_state_reports_the_time_left_on_a_limited_call(harness):
    state = voice_stream.state("s1")
    assert state["max_call_minutes"] == 60
    assert 3590 < state["remaining_seconds"] <= 3600


@pytest.mark.asyncio
async def test_renewals_and_expiries_never_count_toward_the_reconnect_limit(harness, monkeypatch):
    """An unlimited call renews its provider session every hour, all day."""
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 1)
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    providers = [FakeProvider([json.dumps({"type": "session.closed", "reason": "expired"})]) for _ in range(4)]
    last = FakeProvider()
    harness.script += [*providers, last]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: last.started() and harness.call.connected)

    assert harness.call.reconnects == 4 and not harness.call.failures
    assert harness.browser.events("error") == []
    await harness.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_failures_are_limited_within_the_budget_window_only(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 1)
    first, second, third = FakeProvider(), FakeProvider(), FakeProvider()
    harness.script += [first, second, third]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    first.drop()
    await until(lambda: second.started() and harness.call.connected)
    # A drop an hour later is a fresh budget, so it is restored too.
    harness.call.failures[0] -= voice_stream.RECONNECT_BUDGET_S + 1
    second.drop()
    await until(lambda: third.started() and harness.call.connected)
    # Two failures within the hour exceed the limit and end the call.
    third.drop()
    await asyncio.wait_for(run, 3)
    assert harness.browser.events("error") == [{"type": "error", "message": voice_stream.DROPPED_MESSAGE}]


@pytest.mark.asyncio
async def test_an_expiry_right_after_start_counts_as_a_failure(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "MAX_RECONNECTS", 1)
    closing = [FakeProvider([json.dumps({"type": "session.closed", "reason": "expired"})]) for _ in range(3)]
    harness.script += closing
    run = asyncio.create_task(harness.call.run())
    await asyncio.wait_for(run, 3)  # A provider that keeps closing at once cannot loop.
    assert len(harness.attempts) == 2
    assert harness.browser.events("error") == [{"type": "error", "message": voice_stream.DROPPED_MESSAGE}]


@pytest.mark.asyncio
async def test_renewal_moves_old_handoffs_out_of_the_per_session_bound(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "MAX_STALE_DELEGATIONS", 100)
    first, second = FakeProvider(), FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    harness.call.delegations |= {f"d-{n}" for n in range(150)}
    first.drop()
    await until(lambda: second.started() and harness.call.connected)
    assert harness.call.delegations == set()
    # Old handoff ids are kept for their late results, but bounded.
    assert len(harness.call.stale_delegations) == 100
    await harness.call.close()
    await asyncio.wait_for(run, 3)


class SlowToStop(FakeProvider):
    """A provider socket whose reader takes a moment to stop when cancelled."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopping = asyncio.Event()
        self.release = asyncio.Event()

    async def _frames(self):
        try:
            async for item in super()._frames():
                yield item
        finally:
            self.stopping.set()
            await self.release.wait()


@pytest.mark.asyncio
async def test_note_sent_while_a_renewal_closes_the_old_session_waits_for_the_new_one(harness, monkeypatch):
    monkeypatch.setattr(voice_stream, "RENEW_MIN_AGE_S", 0)
    monkeypatch.setattr(voice_stream, "RENEW_LEAD_S", 1.9)
    monkeypatch.setattr(voice_stream, "RENEW_LAST_S", 0.5)
    first, second = SlowToStop(expires_in=2), FakeProvider()
    harness.script += [first, second]
    run = asyncio.create_task(harness.call.run())
    await asyncio.wait_for(first.stopping.wait(), 3)  # Renewal chosen; old reader still stopping.

    assert harness.call.restoring
    await harness.call.send({"type": "session.commentary.append", "delegation_id": None, "content": "Chat result."})
    assert not [e for e in first.sent if e.get("content") == "Chat result."]
    first.release.set()
    await until(lambda: any(e.get("content") == "Chat result." for e in second.sent))
    await harness.call.close()
    await asyncio.wait_for(run, 3)


@pytest.mark.asyncio
async def test_typed_messages_are_deduped_apart_from_the_handoff_bound(harness, monkeypatch):
    from tests.unit.test_voice_stream import Upstream
    call = harness.call
    call.flush = AsyncMock()
    call.upstream = Upstream([])
    delegate = AsyncMock(return_value="Done.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    for n in range(250):  # More than the 200 handoffs one provider session allows.
        await call.receive_text(json.dumps({"type": "input_text", "id": f"t-{n}", "text": f"Task {n}"}))
        await call.delegation_task
    assert harness.browser.events("text.rejected") == []
    assert delegate.await_count == 250
    assert call.delegations == set() and len(call.typed_ids) == voice_stream.MAX_TYPED_IDS
    # A recent draft sent again is acknowledged without a second dispatch.
    await call.receive_text(json.dumps({"type": "input_text", "id": "t-249", "text": "Task 249"}))
    assert delegate.await_count == 250


@pytest.mark.asyncio
async def test_a_browser_that_goes_silent_ends_the_call(harness, monkeypatch, caplog):
    """No WebSocket pings run, so a vanished phone is noticed by its silence."""
    caplog.set_level("INFO", logger="app.core.voice_stream")
    monkeypatch.setattr(voice_stream, "BROWSER_SILENT_END_S", 0.4)
    monkeypatch.setattr(voice_stream, "HEALTH_PUSH_S", 0.05)
    provider = FakeProvider()
    harness.script += [provider]
    run = asyncio.create_task(harness.call.run())
    await until(lambda: harness.browser.events("ready"))
    for _ in range(4):  # client_stats (sent even while muted) keep the call up.
        await asyncio.sleep(0.2)
        await harness.browser.inbox.put({"type": "websocket.receive",
                                         "text": json.dumps({"type": "client_stats", "stats": {}})})
    assert not run.done()
    await asyncio.wait_for(run, 3)  # Then nothing arrives at all.

    assert harness.call.closed and provider.closed and "s1" not in voice_stream.calls
    assert harness.call.browser_close.startswith("no data for")
    assert "the browser has sent nothing for" in caplog.text
    assert harness.browser.events("error") == []
