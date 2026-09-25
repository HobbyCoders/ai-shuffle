"""Exercise lifecycle wiring without providers, live connectors, or delivery."""
import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import lifecycle_events, session_runtime, interaction_events
from app.core.automation_manager import AutomationManager, automation_manager
from app.core.connectors.manager import ConnectorManager, connector_manager
from app.db import turns


@pytest.fixture(autouse=True)
async def clean_delivery():
    await lifecycle_events.stop()
    yield
    await lifecycle_events.stop()


async def test_runtime_enqueues_only_winning_start_and_settlement(monkeypatch, mock_db):
    from tests.core.helpers import make_pump
    from tests.core.test_session_runtime import _runtime_env
    emitted = []
    monkeypatch.setattr(lifecycle_events, "enqueue", lambda *args, **kw: emitted.append(args))
    row = turns.enqueue_turn("fixture-session", "user", prompt="not sent to a model")
    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        assert await session_runtime.SessionRuntime(row).execute() == "completed"
        assert await session_runtime.SessionRuntime(row).execute() is None
    assert emitted == [("started", "fixture-session", row["id"]),
                       ("completed", "fixture-session", row["id"])]


@pytest.mark.parametrize("metadata", [{"is_error": True}, {"errors": ["max turns exceeded"]}])
async def test_durable_failed_result_reaches_error_lifecycle(monkeypatch, mock_db, metadata):
    from tests.core.helpers import make_pump
    from tests.core.test_session_runtime import _runtime_env
    from app.core.turn_recorder import TurnRecorder
    monkeypatch.setattr(lifecycle_events.database, "get_session", lambda sid: {"profile_id": "p"})
    monkeypatch.setattr(lifecycle_events.database, "get_connector_sessions_for_ai_session", lambda sid: [])
    async def create_recorder(sid, **kw):
        recorder = TurnRecorder(sid, None, kw["run_id"], kw["fence_token"], "fixture")
        recorder.turn_id = kw["turn_id"]
        recorder._owns_lease = False
        return recorder
    row = turns.enqueue_turn("failed-result", "user", prompt="fixture")
    # Restore our factory override before _runtime_env restores the real class
    # method. Function-scoped teardown would reinstall that helper's AsyncMock
    # and silently disable recording in later tests on the same worker.
    with _runtime_env(pump=make_pump([{"type": "done", "metadata": metadata}])), \
            monkeypatch.context() as recorder_patch:
        recorder_patch.setattr(session_runtime.TurnRecorder, "create_for_run", create_recorder)
        assert await session_runtime.SessionRuntime(row).execute() == "completed"
    event = lifecycle_events._payload("completed", "failed-result", row["id"], {})
    assert event[0] == "session.error"
    assert event[1]["error_message"] == "Provider execution failed"


async def test_interaction_creation_notifies_without_tool_arguments(monkeypatch):
    monkeypatch.setattr(interaction_events, "_emit", AsyncMock())
    monkeypatch.setattr(turns, "get_live_turn", lambda sid: {"source": "automation"})
    emit = MagicMock()
    monkeypatch.setattr(lifecycle_events, "enqueue", emit)
    await interaction_events.emit_interaction_requested("s", "i", "r", "permission", {"tool_input": "private"})
    emit.assert_called_once_with("permission.requested", "s", "i", request_id="r", source="automation")


async def test_delivery_is_ordered_and_survives_failure(monkeypatch):
    monkeypatch.setattr(lifecycle_events, "_payload", lambda kind, sid, identity, extra: (kind, {"event_id": identity}))
    send = AsyncMock(side_effect=[RuntimeError("fake failure"), None])
    monkeypatch.setattr(connector_manager, "dispatch_event", send)
    lifecycle_events.start()
    lifecycle_events.enqueue("session.started", "s", "start")
    lifecycle_events.enqueue("session.complete", "s", "end")
    await asyncio.wait_for(lifecycle_events._queue.join(), 2)
    assert [call.args[1]["event_id"] for call in send.call_args_list] == ["start", "end"]


async def test_automations_receive_events_without_outbound_subscriptions(monkeypatch):
    from app.core import webhook_service
    from app.db import database
    monkeypatch.setattr(database, "get_connectors_for_event", lambda event: [])
    webhook = AsyncMock()
    automation = AsyncMock()
    monkeypatch.setattr(webhook_service, "dispatch_webhook", webhook)
    monkeypatch.setattr(automation_manager, "handle_connector_event", automation)
    await ConnectorManager().dispatch_event("session.complete", {"session_id": "s"})
    webhook.assert_awaited_once()
    automation.assert_awaited_once()


