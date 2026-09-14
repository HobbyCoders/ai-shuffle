"""Durable background-task snapshot writer tests (Phase 1, WRITE-ONLY).

Every row asserted here was produced by REAL task_registry writer sequences
(start_task / update_task / complete_task) flowing through the
fire-and-forget enqueue -> coalescing queue -> single async consumer -> v55
``background_tasks`` upsert. NO reader flips: the assertions read the table
with raw SQL precisely because no application read path exists yet.
"""

import asyncio
import contextlib
import logging

import pytest

from app.core import task_persistence as tp
from app.core.config import settings
from app.core.task_registry import task_registry
from app.db import database


@pytest.fixture(autouse=True)
async def _fresh_writer():
    # Async (not sync) fixture on purpose: the consumer task started during a
    # test is bound to the test's event loop, and reset() cancels it via
    # loop.call_soon. A sync fixture's teardown can run AFTER pytest-asyncio has
    # closed that loop, so the cancel raises "RuntimeError: Event loop is closed"
    # (issue #619). An async fixture's teardown runs INSIDE the still-open loop,
    # so the cancel lands cleanly; awaiting the cancelled task also drains it so
    # nothing is left "pending" at collection end.
    tp.reset()
    yield
    consumer = tp._consumer
    tp.reset()
    if consumer is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


def _rows(mock_db, sid):
    cur = mock_db.execute(
        "SELECT task_id, session_id, instance_id, kind, is_sdk_background, "
        "tool_use_id, title, status, terminal_notified, started_at, "
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


@pytest.mark.parametrize("field", ["title", "summary", "reason", "report_path"])
async def test_nul_task_text_does_not_poison_batch_or_durable_restore(mock_db, field):
    """Registry-produced text is made visibly safe only in its durable mirror."""
    sid = "tpers-nul-" + field
    ordinary = "café 日本語 🦋\n\t literal \\u0000 ␀"
    text = "before\x00after " + ordinary
    task_registry.start_task(sid, "nul", "agent", text if field == "title" else "work")
    metadata = {field: text} if field != "title" else {}
    task_registry.complete_task(sid, "nul", "completed", **metadata)
    task_registry.start_task(sid, "ordinary", "agent", "ordinary")
    task_registry.complete_task(sid, "ordinary", "completed", summary=ordinary)
    assert tp.pending_count() == 2
    await tp.flush()

    assert not tp._outage_logged
    assert task_registry._tasks[sid]["nul"][field] == text  # live output is unchanged
    task_registry.reset_all()  # TaskGet must now use its real durable fallback
    restored = task_registry.get_task(sid, "nul", durable=True)
    wire_field = {"title": "subject", "report_path": "reportPath"}.get(field, field)
    assert restored[wire_field] == text.replace("\x00", "␀")
    assert restored["status"] == "completed"
    assert task_registry.get_task(sid, "ordinary", durable=True)["summary"] == ordinary


@pytest.mark.parametrize(
    "field, limit", [("summary", 20_000), ("reason", 256), ("report_path", 4096)]
)
async def test_nul_normalization_preserves_task_text_bounds(mock_db, field, limit):
    from app.db.background_tasks import get_background_task

    sid = "tpers-nul-bounds"
    text = "\x00" + "日" * limit + "\x00"
    task_registry.start_task(sid, "bounded", "agent", "work")
    task_registry.complete_task(sid, "bounded", "completed", **{field: text})
    await tp.flush()
    stored = get_background_task("bounded", sid)[field]
    assert len(stored) == limit
    assert stored.startswith("␀")
    assert "\x00" not in stored
    if field == "summary":
        assert "[middle of terminal summary clipped]" in stored
        assert stored.endswith("␀")
    else:
        assert stored == text.replace("\x00", "␀")[:limit]


async def test_real_monitor_nul_stdout_survives_persistence_and_restart(mock_db, tmp_path):
    import shlex
    import sys
    from app.mcp import builtin_tools as bt

    sid = "tpers-nul-monitor"
    database.create_session(sid, None)
    code = "import sys; sys.stdout.buffer.write(b'before\\x00after\\n')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    task = await bt._spawn_background(command, str(tmp_path), sid)
    try:
        await asyncio.wait_for(task._reader_task, timeout=10)
        assert task.exit_code == 0
        assert task.lines == ["before\x00after"]
        await tp.flush()
        assert not tp._outage_logged
        task_registry.reset_all()
        restored = task_registry.get_task(sid, task.task_id, durable=True)
        assert restored["status"] == "completed"
        assert restored["summary"] == "before␀after"
    finally:
        if task.process.returncode is None:
            task.process.kill()
            await task.process.wait()
        bt._background_tasks.pop(task.task_id, None)


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
    from app.db import background_tasks
    real = background_tasks.upsert_background_tasks

    def _flaky(snapshot):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db down")
        return real(snapshot)

    monkeypatch.setattr(background_tasks, "upsert_background_tasks", _flaky)

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


async def test_terminal_notified_persisted_when_a_wake_delivers_a_completion(mock_db):
    """A CLEAN completion never reaches ``complete_task``'s error/killed chip,
    so its ``terminal_notified`` used to stay False forever — even after the
    parent was woken with the result. The scheduler latches it at the delivery
    itself (steer receipt or wake turn) and flushes the record."""
    from app.core import turn_scheduler

    sid = "tpers-delivered"
    task_registry.mark_background_launch(sid, "toolu_del")
    task_registry.start_task(sid, "t1", "agent", "bg work", tool_use_id="toolu_del")
    task_registry.complete_task(sid, "t1", "completed", summary="the report")
    await tp.flush()
    assert not _rows(mock_db, sid)[0]["terminal_notified"]

    turn_scheduler.on_task_notification_delivered(sid, ["t1"])
    await tp.flush()

    row = _rows(mock_db, sid)[0]
    assert row["status"] == "completed"
    assert row["terminal_notified"]

@pytest.mark.parametrize("exit_code", [0, 1])
async def test_monitor_spawn_and_exit_publish_shared_presence(mock_db, monkeypatch, exit_code):
    from unittest.mock import AsyncMock
    from app.api.sessions_helpers import enrich_sessions_with_tags
    from app.core.models import Session
    from app.core.sync_engine import sync_engine
    from app.mcp import builtin_tools as bt

    sid = "tpres-real-monitor"
    database.create_session(sid, None)
    exited = asyncio.Event()

    class Stdout:
        async def read(self, _n):
            await exited.wait()
            return b""

    class Process:
        pid = 999
        stdout = Stdout()
        returncode = None

        async def wait(self):
            return exit_code

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=Process()))
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    task = await bt._spawn_background("test monitor", None, sid)
    try:
        await tp.flush()
        assert task_registry.get_active_background_work(sid) == []
        row = enrich_sessions_with_tags([database.get_session(sid)])[0]
        assert Session(**row).model_dump()["has_running_tasks"] is True
        assert row["parked_task_kind"] is None
        broadcast.assert_awaited_with(
            user_id="admin", session_id=sid, updates={"has_running_tasks": True},
        )
        exited.set()
        await task._reader_task
        await tp.flush()
        row = enrich_sessions_with_tags([database.get_session(sid)])[0]
        assert Session(**row).model_dump()["has_running_tasks"] is False
        broadcast.assert_awaited_with(
            user_id="admin", session_id=sid, updates={"has_running_tasks": False},
        )
    finally:
        exited.set()
        await task._reader_task
        bt._background_tasks.pop(task.task_id, None)


