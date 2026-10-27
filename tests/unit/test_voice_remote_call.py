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


# -- review fixes: repeats, lost answers, status and steered turns ----------


@pytest.mark.asyncio
async def test_a_forwarded_request_carries_a_stable_command_id(mirror):
    call = streamed_call()
    call.upstream = Upstream()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    await call.caption("user", "Make the rail icons bigger.")

    await call.run_delegation("delegation-1")
    await call.run_delegation("delegation-1")  # A repeat of the same handoff.

    ids = [body["command_id"] for _, _, body in mirror.sent]
    assert ids == [f"voice:{call.call_key}:delegation-1"] * 2  # The owner dedupes the repeat.


@pytest.mark.asyncio
async def test_a_lost_answer_says_it_may_have_reached_the_chat(mirror, monkeypatch):
    async def lost(*_args, **_kwargs):
        raise control.MaybeApplied("GamingPC did not answer in time.")

    monkeypatch.setattr(control, "call", lost)
    call = streamed_call()
    call.upstream = Upstream()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    await call.caption("user", "Run the tests.")

    await call.run_delegation("delegation-1")

    call.progress.submitted.assert_not_awaited()
    spoken = call.upstream.sent[0]["content"]
    assert "may already be in the chat" in spoken and "check the chat before asking again" in spoken
    assert "nothing was sent" not in spoken
    assert_mirror_untouched(mirror)


def sideband(monkeypatch):
    monkeypatch.setattr(voice_bridge, "_record_session_usage", lambda *a, **k: None)
    bridge = voice_bridge.VoiceBridge(SESSION, "rtc_1", api_key="k", model="gpt-realtime-2",
                                      max_call_minutes=30, conversation_surface=True)
    bridge._remote_owner = PEER
    bridge._send = AsyncMock()
    return bridge


def tool_output(bridge):
    return json.loads(bridge._send.await_args_list[0].args[0]["item"]["output"])


@pytest.mark.asyncio
async def test_a_sideband_request_carries_the_tool_call_as_its_command_id(mirror, monkeypatch):
    bridge = sideband(monkeypatch)

    await bridge._service_call("call-7", "submit_to_chat", json.dumps({"request": "Fix the test"}))

    assert mirror.sent[0][2]["command_id"] == "voice:rtc_1:call-7"


@pytest.mark.asyncio
async def test_an_owner_answer_without_a_turn_id_is_still_a_success(mirror, monkeypatch):
    async def no_turn(device_id, action, body=None, **_kwargs):
        mirror.sent.append((device_id, action, body))
        return {"session_id": body["session_id"]}

    monkeypatch.setattr(control, "call", no_turn)
    bridge = sideband(monkeypatch)

    await bridge._service_call("call-1", "submit_to_chat", json.dumps({"request": "Fix the test"}))

    output = tool_output(bridge)
    assert output["ok"] is True and output["error"] is None and output["turn_id"] is None


@pytest.mark.asyncio
async def test_a_sideband_lost_answer_is_not_reported_as_unsent(mirror, monkeypatch):
    async def lost(*_args, **_kwargs):
        raise control.MaybeApplied("no answer")

    monkeypatch.setattr(control, "call", lost)
    bridge = sideband(monkeypatch)

    await bridge._service_call("call-1", "submit_to_chat", json.dumps({"request": "Fix the test"}))

    output = tool_output(bridge)
    assert output["ok"] is False and "may already be in the chat" in output["error"]


@pytest.mark.asyncio
async def test_a_spoken_answer_with_a_lost_reply_says_check_the_screen(mirror, monkeypatch):
    from app.core.voice_approval import SpokenApprovals

    async def lost(*_args, **_kwargs):
        raise control.MaybeApplied("no answer")

    monkeypatch.setattr(control, "call", lost)
    desk = SpokenApprovals(SESSION, owner=PEER)
    desk.offer({"request_id": "req-1", "tool_name": "Bash"})
    desk.delivered("req-1")

    outcome = await desk.hear("Yes.")

    assert "may already be recorded" in outcome.text and "on screen" in outcome.text
    assert "could not reach" not in outcome.text


