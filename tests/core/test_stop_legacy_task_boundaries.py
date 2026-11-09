"""Legacy task bounds must never turn into permission to stop newer work."""
from unittest.mock import AsyncMock, patch

import pytest

from tests.core.test_event_write_lock_timeout import (
    NEXT_TURN, SID, _late_stop_reaches_the_old_runtime, _second_worker,
    _session, real_worker,  # noqa: F401 - shared focused runtime fixtures
)


def _restore_legacy(bound, *, remote=True):
    from app.core.task_registry import task_registry

    row = task_registry._tasks[SID].pop("worker-a")
    row.update(remote_device_id="other-install" if remote else None, turn_order=None,
               turn_order_upper_bound=bound, turn_order_incarnation=row.get("started_at"))
    task_registry.hydrate_rows(SID, [row])


async def test_late_stop_defers_unknown_remote_owner_even_with_upgrade_bound(real_worker):
    from app.core.task_registry import task_registry

    _restore_legacy(NEXT_TURN - 1)
    newer = _second_worker()
    signal = await _late_stop_reaches_the_old_runtime()
    # A legacy upper bound is not affirmative receiver enforcement. The new
    # compatibility policy must leave this owner alone and say it is incomplete.
    assert real_worker.torn_down is False
    assert task_registry.get_task(SID, "worker-a")["status"] == "running"
    from app.db import session_events
    assert any("STILL BE RUNNING" in str(e) and "other-install" in str(e)
               for e in session_events.get_events(SID))
    assert newer.torn_down is False
    signal.assert_not_awaited()


@pytest.mark.parametrize("bound", [None, NEXT_TURN])
async def test_unattributed_legacy_task_is_spared_and_reported(real_worker, bound):
    from app.core import query_session_lifecycle as qsl

    _restore_legacy(bound)
    scope = qsl.capture_stop_scope(SID, before_turn=NEXT_TURN)
    with patch.object(qsl, "get_session_pool") as pool:
        pool.return_value.peek.return_value = None
        results = await qsl.stop_all_background_tasks(SID, scope=scope)
    assert real_worker.torn_down is False
    assert any(row.get("task_id") == "worker-a" and row.get("incomplete") for row in results)


async def test_resuming_legacy_task_replaces_upgrade_bound(real_worker):
    from app.core.task_registry import task_registry

    _restore_legacy(NEXT_TURN - 1)
    task_registry.complete_task(SID, "worker-a", "completed")
    task_registry.note_turn_live(SID, NEXT_TURN)
    task_registry.resume_task(SID, "worker-a")
    await _late_stop_reaches_the_old_runtime()
    assert real_worker.torn_down is False
    assert task_registry.task_turn_order(SID, "worker-a") == NEXT_TURN
    assert task_registry.get_task(SID, "worker-a").get("turn_order_upper_bound") is None


async def test_late_stop_reports_unattributed_work_durably(real_worker):
    from app.db import session_events

    _restore_legacy(None)
    await _late_stop_reaches_the_old_runtime()
    notes = [event for event in session_events.get_events(SID)
             if event["event_type"] == "system.note" and event["payload"].get("subtype") == "stop_incomplete"]
    assert len(notes) == 1
    assert notes[0]["payload"]["data"]["incomplete"] is True
    assert notes[0]["payload"]["data"]["tasks"] == ["worker-a"]


async def test_peer_stop_returns_incomplete_cleanup(monkeypatch):
    from app.core import query_session_lifecycle as qsl, turn_manager
    from app.network import control

    monkeypatch.setattr(control, "_require_owned", lambda _sid: None)
    monkeypatch.setattr(turn_manager, "interrupt_async", AsyncMock(return_value=None))
    monkeypatch.setattr(turn_manager, "capture_stop_receipt", lambda *_args: {})
    results = [{"ok": False, "task_id": "legacy", "incomplete": True}]
    monkeypatch.setattr(qsl, "stop_all_background_tasks", AsyncMock(return_value=results))
    result = await control._interrupt({"session_id": SID, "stop_background": True}, "peer")
    assert result["interrupted"] is True
    assert result["incomplete"] is True
    assert result["tasks"] == results


@pytest.mark.parametrize("remote", [False, True])
async def test_websocket_reports_incomplete_stop(monkeypatch, remote):
    from app.core import query_session_lifecycle as qsl, turn_manager
    from app.network import control
    from tests.api.test_chat_ws_v2_turns import _make_handler

    handler = _make_handler()
    handler._may_act_on_session = AsyncMock(return_value=True)
    handler._network_owner = AsyncMock(return_value="peer" if remote else None)
    handler._v2_broadcast_queue = AsyncMock()
    monkeypatch.setattr(control, "interrupt", AsyncMock(return_value={"incomplete": True}))
    monkeypatch.setattr(turn_manager, "interrupt_async", AsyncMock(return_value=None))
    monkeypatch.setattr(turn_manager, "capture_stop_receipt", lambda *_args: {})
    monkeypatch.setattr(qsl, "stop_all_background_tasks", AsyncMock(return_value=[{"incomplete": True}]))
    await handler.handle_stop({"session_id": SID})
    errors = [call.args[0] for call in handler.send_json.call_args_list
              if call.args[0].get("type") == "error"]
    assert len(errors) == 1
    assert "incomplete" in errors[0]["message"]