async def test_automation_matches_connector_and_filter_on_same_trigger(monkeypatch):
    from app.db import database
    manager = AutomationManager()
    rows = [{"id": "a", "triggers": [
        {"type": "connector_event", "config": {"event_type": "session.complete", "connector_id": "other"}},
        {"type": "connector_event", "config": {"event_type": "session.complete", "connector_id": "right", "filter": {"profile_id": "p"}}},
    ]}]
    monkeypatch.setattr(database, "get_automations_for_event", lambda **kw: rows)
    execute = AsyncMock()
    monkeypatch.setattr(manager, "_execute", execute)
    # Await captured work directly instead of letting the real scheduler run.
    pending = []
    monkeypatch.setattr(manager, "_create_tracked_task", lambda coro, **kw: pending.append(coro))
    await manager.handle_connector_event("session.complete", {"connector_ids": ["unrelated"], "profile_id": "p"})
    assert pending == []
    await manager.handle_connector_event("session.complete", {"connector_ids": ["right"], "profile_id": "p"})
    assert len(pending) == 1
    await pending[0]
    execute.assert_awaited_once()


async def test_unfiltered_trigger_fires_only_for_connector_linked_sessions(monkeypatch):
    """Every turn emits lifecycle events; a trigger with no connector set must
    not start a run after each wake, queue or subagent turn on plain sessions."""
    from app.db import database
    manager = AutomationManager()
    rows = [{"id": "a", "triggers": [{"type": "connector_event", "config": {"event_type": "session.complete"}}]}]
    monkeypatch.setattr(database, "get_automations_for_event", lambda **kw: rows)
    pending = []
    monkeypatch.setattr(manager, "_execute", AsyncMock())
    monkeypatch.setattr(manager, "_create_tracked_task", lambda coro, **kw: pending.append(coro))
    await manager.handle_connector_event("session.complete", {"session_id": "s", "source": "wakeup", "connector_ids": []})
    assert pending == []
    await manager.handle_connector_event("session.complete", {"session_id": "s", "source": "user", "connector_ids": ["any"]})
    assert len(pending) == 1
    pending[0].close()


@pytest.mark.parametrize("data", [
    {"source": "automation"}, {"source": "automation_step"},
    {"source": "wakeup", "automation_id": "parent"},
])
async def test_automation_lifecycle_does_not_recursively_start_models(monkeypatch, data):
    from app.db import database
    lookup = MagicMock()
    monkeypatch.setattr(database, "get_automations_for_event", lookup)
    await AutomationManager().handle_connector_event("session.complete", data)
    lookup.assert_not_called()


@pytest.mark.parametrize("outcome,reason,payload,event", [
    ("completed", None, {"usage": {"tokens_in": 7, "tokens_out": 3, "duration_ms": 1200}}, "session.complete"),
    ("completed", None, {"api_error_status": 429}, "session.error"),
    ("completed", None, {"is_error": True}, "session.error"),
    ("completed", None, {"errors": ["max turns exceeded"]}, "session.error"),
    ("aborted", "error", {}, "session.error"),
    ("aborted", "interrupted", {}, None),
    ("aborted", "interrupt_escalation", {}, None),
    ("aborted", "drain_stale_result", {}, None),
])
def test_terminal_payload_uses_turn_identity_and_distinguishes_failure(monkeypatch, outcome, reason, payload, event):
    monkeypatch.setattr(lifecycle_events.database, "get_session", lambda sid: {"profile_id": "p", "title": "fixture"})
    monkeypatch.setattr(lifecycle_events.database, "get_connector_sessions_for_ai_session", lambda sid: [{"connector_id": "c"}])
    monkeypatch.setattr(turns, "get_turn", lambda tid: {"source": "user", "stop_reason": reason, "error": "fixture failure"})
    cursor = MagicMock()
    cursor.fetchone.return_value = {"payload": payload}
    connection = MagicMock()
    connection.cursor.return_value = cursor
    @contextmanager
    def fake_db():
        yield connection
    monkeypatch.setattr(lifecycle_events, "get_db", fake_db)
    result = lifecycle_events._payload(outcome, "s", "specific-turn", {})
    assert cursor.execute.call_args.args[1] == ("s", "specific-turn")
    if event is None:
        assert result is None
    else:
        assert result[0] == event
        assert result[1]["event_id"] == f"specific-turn:{event}"
        assert result[1]["connector_ids"] == ["c"]
        if event == "session.complete":
            assert result[1]["input_tokens"] == 7
            assert result[1]["duration_seconds"] == 1.2
