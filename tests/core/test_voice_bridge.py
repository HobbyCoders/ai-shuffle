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
    # The coordinator prompt (VW4 replaced the V1 placeholder; alias kept).
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
        "usage": {}, "estimated_cost_usd": None,
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


# ---------------------------------------------------------------------------
# VW4 helpers
# ---------------------------------------------------------------------------

def _frames(sock: FakeSideband):
    return [json.loads(raw) for raw in sock.sent]


def _frame_types(sock: FakeSideband):
    return [f.get("type") for f in _frames(sock)]


def _items(sock: FakeSideband, item_type: str):
    return [
        f["item"] for f in _frames(sock)
        if f.get("type") == "conversation.item.create"
        and (f.get("item") or {}).get("type") == item_type
    ]


async def _settle(bridge: vb.VoiceBridge, timeout: float = 5.0) -> None:
    """Wait for every in-flight tool-servicing task (never for the watchers)."""
    tasks = list(bridge._call_tasks)
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout,
        )
    await asyncio.sleep(0)


class _Recorder(list):
    """A list that also carries test-side attributes."""


def _call_done(call_id="call-1", name="dispatch_agent", **args) -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args or {"prompt": "add a dark mode toggle"}),
    }


def _response_done_with_call(call_id="call-1", name="dispatch_agent", **args) -> dict:
    return {
        "type": "response.done",
        "response": {
            "output": [{
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args or {"prompt": "add a dark mode toggle"}),
            }],
        },
    }


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Stub ``task_dispatch.dispatch_task_tool``; records every call."""
    from app.core import task_dispatch

    calls = _Recorder()
    result = {"ok": True, "worker_session_id": "worker-1", "agent": "coder"}

    async def _dispatch(args, *, caller_session_id, caller_profile_id=None, **kw):
        calls.append({
            "args": args,
            "caller_session_id": caller_session_id,
            "caller_profile_id": caller_profile_id,
        })
        return dict(result)

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", _dispatch)
    calls.result = result          # type: ignore[attr-defined]
    return calls


@pytest.fixture
def idle_gate(monkeypatch):
    """Control ``wait_session_idle``: the watcher blocks until released."""
    from app.core import turn_manager

    gate = asyncio.Event()
    watched = []

    async def _wait(session_id, timeout=300.0, **kw):
        watched.append((session_id, timeout))
        await gate.wait()
        return True

    monkeypatch.setattr(turn_manager, "wait_session_idle", _wait)
    gate.watched = watched         # type: ignore[attr-defined]
    return gate


@pytest.fixture
def fake_permissions(monkeypatch):
    """Stub the permission handler: pending list + recorded responses."""
    from app.core.permission_handler import permission_handler

    pending = _Recorder()
    responded = []

    async def _respond(**kwargs):
        responded.append(kwargs)
        return {"resolved": [kwargs.get("request_id")]}

    monkeypatch.setattr(
        permission_handler, "get_pending_requests", lambda sid: list(pending),
    )
    monkeypatch.setattr(permission_handler, "respond", _respond)
    monkeypatch.setattr(vb, "APPROVAL_POLL_SECONDS", 0.01)
    pending.responded = responded  # type: ignore[attr-defined]
    return pending


# ---------------------------------------------------------------------------
# VW4 — tool declarations + instruction contract
# ---------------------------------------------------------------------------

async def test_session_update_declares_the_four_dispatch_tools(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)

    session = fake_sockets[0].session_update()["session"]
    tools = session["tools"]
    assert [t["name"] for t in tools] == [
        "dispatch_agent", "check_agent", "list_agents", "respond_approval",
    ]
    assert {t["type"] for t in tools} == {"function"}
    # Realtime schema is FLAT (no nested "function" object) and every tool
    # carries a JSON-schema parameters object.
    assert all("function" not in t for t in tools)
    assert all(t["parameters"]["type"] == "object" for t in tools)

    by_name = {t["name"]: t for t in tools}
    assert by_name["dispatch_agent"]["parameters"]["required"] == ["prompt"]
    assert set(by_name["dispatch_agent"]["parameters"]["properties"]) == {
        "prompt", "agent", "label",
    }
    assert by_name["check_agent"]["parameters"]["required"] == ["task_id"]
    assert by_name["list_agents"]["parameters"]["properties"] == {}
    assert by_name["respond_approval"]["parameters"]["required"] == [
        "request_id", "decision",
    ]
    decision = by_name["respond_approval"]["parameters"]["properties"]["decision"]
    assert decision["enum"] == ["allow", "deny"]
    await bridge.stop()


async def test_instructions_are_the_delegation_first_contract(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    instructions = fake_sockets[0].session_update()["session"]["instructions"]

    assert instructions == vb.VOICE_INSTRUCTIONS
    assert vb.VOICE_INSTRUCTIONS_V1 is vb.VOICE_INSTRUCTIONS   # VW3 alias holds
    assert "COORDINATOR only" in instructions
    assert "dispatch_agent" in instructions and "respond_approval" in instructions
    await bridge.stop()


# ---------------------------------------------------------------------------
# VW4 — function-call servicing
# ---------------------------------------------------------------------------

async def test_function_call_round_trips_to_output_and_response_create(
    mock_db, fake_sockets, fake_dispatch, idle_gate
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done(prompt="add a dark mode toggle", label="dark mode"))
    await _drain(bridge, sock)
    await _settle(bridge)

    assert len(fake_dispatch) == 1
    args = fake_dispatch[0]["args"]
    assert args["prompt"] == "add a dark mode toggle"
    assert args["label"] == "dark mode"
    assert args["mode"] == "background"                 # VM10: never blocking
    assert fake_dispatch[0]["caller_session_id"] == SID

    outputs = _items(sock, "function_call_output")
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call-1"
    payload = json.loads(outputs[0]["output"])
    assert payload["ok"] is True
    assert payload["task_id"] == "worker-1"
    # The model is asked to speak the ack right away.
    assert _frame_types(sock)[-1] == "response.create"
    await bridge.stop()


async def test_a_call_is_serviced_once_across_both_event_shapes(
    mock_db, fake_sockets, fake_dispatch, idle_gate
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done(call_id="call-dup"))
    sock.push(_response_done_with_call(call_id="call-dup"))
    await _drain(bridge, sock)
    await _settle(bridge)

    assert len(fake_dispatch) == 1                       # not two workers
    assert len(_items(sock, "function_call_output")) == 1
    await bridge.stop()


async def test_a_call_seen_only_in_response_done_is_still_serviced(
    mock_db, fake_sockets, fake_dispatch, idle_gate
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_response_done_with_call(call_id="call-only-in-done"))
    await _drain(bridge, sock)
    await _settle(bridge)

    assert len(fake_dispatch) == 1
    assert _items(sock, "function_call_output")[0]["call_id"] == "call-only-in-done"
    await bridge.stop()


async def test_dispatch_acks_immediately_while_the_worker_still_runs(
    mock_db, fake_sockets, fake_dispatch, idle_gate
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done())
    await _drain(bridge, sock)
    await _settle(bridge)

    # Ack delivered; the watcher is still waiting on the (ungated) worker.
    assert len(_items(sock, "function_call_output")) == 1
    assert idle_gate.watched and idle_gate.watched[0][0] == "worker-1"
    assert bridge._watchers
    assert _items(sock, "message") == []                 # nothing announced yet
    await bridge.stop()


async def test_dispatch_error_is_passed_through_to_the_model(
    mock_db, fake_sockets, fake_dispatch, idle_gate
):
    _mk_session()
    fake_dispatch.result.clear()
    fake_dispatch.result.update({"ok": False, "error": "Worker budget reached (8)."})

    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done())
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is False
    assert "Worker budget reached" in payload["error"]
    assert not bridge._watchers                          # nothing to watch
    await bridge.stop()


async def test_unknown_tool_returns_an_error_output(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(call_id="c9", name="rm_rf", prompt="nope"))
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is False
    assert "rm_rf" in payload["error"]
    await bridge.stop()


async def test_a_failing_tool_never_kills_the_call(mock_db, fake_sockets, monkeypatch):
    from app.core import task_dispatch

    _mk_session()

    async def _boom(*a, **kw):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", _boom)
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done())
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is False
    assert "dispatch exploded" in payload["error"]
    assert bridge.active is True
    await bridge.stop()


# ---------------------------------------------------------------------------
# VW4 — completion injection
# ---------------------------------------------------------------------------

async def test_watcher_injects_the_completion_update(
    mock_db, fake_sockets, fake_dispatch, idle_gate, monkeypatch
):
    _mk_session()
    monkeypatch.setattr(
        vb, "_worker_summary", lambda sid: ("completed", "shipped the toggle"),
    )
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done(prompt="add a dark mode toggle", label="dark mode"))
    await _drain(bridge, sock)
    await _settle(bridge)
    assert _items(sock, "message") == []

    watchers = list(bridge._watchers)
    idle_gate.set()                                      # the worker settles
    await asyncio.wait_for(asyncio.gather(*watchers), 5)

    messages = _items(sock, "message")
    assert len(messages) == 1
    item = messages[0]
    assert item["role"] == vb.INJECTED_ITEM_ROLE
    assert item["content"][0]["type"] == "input_text"
    text = item["content"][0]["text"]
    assert text.startswith(vb.COMPLETION_PREFIX)
    assert "dark mode" in text and "completed" in text
    assert "shipped the toggle" in text
    assert _frame_types(sock)[-1] == "response.create"
    await bridge.stop()


async def test_watcher_stays_silent_when_the_bridge_stopped(
    mock_db, fake_sockets, fake_dispatch, idle_gate, monkeypatch
):
    _mk_session()
    monkeypatch.setattr(vb, "_worker_summary", lambda sid: ("completed", "done"))
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done())
    await _drain(bridge, sock)
    await _settle(bridge)

    watcher = next(iter(bridge._watchers))
    # Hang up first (VM12: the worker itself keeps running), THEN settle it.
    await bridge.stop()
    idle_gate.set()
    await asyncio.sleep(0.05)

    assert watcher.cancelled() or watcher.done()
    assert _items(sock, "message") == []


async def test_watcher_window_follows_the_call_cap_but_is_bounded(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets, max_call_minutes=5)
    assert bridge._watch_timeout() == 5 * vb.SECONDS_PER_MINUTE
    bridge.max_call_minutes = 10_000
    assert bridge._watch_timeout() == vb.WATCHER_MAX_SECONDS
    await bridge.stop()


# ---------------------------------------------------------------------------
# VW4 — approvals
# ---------------------------------------------------------------------------

async def test_respond_approval_passes_through_to_the_handler(
    mock_db, fake_sockets, fake_permissions
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]

    sock.push(_call_done(
        call_id="c-appr", name="respond_approval",
        request_id="req-7", decision="allow",
    ))
    await _drain(bridge, sock)
    await _settle(bridge)

    assert fake_permissions.responded == [{
        "request_id": "req-7",
        # Parent-keyed: approvals for a worker land on the VOICE session.
        "session_id": SID,
        "decision": "allow",
        "broadcast_func": None,
    }]
    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload == {
        "ok": True, "request_id": "req-7", "decision": "allow",
        "result": {"resolved": ["req-7"]},
    }
    await bridge.stop()


async def test_respond_approval_rejects_a_bad_decision(
    mock_db, fake_sockets, fake_permissions
):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(
        call_id="c-bad", name="respond_approval",
        request_id="req-7", decision="maybe",
    ))
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is False
    assert fake_permissions.responded == []
    await bridge.stop()


async def test_pending_approval_is_announced_exactly_once(
    mock_db, fake_sockets, fake_dispatch, idle_gate, fake_permissions
):
    _mk_session()
    fake_permissions.append({
        "request_id": "req-1",
        "tool_name": "Bash",
        "title": "Run npm test",
    })

    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done())                     # a dispatch starts the poller
    await _drain(bridge, sock)
    await asyncio.sleep(0.1)                    # several poll cycles

    prompts = [
        item for item in _items(sock, "message")
        if item["content"][0]["text"].startswith(vb.APPROVAL_PREFIX)
    ]
    assert len(prompts) == 1
    text = prompts[0]["content"][0]["text"]
    assert "Run npm test" in text and "req-1" in text
    await bridge.stop()


async def test_the_approval_poller_only_starts_after_a_dispatch(
    mock_db, fake_sockets, fake_permissions
):
    _mk_session()
    fake_permissions.append({"request_id": "req-2", "tool_name": "Bash"})
    bridge = await _bridge(fake_sockets)
    await asyncio.sleep(0.05)

    assert bridge._approval_task is None
    assert _items(fake_sockets[0], "message") == []
    await bridge.stop()


# ---------------------------------------------------------------------------
# VW4 — read tools
# ---------------------------------------------------------------------------

async def test_check_agent_reports_status_and_report(mock_db, fake_sockets, monkeypatch):
    from app.core import provider_agent as pa
    from app.core.task_registry import task_registry

    _mk_session()
    monkeypatch.setattr(
        task_registry, "get_task",
        lambda sid, tid: {
            "id": tid, "subject": "dark mode", "status": "completed",
            "kind": "agent", "summary": "openai · gpt-5",
        },
    )
    monkeypatch.setattr(pa, "final_turn_outcome", lambda sid: {"terminal_found": True})
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "toggle shipped")

    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(call_id="c-chk", name="check_agent", task_id="worker-1"))
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is True
    assert payload["label"] == "dark mode"
    assert payload["worker_status"] == "completed"
    assert payload["report"] == "toggle shipped"
    await bridge.stop()


async def test_check_agent_on_an_unknown_task(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(call_id="c-chk2", name="check_agent", task_id="nope"))
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is False
    assert "nope" in payload["error"]
    await bridge.stop()


async def test_list_agents_returns_definitions_and_active_tasks(
    mock_db, fake_sockets, monkeypatch
):
    from app.core.task_registry import task_registry
    from app.db import database

    _mk_session()
    monkeypatch.setattr(
        database, "get_all_profiles",
        lambda: [{"id": "coder", "name": "Coder", "description": "writes code"}],
    )
    monkeypatch.setattr(
        task_registry, "get_all",
        lambda sid, **kw: [
            {"id": "worker-1", "subject": "dark mode", "status": "running"},
        ],
    )
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(call_id="c-list", name="list_agents"))
    await _drain(bridge, sock)
    await _settle(bridge)

    payload = json.loads(_items(sock, "function_call_output")[0]["output"])
    assert payload["ok"] is True
    assert payload["agents"] == [
        {"id": "coder", "name": "Coder", "description": "writes code"},
    ]
    assert payload["active_tasks"] == [
        {"task_id": "worker-1", "label": "dark mode", "status": "running"},
    ]
    await bridge.stop()


async def test_dispatch_without_an_agent_picks_a_default_definition(
    mock_db, fake_sockets, fake_dispatch, idle_gate, monkeypatch
):
    from app.db import database

    _mk_session()                                   # session profile = "default"
    monkeypatch.setattr(
        database, "get_all_profiles",
        lambda: [{"id": "default", "name": "Default"}, {"id": "coder", "name": "Coder"}],
    )
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done(prompt="look into the flaky test"))
    await _drain(bridge, sock)
    await _settle(bridge)

    # Never the caller's own agent — task_dispatch refuses that as a cycle.
    assert fake_dispatch[0]["args"]["agent"] == "coder"
    assert fake_dispatch[0]["caller_profile_id"] == "default"
    await bridge.stop()


# ---------------------------------------------------------------------------
# VW4 — hangup never touches the workers (ruling VM12)
# ---------------------------------------------------------------------------

async def test_stop_cancels_bridge_tasks_but_never_stops_a_worker(
    mock_db, fake_sockets, fake_dispatch, idle_gate, fake_permissions, monkeypatch
):
    from app.core import provider_agent_supervisor as sup

    _mk_session()
    torn_down = []

    async def _no(*args, **kwargs):
        torn_down.append(args)
        return []

    monkeypatch.setattr(sup, "teardown_workers", _no)
    monkeypatch.setattr(sup, "stop_all_workers", _no)

    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push(_call_done())
    await _drain(bridge, sock)
    await _settle(bridge)

    watcher = next(iter(bridge._watchers))
    poller = bridge._approval_task
    assert poller is not None

    await bridge.stop()
    await asyncio.sleep(0.02)

    assert watcher.cancelled() or watcher.done()
    assert poller.cancelled() or poller.done()
    assert bridge._watchers == set() and bridge._call_tasks == set()
    assert bridge._approval_task is None
    assert torn_down == []                       # workers survive the hangup


# ---------------------------------------------------------------------------
# VW5 — the call epoch (cap spans reconnects)
# ---------------------------------------------------------------------------

async def test_replace_attach_preserves_the_call_epoch(mock_db, fake_sockets, registry):
    _mk_session()
    first = await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    await asyncio.sleep(0.02)
    second = await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=60)

    # The FE's 50-minute re-handshake must NOT restart the clock.
    assert second.call_started_at == first.call_started_at
    assert registry.call_started_at(SID) == first.call_started_at
    assert second.started_at > first.started_at   # but it IS a new attachment
    await registry.detach(SID)


async def test_hangup_clears_the_epoch_so_the_next_call_starts_fresh(
    mock_db, fake_sockets, registry
):
    _mk_session()
    first = await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    await registry.detach(SID)
    assert registry.call_started_at(SID) is None

    await asyncio.sleep(0.02)
    third = await registry.attach(SID, "call-3", api_key=KEY, model=MODEL, max_call_minutes=60)
    assert third.call_started_at > first.call_started_at
    await registry.detach(SID)


async def test_a_failed_attach_leaves_no_epoch_behind(mock_db, monkeypatch, registry):
    _mk_session()

    async def _boom(call_id, api_key):
        raise ConnectionError("handshake refused")

    monkeypatch.setattr(vb, "_open_sideband", _boom)
    with pytest.raises(ConnectionError):
        await registry.attach(SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=60)
    assert registry.call_started_at(SID) is None


async def test_the_cap_counts_from_the_epoch_not_the_attachment(
    mock_db, fake_sockets, registry, monkeypatch
):
    _mk_session()
    monkeypatch.setattr(vb, "SECONDS_PER_MINUTE", 0.3)
    await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=1)
    await asyncio.sleep(0.2)

    # Reconnect two thirds of the way through the cap: the replacement inherits
    # the REMAINING time, it does not get a fresh minute.
    second = await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=1)
    assert second.remaining_seconds() < 0.15

    await asyncio.sleep(0.25)
    assert second.active is False
    assert second.stop_reason == "duration_cap"
    assert fake_sockets[1].closed is True
    # And the session is now inside the post-cap grace window.
    assert registry.capped_minutes(SID) == 1


async def test_the_cap_marker_drops_the_epoch_and_expires(mock_db, registry, monkeypatch):
    registry.mark_capped(SID, 240)
    assert registry.capped_minutes(SID) == 240
    assert registry.call_started_at(SID) is None   # a new call must start clean

    monkeypatch.setattr(vb, "CAP_GRACE_SECONDS", -1.0)
    assert registry.capped_minutes(SID) is None


async def test_hangup_clears_the_cap_marker(mock_db, registry):
    registry.mark_capped(SID, 240)
    assert await registry.detach(SID) is False     # no bridge, but state clears
    assert registry.capped_minutes(SID) is None


async def test_shutdown_all_clears_epochs_and_cap_markers(mock_db, fake_sockets, registry):
    _mk_session()
    await registry.attach(SID, CALL, api_key=KEY, model=MODEL, max_call_minutes=60)
    registry.mark_capped("other", 240)
    await registry.shutdown_all()
    assert registry.call_started_at(SID) is None
    assert registry.capped_minutes("other") is None


# ---------------------------------------------------------------------------
# VW5 — deterministic default agent + roster
# ---------------------------------------------------------------------------

def test_default_agent_prefers_one_literally_called_default():
    rows = [
        {"id": "p1", "name": "Zeta"},
        {"id": "p2", "name": "Default"},        # name match wins over ordering
        {"id": "p3", "name": "Alpha"},
    ]
    assert vb._pick_default_agent(rows) == "p2"
    assert vb._pick_default_agent([
        {"id": "default", "name": "Coordinator"}, {"id": "p9", "name": "Alpha"},
    ]) == "default"


def test_default_agent_otherwise_takes_the_alphabetically_first_name():
    rows = [{"id": "p3", "name": "Zeta"}, {"id": "p1", "name": "beta"},
            {"id": "p2", "name": "Alpha"}]
    assert vb._pick_default_agent(rows) == "p2"
    # Order of the table read must not change the answer.
    assert vb._pick_default_agent(list(reversed(rows))) == "p2"
    assert vb._pick_default_agent([]) is None


async def test_the_default_pick_is_stable_across_row_order(
    mock_db, fake_sockets, fake_dispatch, idle_gate, monkeypatch
):
    from app.db import database

    _mk_session()                                   # session profile = "default"
    rows = [
        {"id": "zeta", "name": "Zeta"},
        {"id": "default", "name": "Default"},       # the caller — never eligible
        {"id": "alpha", "name": "Alpha"},
    ]
    for ordering in (rows, list(reversed(rows))):
        monkeypatch.setattr(database, "get_all_profiles", lambda o=ordering: list(o))
        bridge = await _bridge(fake_sockets)
        assert await bridge._default_agent_ref() == "alpha"
        await bridge.stop()


async def test_instructions_carry_the_dispatchable_agent_roster(
    mock_db, fake_sockets, monkeypatch
):
    from app.db import database

    _mk_session()
    monkeypatch.setattr(database, "get_all_profiles", lambda: [
        {"id": "default", "name": "Default"},       # the caller — excluded
        {"id": "coder", "name": "Coder",
         "description": "Writes code. Also reviews pull requests."},
        {"id": "scout", "name": "Scout"},
    ])
    bridge = await _bridge(fake_sockets)
    instructions = fake_sockets[0].session_update()["session"]["instructions"]

    # The VM16 contract text still leads, verbatim.
    assert instructions.startswith(vb.VOICE_INSTRUCTIONS)
    roster = instructions[instructions.index(vb.ROSTER_HEADER):]
    assert "- Coder — Writes code." in roster
    assert "reviews pull requests" not in roster    # first sentence only
    assert "- Scout" in roster
    assert "Default" not in roster                  # the caller is not offered
    await bridge.stop()


def test_the_roster_is_capped():
    rows = [
        {"id": f"a{i}", "name": f"Agent {i:02d}", "description": "x" * 400}
        for i in range(40)
    ]
    roster = vb._agent_roster_text(rows)
    assert len(roster) <= vb.ROSTER_MAX_CHARS
    assert roster.startswith(vb.ROSTER_HEADER)
    assert "Agent 00" in roster                     # budgeted from the top
    assert vb._agent_roster_text([]) == ""


def test_the_dispatch_tool_points_at_the_roster():
    tool = next(t for t in vb.VOICE_TOOLS if t["name"] == "dispatch_agent")
    description = tool["parameters"]["properties"]["agent"]["description"]
    assert "Agents you can dispatch" in description
    assert "list_agents" in description


# ---------------------------------------------------------------------------
# VW5 — retention via a SECOND session.update
# ---------------------------------------------------------------------------

def _session_updates(sock: FakeSideband):
    return [f for f in _frames(sock) if f.get("type") == "session.update"]


async def test_truncation_travels_in_its_own_session_update(mock_db, fake_sockets):
    _mk_session()
    bridge = await _bridge(fake_sockets)

    updates = _session_updates(fake_sockets[0])
    assert len(updates) == 2
    assert "truncation" not in updates[0]["session"]     # never bundled
    assert updates[1]["session"] == {
        "type": "realtime",
        "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
    }
    await bridge.stop()


async def test_a_rejected_truncation_update_costs_only_itself(
    mock_db, fake_sockets, monkeypatch
):
    _mk_session()
    original = vb.VoiceBridge._send

    async def _send(self, payload):
        if "truncation" in json.dumps(payload):
            raise RuntimeError("Unknown parameter: session.truncation")
        await original(self, payload)

    monkeypatch.setattr(vb.VoiceBridge, "_send", _send)
    bridge = await _bridge(fake_sockets)

    assert bridge.connected is True                      # the call survives
    updates = _session_updates(fake_sockets[0])
    assert len(updates) == 1
    assert updates[0]["session"]["instructions"].startswith(vb.VOICE_INSTRUCTIONS)
    assert updates[0]["session"]["tools"]                # tools kept
    await bridge.stop()


async def test_a_truncation_error_event_is_logged_and_the_call_continues(
    mock_db, fake_sockets, caplog
):
    import logging

    _mk_session()
    bridge = await _bridge(fake_sockets)
    sock = fake_sockets[0]
    sock.push({
        "type": "error",
        "error": {"message": "Unknown parameter: 'session.truncation'."},
    })
    sock.push({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "still listening",
    })
    sock.push({"type": "response.done", "response": {}})
    with caplog.at_level(logging.WARNING, logger="app.core.voice_bridge"):
        await _drain(bridge, sock)

    assert any("truncation" in r.getMessage() for r in caplog.records)
    assert _types() == ["turn.started", "message.user", "message.assistant",
                        "turn.completed"]


# ---------------------------------------------------------------------------
# VW5 — usage + cost estimate
# ---------------------------------------------------------------------------

USAGE_BLOCK = {
    "total_tokens": 100,
    "input_tokens": 60,
    "output_tokens": 40,
    "input_token_details": {"text_tokens": 10, "audio_tokens": 50, "cached_tokens": 5},
    "output_token_details": {"text_tokens": 8, "audio_tokens": 32},
}


async def test_usage_accumulates_across_a_replace_attach(
    mock_db, fake_sockets, registry
):
    _mk_session()
    await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    fake_sockets[0].push({"type": "response.done", "response": {"usage": USAGE_BLOCK}})
    await asyncio.sleep(0.05)

    second = await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=60)
    fake_sockets[1].push({"type": "response.done", "response": {"usage": USAGE_BLOCK}})
    await asyncio.sleep(0.05)

    state = registry.state(SID)
    assert state["usage"] == {
        "total_tokens": 200, "input_tokens": 120, "output_tokens": 80,
        "input_text_tokens": 20, "input_audio_tokens": 100,
        "input_cached_tokens": 10,
        "output_text_tokens": 16, "output_audio_tokens": 64,
    }
    # The replacement bridge counts into the SAME record, it does not restart.
    assert second.usage == state["usage"]
    assert state["estimated_cost_usd"] == vb.estimate_cost_usd(MODEL, state["usage"])
    assert state["estimated_cost_usd"] > 0
    await registry.detach(SID)


async def test_usage_starts_clean_on_a_new_call(mock_db, fake_sockets, registry):
    _mk_session()
    await registry.attach(SID, "call-1", api_key=KEY, model=MODEL, max_call_minutes=60)
    fake_sockets[0].push({"type": "response.done", "response": {"usage": USAGE_BLOCK}})
    await asyncio.sleep(0.05)
    await registry.detach(SID)

    await registry.attach(SID, "call-2", api_key=KEY, model=MODEL, max_call_minutes=60)
    assert registry.state(SID)["usage"] == {}
    await registry.detach(SID)


def test_usage_deltas_ignore_absent_and_non_numeric_fields():
    assert vb._usage_deltas({}) == {}
    assert vb._usage_deltas({"input_tokens": None, "output_tokens": "12"}) == {}
    assert vb._usage_deltas({"input_tokens": 7}) == {"input_tokens": 7}


def test_estimated_cost_prices_every_allowlisted_model():
    million = {
        "input_text_tokens": 1_000_000, "input_audio_tokens": 1_000_000,
        "output_text_tokens": 1_000_000, "output_audio_tokens": 1_000_000,
    }
    assert vb.estimate_cost_usd("gpt-realtime-2.1", million) == pytest.approx(124.0)
    assert vb.estimate_cost_usd("gpt-realtime-2.1-mini", million) == pytest.approx(33.0)
    assert vb.estimate_cost_usd("gpt-realtime", million) == pytest.approx(116.0)


def test_estimated_cost_falls_back_and_declines_the_unknown():
    # No token details: everything that isn't audio counts as text.
    assert vb.estimate_cost_usd(
        "gpt-realtime-2.1", {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    ) == pytest.approx(28.0)
    assert vb.estimate_cost_usd("gpt-4o-realtime-preview", {"input_tokens": 10}) is None
    assert vb.estimate_cost_usd("gpt-realtime-2.1", {}) is None
    assert vb.estimate_cost_usd(None, {"input_tokens": 10}) is None
