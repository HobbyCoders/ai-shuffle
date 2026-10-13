"""SessionRuntime: the park -> unpark hand-off, the run-lease heartbeat, and the
WS tool-input cap the chat-socket docs promise ("replaced by a truncation
marker when over 4 KB").

The turns/runs tables are real (pg_shim SQLite); the pump, recorder, idle
reader and task registry are the same mocks ``tests/core/test_session_runtime.py``
uses (``_runtime_env``), so these read as a continuation of that suite.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest

from app.core import session_runtime
from app.db import runs as runs_db
from app.db import turns as turns_db
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import (  # noqa: F401 — autouse fixture
    _clean_module_state,
    _runtime_env,
)

DONE = [{"type": "done", "metadata": {}}]


# ---------------------------------------------------------------------------
# park -> unpark
# ---------------------------------------------------------------------------


async def test_park_then_unpark_settles_the_parked_row_and_logs_it(mock_db, caplog):
    """The ordinary unpark resumes and completes the original parked turn,
    rather than completing an old row and starting a separate logical turn."""
    sid = "park-unpark"
    first = turns_db.enqueue_turn(sid, "user", prompt="launch a worker")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "worker-1"}]) as env:
        assert await session_runtime.SessionRuntime(first).execute() == "parked"
    parked = turns_db.get_turn(first["id"])
    assert parked["state"] == "parked"
    assert parked["payload"]["task_ids"] == ["worker-1"]
    env["scheduler"].assert_called_once_with(sid, first["id"], "parked")
    env["record_parked"].assert_awaited_once_with(sid, ["worker-1"], turn_id=first["id"], usage={})
    # The lease was released even though the turn parked.
    assert runs_db.get_active_run(sid) is None

    wake = turns_db.enqueue_turn(
        sid, "unpark", prompt="background task completed",
        payload={"task_ids": ["worker-1"]},
    )
    caplog.set_level(logging.INFO, logger="app.core.session_runtime")
    with _runtime_env(pump=make_pump(DONE)) as env:
        assert await session_runtime.SessionRuntime(wake).execute() == "completed"
    env["scheduler"].assert_called_once_with(sid, first["id"], "completed")

    settled = turns_db.get_turn(first["id"])
    assert settled["state"] == "completed"
    assert settled["stop_reason"] is None
    assert turns_db.get_turn(wake["id"])["stop_reason"] == "steered"
    assert "resolved parked turn(s)" not in caplog.text


async def test_unpark_while_other_work_still_runs_leaves_the_park(mock_db, caplog):
    """A wake for ONE finished task resumes the same turn, then parks it
    again on the remaining work instead of creating a second parked row."""
    sid = "park-partial"
    first = turns_db.enqueue_turn(sid, "user", prompt="launch two")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "a"}, {"id": "b"}]):
        assert await session_runtime.SessionRuntime(first).execute() == "parked"

    wake = turns_db.enqueue_turn(sid, "unpark", prompt="a finished")
    caplog.set_level(logging.INFO, logger="app.core.session_runtime")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "b"}]):
        assert await session_runtime.SessionRuntime(wake).execute() == "parked"

    assert turns_db.get_turn(first["id"])["state"] == "parked"
    assert turns_db.get_turn(first["id"])["payload"]["task_ids"] == ["b"]
    assert turns_db.get_turn(wake["id"])["stop_reason"] == "steered"
    assert "resolved parked turn(s)" not in caplog.text


async def test_a_failing_parked_settle_never_blocks_the_next_turn(mock_db, caplog):
    sid = "park-settle-fails"
    stale = turns_db.enqueue_turn(sid, "user", prompt="old")
    turns_db.start_turn(stale["id"], run_id="run-old")
    turns_db.park_turn(stale["id"], ["gone"])
    # An incompatible send still exercises the separate-turn cleanup path.
    fresh = turns_db.enqueue_turn(sid, "user", prompt="new", profile_id="different")
    with _runtime_env(pump=make_pump(DONE)), patch.object(
        turns_db, "settle_parked_turns", side_effect=RuntimeError("db blip"),
    ):
        assert await session_runtime.SessionRuntime(fresh).execute() == "completed"
    assert turns_db.get_turn(stale["id"])["state"] == "parked"
    assert f"SessionRuntime: settle_parked_turns failed for {sid}" in caplog.text


async def test_a_failed_park_write_does_not_fire_the_scheduler(mock_db, caplog):
    """``settled_here`` is True only when THIS finalize performed the DB
    transition. A park that did not land reports ``parked`` but leaves the
    scheduler seam alone (the reap sweep owns the row now)."""
    turn = turns_db.enqueue_turn("park-write-fails", "user", prompt="hi")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "t"}]) as env, patch.object(
        turns_db, "park_turn", side_effect=RuntimeError("db blip"),
    ):
        outcome = await session_runtime.SessionRuntime(turn).execute()
    assert outcome == "parked"
    env["scheduler"].assert_not_called()
    env["record_parked"].assert_not_awaited()
    assert f"SessionRuntime: park_turn failed for {turn['id']}" in caplog.text
    assert runs_db.get_active_run("park-write-fails") is None


async def test_a_failing_parked_event_still_parks(mock_db):
    turn = turns_db.enqueue_turn("park-event-fails", "user", prompt="hi")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "t"}]) as env:
        env["record_parked"].side_effect = RuntimeError("event log down")
        outcome = await session_runtime.SessionRuntime(turn).execute()
    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["state"] == "parked"
    env["scheduler"].assert_called_once_with("park-event-fails", turn["id"], "parked")


async def test_a_user_stopped_turn_never_parks(mock_db):
    """Stop kills the session's work; a turn aborted by it settles instead of
    waiting on background work that is being torn down."""
    turn = turns_db.enqueue_turn("park-stopped", "user", prompt="hi")
    with _runtime_env(
        pump=make_pump([{"type": "interrupted"}]), bg_work=[{"id": "t"}],
    ) as env:
        rt = session_runtime.SessionRuntime(turn)
        rt._abort_reason = "user_stopped"
        outcome = await rt.execute()
    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"
    env["record_parked"].assert_not_awaited()


# ---------------------------------------------------------------------------
# run-lease heartbeat
# ---------------------------------------------------------------------------


def _lease(run_id):
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT state, heartbeat_at, lease_expires_at FROM runs WHERE id=%s", (run_id,)
        )
        return dict(cur.fetchone())


async def test_the_heartbeat_renews_the_lease_while_the_turn_runs(mock_db, monkeypatch):
    sid = "lease-renew"
    turn = turns_db.enqueue_turn(sid, "user", prompt="long turn")
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.02)
    renewals = []
    real_renew = runs_db.renew_run
    enough = asyncio.Event()
    seen = {}

    def _renew(*args, **kwargs):
        ok = real_renew(*args, **kwargs)
        renewals.append(ok)
        if len(renewals) >= 3:
            enough.set()
        return ok

    async def pump(**kwargs):
        run_id = turns_db.get_turn(turn["id"])["run_id"]
        seen["before"] = _lease(run_id)
        await asyncio.wait_for(enough.wait(), 3.0)
        seen["after"] = _lease(run_id)
        seen["run_id"] = run_id
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump), patch.object(runs_db, "renew_run", side_effect=_renew), \
            patch.object(session_runtime.SessionRuntime, "_on_lease_lost") as lost:
        outcome = await session_runtime.execute(turn)

    assert outcome == "completed"
    assert renewals[:3] == [True, True, True]
    lost.assert_not_called()
    # Each True above is the guarded UPDATE matching this runner's active,
    # unexpired run; the row's clocks only move forward (second resolution).
    assert str(seen["after"]["heartbeat_at"]) >= str(seen["before"]["heartbeat_at"])
    assert str(seen["after"]["lease_expires_at"]) >= str(seen["before"]["lease_expires_at"])
    # Released at the end, and the heartbeat stopped with the turn.
    assert _lease(seen["run_id"])["state"] == "released"
    count = len(renewals)
    await asyncio.sleep(0.08)
    assert len(renewals) == count


async def test_a_renewal_error_inside_the_ttl_is_retried_not_fatal(mock_db, monkeypatch, caplog):
    sid = "lease-blip"
    turn = turns_db.enqueue_turn(sid, "user", prompt="long turn")
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.02)
    calls = []
    real_renew = runs_db.renew_run
    recovered = asyncio.Event()

    def _renew(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        ok = real_renew(*args, **kwargs)
        if ok:
            recovered.set()
        return ok

    async def pump(**kwargs):
        await asyncio.wait_for(recovered.wait(), 3.0)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump), patch.object(runs_db, "renew_run", side_effect=_renew), \
            patch.object(session_runtime.SessionRuntime, "_on_lease_lost") as lost:
        outcome = await session_runtime.execute(turn)

    assert outcome == "completed"
    lost.assert_not_called()
    assert "SessionRuntime: renew_run raised for run" in caplog.text
    assert turns_db.get_turn(turn["id"])["stop_reason"] != "lease_lost"


# ---------------------------------------------------------------------------
# WS tool-input cap (docs: "replaced by a truncation marker when over 4 KB")
# ---------------------------------------------------------------------------


def _input_of_encoded_length(n):
    """A tool input whose ``json.dumps`` is exactly ``n`` characters."""
    overhead = len('{"content": ""}')
    return {"content": "x" * (n - overhead)}


def test_the_ws_tool_input_cap_is_4_kb():
    assert session_runtime._WS_MAX_TOOL_INPUT_CHARS == 4096


@pytest.mark.parametrize("frame_type", ["tool_use", "subagent_tool_use"])
def test_tool_input_at_the_cap_is_sent_whole(frame_type):
    event = {"type": frame_type, "id": "t1", "input": _input_of_encoded_length(4096)}
    assert session_runtime._cap_ws_tool_input(event) is event


@pytest.mark.parametrize("frame_type", ["tool_use", "subagent_tool_use"])
def test_tool_input_over_the_cap_becomes_the_truncation_marker(frame_type):
    import json

    tool_input = _input_of_encoded_length(4097)
    event = {"type": frame_type, "id": "t1", "name": "Write", "input": tool_input}
    capped = session_runtime._cap_ws_tool_input(event)
    encoded = json.dumps(tool_input)
    assert capped == {
        "type": frame_type,
        "id": "t1",
        "name": "Write",
        "input": {
            "_ws_truncated": True,
            "_original_chars": 4097,
            "_preview": encoded[:4096],
        },
    }
    # The recorded frame is never mutated.
    assert event["input"] is tool_input


def test_other_frames_and_missing_inputs_pass_through():
    big = _input_of_encoded_length(10_000)
    for event in (
        {"type": "tool_result", "tool_use_id": "t1", "input": big},
        {"type": "tool_use", "id": "t1"},
    ):
        assert session_runtime._cap_ws_tool_input(event) is event


def test_an_unencodable_input_is_measured_by_its_repr():
    class Opaque:
        def __repr__(self):
            return "O" * 5000

    event = {"type": "tool_use", "id": "t1", "input": {Opaque(): 1}}
    capped = session_runtime._cap_ws_tool_input(event)
    assert capped["input"]["_ws_truncated"] is True
    assert capped["input"]["_preview"].startswith("{O")
