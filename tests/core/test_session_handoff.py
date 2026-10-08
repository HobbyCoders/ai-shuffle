"""Agent-queued hand-off to another worker, run when the session is idle."""
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.core import session_handoff as sh

DIRECTORY = {"runners": [
    {"device_id": "dev-unraid", "device_name": "Unraid", "online": True, "is_self": True},
    {"device_id": "dev-gaming", "device_name": "DesktopWorker", "online": True, "is_self": False},
    {"device_id": "dev-laptop", "device_name": "Laptop", "online": False, "is_self": False},
]}


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    sh._pending.clear()
    sh._running.clear()
    sh._waiting.clear()
    sh._moving.clear()
    sh._delivering.clear()
    store = tmp_path / "pending-handoffs.json"
    with patch.object(sh, "_workers", return_value=DIRECTORY), \
            patch.object(sh, "_store_path", lambda: store, create=True), \
            patch.object(sh, "_queue_changed", AsyncMock()), \
            patch("app.core.turn_manager._kick"):
        yield
    sh._pending.clear()
    sh._waiting.clear()
    sh._moving.clear()


def test_move_wake_barrier_schema_upgrade_is_idempotent(mock_db):
    from app.db import database, session_moves
    from tests.db.migration_helpers import column_names
    from tests.pg_shim import ShimCursor

    session_moves.save("legacy", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=1, expires_at=2)
    session_moves.append_held("legacy", {"id": "held-legacy", "prompt": "keep me"})
    mock_db.execute("ALTER TABLE session_moves DROP COLUMN wake_token")
    mock_db.execute("ALTER TABLE session_moves DROP COLUMN wakes_superseded")
    mock_db.execute("DROP TABLE handoff_wakes")
    mock_db.commit()
    for _ in range(2):
        database._run_column_migrations(ShimCursor(mock_db.cursor()))
    assert {"wake_token", "wakes_superseded"} <= column_names(mock_db, "session_moves")
    assert {"token", "session_id", "source_id", "branch", "wakes", "superseded", "keep_peer", "finalized"} <= column_names(mock_db, "handoff_wakes")
    row = session_moves.get("legacy")
    assert row["held"] == [{"id": "held-legacy", "prompt": "keep me"}]
    assert row["wake_token"] is None
    assert row["wakes_superseded"]


@pytest.mark.parametrize("revoke_during_retry", [False, True])
async def test_revoked_source_stops_retrying_and_keeps_human_evidence(mock_db, monkeypatch, revoke_during_retry):
    from types import SimpleNamespace
    from app.db import session_moves
    from app.network import control, engine

    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=1, expires_at=time.time() + 600)
    session_moves.set_wake_token("s1", "old-token")
    held = {"id": "undelivered", "source": "user", "prompt": "preserve my human message"}
    session_moves.append_held("s1", held)
    member = SimpleNamespace(status="active" if revoke_during_retry else "revoked")
    ctx = SimpleNamespace(device_id="self", member=lambda device: member)
    monkeypatch.setattr(engine, "context", lambda: ctx)
    async def rejected(*args, **kwargs):
        member.status = "revoked"
        raise control.ControlRefused("source revoked")
    call = AsyncMock(side_effect=rejected)
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(sh, "FORWARD_RETRY_S", 0)
    record, notify, spawn = AsyncMock(), AsyncMock(), []
    monkeypatch.setattr(sh, "_record", record)
    monkeypatch.setattr(sh, "_notify", notify)
    monkeypatch.setattr(sh, "_spawn", spawn.append)
    await sh._deliver("s1", "dev-gaming", "DesktopWorker")
    assert call.await_count == int(revoke_during_retry)
    assert session_moves.get("s1")["held"] == [held]
    assert record.await_args.args[2]["reason"] == "source_revoked"
    assert "preserve my human message" in record.await_args.args[1]
    assert notify.await_args.args[1] == [held]
    # A subsequent boot reports the retained evidence, but spawns no delivery.
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"id": sid, "owner_device_id": "dev-gaming"})
    assert await sh.resume_queued() == 0
    assert spawn == []
    assert call.await_count == int(revoke_during_retry)
    assert session_moves.get("s1")["held"] == [held]


def test_resolves_by_name_or_device_id_and_refuses_offline_or_self():
    assert sh.resolve_worker("desktopworker")["device_id"] == "dev-gaming"
    assert sh.resolve_worker("dev-gaming")["device_name"] == "DesktopWorker"
    with pytest.raises(ValueError, match="offline"):
        sh.resolve_worker("Laptop")
    with pytest.raises(ValueError, match="Unraid is this worker"):
        sh.resolve_worker("Unraid")
    with pytest.raises(ValueError, match=r"Unknown worker 'Desktop'.*DesktopWorker \(online\)"):
        sh.resolve_worker("Desktop")
    with pytest.raises(ValueError, match="Name the worker"):
        sh.resolve_worker("  ")


def test_request_queues_and_cancel_drops_it():
    result = sh.request("s1", "DesktopWorker", "  tests pass;\n next: wire the UI ")
    assert result["queued"] and result["worker"] == "DesktopWorker"
    assert sh.pending_for("s1").note == "tests pass; next: wire the UI"
    assert sh.cancel("s1") and sh.pending_for("s1") is None


async def test_runs_the_move_when_idle_and_records_the_outcome():
    sh.request("s1", "DesktopWorker", "halfway")
    move = AsyncMock()
    with patch.object(sh, "_busy", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        outcome = await sh.run_pending("s1")
    move.assert_awaited_once_with("s1", {"id": "s1"}, "dev-gaming")
    assert outcome.startswith("Handed off to DesktopWorker")
    texts = [call.args[1] for call in record.await_args_list]
    assert texts[0] == "Handing off to DesktopWorker. Note: halfway"
    assert sh.pending_for("s1") is None


async def test_a_refused_move_is_reported_not_raised():
    sh.request("s1", "DesktopWorker")
    with patch.object(sh, "_busy", return_value=False), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to",
                  AsyncMock(side_effect=HTTPException(409, "project isn't checked out on DesktopWorker"))), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        outcome = await sh.run_pending("s1")
    assert outcome == "Hand-off to DesktopWorker failed: project isn't checked out on DesktopWorker"


async def test_gives_up_when_the_session_stays_busy():
    with patch.object(sh, "busy_state", return_value={"running": True, "tasks": []}), \
            patch.object(sh, "IDLE_TIMEOUT_S", 0), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", AsyncMock()) as move:
        sh.request("s1", "DesktopWorker")
        outcome = await sh.run_pending("s1")
    assert "still busy" in outcome
    move.assert_not_awaited()


async def test_nothing_queued_is_a_no_op():
    assert await sh.run_pending("nobody") is None


# #1819: the owner's move of a running or parked chat queues the same hand-off.

class _Busy:
    """A session that stays busy until ``idle`` is set."""
    def __init__(self):
        self.idle = False

    def __call__(self, _sid):
        return {"running": not self.idle, "tasks": []}


