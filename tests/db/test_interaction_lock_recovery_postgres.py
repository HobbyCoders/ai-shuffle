"""The real permission producer must survive a rolled-back event append (#1089)."""
import asyncio
import json
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from app.core import history_backfill, turn_recorder
from app.core.permission_handler import PermissionHandler
from app.db import interactions, session_events
from tests.core.interaction_harness import (  # noqa: F401
    no_connector_delivery, no_gateway_listener, no_push,
    reset_backfill_memo_on_setup, reset_interaction_wait_state,
)
from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (  # noqa: F401
    cluster, connect, session_id,
)

pytestmark = requires_real_pg


async def test_permission_card_arrives_once_after_counter_lock_timeout(connect, session_id):
    await history_backfill.ensure_backfilled(session_id)
    handler = PermissionHandler()
    broadcast = AsyncMock()
    holder = connect()
    holder.cursor().execute(
        "INSERT INTO session_event_counters (session_id, next_seq) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING", (session_id,),
    )
    holder.commit()
    holder.cursor().execute(
        "SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,),
    )
    task = asyncio.create_task(handler.request_permission(
        request_id="lock-request", session_id=session_id, profile_id="default",
        tool_name="Bash", tool_input={"command": "pwd"}, broadcast_func=broadcast,
    ))
    try:
        # The row is already durable while the event writer is blocked.
        async with asyncio.timeout(5):
            while not interactions.get_pending_interactions(session_id):
                await asyncio.sleep(0.01)
        await asyncio.sleep(2.3)  # past this private cluster's 2-second lock_timeout
        assert not session_events.get_events(session_id)
        holder.rollback()
        async with asyncio.timeout(4):
            while not session_events.get_events(session_id):
                await asyncio.sleep(0.01)
        rows = session_events.get_events(session_id)
        assert [r["event_type"] for r in rows] == ["interaction.requested"]
        assert rows[0]["payload"]["request"]["tool_name"] == "Bash"
        await handler.respond("lock-request", session_id, "deny", broadcast_func=broadcast)
        await asyncio.wait_for(task, 4)
        assert [r["event_type"] for r in session_events.get_events(session_id)] == [
            "interaction.requested", "interaction.answered",
        ]
    finally:
        holder.rollback()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        tail = turn_recorder._live_unfenced_write(session_id)
        if tail:
            await tail.task


# A separate interpreter has its own _unfenced_write_tail queue, as a second
# install sharing a database does. Pause ONLY after a real confirmed rollback.
_REQUESTING_INSTALL = r"""
import asyncio, json, sys, time
from pathlib import Path
from psycopg2.errors import LockNotAvailable
from app.core.config import settings
from app.db import base, session_events
from app.core import interaction_events, lifecycle_events
connection, sid, gate = json.loads(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
base.init_pool(min_conn=1, max_conn=3)
lifecycle_events.enqueue = lambda *a, **kw: None
real = session_events.append_events

def append(*args, **kwargs):
    try:
        return real(*args, **kwargs)
    except LockNotAvailable:
        (gate / "rolled-back").write_text("ready")
        deadline = time.monotonic() + 12
        while not (gate / "retry").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("retry gate timed out")
            time.sleep(.01)
        raise

session_events.append_events = append
async def request():
    from app.core.turn_recorder import _live_unfenced_write
    await interaction_events.emit_interaction_requested(
        sid, sys.argv[4], "race-request", "permission", json.loads(sys.argv[5]))
    tail = _live_unfenced_write(sid)
    if tail:
        await tail.task
try:
    asyncio.run(request())
finally:
    base.close_pool()
"""


async def test_two_installs_cannot_publish_an_answer_before_its_request(cluster, connect, session_id, tmp_path):
    await history_backfill.ensure_backfilled(session_id)
    payload = {"request_id": "race-request", "tool_name": "Bash",
               "tool_input": {"command": "pwd"},
               "assistant_confirm": {"question": "May I run pwd? Yes or no."}}
    row = interactions.create_interaction(session_id, "permission", payload, None)
    holder = connect()
    holder.cursor().execute(
        "INSERT INTO session_event_counters (session_id, next_seq) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING", (session_id,),
    )
    holder.commit()
    holder.cursor().execute(
        "SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _REQUESTING_INSTALL, json.dumps(cluster), session_id,
        str(tmp_path), row["id"], json.dumps(payload),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(8):
            while not (tmp_path / "rolled-back").exists():
                assert process.returncode is None
                await asyncio.sleep(.02)
        holder.rollback()
        # Install B wins the event counter while A is parked after rollback.
        result = await PermissionHandler().respond(
            "race-request", session_id, "deny", broadcast_func=AsyncMock(),
        )
        assert result.get("resolved") is True
        (tmp_path / "retry").write_text("go")
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        assert process.returncode == 0, (stdout + stderr).decode()
        rows = session_events.get_events(session_id)
        assert [event["event_type"] for event in rows] == [
            "interaction.requested", "interaction.answered",
        ]
        assert rows[0]["payload"]["interaction_id"] == rows[1]["payload"]["interaction_id"] == row["id"]
    finally:
        holder.rollback()
        (tmp_path / "retry").write_text("cleanup")
        if process.returncode is None:
            await asyncio.wait_for(process.communicate(), 8)


async def test_recording_failure_retries_a_real_counter_lock(connect, session_id, monkeypatch):
    from psycopg2.errors import LockNotAvailable
    from app.db import runs, turns

    run = runs.claim_run(session_id, "runner", 60)
    turn = turns.enqueue_turn(session_id, "user", prompt="hello")
    turns.start_turn(turn["id"], run["id"], "runner")
    holder = connect()
    holder.cursor().execute(
        "INSERT INTO session_event_counters (session_id, next_seq) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING", (session_id,),
    )
    holder.commit()
    holder.cursor().execute(
        "SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,),
    )
    failed = threading.Event()
    real = turns._lock_for_event_write

    def lock(*args):
        try:
            return real(*args)
        except LockNotAvailable:
            failed.set()
            raise

    monkeypatch.setattr(turns, "_lock_for_event_write", lock)
    monkeypatch.setattr(session_events, "LOCK_RETRY_DELAYS_SECONDS", (.1,))
    task = asyncio.create_task(asyncio.to_thread(
        turns.settle_turn, turn["id"], "aborted", "recording_failed", run_id=run["id"],
    ))
    try:
        assert await asyncio.to_thread(failed.wait, 4)
        holder.rollback()
        result = await asyncio.wait_for(task, 4)
        assert result["state"] == "aborted"
        runs.release_run(run["id"], run["fence_token"])
        assert turns.reap_expired() == []
        rows = session_events.get_events(session_id)
        assert len(rows) == 1
        assert rows[0]["id"] == "recording-failed:" + turn["id"]
    finally:
        holder.rollback()
        await asyncio.gather(task, return_exceptions=True)
