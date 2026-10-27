"""#700: a voice call on a conversation another network worker runs.

The call runs on this worker (its voice settings and login) and the
conversation is a read-only mirror here. These tests drive the producing path
down to ``control.call``, the request that would reach the owning worker, on
the real database layer, and check that nothing the call does is written into
the local mirror: no transcript events, no turns, no usage, no active flag.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import turn_manager, voice_bridge, voice_stream
from app.db import database, session_events, turns
from app.network import control

PEER = "peer-device-gamingpc"
SESSION = "chat-on-peer"


@pytest.fixture
def mirror(mock_db, monkeypatch):
    """A conversation PEER runs, mirrored here, and a fake owner endpoint."""
    database.create_session(SESSION, "default", mode="chat", title="Peer chat")
    before = dict(database.get_session(SESSION))
    monkeypatch.setattr(control, "owner_of", lambda row: PEER if (row or {}).get("id") == SESSION else None)
    sent = []

    async def owner_call(device_id, action, body=None, **_kwargs):
        sent.append((device_id, action, body))
        if action == "turns":
            return {"turn_id": "owner-turn-1", "session_id": body["session_id"]}
        if action == "permission":
            return {"result": {"success": True}}
        raise AssertionError(f"unexpected control action {action}")

    monkeypatch.setattr(control, "call", owner_call)

    async def no_local_submit(*_args, **_kwargs):
        raise AssertionError("a mirror's request must never be queued locally")

    monkeypatch.setattr(turn_manager, "submit_async", no_local_submit)
    return SimpleNamespace(sent=sent, before=before)


def assert_mirror_untouched(mirror):
    assert session_events.get_events(SESSION, from_seq=0, limit=50) == []
    assert turns.get_latest_turn(SESSION) is None
    after = dict(database.get_session(SESSION))
    for key in ("is_active", "launched_model", "total_cost_usd", "total_model_calls", "title", "turn_count"):
        assert after.get(key) == mirror.before.get(key), key


def streamed_call():
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": SESSION}, {"model": "gpt-live-1", "voice": "marin"}, "secret", 5)
    call.remote_owner = PEER
    return call


class Upstream:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(json.loads(event))


@pytest.mark.asyncio
async def test_a_spoken_request_goes_to_the_owner_and_nothing_is_written_here(mirror):
    call = streamed_call()
    call.upstream = Upstream()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    await call.caption("user", "Make the rail icons bigger.")
    await call.caption("assistant", "Sure, asking the agent now.")

    await call.run_delegation("delegation-1")

    assert len(mirror.sent) == 1
    device, action, body = mirror.sent[0]
    assert (device, action) == (PEER, "turns")
    assert body["session_id"] == SESSION and body["source"] == "user"
    # The owner records the caller's own words, not the call's context block.
    assert body["prompt"] == "Make the rail icons bigger."
    payload = body["payload"]
    assert payload["voice_delegation"] is True
    assert "user_message_already_recorded" not in payload
    assert "Live conversation context" in payload["voice_instructions"]
    assert "assistant: Sure, asking the agent now." in payload["voice_instructions"]
    # Progress follows the owner's turn through the synced events.
    call.progress.submitted.assert_awaited_once_with("owner-turn-1", "delegation-1")
    assert call.upstream.sent == []  # Nothing to say yet; the reply arrives as events.

    await call.flush()  # Hang-up stores the rest of the exchange: still nothing here.
    assert_mirror_untouched(mirror)


@pytest.mark.asyncio
async def test_an_unreachable_owner_is_said_aloud_and_nothing_is_queued_here(mirror, monkeypatch):
    async def offline(*_args, **_kwargs):
        raise control.RunnerUnreachable("Couldn't reach GamingPC from Unraid.")

    monkeypatch.setattr(control, "call", offline)
    call = streamed_call()
    call.upstream = Upstream()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    await call.caption("user", "Run the tests.")

    await call.run_delegation("delegation-1")

    call.progress.submitted.assert_not_awaited()
    spoken = call.upstream.sent[0]
    assert spoken["type"] == "session.commentary.append"
    assert "could not take the request" in spoken["content"] and "Couldn't reach GamingPC" in spoken["content"]
    assert_mirror_untouched(mirror)


@pytest.mark.asyncio
async def test_a_conversation_run_here_still_records_and_queues_locally(mock_db, monkeypatch):
    """The ordinary path is unchanged: no owner, local queue, stored exchange."""
    database.create_session("local-chat", "default", mode="chat")
    from app.core import turn_recorder
    record = AsyncMock()
    monkeypatch.setattr(turn_recorder, "record_voice_exchange", record)
    submit = AsyncMock(return_value="local-turn")
    monkeypatch.setattr(turn_manager, "submit_async", submit)

    async def never(*_args, **_kwargs):
        raise AssertionError("a local conversation is never forwarded")

    monkeypatch.setattr(control, "call", never)
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": "local-chat"}, {"model": "gpt-live-1", "voice": "marin"}, "s", 5)
    call.upstream = Upstream()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    await call.caption("user", "Summarize the diff.")

    await call.run_delegation("d1")

    record.assert_awaited()
    assert submit.await_args.kwargs["payload"]["user_message_already_recorded"] is True
    call.progress.submitted.assert_awaited_once_with("local-turn", "d1")


@pytest.mark.asyncio
async def test_a_sideband_submit_to_chat_goes_to_the_owner_and_nothing_is_written_here(mirror, monkeypatch):
    def no_usage(*_args, **_kwargs):
        raise AssertionError("a mirror's counters are the owner's")

    monkeypatch.setattr(voice_bridge, "_record_session_usage", no_usage)
    bridge = voice_bridge.VoiceBridge(SESSION, "rtc_1", api_key="k", model="gpt-realtime-2",
                                      max_call_minutes=30, conversation_surface=True)
    bridge._remote_owner = PEER
    bridge._send = AsyncMock()
    bridge._pending_user = ["Fix the failing test please"]
    bridge._pending_assistant = ["Okay, sending it to the agent."]

    await bridge._service_call("call-1", "submit_to_chat", json.dumps({"request": "Fix the failing test"}))

    device, action, body = mirror.sent[0]
    assert (device, action, body["session_id"], body["prompt"]) == (PEER, "turns", SESSION, "Fix the failing test")
    assert body["payload"]["voice_delegation"] is True
    assert "user_message_already_recorded" not in body["payload"]
    output = json.loads(bridge._send.await_args_list[0].args[0]["item"]["output"])
    assert output["ok"] is True and output["turn_id"] == "owner-turn-1"
    await bridge._write_usage({"input_tokens": 120}, model_calls=1)
    await bridge._flush_exchange({})
    assert_mirror_untouched(mirror)


@pytest.mark.asyncio
async def test_a_spoken_yes_is_answered_on_the_owner(mirror, monkeypatch):
    from app.core.permission_handler import permission_handler
    from app.core.voice_approval import SpokenApprovals

    async def no_local_answer(**_kwargs):
        raise AssertionError("a mirror's prompt is answered on its owner")

    monkeypatch.setattr(permission_handler, "respond", no_local_answer)
    desk = SpokenApprovals(SESSION, owner=PEER)
    assert desk.offer({"request_id": "req-1", "tool_name": "Bash"})
    desk.delivered("req-1")

    outcome = await desk.hear("Yes, go ahead.")

    assert outcome is not None and "recorded it" in outcome.text
    device, action, body = mirror.sent[0]
    assert (device, action) == (PEER, "permission")
    assert (body["request_id"], body["session_id"], body["decision"]) == ("req-1", SESSION, "allow")
    assert_mirror_untouched(mirror)


@pytest.mark.asyncio
async def test_a_spoken_answer_is_not_sent_once_the_conversation_moved(mirror, monkeypatch):
    from app.core.voice_approval import SpokenApprovals

    monkeypatch.setattr(control, "owner_of", lambda row: "some-other-worker")
    desk = SpokenApprovals(SESSION, owner=PEER)
    desk.offer({"request_id": "req-1", "tool_name": "Bash"})
    desk.delivered("req-1")

    outcome = await desk.hear("Yes.")

    assert "could not be recorded" in outcome.text and "another device" in outcome.text
    assert mirror.sent == []


def test_the_request_prompt_is_the_callers_words_when_there_are_any():
    from app.core.voice_remote import request_prompt
    assert request_prompt("Fix it.", "Live conversation context:\nuser: Fix it.\n") == "Fix it."
    assert request_prompt("", "Fix the failing test") == "Fix the failing test"
    assert request_prompt(None, "  Fix the failing test ") == "Fix the failing test"