async def _settle(predicate, rounds=200):
    import asyncio
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_owner_move_is_queued_while_running_and_runs_when_the_turn_ends():
    busy = _Busy()
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        result = await sh.queue_move("s1", "dev-gaming")
        assert result == {"queued": True, "worker": "DesktopWorker", "after": "turn"}
        assert record.await_args_list[0].args[1] == "Move to DesktopWorker queued for when this turn ends."
        assert record.await_args_list[0].args[2] == {"status": "queued", "worker": "DesktopWorker", "by": "owner",
                                                     "after": "turn"}
        assert not await _settle(lambda: move.await_count, rounds=10)
        assert sh.pending_for("s1").by == "owner"
        state = await sh.owner_op("s1", "status")
        assert state["pending"] == {"worker": "DesktopWorker", "target_id": "dev-gaming", "by": "owner",
                                    "moving": False}
        assert state["busy"]["running"] is True
        busy.idle = True
        assert await _settle(lambda: move.await_count and sh.pending_for("s1") is None)
    move.assert_awaited_once_with("s1", {"id": "s1"}, "dev-gaming")
    assert sh.pending_for("s1") is None


async def test_owner_move_of_an_idle_chat_runs_at_once():
    busy = _Busy()
    busy.idle = True
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(lambda: move.await_count)


async def test_cancel_stops_a_waiting_move_and_says_so():
    busy = _Busy()
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(lambda: "s1" in sh._waiting)
        assert await sh.owner_op("s1", "cancel") == {"cancelled": True}
        assert record.await_args_list[-1].args[2] == {"status": "cancelled", "worker": "DesktopWorker", "by": "owner"}
        assert await _settle(lambda: "s1" not in sh._running)
        busy.idle = True
        assert (await sh.owner_op("s1", "status"))["pending"] is None
    move.assert_not_awaited()


async def test_cancel_with_nothing_queued_still_clears_the_chat():
    with patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.cancel_move("s1") == {"cancelled": False}
    assert record.await_args.args[2]["status"] == "cancelled"


async def test_a_second_queued_move_is_refused_until_the_first_is_cancelled():
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_record", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        with pytest.raises(ValueError, match="already queued"):
            await sh.queue_move("s1", "DesktopWorker")
        sh.cancel("s1")


async def test_stop_and_move_stops_the_work_first():
    stop = AsyncMock()
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_record", AsyncMock()), \
            patch("app.core.desktop_lifecycle.stop_session_work", stop):
        await sh.queue_move("s1", "DesktopWorker", stop=True)
        sh.cancel("s1")
    stop.assert_awaited_once_with("s1", source="session_handoff")


async def test_route_asks_the_owning_worker():
    from app.network import control
    call = AsyncMock(return_value={"queued": True, "worker": "DesktopWorker"})
    with patch.object(control, "owner_of", return_value="dev-unraid"), patch.object(control, "call", call):
        await sh.route({"id": "s1"}, "queue", "dev-gaming", stop=True)
    call.assert_awaited_once_with("dev-unraid", "session-move",
                                  {"session_id": "s1", "op": "queue", "target": "dev-gaming", "stop": True})


def test_busy_state_names_running_tasks():
    from app.core.task_registry import task_registry
    with patch("app.core.streaming_status.has_active_run", return_value=False), \
            patch("app.db.turns.session_has_unsettled_turns", return_value=False), \
            patch.object(task_registry, "get_active", return_value=[{"subject": "Run tests"}, {"agent": "explore"}]):
        assert sh.busy_state("s1") == {"running": False, "tasks": ["Run tests", "explore"],
                                       "queued": 0, "wake": False}


def test_a_queued_turn_keeps_the_session_busy():
    """A message waiting in the turn queue holds the hand-off like a live turn.

    The move itself refuses while a turn is queued (network/handoff._idle), so
    treating the session as idle made a queued hand-off fail instead of wait.
    """
    from app.core.task_registry import task_registry
    with patch("app.core.streaming_status.has_active_run", return_value=False), \
            patch("app.db.turns.get_live_turn", return_value=None), \
            patch("app.db.turns.get_queued_turns", return_value=[{"id": "t1", "state": "queued"}]), \
            patch("app.db.turns.session_has_unsettled_turns", return_value=True), \
            patch.object(task_registry, "get_active", return_value=[]):
        assert sh.busy_state("s1") == {"running": True, "tasks": [], "queued": 1, "wake": False}
        assert sh._busy("s1") is True


def test_an_unreadable_turn_queue_does_not_hold_the_hand_off_forever():
    from app.core.task_registry import task_registry
    with patch("app.core.streaming_status.has_active_run", return_value=False), \
            patch("app.db.turns.session_has_unsettled_turns", side_effect=RuntimeError("db down")), \
            patch.object(task_registry, "get_active", return_value=[]):
        assert sh.busy_state("s1") == {"running": False, "tasks": [], "queued": 0, "wake": False}

@pytest.mark.parametrize("stop", [False, True])
async def test_peer_owner_move_refuses_subagent_on_owning_worker(stop):
    from types import SimpleNamespace
    from app.network import control, engine
    member = SimpleNamespace(kind="worker", status="active")
    context = SimpleNamespace(member=lambda device: member)
    row = {"id": "child", "is_worker": True}
    queue = AsyncMock()
    with patch.object(engine, "require_context", return_value=context), \
            patch.object(control, "_require_owned", return_value=row) as owned, \
            patch.object(sh, "queue_move", queue):
        with pytest.raises(PermissionError, match="subagent"):
            await sh.handle_control({"session_id": "child", "op": "queue",
                                     "target": "dev-gaming", "stop": stop}, "dev-viewer")
    owned.assert_called_once_with("child")
    queue.assert_not_awaited()


@pytest.mark.parametrize("op", ["status", "cancel"])
async def test_peer_subagent_move_status_and_cancel_remain_available(op):
    from types import SimpleNamespace
    from app.network import control, engine
    context = SimpleNamespace(member=lambda device: SimpleNamespace(kind="worker", status="active"))
    operation = AsyncMock(return_value={"ok": True})
    with patch.object(engine, "require_context", return_value=context), \
            patch.object(control, "_require_owned", return_value={"id": "child", "is_worker": True}), \
            patch.object(sh, "owner_op", operation):
        assert await sh.handle_control({"session_id": "child", "op": op}, "dev-viewer") == {"ok": True}
    operation.assert_awaited_once_with("child", op, None, stop=False)


@pytest.mark.parametrize("is_worker", [False, None])
async def test_peer_owner_move_of_main_chat_remains_available(is_worker):
    from types import SimpleNamespace
    from app.network import control, engine
    context = SimpleNamespace(member=lambda device: SimpleNamespace(kind="worker", status="active"))
    queue = AsyncMock(return_value={"queued": True})
    with patch.object(engine, "require_context", return_value=context), \
            patch.object(control, "_require_owned", return_value={"id": "main", "is_worker": is_worker}), \
            patch.object(sh, "queue_move", queue):
        assert await sh.handle_control({"session_id": "main", "op": "queue",
                                        "target": "dev-gaming"}, "dev-viewer") == {"queued": True}
    queue.assert_awaited_once_with("main", "dev-gaming", stop=False)