@pytest.mark.asyncio
async def test_the_sideband_resolves_the_owner_when_it_starts(mirror, monkeypatch):
    """VoiceBridge.start reads the owner before opening the sideband."""
    class Opened(Exception):
        pass

    async def refuse(*_args, **_kwargs):
        raise Opened

    monkeypatch.setattr(voice_bridge, "_open_sideband", refuse)
    bridge = voice_bridge.VoiceBridge(SESSION, "rtc_1", api_key="k", model="gpt-realtime-2",
                                      max_call_minutes=30, conversation_surface=True)
    with pytest.raises(Opened):
        await bridge.start()
    assert bridge._remote_owner == PEER


def publish_runtime(state, owner=PEER):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO network_session_runtime (session_id, owner_device_id, state) VALUES (%s, %s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET owner_device_id = EXCLUDED.owner_device_id, state = EXCLUDED.state",
            (SESSION, owner, state))


@pytest.mark.parametrize("state, chat", [
    ("working", "working"), ("permission", "working"), ("parked", "waiting_on_background"),
    ("idle", "idle"), (None, "unknown"),
])
def test_status_of_a_chat_another_worker_runs_comes_from_its_runtime(mirror, state, chat):
    from app.core import voice_status
    if state:
        publish_runtime(state)

    status = voice_status.work_status(SESSION)

    assert status["chat"] == chat and status["runs_elsewhere"] is True
    if chat == "unknown":
        assert "has not reported" in voice_status.status_sentence(status)


def test_a_runtime_summary_from_another_owner_says_nothing(mirror):
    from app.core import voice_status
    publish_runtime("working", owner="an-earlier-owner")

    assert voice_status.work_status(SESSION)["chat"] == "unknown"


def progress(owner=PEER):
    from app.core.voice_progress import VoiceProgress
    sent = []

    async def send(event):
        sent.append(event)

    return VoiceProgress(SESSION, send, owner=owner), sent


def event(event_type, turn_id, **payload):
    return {"event_type": event_type, "turn_id": turn_id, "payload": payload}


@pytest.mark.asyncio
async def test_a_request_the_owner_steered_into_its_running_turn_is_settled(mirror):
    from app.core.voice_progress import REMOTE_STEERED_NOTE
    publish_runtime("working")
    desk, _ = progress()
    await desk.submitted("owner-turn-1", "delegation-1")
    # The owner's running turn takes the request in and ends; ours never starts.
    desk.consume(event("assistant.text_delta", "running-turn", text="Bigger icons are in."))
    desk.consume(event("turn.completed", "running-turn"))
    await desk.check_unstarted(now=100)
    assert "owner-turn-1" in desk.active_turns  # The owner still reports work.

    publish_runtime("idle")
    await desk.check_unstarted(now=200)

    assert "owner-turn-1" not in desk.active_turns and not desk.unstarted
    assert desk.pending["steer:owner-turn-1"] == (REMOTE_STEERED_NOTE, "delegation-1", False)


@pytest.mark.asyncio
async def test_a_request_queued_behind_the_running_turn_is_not_settled_early(mirror):
    publish_runtime("working")
    desk, _ = progress()
    await desk.submitted("owner-turn-1", "delegation-1")
    desk.consume(event("turn.completed", "running-turn"))
    desk.consume(event("turn.started", "owner-turn-1"))  # Ours starts next.
    publish_runtime("idle")  # Even a stale idle cannot settle a started turn.

    await desk.check_unstarted(now=100)

    assert "owner-turn-1" in desk.active_turns and "steer:owner-turn-1" not in desk.pending
    desk.consume(event("message.assistant", "owner-turn-1", content="Done.", kind="text"))
    desk.consume(event("turn.completed", "owner-turn-1"))
    assert "owner-turn-1" not in desk.active_turns
    assert desk.pending["turn:owner-turn-1"][0].endswith("Done.")


@pytest.mark.asyncio
async def test_a_request_sent_while_the_owner_was_idle_is_not_settled_before_a_turn_ends(mirror):
    publish_runtime("idle")  # The summary from before the request.
    desk, _ = progress()
    await desk.submitted("owner-turn-1", "delegation-1")

    await desk.check_unstarted(now=100)

    assert desk.unstarted == {"owner-turn-1"}


@pytest.mark.asyncio
async def test_a_call_that_starts_mid_work_on_the_owner_hears_so(mirror, monkeypatch):
    from app.core import event_gateway
    publish_runtime("working")
    monkeypatch.setattr(event_gateway, "get_gateway", lambda: SimpleNamespace(subscribe=lambda sid: None,
                                                                                unsubscribe=lambda *a: None))
    desk, _ = progress()

    await desk.open()

    assert desk.pending["chat"][0] == "Chat work is already running. Do not start it again."
