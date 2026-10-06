"""Periodic stale background-task sweep (sibling of the boot reconcile).

The boot reconcile settles this install's stranded non-terminal
``background_tasks`` rows — but only at boot, and an install can stay up for
weeks. Any settle path missed at teardown time (historically: a routine idle
eviction clearing the registry under a live Monitor shell, 2026-08-29 incident
task a0270e18) left the durable row and the UI chip "running" until the next
restart. ``settle_stale_background_task_rows`` runs from
``resource_monitor._periodic_cleanup`` and settles own-instance non-terminal
rows older than the grace window whose task the in-memory registry no longer
knows: flip to killed + emit the terminal ``task.updated`` frame.

Rows whose task the registry still RUNS are skipped: that is genuinely live
work (e.g. a Monitor surviving an eviction deferral). Rows whose task the
registry already finished take the registry's terminal status.
"""

from unittest.mock import AsyncMock, patch


from app.core.startup_reconcile import settle_stale_background_task_rows
from app.core.task_registry import task_registry
from app.db.background_tasks import (
    get_background_task,
    list_stale_nonterminal_background_tasks,
    upsert_background_task,
)
from app.db.base import get_db
from tests.shared_fixtures import clean_task_registry  # noqa: F401  (autouse fixture)

# Old enough to clear any grace window.
_OLD_TS = "2000-01-01T00:00:00+00:00"


def _seed(session_id, task_id, status, *, updated_at=_OLD_TS, tool_use_id=None,
          instance_id=None):
    upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": "shell",
        "tool_use_id": tool_use_id,
        "title": "loop",
        "status": status,
        "started_at": updated_at,
        "updated_at": updated_at,
    })
    if instance_id is not None:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE background_tasks SET instance_id = %s "
                "WHERE task_id = %s AND session_id = %s",
                (instance_id, task_id, session_id),
            )


def _patch_frame():
    return patch(
        "app.core.turn_recorder.record_task_settle_frame", new=AsyncMock()
    )


# =============================================================================
# list_stale_nonterminal_background_tasks
# =============================================================================

def test_list_stale_returns_old_nonterminal_rows_only(mock_db):
    sid = "sw-list"
    _seed(sid, "old-run", "running", tool_use_id="toolu_1")
    _seed(sid, "old-done", "completed")
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _seed(sid, "fresh-run", "running", updated_at=now)

    rows = list_stale_nonterminal_background_tasks(15 * 60)

    assert {r["task_id"] for r in rows} == {"old-run"}
    assert rows[0]["session_id"] == sid
    assert rows[0]["tool_use_id"] == "toolu_1"


def test_list_stale_scopes_to_own_instance(mock_db):
    sid = "sw-scope"
    _seed(sid, "own", "running")
    _seed(sid, "peer", "running", instance_id="some-other-install-9999")

    rows = list_stale_nonterminal_background_tasks(15 * 60)

    assert {r["task_id"] for r in rows} == {"own"}


# =============================================================================
# settle_stale_background_task_rows
# =============================================================================

async def test_sweep_settles_orphaned_row_and_emits_frame(mock_db):
    sid = "sw-orphan"
    _seed(sid, "t-stuck", "running", tool_use_id="toolu_s")

    with _patch_frame() as frame:
        settled = await settle_stale_background_task_rows()

    assert settled == 1
    assert get_background_task("t-stuck", sid)["status"] == "killed"
    frame.assert_awaited_once()
    args = frame.await_args
    assert args.args[0] == sid
    assert args.args[1] == "t-stuck"
    assert args.args[2] == "killed"
    assert args.kwargs.get("tool_use_id") == "toolu_s"


async def test_sweep_skips_row_the_registry_still_tracks(mock_db):
    """A registry-known task is live work (or persists its own truth) —
    the sweep must not kill it, however old its durable row looks."""
    sid = "sw-live"
    _seed(sid, "t-live", "running")
    task_registry.start_task(sid, "t-live", "shell", "loop")

    with _patch_frame() as frame:
        settled = await settle_stale_background_task_rows()

    assert settled == 0
    assert get_background_task("t-live", sid)["status"] == "running"
    frame.assert_not_awaited()


async def test_sweep_skips_recent_rows(mock_db):
    import datetime
    sid = "sw-fresh"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _seed(sid, "t-new", "running", updated_at=now)

    with _patch_frame() as frame:
        settled = await settle_stale_background_task_rows()

    assert settled == 0
    assert get_background_task("t-new", sid)["status"] == "running"
    frame.assert_not_awaited()


async def test_sweep_settles_at_most_once(mock_db):
    """Second run finds the row terminal — no double settle, no second frame."""
    sid = "sw-once"
    _seed(sid, "t-once", "running")

    with _patch_frame() as frame:
        first = await settle_stale_background_task_rows()
        second = await settle_stale_background_task_rows()

    assert (first, second) == (1, 0)
    assert frame.await_count == 1


async def test_sweep_settles_a_row_whose_finished_task_never_reached_the_db(mock_db):
    """The registry holds the task as FINISHED but the row still says running.

    The terminal snapshot is fire-and-forget and a failed write is dropped
    (task_persistence: "the next transition re-mirrors" — a terminal task has
    none). The live record stays in the registry until the session is removed,
    so skipping every registry-known task left the row, task lists and the
    active-work check showing it as running indefinitely.
    """
    sid = "sw-lost-terminal"
    _seed(sid, "t-done", "running", tool_use_id="toolu_d")
    task_registry.start_task(sid, "t-done", "shell", "loop")
    with patch("app.core.task_persistence.enqueue_task_snapshot"):  # the write is lost
        task_registry.complete_task(sid, "t-done", "error", summary="exit code 2")

    with _patch_frame() as frame:
        settled = await settle_stale_background_task_rows()

    assert settled == 1
    assert get_background_task("t-done", sid)["status"] == "error"
    frame.assert_awaited_once()
    assert frame.await_args.args[:3] == (sid, "t-done", "error")
    assert frame.await_args.kwargs.get("tool_use_id") == "toolu_d"
    # The registry record is unchanged: it was already the truth.
    assert task_registry.get_task(sid, "t-done")["status"] == "error"


async def test_sweep_never_settles_a_task_the_registry_still_runs(mock_db):
    sid = "sw-still-live"
    _seed(sid, "t-run", "running")
    task_registry.start_task(sid, "t-run", "shell", "loop")

    with _patch_frame() as frame:
        assert await settle_stale_background_task_rows() == 0

    assert get_background_task("t-run", sid)["status"] == "running"
    assert task_registry.get_task(sid, "t-run")["status"] == "running"
    assert sid in task_registry.active_session_ids()
    frame.assert_not_awaited()