async def test_agent_subagent_handoff_still_runs():
    row = {"id": "child", "is_worker": True}
    move = AsyncMock()
    sh.request("child", "DesktopWorker", "Tests pass")
    assert sh.pending_for("child").by == "agent"
    with patch.object(sh, "_busy", return_value=False), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value=row):
        assert (await sh.run_pending("child")).startswith("Handed off to DesktopWorker")
    move.assert_awaited_once_with("child", row, "dev-gaming")



# ---------------------------------------------------------------------------
# #1819 rules: queued messages, held messages, wakes, open prompts, restart,
# time limit. Each test names its rule.
# ---------------------------------------------------------------------------

def _chat(session_id="s1"):
    from app.db import database
    database.create_session(session_id=session_id, profile_id="default", title="t")


def _queued_prompts(session_id="s1"):
    from app.db import turns
    return [turn["prompt"] for turn in turns.get_queued_turns(session_id)]


def test_rule1_queued_turns_and_a_due_wake_keep_the_chat_busy():
    from app.core.task_registry import task_registry
    from app.db import wakeups
    _chat()
    from app.db import turns
    turns.enqueue_turn("s1", "user", prompt="first")
    turns.enqueue_turn("s1", "user", prompt="second")
    with patch("app.core.streaming_status.has_active_run", return_value=False), \
            patch.object(task_registry, "get_active", return_value=[]):
        state = sh.busy_state("s1")
        assert state["running"] and state["queued"] == 2
        assert sh._busy_words(state) == "the turn and 2 queued messages"
        with patch("app.db.turns.session_has_unsettled_turns", return_value=False):
            wakeups.create_wakeup(id="w-later", session_id="s1", prompt="later", fire_at=time.time() + 3600)
            assert sh.busy_state("s1")["wake"] is False
            wakeups.create_wakeup(id="w-soon", session_id="s1", prompt="soon", fire_at=time.time() + 5)
            state = sh.busy_state("s1")
            assert state["wake"] is True and sh._is_busy(state)
            assert sh._busy_words(state) == "a scheduled wake-up"


async def test_rule1_a_move_behind_queued_messages_says_so():
    state = {"running": True, "tasks": [], "queued": 2, "wake": False}
    with patch.object(sh, "busy_state", return_value=state), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record:
        result = await sh.queue_move("s1", "DesktopWorker")
        assert result["after"] == "queued"
        assert record.await_args.args[1] == "Move to DesktopWorker queued for after the queued messages."
        assert record.await_args.args[2]["after"] == "queued"
        sh.cancel("s1")


async def test_rule2_held_message_is_sent_on_the_new_worker_after_the_move():
    from app.core import turn_manager
    from app.db import session_moves
    _chat()
    busy = _Busy()
    move = AsyncMock()
    submit = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.network.control.submit_turn", submit):
        await sh.queue_move("s1", "DesktopWorker")
        assert sh.holds("s1")
        first = turn_manager._submit_blocking("s1", "user", prompt="one", profile_id="default")
        second = turn_manager._submit_blocking("s1", "queue", prompt="two", profile_id="default",
                                               explicit_permission=True)
        assert first.startswith(sh.HELD_PREFIX) and second.startswith(sh.HELD_PREFIX)
        # Not queued here; shown as queue chips instead.
        assert _queued_prompts() == []
        assert [entry["prompt"] for entry in sh.held_view("s1")] == ["one", "two"]
        # An agent's own turn is not held.
        assert not turn_manager._submit_blocking("s1", "agent_message", prompt="peer").startswith(sh.HELD_PREFIX)
        busy.idle = True
        assert await _settle(lambda: submit.await_count == 2 and session_moves.get("s1") is None)
    move.assert_awaited_once()
    assert [call.kwargs["prompt"] for call in submit.await_args_list] == ["one", "two"]
    assert [call.args for call in submit.await_args_list] == [("dev-gaming", "s1")] * 2
    assert [call.kwargs["source"] for call in submit.await_args_list] == ["user", "queue"]
    assert [call.kwargs["owner_send"] for call in submit.await_args_list] == [False, True]
    assert _queued_prompts() == ["peer"]
    assert session_moves.get("s1") is None
    assert "held_failed" not in [call.args[2]["status"] for call in record.await_args_list]


async def test_rule2_a_held_message_the_new_worker_refuses_is_reported():
    from app.db import session_moves
    from app.network import control
    _chat()
    busy = _Busy()
    submit = AsyncMock(side_effect=control.ControlRefused("This chat is archived"))
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", AsyncMock()), \
            patch("app.network.control.submit_turn", submit):
        await sh.queue_move("s1", "DesktopWorker")
        assert sh.hold("s1", "user", {"prompt": "one"})
        busy.idle = True
        assert await _settle(lambda: record.await_args.args[2]["status"] == "held_failed")
    # A refusal is final: sent once, named in the chat with its text.
    assert submit.await_count == 1
    assert record.await_args.args[1] == ("Could not send 1 of your messages to DesktopWorker (This chat is archived): "
                                         "“one”. Send them again there.")
    assert record.await_args.args[2]["messages"] == ["one"]
    assert session_moves.get("s1") is None


async def test_rule2_held_messages_are_retried_while_the_new_worker_is_unreachable():
    """Nothing was dispatched: resent until the window ends, then named in the chat."""
    from app.db import session_moves
    from app.network import control
    _chat()
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    session_moves.append_held("s1", {"id": "held-1", "source": "user", "prompt": "one"})
    submit = AsyncMock(side_effect=[control.RunnerUnreachable("no route"), {"turn_id": "t1"}])
    with patch.object(sh, "FORWARD_RETRY_S", 0), patch("app.network.control.submit_turn", submit), \
            patch.object(sh, "_record", AsyncMock()) as record:
        await sh.deliver_held("s1", "dev-gaming", "DesktopWorker")
    assert submit.await_count == 2 and not record.await_count
    assert session_moves.get("s1") is None
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    session_moves.append_held("s1", {"id": "held-2", "source": "user", "prompt": "two"})
    with patch.object(sh, "FORWARD_WINDOW_S", 0), patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.network.control.submit_turn", AsyncMock(side_effect=control.RunnerUnreachable("down"))):
        await sh.deliver_held("s1", "dev-gaming", "DesktopWorker")
    assert record.await_args.args[1] == ("Could not send 1 of your messages to DesktopWorker (it did not answer for "
                                         "1 minute): “two”. Send them again there.")


async def test_rule2_held_messages_are_sent_here_when_the_move_is_cancelled():
    from app.core import turn_manager
    _chat()
    move = AsyncMock()
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch.object(turn_manager, "_kick") as kick, \
            patch("app.api.sessions.move_session_to", move):
        await sh.queue_move("s1", "DesktopWorker")
        turn_manager._submit_blocking("s1", "user", prompt="one", profile_id="default")
        turn_manager._submit_blocking("s1", "user", prompt="two", profile_id="default")
        assert _queued_prompts() == []
        assert await sh.cancel_move("s1") == {"cancelled": True}
        assert record.await_args.args[1] == "Queued move to DesktopWorker cancelled. Your 2 messages were sent here."
        kick.assert_called_with("s1")
        # Sent here in order; new messages are no longer held.
        assert _queued_prompts() == ["one", "two"]
        assert not sh.holds("s1")
        assert not turn_manager._submit_blocking("s1", "user", prompt="three",
                                                 profile_id="default").startswith(sh.HELD_PREFIX)
    move.assert_not_awaited()


