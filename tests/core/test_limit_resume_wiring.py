"""Where #1831 plugs into the rest of the app: the settlement hook, the Task
worker pause, the worker report's one note, the park watchdog, the steer
gate for ``/compact`` and the native ``/compact`` command listing."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core import limit_resume as lr
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _clean():
    from app.core import wakeup_service

    task_registry.reset_all()
    yield
    task_registry.reset_all()
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


# ─── The settlement hook ────────────────────────────────────────────────────


async def test_a_usage_limit_settlement_asks_for_a_resume():
    from app.core import turn_scheduler

    terminal = {"api_error_status": 429, "errors": ["resets 11:10pm"]}
    settled = {"id": "t1", "payload": {}}
    stop = AsyncMock(return_value=None)
    with patch("app.core.account_failover.attempt_failover", AsyncMock()), \
            patch.object(lr, "on_limit_stop", stop):
        await turn_scheduler._maybe_failover("s1", settled, terminal)
    stop.assert_awaited_once_with("s1", settled, terminal)


async def test_a_stopped_or_ordinary_settlement_does_not():
    from app.core import turn_scheduler

    stop = AsyncMock()
    with patch("app.core.account_failover.attempt_failover", AsyncMock()), \
            patch.object(lr, "on_limit_stop", stop):
        await turn_scheduler._maybe_failover("s1", {"id": "t1"}, {"stop_reason": "end_turn"})
        task_registry.mark_user_stopped("s1")
        await turn_scheduler._maybe_failover("s1", {"id": "t1"}, {"api_error_status": 429})
    stop.assert_not_awaited()


# ─── Task workers ───────────────────────────────────────────────────────────


async def test_a_worker_stopped_by_a_limit_pauses_when_a_resume_is_scheduled():
    from app.core import provider_agent

    terminal = {"api_error_status": 429, "errors": ["resets 11:10pm"]}
    stop = AsyncMock(return_value={"fire_at": 1.0})
    with patch("app.core.turn_scheduler._turn_terminal_payload", return_value=terminal), \
            patch("app.db.turns.get_turn", return_value={"id": "wt"}), \
            patch.object(lr, "on_limit_stop", stop):
        paused = await provider_agent._pause_for_usage_limit("w1", "wt", "parent-1")
    assert paused == lr.PAUSED_SUMMARY
    assert stop.await_args.kwargs["worker_owner"] == "parent-1"


@pytest.mark.parametrize("terminal,scheduled", [
    ({"stop_reason": "end_turn"}, {"fire_at": 1.0}),   # not a limit
    ({"api_error_status": 429}, None),                  # setting off / no reset
])
async def test_a_worker_settles_as_before_otherwise(terminal, scheduled):
    from app.core import provider_agent

    with patch("app.core.turn_scheduler._turn_terminal_payload", return_value=terminal), \
            patch("app.db.turns.get_turn", return_value={"id": "wt"}), \
            patch.object(lr, "on_limit_stop", AsyncMock(return_value=scheduled)):
        assert await provider_agent._pause_for_usage_limit("w1", "wt", "parent-1") is None


async def test_the_parent_hears_once_with_the_resumed_report(monkeypatch):
    """The resumed worker's report reaches its parent once, with the note."""
    from app.core import agent_messaging, provider_agent, turn_scheduler

    settled = {"id": "wt2", "payload": {"agent_message_reply_to": "parent-1",
                                         lr.PAYLOAD_KEY: {"limit_key": "claude:1"}}}
    monkeypatch.setattr(provider_agent, "_load_session_events", lambda sid: [
        {"turn_id": "wt2", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Done: the report."}},
    ])
    monkeypatch.setattr(turn_scheduler, "_session_has_parked_turn", lambda sid: False)
    monkeypatch.setattr("app.db.turns.session_has_unsettled_turns", lambda sid: False)
    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", lambda sid: {"id": "wt2"})
    terminal = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    monkeypatch.setattr("app.core.worker_checkout.reclaim_untouched_worker_checkout",
                        lambda sid: None)
    await agent_messaging.finish_message_resume("w1", settled, "completed")
    terminal.assert_awaited_once()
    owner, frame = terminal.await_args.args
    assert owner == "parent-1" and frame["status"] == "completed"
    assert frame["summary"].startswith(lr.WORKER_REPORT_NOTE)
    assert frame["summary"].count(lr.WORKER_REPORT_NOTE) == 1
    assert "Done: the report." in frame["summary"]


# ─── Park watchdog ──────────────────────────────────────────────────────────


async def test_the_park_watchdog_skips_a_parent_waiting_on_a_paused_worker(mock_db):
    from app.core import startup_reconcile as sr, wakeup_service
    from tests.core.test_park_watchdog import _seed_task, _seed_turn

    _seed_turn("pw-paused-parent", "t-parked", "parked")
    _seed_task("pw-paused-parent", "w-paused")
    await wakeup_service.schedule(
        session_id="w-paused", profile_id="p", project_id=None, prompt="x",
        delay_seconds=3600, reason=json.dumps({"owner": "pw-paused-parent"}),
        source=lr.LIMIT_RESUME_SOURCE,
    )
    forced = AsyncMock()
    with patch.object(sr, "_force_stalled_park", forced), \
            patch("app.core.startup_reconcile.record_error") as rec:
        assert await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=60) == 0
    forced.assert_not_awaited()
    rec.assert_not_called()


# ─── /compact is a turn of its own ──────────────────────────────────────────


def test_a_queued_compact_is_never_steered_into_a_live_turn(mock_db):
    from app.core import session_runtime
    from app.db import database, turns

    database.create_profile("agent", "Agent", None, {})
    live = turns.enqueue_turn("sc1", "user", prompt="work", profile_id="agent")
    runtime = session_runtime.SessionRuntime(live)
    plain = turns.enqueue_turn("sc1", "user", prompt="and also this", profile_id="agent")
    compact = turns.enqueue_turn("sc1", "user", prompt="/compact", profile_id="agent")
    assert runtime._steer_eligible(plain)
    assert not runtime._steer_eligible(compact)


def test_native_legs_list_compact_and_voice_does_not(tmp_path):
    from app.core.slash_commands import get_all_commands

    for kind in ("openai_responses", "anthropic", "openrouter"):
        names = [c["name"] for c in get_all_commands(str(tmp_path), provider_kind=kind)]
        assert names.count("compact") == 1
    assert "compact" not in [c["name"] for c in get_all_commands(str(tmp_path), provider_kind="voice")]
