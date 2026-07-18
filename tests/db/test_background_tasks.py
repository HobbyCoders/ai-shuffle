"""Tests for the durable background-task read/flip helpers (v55).

Covers the boot-reconcile / stop-session-task force-settle support added on top
of the WRITE-ONLY Phase-1 snapshot writer:

- ``mark_stale_background_tasks_killed`` returns the flipped rows and never
  re-flips already-terminal rows;
- ``get_background_task`` single-row read;
- ``mark_background_task_killed`` single-row flip;
- ``session_events.has_subagent_started`` payload-filtered lookup.

Rows are seeded through the real ``upsert_background_task`` writer (which stamps
this install's instance id) so the instance scoping is exercised end-to-end.
"""

from app.db.base import get_db
from app.db.background_tasks import (
    upsert_background_task,
    mark_stale_background_tasks_killed,
    get_background_task,
    mark_background_task_killed,
)
from app.db import session_events as se


def _seed(session_id, task_id, status, *, kind="agent", tool_use_id=None,
          title=None, instance_id=None):
    upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": kind,
        "tool_use_id": tool_use_id,
        "title": title,
        "status": status,
    })
    if instance_id is not None:
        # Override the instance stamp for the peer-install scoping test.
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE background_tasks SET instance_id = %s "
                "WHERE task_id = %s AND session_id = %s",
                (instance_id, task_id, session_id),
            )


# =============================================================================
# mark_stale_background_tasks_killed
# =============================================================================

def test_mark_stale_returns_flipped_rows_and_skips_terminal(mock_db):
    sid = "bt-stale"
    _seed(sid, "t-run", "running", tool_use_id="toolu_run", title="R")
    _seed(sid, "t-started", "started", tool_use_id=None, title="S")
    _seed(sid, "t-done", "completed", tool_use_id="toolu_done", title="D")
    _seed(sid, "t-err", "error", title="E")
    _seed(sid, "t-killed", "killed", title="K")

    rows = mark_stale_background_tasks_killed()

    assert isinstance(rows, list)
    by_task = {r["task_id"]: r for r in rows}
    assert set(by_task) == {"t-run", "t-started"}
    # Returned rows carry the fields the caller needs to emit terminal frames.
    assert by_task["t-run"]["session_id"] == sid
    assert by_task["t-run"]["tool_use_id"] == "toolu_run"
    assert by_task["t-run"]["title"] == "R"
    assert by_task["t-started"]["tool_use_id"] is None

    # Non-terminal rows are now killed; terminal rows are untouched.
    assert get_background_task("t-run", sid)["status"] == "killed"
    assert get_background_task("t-started", sid)["status"] == "killed"
    assert get_background_task("t-done", sid)["status"] == "completed"
    assert get_background_task("t-err", sid)["status"] == "error"
    assert get_background_task("t-killed", sid)["status"] == "killed"


def test_mark_stale_only_flips_own_instance(mock_db):
    sid = "bt-scope"
    _seed(sid, "own", "running")
    _seed(sid, "peer", "running", instance_id="some-other-install-9999")

    rows = mark_stale_background_tasks_killed()

    assert {r["task_id"] for r in rows} == {"own"}
    assert get_background_task("peer", sid)["status"] == "running"


def test_mark_stale_returns_empty_when_nothing_stale(mock_db):
    sid = "bt-none"
    _seed(sid, "done", "completed")
    assert mark_stale_background_tasks_killed() == []


# =============================================================================
# get_background_task / mark_background_task_killed
# =============================================================================

def test_get_background_task_returns_row_or_none(mock_db):
    sid = "bt-get"
    _seed(sid, "g1", "running", kind="agent", tool_use_id="toolu_g1", title="G")

    row = get_background_task("g1", sid)
    assert row is not None
    assert row["task_id"] == "g1"
    assert row["session_id"] == sid
    assert row["kind"] == "agent"
    assert row["tool_use_id"] == "toolu_g1"
    assert row["title"] == "G"
    assert row["status"] == "running"

    assert get_background_task("missing", sid) is None
    assert get_background_task("g1", "other-session") is None


def test_mark_background_task_killed_flips_single_row(mock_db):
    sid = "bt-kill"
    _seed(sid, "k1", "running")
    _seed(sid, "k2", "running")

    mark_background_task_killed("k1", sid)

    assert get_background_task("k1", sid)["status"] == "killed"
    # Sibling row untouched.
    assert get_background_task("k2", sid)["status"] == "running"


# =============================================================================
# session_events.has_subagent_started
# =============================================================================

def test_has_subagent_started_matches_payload_task_id(mock_db):
    se.append_events("se-1", [
        {"type": "subagent.started", "payload": {"task_id": "sub-a"}},
        {"type": "subagent.progress", "payload": {"task_id": "sub-a"}},
    ])

    assert se.has_subagent_started("se-1", "sub-a") is True
    # A progress event alone (no started) must not match.
    assert se.has_subagent_started("se-1", "nope") is False
    # Scoped per session.
    assert se.has_subagent_started("other-session", "sub-a") is False