async def test_rule2_a_removed_held_message_is_never_sent():
    _chat()
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        held = sh.hold("s1", "user", {"prompt": "oops", "profile_id": "default"})
        assert sh.drop_held("s1", held)
        await sh.cancel_move("s1")
    assert _queued_prompts() == []


async def test_rule2_held_messages_are_sent_here_when_the_move_fails():
    _chat()
    busy = _Busy()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to",
                  AsyncMock(side_effect=HTTPException(409, "DesktopWorker went offline"))):
        await sh.queue_move("s1", "DesktopWorker")
        sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
        busy.idle = True
        assert await _settle(lambda: record.await_args.args[2]["status"] == "failed")
    assert record.await_args.args[1] == ("Move to DesktopWorker failed: DesktopWorker went offline "
                                         "Your message was sent here.")
    assert _queued_prompts() == ["one"]


async def test_rule4_an_open_prompt_refuses_the_move_when_queued_and_when_it_runs():
    from app.db import interactions
    _chat()
    with patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", AsyncMock()) as move:
        interactions.create_interaction("s1", "permission", {"tool_name": "Bash"}, None, interaction_id="card")
        with pytest.raises(ValueError, match="^Answer or dismiss the open prompt first.$"):
            await sh.queue_move("s1", "DesktopWorker")
        assert sh.pending_for("s1") is None
        interactions.cancel_interaction("card")
        # Queued without a card; a question opens before the turn ends.
        busy = _Busy()
        with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01):
            await sh.queue_move("s1", "DesktopWorker")
            sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
            interactions.create_interaction("s1", "question", {"q": "Which?"}, None, interaction_id="q")
            busy.idle = True
            assert await _settle(lambda: record.await_args.args[2]["status"] == "failed")
    move.assert_not_awaited()
    assert record.await_args.args[1] == ("Move to DesktopWorker cancelled. Answer or dismiss the open prompt first. "
                                         "Your message was sent here.")
    assert record.await_args.args[2]["reason"] == "open_prompt"
    assert _queued_prompts() == ["one"]


def test_rule4_an_open_private_access_card_counts_as_an_open_prompt():
    with patch("app.core.chat_access.broker.pending_count", return_value=1):
        assert sh._open_prompt("s1") is True
    with patch("app.core.chat_access.broker.pending_count", return_value=0):
        assert sh._open_prompt("s1") is False


async def test_rule5_a_queued_move_survives_a_restart_with_its_held_messages():
    from app.db import session_moves
    _chat()
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
    stored = session_moves.get("s1")
    assert stored["target_id"] == "dev-gaming" and stored["requested_by"] == "owner"
    assert [entry["prompt"] for entry in stored["held"]] == ["one"]
    # The restart: memory is gone, the row is not.
    for task_state in (sh._pending, sh._waiting, sh._moving):
        task_state.clear()
    sh._running.clear()
    busy = _Busy()
    move = AsyncMock()
    submit = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.network.control.submit_turn", submit):
        assert await sh.resume_queued() == 1
        assert sh.pending_for("s1").by == "owner" and sh.holds("s1")
        assert (await sh.owner_op("s1", "status"))["pending"]["worker"] == "DesktopWorker"
        busy.idle = True
        assert await _settle(lambda: submit.await_count == 1)
    move.assert_awaited_once()
    assert submit.await_args.kwargs["prompt"] == "one"
    record.assert_any_await("s1", "Handed off to DesktopWorker. Open this chat there to continue on the same branch.",
                            {"status": "moved", "worker": "DesktopWorker"})


async def test_rule5_a_restart_clears_moves_that_no_longer_exist():
    from app.db import session_moves
    _chat("gone-elsewhere")
    session_moves.save("deleted", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    session_moves.save("gone-elsewhere", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    session_moves.append_held("gone-elsewhere", {"id": "held-1", "source": "user", "prompt": "one"})
    submit = AsyncMock()
    with patch("app.network.control.owner_of",
               side_effect=lambda session: "dev-gaming" if session["id"] == "gone-elsewhere" else None), \
            patch("app.network.control.submit_turn", submit):
        assert await sh.resume_queued() == 0
        # The held message is sent in the background, kept until it is.
        assert await _settle(lambda: not sh._tasks)
    assert session_moves.list_all() == []
    assert sh.pending_for("deleted") is None and sh.pending_for("gone-elsewhere") is None
    # The move had finished: its held message goes to the new owner.
    assert submit.await_args.args == ("dev-gaming", "gone-elsewhere")
    assert submit.await_args.kwargs["prompt"] == "one"


async def test_rule5_the_legacy_file_note_still_ends_the_queued_state():
    import json
    sh._store_path().write_text(json.dumps({"s1": {"worker": "DesktopWorker", "by": "owner"}}))
    _chat()
    with patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.announce_dropped() == 1
    assert record.await_args.args[2]["status"] == "cancelled"
    assert not sh._store_path().exists()


async def test_rule6_a_move_waits_at_most_ten_minutes_then_runs_messages_here():
    _chat()
    assert sh.IDLE_TIMEOUT_S == 600
    state = {"running": True, "tasks": ["Build"], "queued": 0, "wake": False}
    move = AsyncMock()
    with patch.object(sh, "busy_state", return_value=state), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move):
        await sh.queue_move("s1", "DesktopWorker")
        sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
        pending = sh.pending_for("s1")
        assert pending.expires_at == pytest.approx(pending.requested_at + 600)
        assert not await _settle(lambda: record.await_args.args[2]["status"] == "timeout", rounds=5)
        pending.expires_at = time.time()  # ten minutes pass
        assert await _settle(lambda: record.await_args.args[2]["status"] == "timeout")
    move.assert_not_awaited()
    assert record.await_args.args[1] == ("Move to DesktopWorker cancelled: this chat was still busy after 10 minutes. "
                                         "Still running: the turn and Build. Your message was sent here.")
    assert record.await_args.args[2]["still"] == "the turn and Build"
    assert _queued_prompts() == ["one"]
    assert sh.pending_for("s1") is None and not sh.holds("s1")


async def test_rule6_a_restart_after_the_time_limit_cancels_and_sends_here():
    from app.db import session_moves
    _chat()
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time() - 700, expires_at=time.time() - 100)
    session_moves.append_held("s1", {"id": "held-1", "source": "user", "prompt": "one", "profile_id": "default"})
    with patch.object(sh, "busy_state", return_value={"running": False, "tasks": [], "queued": 0, "wake": False}), \
            patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.resume_queued() == 0
    assert record.await_args.args[2]["status"] == "timeout"
    assert _queued_prompts() == ["one"]
    assert session_moves.get("s1") is None


# ---------------------------------------------------------------------------
# Review of #1954: failure paths of the move and of the held messages.
# ---------------------------------------------------------------------------

