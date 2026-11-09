"""A genuinely contended session-admission row cannot skip captured Stop kills."""
import asyncio
import os
import sys
import pytest
from psycopg2.errors import LockNotAvailable

from app.core import query_session_lifecycle as lifecycle, stop_receipts as recovery
from app.core.task_registry import task_registry
from app.db import runs, session_events, stop_receipts as receipts, turns
from app.mcp import builtin_tools
from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (  # noqa: F401
    cluster, connect, session_id,
)
from tests.shared_fixtures import clean_task_registry  # noqa: F401

pytestmark = requires_real_pg


@pytest.mark.parametrize("takeover", [False, True])
async def test_admission_row_timeout_preserves_retry_and_stops_process(session_id, connect, monkeypatch, takeover):
    owner = runs.claim_run(session_id, "original-owner", 60)
    turn = turns.enqueue_turn(session_id, "user", prompt="executing")
    assert turns.start_turn(turn["id"], owner["id"], "original-owner")
    queued = turns.enqueue_turn(session_id, "queue", prompt="captured words", command_id="captured-send")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                                              start_new_session=os.name != "nt")
    shell = builtin_tools._BackgroundTask("captured-monitor", "controlled wait", None, session_id=session_id)
    shell.process = proc
    monkeypatch.setitem(builtin_tools._background_tasks, shell.task_id, shell)
    task_registry.start_task(session_id, shell.task_id, "shell", "controlled wait")
    task_registry.mark_monitor_shell_task(session_id, shell.task_id)
    flipped = turns.interrupt_turn(session_id, "stop", True, turn["id"], source="user_stop")
    receipt = turns.capture_stop_receipt(session_id, turn["id"], flipped)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    scope = lifecycle.capture_stop_scope(session_id)
    holder = connect()
    try:
        holder.cursor().execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        # Neither validation nor capture can take S. Cleanup must still reach
        # the real process using the scope captured BEFORE validation waited.
        with pytest.raises(LockNotAvailable):
            await asyncio.to_thread(turns.capture_stop_receipt, session_id, turn["id"], flipped)
        results = await asyncio.wait_for(recovery.consume(saved, scope=scope), 15)
        assert proc.returncode is not None
        assert any(result.get("retryable") for result in results)
        assert not task_registry.is_user_stopped(session_id)
        assert turns.get_turn(queued["id"])["state"] == "queued"
        pending = receipts.get(saved["id"])
        assert pending["result"] is None and pending["consumed_at"] is None
        holder.rollback()
        if takeover:
            assert turns.settle_turn(turn["id"], "aborted", "interrupted", run_id=owner["id"])
            assert runs.release_run(owner["id"], owner["fence_token"])
            replacement = runs.claim_run(session_id, "replacement-owner", 60)
            successor = turns.enqueue_turn(session_id, "user", prompt="replacement work")
            assert turns.start_turn(successor["id"], replacement["id"], "replacement-owner")
            newer = turns.enqueue_turn(session_id, "queue", prompt="new words")
        await recovery.consume(saved, scope=scope)
        await recovery.consume(saved, scope=scope)
        assert turns.get_turn(queued["id"])["state"] == ("queued" if takeover else "aborted")
        assert task_registry.is_user_stopped(session_id) is not takeover
        if takeover:
            assert turns.get_turn(successor["id"])["state"] == "running"
            assert turns.get_turn(newer["id"])["state"] == "queued"
        assert receipts.get(saved["id"])["consumed_at"] is not None
        unsent = [event for event in session_events.get_events(session_id)
                  if event["event_type"] == "message.user" and event["payload"].get("not_sent")]
        assert len(unsent) == (0 if takeover else 1)
    finally:
        holder.rollback()
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), 3)