@pytest.mark.parametrize("owner", [None, "owner-a"])
async def test_shared_monitor_presence_list_search_and_last_completion(mock_db, monkeypatch, owner):
    from unittest.mock import AsyncMock
    from app.api.sessions_helpers import enrich_sessions_with_tags, enrich_search_results_with_tags
    from app.core.sync_engine import sync_engine
    from app.db.background_tasks import get_running_task_presence

    sid = "tpres-monitor"
    if owner is not None:
        database.create_api_user(owner, "A", "test-owner-a")
    database.create_session(sid, None, api_user_id=owner)
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    # Unmarked, non-SDK shell: no park marker or wake eligibility required.
    task_registry.start_task(sid, "shell", "shell", "Monitor server")
    task_registry.start_task(sid, "second", "shell", "second server")
    await tp.flush()
    assert database.get_session(sid).get("parked_task_kind") is None
    for enrich in (enrich_sessions_with_tags, enrich_search_results_with_tags):
        assert enrich([database.get_session(sid)])[0]["has_running_tasks"] is True
    assert broadcast.call_args.kwargs == {
        "user_id": owner or "admin", "session_id": sid,
        "updates": {"has_running_tasks": True},
    }
    task_registry.complete_task(sid, "shell", "completed")
    await tp.flush()
    assert get_running_task_presence([sid])[sid]["has_running_tasks"] is True
    assert broadcast.call_args.kwargs["updates"] == {"has_running_tasks": True}
    task_registry.complete_task(sid, "second", "completed")
    await tp.flush()
    assert len(_rows(mock_db, sid)) == 2  # retained terminal rows do not count
    assert enrich_sessions_with_tags([database.get_session(sid)])[0]["has_running_tasks"] is False
    assert broadcast.call_args.kwargs["updates"] == {"has_running_tasks": False}