GAMING_NEW = {"device_id": "dev-gaming", "device_name": "DesktopWorker", "capabilities": {"move_wakes": 1}}
GAMING_OLD = {"device_id": "dev-gaming", "device_name": "DesktopWorker", "capabilities": {"git_resume": 1}}


def _held_row(*prompts):
    from app.db import session_moves
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    for number, prompt in enumerate(prompts, 1):
        session_moves.append_held("s1", {"id": f"held-{number}", "source": "user", "prompt": prompt,
                                         "profile_id": "default"})


def _new_worker(lost_replies):
    """``control.call`` as the new worker answers it. This test's database plays
    that worker: the real ``control._turns`` takes each send, and the first
    ``lost_replies`` replies are lost after it did."""
    from app.network import control
    sends = []

    async def call(device, action, body, **kwargs):
        if action != "turns":
            raise control.ControlRefused("Unknown move operation")
        sends.append(body)
        reply = await control._turns(body, "dev-unraid")
        if len(sends) <= lost_replies:
            raise control.MaybeApplied("DesktopWorker did not answer in time. It may still have been applied.")
        return reply
    return call, sends


async def test_a_held_message_whose_reply_was_lost_runs_exactly_once():
    """F1: the new worker took the message but its reply timed out."""
    from app.db import session_moves
    _chat()
    _held_row("one", "two")
    call, sends = _new_worker(lost_replies=1)
    with patch("app.network.control.call", call), patch("app.network.runners.find", return_value=GAMING_NEW), \
            patch.object(sh, "FORWARD_RETRY_S", 0), patch.object(sh, "_record", AsyncMock()) as record:
        await sh.deliver_held("s1", "dev-gaming", "DesktopWorker")
    # "one" was sent twice (its first reply was lost) but is queued once.
    assert [body["prompt"] for body in sends] == ["one", "one", "two"]
    assert sends[0]["command_id"] == sends[1]["command_id"] == "held-1"
    assert _queued_prompts() == ["one", "two"]
    assert session_moves.get("s1") is None
    record.assert_not_awaited()


async def test_an_older_worker_gets_a_held_message_once_and_the_chat_says_it_was_not_confirmed():
    """F1: a worker that cannot drop a repeat is never sent the message twice."""
    _chat()
    _held_row("one", "two")
    call, sends = _new_worker(lost_replies=1)
    with patch("app.network.control.call", call), patch("app.network.runners.find", return_value=GAMING_OLD), \
            patch.object(sh, "FORWARD_RETRY_S", 0), patch.object(sh, "_record", AsyncMock()) as record:
        await sh.deliver_held("s1", "dev-gaming", "DesktopWorker")
    assert [body["prompt"] for body in sends] == ["one", "two"]
    assert _queued_prompts() == ["one", "two"]
    assert record.await_args.args[1] == ("DesktopWorker did not confirm 1 of your messages: “one”. "
                                         "Check there before sending them again.")
    assert record.await_args.args[2]["unconfirmed"] == 1


