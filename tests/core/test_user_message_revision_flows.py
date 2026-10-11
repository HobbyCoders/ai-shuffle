"""Actual intake -> queue -> runtime -> authoritative event/row revision flows."""
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_runtime, turn_manager
from app.core.turn_recorder import TurnRecorder
from app.db import database, session_events, turns
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import _runtime_env
from tests.core.test_turn_queue_intake import API_USER, _FakeConnector, _incoming, _wire_connector
from tests.core.test_send_steering import _ws_handler


@pytest.fixture(autouse=True)
def clean_runtime():
    def reset():
        session_runtime._reader_locks.clear()
        session_runtime._idle_readers.clear()
        session_runtime._live_runtimes.clear()
        session_runtime.register_scheduler(None)
        session_runtime.register_broadcaster(None)
        turn_manager.register_kick(None)
    reset()
    yield
    reset()


@pytest.fixture
def drain(monkeypatch):
    delivered = []
    real_recorder = TurnRecorder.create_for_run

    async def run(sid, **kwargs):
        assert database.get_session(sid)["last_user_message_at"] is None
        queued = turns.get_queued_turns(sid)
        assert len(queued) == 1
        assert not (queued[0].get("payload") or {}).get("deferred_user_record")
        # Only the provider/pump and unrelated lifecycle collaborators are fake.
        # The intake, submit, turn/run DBs, runtime and recorder are real.
        with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
            with patch.object(TurnRecorder, "create_for_run", real_recorder):
                assert await session_runtime.SessionRuntime(queued[0]).execute() == "completed"
        assert database.get_session(sid)["last_user_message_at"] is not None
        assert database.get_session_messages(sid) == []  # event-only path
        users = [e for e in session_events.get_events(sid, 0) if e["event_type"] == "message.user"]
        assert len(users) == 1
        delivered.append(sid)
        return True

    monkeypatch.setattr(turn_manager, "wait_session_idle", run)
    return run, delivered


async def test_rest_conversation_delivery(drain):
    from app.api.query import conversation
    from app.core.models import ConversationRequest

    _, delivered = drain
    result = await conversation(ConversationRequest(prompt="An API answer"), api_user=API_USER)
    assert delivered == [result["session_id"]]


async def test_connector_delivery(monkeypatch, drain):
    from app.core.connectors.manager import ConnectorManager

    database.create_session("connector-revision", "default")
    manager, connector = ConnectorManager(), _FakeConnector()
    _wire_connector(monkeypatch, manager, connector,
                    session_row={"id": "cs-1", "session_id": "connector-revision"})
    await manager.handle_incoming_message(_incoming())
    assert drain[1] == ["connector-revision"]


async def test_queue_handler_drains_only_at_delivery(monkeypatch, drain):
    database.create_session("queue-revision", "default")
    handler = _ws_handler()
    handler._may_act_on_session = AsyncMock(return_value=True)
    handler._refuse_archived = AsyncMock(return_value=False)
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    monkeypatch.setattr("app.core.placement.resolve_placement", lambda row: None)
    await handler.handle_queue_message({"session_id": "queue-revision", "content": "Queued answer"})
    assert database.get_session("queue-revision")["last_user_message_at"] is None
    await drain[0]("queue-revision")


async def test_idle_relay_stamps_executing_worker_and_deduplicates(drain):
    from app.network import control

    database.create_session("relay-revision", "default", title="Remote chat")
    body = {"session_id": "relay-revision", "prompt": "Forwarded answer", "source": "user",
            "profile_id": "default", "command_id": "remote-command", "owner_send": True}
    result = await control._turns(body, "sending-worker")
    assert result["turn_id"]
    await drain[0]("relay-revision")
    timestamp = database.get_session("relay-revision")["last_user_message_at"]
    assert (await control._turns(body, "sending-worker"))["duplicate"] is True
    assert not turns.get_queued_turns("relay-revision")
    assert database.get_session("relay-revision")["last_user_message_at"] == timestamp


async def test_owner_configured_automation_delivery(drain):
    from app.core.automation_manager import AutomationManager

    database.create_automation(automation_id="revision-auto", name="Owner schedule",
        trigger_type="cron", trigger_config={"expression": "0 * * * *"},
        prompt="Scheduled work", profile_id="default")
    database.create_automation_run(run_id="revision-run", automation_id="revision-auto", status="running")
    result = await AutomationManager()._run_prompt_as_session(
        database.get_automation("revision-auto"), "revision-run", "Scheduled work", None)
    assert drain[1] == [result["session_id"]]


async def test_queued_agent_note_does_not_count_but_user_steer_does():
    sid = "steer-provenance"
    database.create_session(sid, "default")
    live = turns.enqueue_turn(sid, "user", prompt="Original")
    turns.start_turn(live["id"])
    runtime = session_runtime.SessionRuntime(turns.get_turn(live["id"]))
    recorder = await TurnRecorder.maybe_create(sid)
    assert recorder is not None
    try:
        for source in ("agent_message", "user"):
            row = turns.enqueue_turn(sid, source, prompt=f"{source} delivered")
            with patch("app.core.sync_engine.sync_engine.broadcast_message_added", new=AsyncMock()):
                await runtime._on_steer_delivered(recorder, {"steers": [{"id": row["id"]}]})
            assert turns.get_turn(row["id"])["state"] == "completed"
            assert (database.get_session(sid)["last_user_message_at"] is None) == (source == "agent_message")
        revision = database.get_session(sid)["last_user_message_at"]
        await runtime._on_steer_delivered(recorder, {"steers": [{"id": row["id"]}]})
        assert database.get_session(sid)["last_user_message_at"] == revision
        legacy = database.get_session_messages(sid)
        assert [m["metadata"]["source"] for m in legacy] == ["agent_message", "user"]
        events = [e for e in session_events.get_events(sid, 0) if e["event_type"] == "message.user"]
        assert events[0]["payload"]["source"] == "agent_message"
    finally:
        await recorder.close()


@pytest.mark.parametrize("source", ["agent_message", "wakeup", "unpark", "promotion"])
async def test_autonomous_start_is_not_a_user_answer(source):
    sid = "autonomous"
    database.create_session(sid, "default")
    recorder = await TurnRecorder.maybe_create(sid)
    assert recorder is not None
    try:
        await recorder.start("Agent generated work", source=source)
        assert database.get_session(sid)["last_user_message_at"] is None
    finally:
        await recorder.close()


async def test_voice_system_note_does_not_count_but_utterance_does():
    from app.core.turn_recorder import record_voice_exchange

    database.create_session("voice-revision", "default")
    await record_voice_exchange("voice-revision", None, "Ready", system_text="Call started")
    assert database.get_session("voice-revision")["last_user_message_at"] is None
    await record_voice_exchange("voice-revision", "Yes please", "Okay")
    assert database.get_session("voice-revision")["last_user_message_at"] is not None
