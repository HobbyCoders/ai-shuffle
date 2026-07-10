"""Durable background-task snapshot writer tests (Phase 1, WRITE-ONLY).

Every row asserted here was produced by REAL task_registry writer sequences
(start_task / update_task / complete_task / mark_unparked) flowing through the
fire-and-forget enqueue -> coalescing queue -> single async consumer -> v55
``background_tasks`` upsert. NO reader flips: the assertions read the table
with raw SQL precisely because no application read path exists yet.
"""

import logging

import pytest

from app.core import task_persistence as tp
from app.core.config import settings
from app.core.task_registry import task_registry
from app.db import database


@pytest.fixture(autouse=True)
def _fresh_writer():
    tp.reset()
    yield
    tp.reset()


def _rows(mock_db, sid):
    cur = mock_db.execute(
        "SELECT task_id, session_id, instance_id, kind, is_sdk_background, "
        "tool_use_id, title, status, unparked, terminal_notified, started_at, "
        "updated_at FROM background_tasks WHERE session_id = ? ORDER BY task_id",
        (sid,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# =============================================================================
# Registry transitions -> durable rows
# =============================================================================

async def test_start_task_persists_running_row(mock_db):
    sid = "tpers-start"
    task_registry.start_task(sid, "t1", "agent", "bg research", tool_use_id="toolu_1")
    await tp.flush()

    rows = _rows(mock_db, sid)
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "t1"
    assert row["kind"] == "agent"
    assert row["title"] == "bg research"
    assert row["status"] == "running"
    assert row["tool_use_id"] == "toolu_1"
    assert not row["is_sdk_background"]
    assert not row["unparked"]
    assert not row["terminal_notified"]
    assert row["instance_id"] == settings.effective_instance_id
    assert row["started_at"] and row["updated_at"]


async def test_full_lifecycle_coalesces_to_latest_snapshot(mock_db):
    """start -> update -> complete before the consumer drains: ONE row, the
    terminal state (latest snapshot per (session, task) wins)."""
    sid = "tpers-coalesce"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.update_task(sid, "t1", summary="halfway")
    task_registry.complete_task(sid, "t1", "completed", summary="done")
    assert tp.pending_count() == 1  # coalesced, not three queued writes
    await tp.flush()

    rows = _rows(mock_db, sid)
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"


async def test_upsert_updates_existing_row(mock_db):
    sid = "tpers-upsert"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await tp.flush()
    assert _rows(mock_db, sid)[0]["status"] == "running"

    task_registry.complete_task(sid, "t1", "error", summary="boom")
    await tp.flush()

    rows = _rows(mock_db, sid)
    assert len(rows) == 1  # upsert, never a duplicate
    assert rows[0]["status"] == "error"


async def test_terminal_notified_persisted_via_callback_point(mock_db):
    """A killed OWN-background task fires the #514 guaranteed-notification
    latch; the snapshot written from complete_task (placed AFTER the callback
    block) carries terminal_notified=True."""
    sid = "tpers-notified"
    task_registry.set_terminal_notification_callback(lambda s, t: None)
    task_registry.mark_background_launch(sid, "toolu_bg")
    task_registry.start_task(sid, "t1", "agent", "bg work", tool_use_id="toolu_bg")
    task_registry.complete_task(sid, "t1", "killed")
    try:
        await tp.flush()
    finally:
        task_registry.set_terminal_notification_callback(None)

    row = _rows(mock_db, sid)[0]
    assert row["status"] == "killed"
    assert row["terminal_notified"]


async def test_mark_unparked_persists_flag(mock_db):
    sid = "tpers-unparked"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.complete_task(sid, "t1", "completed")
    assert task_registry.mark_unparked(sid, "t1") is True
    await tp.flush()

    row = _rows(mock_db, sid)[0]
    assert row["unparked"]
    assert row["status"] == "completed"


async def test_sdk_background_flag_persisted(mock_db):
    sid = "tpers-sdkbg"
    task_registry.start_task(sid, "sh1", "shell", "sleep 100")
    task_registry.mark_sdk_background_task(sid, "sh1")
    # Next transition mirrors the flag.
    task_registry.update_task(sid, "sh1", summary="still running")
    await tp.flush()

    row = _rows(mock_db, sid)[0]
    assert row["is_sdk_background"]
    assert row["kind"] == "shell"


# =============================================================================
# Kill switch + failure containment
# =============================================================================

async def test_kill_switch_disables_all_writes(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "durable_task_state", False)
    sid = "tpers-off"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.complete_task(sid, "t1", "completed")
    assert tp.pending_count() == 0
    await tp.flush()
    assert _rows(mock_db, sid) == []


async def test_write_failure_never_raises_and_logs_once(mock_db, monkeypatch, caplog):
    """DB outage: registry writers stay unaffected, exactly ONE warning per
    outage, and recovery logs + resumes writing."""

    calls = {"n": 0}
    real = database.upsert_background_task

    def _flaky(snapshot):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db down")
        return real(snapshot)

    monkeypatch.setattr(database, "upsert_background_task", _flaky)

    sid = "tpers-outage"
    with caplog.at_level(logging.WARNING, logger="app.core.task_persistence"):
        task_registry.start_task(sid, "t1", "agent", "one")  # write 1: fails
        await tp.flush()
        task_registry.start_task(sid, "t2", "agent", "two")  # write 2: fails
        await tp.flush()
    outage_logs = [r for r in caplog.records if "task persistence write failed" in r.message]
    assert len(outage_logs) == 1  # one log per outage, not per drop

    # Recovery: the next transition writes again and logs the recovery.
    with caplog.at_level(logging.INFO, logger="app.core.task_persistence"):
        task_registry.start_task(sid, "t3", "agent", "three")
        await tp.flush()
    assert any("recovered" in r.message for r in caplog.records)
    assert [r["task_id"] for r in _rows(mock_db, sid)] == ["t3"]


async def test_consumer_drains_without_explicit_flush(mock_db):
    """The fire-and-forget path alone (no flush) reaches the table."""
    import asyncio

    sid = "tpers-auto"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    for _ in range(50):
        if _rows(mock_db, sid):
            break
        await asyncio.sleep(0.02)
    assert _rows(mock_db, sid), "consumer never drained the queued snapshot"


# =============================================================================
# Boot reconcile: stale non-terminal rows -> killed
# =============================================================================

async def test_boot_reconcile_marks_stale_rows_killed(mock_db):
    from app.core.startup_reconcile import reconcile_from_durable_truth

    sid = "tpers-boot"
    # Previous-process reality: a running task was mirrored durably...
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.start_task(sid, "t2", "agent", "done work")
    task_registry.complete_task(sid, "t2", "completed")
    await tp.flush()
    # ...then the server died (in-memory registry is rebuilt empty).
    task_registry.reset_all()
    tp.reset()

    counts = await reconcile_from_durable_truth()

    assert counts["background_task_rows_killed"] == 1
    by_id = {r["task_id"]: r for r in _rows(mock_db, sid)}
    assert by_id["t1"]["status"] == "killed"  # stranded running row reaped
    assert by_id["t2"]["status"] == "completed"  # terminal rows untouched


async def test_boot_reconcile_spares_peer_install_rows(mock_db):
    from app.core.startup_reconcile import reconcile_from_durable_truth

    sid = "tpers-boot-peer"
    task_registry.start_task(sid, "t1", "agent", "peer work")
    await tp.flush()
    # Re-attribute the row to a peer install (shared-DB topology).
    mock_db.execute(
        "UPDATE background_tasks SET instance_id = 'peer-install' WHERE session_id = ?",
        (sid,),
    )
    mock_db.commit()
    task_registry.reset_all()
    tp.reset()

    counts = await reconcile_from_durable_truth()

    assert counts["background_task_rows_killed"] == 0
    assert _rows(mock_db, sid)[0]["status"] == "running"