async def test_a_crash_between_the_move_and_the_held_messages_loses_nothing():
    """F2: the worker dies while it sends the held messages; the next boot sends them."""
    import asyncio
    from app.db import session_moves
    _chat()
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    sh.request("s1", "DesktopWorker")
    sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
    sh.hold("s1", "user", {"prompt": "two", "profile_id": "default"})
    with patch.object(sh, "_busy", return_value=False), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", AsyncMock()), \
            patch("app.network.control.submit_turn", hang):
        task = asyncio.get_running_loop().create_task(sh.run_pending("s1"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()  # the process dies here
        with pytest.raises(asyncio.CancelledError):
            await task
    sh._delivering.clear()
    assert [entry["prompt"] for entry in session_moves.get("s1")["held"]] == ["one", "two"]
    submit = AsyncMock()
    with patch("app.network.control.owner_of", return_value="dev-gaming"), \
            patch("app.network.control.submit_turn", submit), patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.resume_queued() == 0
        assert await _settle(lambda: not sh._tasks)
    assert [call.kwargs["prompt"] for call in submit.await_args_list] == ["one", "two"]
    assert session_moves.get("s1") is None
    record.assert_not_awaited()


async def test_held_messages_the_new_worker_never_takes_after_a_restart_are_named():
    """F2: a boot-time delivery that fails says so in the chat, never silently."""
    from app.network import control
    _chat()
    _held_row("one")
    with patch("app.network.control.owner_of", return_value="dev-gaming"), \
            patch("app.network.control.submit_turn", AsyncMock(side_effect=control.ControlRefused("Unknown session"))), \
            patch.object(sh, "_record", AsyncMock()) as record:
        await sh.resume_queued()
        assert await _settle(lambda: not sh._tasks)
    assert record.await_args.args[2]["status"] == "held_failed"
    assert "“one”" in record.await_args.args[1]


async def test_a_cancel_racing_the_start_of_the_move_is_refused():
    """F6: from the moment the wait ends, a cancel or a second move is refused,
    and new messages are still held."""
    import threading
    _chat()
    gate, entered = threading.Event(), threading.Event()
    checks = []

    def open_prompt(session_id):
        checks.append(session_id)
        if len(checks) > 1:  # the check as the move starts (the first is queue_move's)
            entered.set()
            gate.wait(5)
        return False
    move = AsyncMock()
    with patch.object(sh, "_busy", return_value=False), patch.object(sh, "_open_prompt", open_prompt), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.network.control.submit_turn", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(entered.is_set)
        try:
            with pytest.raises(ValueError, match="has already started"):
                await sh.cancel_move("s1")
            with pytest.raises(ValueError, match="already queued"):
                await sh.queue_move("s1", "DesktopWorker")
            assert sh.holds("s1")
        finally:
            gate.set()
        assert await _settle(lambda: move.await_count == 1 and "s1" not in sh._running)
    statuses = [call.args[2]["status"] for call in record.await_args_list]
    assert "cancelled" not in statuses and "moved" in statuses


async def test_rule2_after_an_ambiguous_move_failure_held_messages_follow_the_new_owner():
    """F3: the move raised, but the new worker had committed it."""
    _chat()
    committed = []

    async def move(*args):
        committed.append(True)
        raise HTTPException(504, "DesktopWorker did not answer in time")
    submit = AsyncMock()
    sh.request("s1", "DesktopWorker")
    sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
    with patch.object(sh, "_busy", return_value=False), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.network.control.owner_of", side_effect=lambda session: "dev-gaming" if committed else None), \
            patch("app.network.control.submit_turn", submit):
        outcome = await sh.run_pending("s1")
    assert outcome.startswith("Handed off to DesktopWorker")
    assert submit.await_args.args == ("dev-gaming", "s1") and submit.await_args.kwargs["prompt"] == "one"
    assert _queued_prompts() == []
    assert [call.args[2]["status"] for call in record.await_args_list] == ["started", "moved"]


async def test_the_outcome_is_recorded_on_the_worker_that_now_runs_the_chat():
    """F7: the old worker's rows do not reach the chat on the new one."""
    call = AsyncMock(return_value={"recorded": True})
    sh.request("s1", "DesktopWorker")
    with patch.object(sh, "_busy", return_value=False), patch.object(sh, "_open_prompt", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", AsyncMock()), \
            patch("app.db.database.get_session", return_value={"id": "s1"}), \
            patch("app.network.control.call", call):
        await sh.run_pending("s1")
    # DesktopWorker has not reported its capabilities: no resume, and the row says so (#2012).
    call.assert_awaited_once_with("dev-gaming", "session-move", {
        "session_id": "s1", "op": "note",
        "content": ("Handed off to DesktopWorker. Open this chat there to continue on the same branch. DesktopWorker has "
                    "not reported that it can resume it by itself: send a message there."),
        "data": {"status": "moved", "worker": "DesktopWorker", "resume": "unknown"}})
    assert [c.args[2]["status"] for c in record.await_args_list] == ["started"]


async def test_the_note_operation_records_the_old_owners_row_here():
    from types import SimpleNamespace
    from app.network import control, engine
    context = SimpleNamespace(member=lambda device: SimpleNamespace(kind="worker", status="active"))
    with patch.object(engine, "require_context", return_value=context), \
            patch.object(control, "_require_owned", return_value={"id": "s1"}), \
            patch.object(sh, "_record", AsyncMock()) as record:
        await sh.handle_control({"session_id": "s1", "op": "note", "content": "Handed off to DesktopWorker.",
                                 "data": {"status": "moved", "worker": "DesktopWorker", "other": "x"}}, "dev-unraid")
        record.assert_awaited_once_with("s1", "Handed off to DesktopWorker.", {"status": "moved", "worker": "DesktopWorker"})
        # #2012: the resume plan and a refused resume reach the new owner's chat.
        await sh.handle_control({"session_id": "s1", "op": "note", "content": "Could not continue.",
                                 "data": {"status": "resume_failed", "worker": "DesktopWorker", "reason": "x",
                                          "resume": "send"}}, "dev-unraid")
        assert record.await_args.args[2] == {"status": "resume_failed", "worker": "DesktopWorker", "reason": "x",
                                             "resume": "send"}
        with pytest.raises(ValueError, match="Unknown hand-off note"):
            await sh.handle_control({"session_id": "s1", "op": "note", "content": "x",
                                     "data": {"status": "queued"}}, "dev-unraid")


def test_rule3_a_held_message_cancels_the_pending_wakes():
    """F8: the user's held message supersedes the agent's wakes at once."""
    from app.core import turn_manager
    from app.db import wakeups
    _chat()
    wakeups.create_wakeup(id="w-soon", session_id="s1", prompt="check CI", fire_at=time.time() + 30)
    sh.request("s1", "DesktopWorker")
    held = turn_manager._submit_blocking("s1", "user", prompt="new plan", profile_id="default")
    assert held.startswith(sh.HELD_PREFIX)
    assert wakeups.get_session_wakeups("s1") == []
    sh.cancel("s1")


class _SendsOnExit:
    """``lock``, but the first time it is left ``send`` runs (another thread's send)."""

    def __init__(self, lock, send):
        self.lock, self.send, self.sent = lock, send, False

    def __enter__(self):
        return self.lock.__enter__()

    def __exit__(self, *exc):
        result = self.lock.__exit__(*exc)
        if not self.sent:
            self.sent = True
            self.send()
        return result


@pytest.mark.parametrize("moment", ["after the cancel", "during the release"])
def test_a_message_sent_while_a_cancel_releases_the_held_ones_runs_after_them(moment):
    """N2: between the cancel and the release a newer message must not be
    queued ahead of the older held ones."""
    import threading
    from app.core import turn_manager
    from app.db import session_moves
    _chat()
    sh.request("s1", "DesktopWorker")
    assert turn_manager._submit_blocking("s1", "user", prompt="older", profile_id="default").startswith(
        sh.HELD_PREFIX)
    sender = []

    def send():
        # The person sends again; it waits at most 0.5 s for the cancel.
        thread = threading.Thread(target=turn_manager._submit_blocking, args=("s1", "user"),
                                  kwargs={"prompt": "newer", "profile_id": "default"})
        sender.append(thread)
        thread.start()
        thread.join(0.5)
    if moment == "after the cancel":
        patcher = patch.object(sh, "_state_lock", _SendsOnExit(sh._state_lock, send))
    else:
        original_take = session_moves.take

        def take(session_id):
            send()
            return original_take(session_id)
        patcher = patch.object(session_moves, "take", take)
    with patcher:
        assert sh.cancel("s1")
    sender[0].join(5)
    assert _queued_prompts() == ["older", "newer"]


async def test_a_cancel_landing_as_the_wait_ends_stops_the_move():
    """N7: the cancel comes after the last busy check but before the move starts."""
    _chat()
    sh.request("s1", "DesktopWorker")

    def busy(session_id):
        sh.cancel(session_id)
        return False
    move = AsyncMock()
    with patch.object(sh, "_busy", busy), patch("app.api.sessions.move_session_to", move), \
            patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.run_pending("s1") is None
    move.assert_not_awaited()
    record.assert_not_awaited()
    assert sh.pending_for("s1") is None


async def test_a_peers_note_is_kept_to_a_bounded_size():
    """N6: another worker's note cannot write an unbounded row here."""
    from types import SimpleNamespace
    from app.network import control, engine
    context = SimpleNamespace(member=lambda device: SimpleNamespace(kind="worker", status="active"))
    with patch.object(engine, "require_context", return_value=context), \
            patch.object(control, "_require_owned", return_value={"id": "s1"}), \
            patch.object(sh, "_record", AsyncMock()) as record:
        await sh.handle_control({"session_id": "s1", "op": "note", "content": "Could not send.",
                                 "data": {"status": "held_failed", "messages": ["x" * 100_000] * 1000}},
                                "dev-unraid")
        kept = record.await_args.args[2]["messages"]
        assert len(kept) == sh.NOTE_MESSAGES and {len(text) for text in kept} == {sh.NOTE_MESSAGE_CHARS}
        with pytest.raises(ValueError, match="too large"):
            await sh.handle_control({"session_id": "s1", "op": "note", "content": "x",
                                     "data": {"status": "moved", "worker": "w" * 200_000}}, "dev-unraid")


async def test_large_non_ascii_undelivered_note_is_recorded_on_the_new_worker():
    """#1964: drive failed delivery, its peer note, and the receiving recorder."""
    from types import SimpleNamespace
    from app.network import control, engine
    _chat()
    prompts = ["漢字" * 1000] * 50
    _held_row(*prompts)
    context = SimpleNamespace(member=lambda device: SimpleNamespace(kind="worker", status="active"))
    notes = []

    async def receive_note(device, action, body, **kwargs):
        assert device == "dev-gaming" and action == "session-move"
        notes.append(body)
        return await sh.handle_control(body, "dev-unraid")

    with patch.object(control, "submit_turn", AsyncMock(side_effect=control.ControlRefused("refused"))), \
            patch.object(control, "call", receive_note), \
            patch.object(control, "_require_owned", return_value={"id": "s1"}), \
            patch.object(engine, "require_context", return_value=context), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch.object(sh, "_notify", AsyncMock()) as notify:
        await sh.deliver_held("s1", "dev-gaming", "DesktopWorker")
    assert len(notes) == 1 and len(notes[0]["content"]) > 8000
    record.assert_awaited_once()
    sid, content, data = record.await_args.args
    assert sid == "s1" and len(content) == 8000 and content.endswith("…")
    assert data["count"] == 50 and data["messages"] == prompts
    notify.assert_not_awaited()


async def test_held_messages_of_a_chat_archived_meanwhile_are_named_not_dropped():
    """F10: the chat was archived while the move waited."""
    from app.db.base import get_db
    _chat()
    sh.request("s1", "DesktopWorker")
    sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET status='archived' WHERE id=%s", ("s1",))
    with patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.services.notification_service.emit_notification", AsyncMock()) as notify:
        assert await sh.cancel_move("s1") == {"cancelled": True}
    assert record.await_args.args[1] == ("Could not send 1 of your messages here because this chat is archived: "
                                         "“one”. Send them again.")
    assert record.await_args.args[2]["reason"] == "archived"
    assert notify.await_args.kwargs["message"] == record.await_args.args[1]
    assert _queued_prompts() == []


def test_the_agent_cannot_replace_a_queued_move():
    """F10: request() refuses a move to another worker while one is queued."""
    directory = {"runners": DIRECTORY["runners"] + [
        {"device_id": "dev-mac", "device_name": "Mac", "online": True, "is_self": False}]}
    with patch.object(sh, "_workers", return_value=directory):
        sh.request("s1", "DesktopWorker", "first")
        with pytest.raises(ValueError, match="A move to DesktopWorker is already queued"):
            sh.request("s1", "Mac")
        again = sh.request("s1", "DesktopWorker", "second")
    assert again["queued"] and "already queued" in again["message"]
    assert sh.pending_for("s1").note == "first"


async def test_rule6_a_restart_keeps_the_original_ten_minute_limit():
    """The limit runs from when the move was queued, not from the restart."""
    from app.db import session_moves
    _chat()
    now = time.time()
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="", requested_by="owner",
                       requested_at=now - 599.5, expires_at=now + 0.5)
    session_moves.append_held("s1", {"id": "held-1", "source": "user", "prompt": "one", "profile_id": "default"})
    state = {"running": True, "tasks": [], "queued": 0, "wake": False}
    with patch.object(sh, "busy_state", return_value=state), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.resume_queued() == 1
        assert sh.pending_for("s1").expires_at == pytest.approx(now + 0.5)
        assert await _settle(lambda: record.await_count and record.await_args.args[2]["status"] == "timeout",
                             rounds=300)
    assert _queued_prompts() == ["one"]


# ---------------------------------------------------------------------------
# #2012: an agent's hand-off resumes the chat on the new worker.
# ---------------------------------------------------------------------------

GAMING_RESUME = {"device_id": "dev-gaming", "device_name": "DesktopWorker", "capabilities": {"move_wakes": 2}}
UNRAID_ROW = {"device_id": "dev-unraid", "device_name": "Unraid", "capabilities": {"move_wakes": 2}}


def _runners(gaming):
    return lambda device: {"dev-gaming": gaming, "dev-unraid": UNRAID_ROW}.get(device)


def _gaming():
    """``control.call`` as DesktopWorker answers it; this test's database plays it."""
    from app.network import control
    turns_sent, notes = [], []

    async def call(device, action, body, **kwargs):
        if action == "session-move" and body.get("op") == "note":
            notes.append(body)
            return {"recorded": True}
        if action == "turns":
            turns_sent.append(body)
            return await control._turns(body, "dev-unraid")
        raise control.ControlRefused("Unknown move operation")
    return call, turns_sent, notes


def _queued(session_id="s1"):
    from app.db import turns
    return [(turn["source"], turn["prompt"]) for turn in turns.get_queued_turns(session_id)]


def _moving(gaming, call, move=None):
    """Patches for a move to DesktopWorker that lands, seen from Unraid."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    stack = ExitStack()
    stack.enter_context(patch.object(sh, "_busy", return_value=False))
    stack.enter_context(patch.object(sh, "_open_prompt", return_value=False))
    stack.enter_context(patch.object(sh, "FORWARD_RETRY_S", 0))
    stack.enter_context(patch("app.api.sessions.move_session_to", move or AsyncMock()))
    stack.enter_context(patch("app.network.runners.find", side_effect=_runners(gaming)))
    context = SimpleNamespace(device_id="dev-unraid", member=lambda device: SimpleNamespace(kind="worker", status="active"))
    stack.enter_context(patch("app.network.engine.context", return_value=context))
    stack.enter_context(patch("app.network.control.call", call))
    return stack


RESUME_PROMPT = ("[hand-off] Moved from Unraid to DesktopWorker at the owner's request. Your hand-off note: "
                 "tests pass; next: wire the UI Continue the work from where you left off. If it was already "
                 "complete, stop after a brief acknowledgement; do not invent new work.")


async def test_2012_an_agent_hand_off_resumes_the_chat_there_with_its_note():
    """(a) No held messages: exactly one resume turn, carrying the note."""
    from app.db import session_moves
    _chat()
    call, turns_sent, notes = _gaming()
    sh.request("s1", "DesktopWorker", "tests pass; next: wire the UI")
    with _moving(GAMING_RESUME, call), patch.object(sh, "_record", AsyncMock()) as record:
        outcome = await sh.run_pending("s1")
    assert outcome == "Handed off to DesktopWorker. This chat continues there on the same branch."
    assert [body["data"] for body in notes] == [{"status": "moved", "worker": "DesktopWorker", "resume": "send"}]
    assert [c.args[2]["status"] for c in record.await_args_list] == ["started"]
    assert len(turns_sent) == 1
    body = turns_sent[0]
    assert body["source"] == "wakeup" and body["prompt"] == RESUME_PROMPT
    assert body["profile_id"] == "default" and "owner_send" not in body
    assert body["command_id"].startswith(sh.RESUME_COMMAND_PREFIX)
    assert body["payload"] == {sh.RESUME_PAYLOAD: {"from": "Unraid", "note": "tests pass; next: wire the UI"}}
    assert _queued() == [("wakeup", RESUME_PROMPT)]
    assert session_moves.get("s1") is None


async def test_2012_held_messages_go_first_then_the_resume():
    """(b) The owner's held messages run before the resume, in order."""
    _chat()
    call, turns_sent, _notes = _gaming()
    sh.request("s1", "DesktopWorker", "tests pass; next: wire the UI")
    sh.hold("s1", "user", {"prompt": "one", "profile_id": "default"})
    sh.hold("s1", "queue", {"prompt": "two", "profile_id": "default"})
    with _moving(GAMING_RESUME, call), patch.object(sh, "_record", AsyncMock()):
        await sh.run_pending("s1")
    assert [body["prompt"] for body in turns_sent] == ["one", "two", RESUME_PROMPT]
    assert _queued() == [("user", "one"), ("queue", "two"), ("wakeup", RESUME_PROMPT)]


async def test_2012_the_owners_own_move_of_an_idle_chat_gets_no_resume():
    """(c) A move from the worker selector stays as it was: no turn there."""
    _chat()
    call, turns_sent, notes = _gaming()
    move = AsyncMock()
    idle = _Busy()
    idle.idle = True
    with _moving(GAMING_RESUME, call, move), patch.object(sh, "busy_state", idle), \
            patch.object(sh, "_record", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(lambda: move.await_count and notes)
        assert await _settle(lambda: "s1" not in sh._running)
    assert turns_sent == [] and _queued() == []
    assert notes[0]["content"] == "Handed off to DesktopWorker. Open this chat there to continue on the same branch."
    assert notes[0]["data"] == {"status": "moved", "worker": "DesktopWorker"}


async def test_2012_a_cancelled_or_failed_hand_off_gets_no_resume():
    """(d) Neither a cancelled nor a failed hand-off resumes anything."""
    _chat()
    call, turns_sent, _notes = _gaming()
    submit = AsyncMock()
    sh.request("s1", "DesktopWorker", "halfway")
    assert sh.cancel("s1")
    with _moving(GAMING_RESUME, call), patch("app.network.control.submit_turn", submit), \
            patch.object(sh, "_record", AsyncMock()):
        assert await sh.run_pending("s1") is None
    sh.request("s1", "DesktopWorker", "halfway")
    with _moving(GAMING_RESUME, call, AsyncMock(side_effect=HTTPException(409, "DesktopWorker went offline"))), \
            patch("app.network.control.submit_turn", submit), patch.object(sh, "_record", AsyncMock()) as record:
        outcome = await sh.run_pending("s1")
    assert outcome == "Hand-off to DesktopWorker failed: DesktopWorker went offline"
    assert record.await_args.args[2] == {"status": "failed", "worker": "DesktopWorker"}
    submit.assert_not_awaited()
    assert turns_sent == [] and _queued() == []
    # A pending hand-off cancelled after it was read never resumes either.
    pending = sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time(), by="agent")
    pending.cancelled = True
    with patch("app.network.runners.find", side_effect=_runners(GAMING_RESUME)):
        assert sh.resume_plan("s1", pending, "dev-gaming") is None


async def test_2012_a_chat_the_owner_stopped_is_not_resumed():
    """A Stop here before the move ends, or there before the resume lands, wins."""
    from app.core.task_registry import task_registry
    _chat()
    call, turns_sent, notes = _gaming()
    sh.request("s1", "DesktopWorker", "halfway")
    task_registry.mark_user_stopped("s1")
    try:
        with _moving(GAMING_RESUME, call), patch.object(sh, "_record", AsyncMock()):
            await sh.run_pending("s1")
        assert turns_sent == []
        assert notes[0]["data"] == {"status": "moved", "worker": "DesktopWorker"}
        # The new worker drops a resume that reaches a chat stopped there.
        from app.network import control
        resume = {"session_id": "s1", "source": "wakeup", "prompt": "resume", "profile_id": "default",
                  "payload": {sh.RESUME_PAYLOAD: {"from": "Unraid", "note": ""}}, "command_id": "handoff-resume-1"}
        reply = await control._turns(resume, "dev-unraid")
        assert reply["dropped"] == "stopped" and reply["turn_id"] is None
        assert _queued() == []
        # Any other forwarded turn is unaffected by this rule.
        assert not sh.drop_resume("s1", {"forwarded_by_device": "dev-unraid"})
    finally:
        task_registry.clear_user_stop("s1")


async def test_2012_an_older_or_silent_new_worker_degrades_and_the_outcome_says_so():
    """(e) No resume capability there: the move still lands, as before, with a note why."""
    expected = {
        "unsupported": (GAMING_NEW, ("Handed off to DesktopWorker. Open this chat there to continue on the same "
                                     "branch. DesktopWorker runs an older version that cannot resume it by itself: "
                                     "send a message there.")),
        "unknown": ({"device_id": "dev-gaming", "device_name": "DesktopWorker"},
                    ("Handed off to DesktopWorker. Open this chat there to continue on the same branch. DesktopWorker has "
                     "not reported that it can resume it by itself: send a message there.")),
    }
    for plan, (gaming, text) in expected.items():
        sh._pending.clear()
        _chat(f"s-{plan}")
        call, turns_sent, notes = _gaming()
        sh.request(f"s-{plan}", "DesktopWorker", "halfway")
        with _moving(gaming, call), patch.object(sh, "_record", AsyncMock()):
            outcome = await sh.run_pending(f"s-{plan}")
        assert outcome == text
        assert notes[0]["data"] == {"status": "moved", "worker": "DesktopWorker", "resume": plan}
        assert turns_sent == [] and _queued(f"s-{plan}") == []


async def test_2012_a_resume_whose_reply_was_lost_or_resent_after_a_restart_runs_once():
    """The resume reuses one command_id, so the new worker drops every repeat."""
    from app.db import session_moves
    from app.network import control
    _chat()
    sends = []

    async def call(device, action, body, **kwargs):
        if action == "session-move":
            return {"recorded": True}
        sends.append(body)
        with patch("app.network.control.owner_of", return_value=None):  # DesktopWorker owns it there
            reply = await control._turns(body, "dev-unraid")
        if len(sends) == 1:
            raise control.MaybeApplied("DesktopWorker did not answer in time. It may still have been applied.")
        return reply
    sh.request("s1", "DesktopWorker", "halfway")
    with _moving(GAMING_RESUME, call), patch.object(sh, "_record", AsyncMock()):
        await sh.run_pending("s1")
    assert len(sends) == 2 and sends[0]["command_id"] == sends[1]["command_id"]
    assert [source for source, _prompt in _queued()] == ["wakeup"]
    # The process died after the resume landed but before the row was cleared:
    # the next boot resends it and the new worker drops the repeat.
    session_moves.save("s1", target_id="dev-gaming", label="DesktopWorker", note="halfway", requested_by="agent",
                       requested_at=float(sends[0]["command_id"][len(sh.RESUME_COMMAND_PREFIX):]),
                       expires_at=time.time() + 600)
    with _moving(GAMING_RESUME, call), patch("app.network.control.owner_of", return_value="dev-gaming"), \
            patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.resume_queued() == 0
        assert await _settle(lambda: not sh._tasks)
    assert len(sends) == 3 and sends[2]["command_id"] == sends[0]["command_id"]
    assert [source for source, _prompt in _queued()] == ["wakeup"]
    assert session_moves.get("s1") is None
    record.assert_not_awaited()


async def test_2012_a_resume_the_new_worker_refuses_is_reported_there():
    from app.network import control
    _chat()
    notes = []

    async def call(device, action, body, **kwargs):
        if action == "session-move":
            notes.append(body)
            return {"recorded": True}
        raise control.ControlRefused("This chat is archived")
    sh.request("s1", "DesktopWorker", "halfway")
    with _moving(GAMING_RESUME, call), patch.object(sh, "_record", AsyncMock()):
        await sh.run_pending("s1")
    assert notes[-1]["content"] == ("Could not continue this chat on DesktopWorker (This chat is archived). "
                                    "Send a message there to continue.")
    assert notes[-1]["data"] == {"status": "resume_failed", "worker": "DesktopWorker", "reason": "This chat is archived"}
