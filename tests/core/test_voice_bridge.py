"""Tests for the voice sideband bridge (wave VW3 of docs/VOICE_MODE_PLAN.md).

NO live OpenAI anywhere: ``voice_bridge._open_sideband`` is replaced by an
in-process fake socket whose frames the test pushes by hand, so the read loop,
exchange assembly and persistence run for real against the per-test SQLite shim
(the autouse ``_auto_db_shim`` conftest fixture).
"""

import asyncio
import json

import pytest

from app.core import voice_bridge as vb
from app.db import session_events as se
from app.db import sessions as sessions_db


SID = "sess-voice"
CALL = "rtc_call_1"
KEY = "sk-test"
MODEL = "gpt-realtime-2.1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeSideband:
    """Minimal stand-in for a ``websockets`` client connection."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    # -- what the bridge uses --
    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    # -- test-side controls --
    def push(self, event: dict):
        self._queue.put_nowait(json.dumps(event))

    def push_raw(self, raw: str):
        self._queue.put_nowait(raw)

    def eof(self):
        self._queue.put_nowait(None)

    def session_update(self) -> dict:
        for raw in self.sent:
            payload = json.loads(raw)
            if payload.get("type") == "session.update":
                return payload
        raise AssertionError(f"no session.update was sent (sent={self.sent!r})")


@pytest.fixture
def fake_sockets(monkeypatch):
    """Install the fake transport; yields the list of sockets handed out."""
    sockets = []

    async def _open(call_id, api_key):
        sock = FakeSideband()
        sock.call_id = call_id
        sock.api_key = api_key
        sockets.append(sock)
        return sock

    monkeypatch.setattr(vb, "_open_sideband", _open)
    return sockets


@pytest.fixture(autouse=True)
async def _stop_bridges(monkeypatch):
    """Stop every bridge a test built, so no reader/deadline task outlives it."""
    created = []
    original_init = vb.VoiceBridge.__init__

    def _init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(vb.VoiceBridge, "__init__", _init)
    yield
    for bridge in created:
        try:
            await bridge.stop(reason="test-teardown")
        except Exception:
            pass


@pytest.fixture
def registry():
    """A private registry so tests never share the module singleton."""
    return vb.VoiceCallRegistry()


def _mk_session(session_id: str = SID) -> None:
    sessions_db.create_session(session_id, "default")


async def _bridge(fake_sockets, *, max_call_minutes: int = 60) -> vb.VoiceBridge:
    bridge = vb.VoiceBridge(
        SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=max_call_minutes,
    )
    await bridge.start()
    return bridge


async def _drain(bridge: vb.VoiceBridge, sock: FakeSideband) -> None:
    """Let the reader consume everything queued so far."""
    sock.eof()
    await asyncio.wait_for(bridge._reader_task, 5)


def _types(session_id: str = SID):
    return [r["event_type"] for r in se.get_events(session_id)]


# ---------------------------------------------------------------------------
# attach / session.update
# ---------------------------------------------------------------------------

async def test_start_opens_sideband_and_configures_the_session(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)

    sock = fake_sockets[0]
    assert (sock.call_id, sock.api_key) == (CALL, KEY)
    assert bridge.connected is True
    assert bridge.started_at is not None

    session = sock.session_update()["session"]
    assert session["type"] == "realtime"
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "medium",
        "interrupt_response": True,
        "create_response": True,
    }
    # V1 placeholder coordinator prompt (VW4 replaces the constant).
    assert session["instructions"] == vb.VOICE_INSTRUCTIONS_V1
    await bridge.stop()


async def test_start_seeds_recent_history_into_instructions(mock_db, fake_sockets):
    from app.db import database

    _mk_session()
    database.add_session_message(SID, "user", "what is the deploy status")
    database.add_session_message(SID, "assistant", "it finished ten minutes ago")

    bridge = await _bridge(fake_sockets)
    instructions = fake_sockets[0].session_update()["session"]["instructions"]
    assert instructions.startswith(vb.VOICE_INSTRUCTIONS_V1)
    assert vb.CONTEXT_SEED_HEADER in instructions
    assert "User: what is the deploy status" in instructions
    assert "Assistant: it finished ten minutes ago" in instructions
    await bridge.stop()


async def test_context_seed_is_bounded_and_keeps_the_newest(mock_db):
    from app.db import database

    _mk_session()
    for i in range(60):
        database.add_session_message(SID, "user", f"message-{i} " + "x" * 200)

    seed = vb.build_context_seed(SID)
    assert len(seed) <= vb.CONTEXT_SEED_MAX_CHARS + len(vb.CONTEXT_SEED_HEADER) + 1
    assert "message-59" in seed
    assert "message-0 " not in seed


async def test_context_seed_empty_for_a_fresh_session(mock_db):
    _mk_session()
    assert vb.build_context_seed(SID) == ""


async def test_session_update_failure_does_not_kill_the_call(mock_db, fake_sockets, monkeypatch):
    _mk_session()

    async def _boom(self, payload):
        raise RuntimeError("send failed")

    monkeypatch.setattr(vb.VoiceBridge, "_send", _boom)
    bridge = await _bridge(fake_sockets)
    assert bridge.connected is True          # the call survives a config failure
    await bridge.stop()


# ---------------------------------------------------------------------------
# event loop + transcript persistence
# ---------------------------------------------------------------------------

async def test_completed_exchange_persists_one_synthetic_turn(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "hey, what is the build doing",
    })
    sock.push({
        "type": "response.output_audio_transcript.done",
        "transcript": "it is green as of a minute ago",
    })
    sock.push({"type": "response.done", "response": {"usage": {"total_tokens": 42}}})
    await _drain(bridge, sock)

    rows = se.get_events(SID)
    assert [r["event_type"] for r in rows] == [
        "turn.started", "message.user", "message.assistant", "turn.completed",
    ]
    # One batch → one synthetic turn id, contiguous seqs, unfenced.
    turn_ids = {r["turn_id"] for r in rows}
    assert len(turn_ids) == 1
    assert turn_ids.pop().startswith("voice-")
    seqs = [r["seq"] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + 4))
    assert all(r["run_id"] is None for r in rows)

    assert rows[1]["payload"]["content"] == "hey, what is the build doing"
    assert rows[1]["payload"]["source"] == "voice"
    assert rows[2]["payload"]["content"] == "it is green as of a minute ago"
    assert rows[2]["payload"]["model"] == MODEL
    assert rows[3]["payload"]["stop_reason"] == "end_turn"
    # Usage is passed through verbatim under its own key (VW5 does attribution).
    assert rows[3]["payload"]["usage"] == {"voice_realtime": {"total_tokens": 42}}


async def test_model_initiated_response_persists_without_a_user_event(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push({
        "type": "response.output_audio_transcript.done",
        "transcript": "the agent you asked about just finished",
    })
    sock.push({"type": "response.done", "response": {}})
    await _drain(bridge, sock)

    assert _types() == ["turn.started", "message.assistant", "turn.completed"]


async def test_two_exchanges_do_not_bleed_into_each_other(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    for user, reply in (("first ask", "first reply"), ("second ask", "second reply")):
        sock.push({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": user,
        })
        sock.push({"type": "response.output_audio_transcript.done", "transcript": reply})
        sock.push({"type": "response.done", "response": {}})
    await _drain(bridge, sock)

    rows = se.get_events(SID)
    assert len(rows) == 8
    assert len({r["turn_id"] for r in rows}) == 2
    assert rows[1]["payload"]["content"] == "first ask"
    assert rows[5]["payload"]["content"] == "second ask"


async def test_empty_response_writes_nothing(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
    await _drain(bridge, sock)
    assert se.get_events(SID) == []


async def test_unknown_and_malformed_frames_are_ignored(mock_db, fake_sockets, caplog):
    import logging

    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push({"type": "response.audio.delta", "delta": "..."})
    sock.push({"type": "error", "error": {"message": "rate limited"}})
    sock.push_raw("not json at all")
    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "still works",
    })
    sock.push({"type": "response.done", "response": {}})
    with caplog.at_level(logging.WARNING, logger="app.core.voice_bridge"):
        await _drain(bridge, sock)

    assert _types() == ["turn.started", "message.user", "message.assistant", "turn.completed"]
    assert any("rate limited" in r.getMessage() for r in caplog.records)


async def test_socket_close_marks_disconnected_without_reconnecting(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    await _drain(bridge, sock)

    assert bridge.connected is False
    assert bridge.active is True             # still the session's attachment
    assert len(fake_sockets) == 1            # the server never re-dials


async def test_hangup_flushes_an_in_flight_exchange(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "cut me off mid answer",
    })
    sock.push({"type": "response.output_audio_transcript.done", "transcript": "sure, so the"})
    # No response.done — the user hangs up first.
    await asyncio.sleep(0.05)
    await bridge.stop()

    assert _types() == ["turn.started", "message.user", "message.assistant", "turn.completed"]


async def test_live_turn_blocks_the_transcript_write(mock_db, fake_sockets, monkeypatch, caplog):
    import logging

    from app.db import turns as turns_db

    _mk_session()
    monkeypatch.setattr(turns_db, "get_live_turn", lambda sid: {"id": "turn-live"})

    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "hello",
    })
    sock.push({"type": "response.output_audio_transcript.done", "transcript": "hi"})
    sock.push({"type": "response.done", "response": {}})
    with caplog.at_level(logging.WARNING, logger="app.core.voice_bridge"):
        await _drain(bridge, sock)

    assert se.get_events(SID) == []
    assert any("live turn" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# duration cap
# ---------------------------------------------------------------------------

async def test_duration_cap_closes_the_sideband(mock_db, fake_sockets, monkeypatch):
    _mk_session()
    # Shrink the clock instead of waiting on real minutes.
    monkeypatch.setattr(vb, "SECONDS_PER_MINUTE", 0.01)
    bridge = await _bridge(fake_sockets, max_call_minutes=1)

    await asyncio.sleep(0.1)
    assert fake_sockets[0].closed is True
    assert bridge.active is False
    assert bridge.connected is False
    assert bridge.stop_reason == "duration_cap"


async def test_stop_cancels_the_duration_timer(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets, max_call_minutes=60)
    await bridge.stop()
    await asyncio.sleep(0.01)
    assert bridge._deadline_task.cancelled()


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

async def test_registry_attach_and_state(mock_db, fake_sockets, registry):
    _mk_session()
    assert registry.state(SID) == {
        "active": False, "connected": False, "started_at": None, "model": None,
    }

    bridge = await registry.attach(
        SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=60,
    )
    state = registry.state(SID)
    assert state["active"] is True
    assert state["connected"] is True
    assert state["model"] == MODEL
    assert state["started_at"] == bridge.started_at
    await registry.detach(SID)


async def test_registry_attach_replaces_the_existing_bridge(mock_db, fake_sockets, registry):
    _mk_session()
    first = await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    second = await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=60)

    assert first is not second
    assert registry.get(SID) is second
    assert first.active is False
    assert fake_sockets[0].closed is True     # old attachment was closed first
    assert fake_sockets[1].closed is False
    assert second.call_id == "call-2"
    await registry.detach(SID)


async def test_replacement_reseeds_context_from_the_persisted_transcript(
    mock_db, fake_sockets, registry
):
    _mk_session()
    bridge = await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    sock = fake_sockets[0]
    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "remember the release date",
    })
    sock.push({"type": "response.output_audio_transcript.done", "transcript": "noted"})
    sock.push({"type": "response.done", "response": {}})
    await asyncio.sleep(0.1)

    await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=60)
    instructions = fake_sockets[1].session_update()["session"]["instructions"]
    assert "User: remember the release date" in instructions
    assert "Assistant: noted" in instructions
    await registry.detach(SID)
    assert bridge.active is False


async def test_registry_detach_is_idempotent(mock_db, fake_sockets, registry):
    _mk_session()
    await registry.attach(SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=60)
    assert await registry.detach(SID) is True
    assert registry.get(SID) is None
    assert fake_sockets[0].closed is True
    # Hanging up twice (or with no call at all) is fine.
    assert await registry.detach(SID) is False
    assert await registry.detach("never-existed") is False


async def test_registry_shutdown_all_closes_every_bridge(mock_db, fake_sockets, registry):
    _mk_session("sess-a")
    _mk_session("sess-b")
    a = await registry.attach("sess-a", "c1", api_key=KEY, model=MODEL, max_call_minutes=60)
    b = await registry.attach("sess-b", "c2", api_key=KEY, model=MODEL, max_call_minutes=60)

    await registry.shutdown_all()
    assert registry.get("sess-a") is None and registry.get("sess-b") is None
    assert a.active is False and b.active is False
    assert all(sock.closed for sock in fake_sockets)


async def test_failed_attach_leaves_nothing_registered(mock_db, monkeypatch, registry):
    _mk_session()

    async def _boom(call_id, api_key):
        raise ConnectionError("handshake refused")

    monkeypatch.setattr(vb, "_open_sideband", _boom)
    with pytest.raises(ConnectionError):
        await registry.attach(SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=60)
    assert registry.get(SID) is None