async def test_presence_batch_owners_peer_and_broadcast_failure(mock_db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core.sync_engine import sync_engine
    from app.db.background_tasks import get_running_task_presence

    for sid, owner in (("tpres-a", "owner-a"), ("tpres-b", "owner-b")):
        database.create_api_user(owner, owner, "test-" + owner)
        database.create_session(sid, None, api_user_id=owner)
        task_registry.start_task(sid, sid, "shell", "server")
    broadcast = AsyncMock(side_effect=[RuntimeError("offline"), None])
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    await tp.flush()
    assert broadcast.await_count == 2  # one owner's failure does not stop others
    assert {c.kwargs["user_id"] for c in broadcast.call_args_list} == {"owner-a", "owner-b"}
    mock_db.execute("UPDATE background_tasks SET instance_id = 'peer' WHERE session_id = 'tpres-b'")
    mock_db.commit()
    task_registry.reset_all()
    traced = []
    mock_db.set_trace_callback(traced.append)
    try:
        presence = get_running_task_presence(["tpres-a", "tpres-b", "tpres-a", "missing"])
        assert get_running_task_presence([]) == {}
    finally:
        mock_db.set_trace_callback(None)
    assert len([s for s in traced if s.lstrip().upper().startswith("SELECT")]) == 1
    assert presence["tpres-b"] == {"api_user_id": "owner-b", "has_running_tasks": True}
    assert "missing" not in presence
    broadcast.side_effect = None
    from app.core.startup_reconcile import reconcile_from_durable_truth
    await reconcile_from_durable_truth()
    assert get_running_task_presence(["tpres-a", "tpres-b"]) == {
        "tpres-a": {"api_user_id": "owner-a", "has_running_tasks": False},
        "tpres-b": {"api_user_id": "owner-b", "has_running_tasks": True},
    }


async def test_failed_batch_never_broadcasts_uncommitted_presence(mock_db, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from app.core.sync_engine import sync_engine
    from app.db import background_tasks

    sid = "tpres-failed"
    database.create_api_user("owner", "Owner", "test-owner")
    database.create_session(sid, None, api_user_id="owner")
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    real = background_tasks.upsert_background_tasks
    monkeypatch.setattr(background_tasks, "upsert_background_tasks", Mock(side_effect=RuntimeError("down")))
    task_registry.start_task(sid, "t", "shell", "server")
    await tp.flush()
    broadcast.assert_not_awaited()
    assert _rows(mock_db, sid) == []
    monkeypatch.setattr(background_tasks, "upsert_background_tasks", real)
    task_registry.complete_task(sid, "t", "completed")
    await tp.flush()
    assert broadcast.call_args.kwargs["updates"] == {"has_running_tasks": False}


@pytest.mark.parametrize("settler", ["stop", "reap"])
async def test_direct_durable_settle_publishes_presence(mock_db, monkeypatch, settler):
    from unittest.mock import AsyncMock
    from app.core.sync_engine import sync_engine
    from app.core import turn_recorder
    from app.core.query_session_lifecycle import _durable_force_settle
    from app.mcp.builtin_tools import _settle_reaped_shell

    sid = "tpres-settle"
    database.create_api_user("owner", "Owner", "test-owner")
    database.create_session(sid, None, api_user_id="owner")
    task_registry.start_task(sid, "t", "shell", "server")
    await tp.flush()
    task_registry.reset_all()
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    monkeypatch.setattr(turn_recorder, "record_task_settle_frame", AsyncMock())
    settle = _durable_force_settle if settler == "stop" else _settle_reaped_shell
    await settle(sid, "t")
    assert _rows(mock_db, sid)[0]["status"] == "killed"
    broadcast.assert_awaited_once_with(
        user_id="owner", session_id=sid, updates={"has_running_tasks": False},
    )


async def test_coalesced_batch_one_write_and_presence_read_failure_recovers(mock_db, monkeypatch):
    from unittest.mock import Mock
    from app.db import background_tasks

    write = Mock(wraps=background_tasks.upsert_background_tasks)
    read = Mock(side_effect=RuntimeError("presence unavailable"))
    monkeypatch.setattr(background_tasks, "upsert_background_tasks", write)
    monkeypatch.setattr(background_tasks, "get_running_task_presence", read)
    for i in range(10):
        task_registry.start_task("batch", str(i), "shell", "server")
    task_registry.complete_task("batch", "0", "completed")
    await tp.flush()
    write.assert_called_once()
    assert len(write.call_args.args[0]) == 10
    read.assert_called_once()
    assert len(_rows(mock_db, "batch")) == 10
    # A broken presence lookup must not kill the consumer or subsequent writes.
    task_registry.complete_task("batch", "1", "completed")
    await tp.flush()
    assert write.call_count == 2
    assert {r["task_id"]: r["status"] for r in _rows(mock_db, "batch")}["1"] == "completed"
