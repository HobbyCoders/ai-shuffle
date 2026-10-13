"""#1286 shared-database activity summary on disposable PostgreSQL.

The SQLite shim cannot prove the lease comparison against ``NOW()`` with
``TIMESTAMPTZ`` columns or the join/grouping in real PostgreSQL.
"""
from uuid import uuid4

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401  (pg_schema: autouse fixture)

pytestmark = requires_real_pg

COUNTS = (
    "local_active_runs", "other_active_runs", "other_active_runners", "local_live_turns",
    "other_live_turns", "stale_live_turns", "local_running_background_tasks",
    "other_running_background_tasks", "unattributed_running_background_tasks",
)


def test_activity_summary_counts_live_leases_on_real_postgres():
    from app.db import base, runs, turns

    tag = uuid4().hex
    local, sibling, crashed = f"local-{tag}", f"sibling-{tag}", f"crashed-{tag}"
    before = runs.activity_summary(local)
    mine = runs.claim_run(f"s-local-{tag}", local, 60)
    theirs = runs.claim_run(f"s-sibling-{tag}", sibling, 60)
    lapsed = runs.claim_run(f"s-crashed-{tag}", crashed, -1)
    for run in (mine, theirs, lapsed):
        turn = turns.enqueue_turn(run["session_id"], "user", prompt="never-collect-this-prompt")
        assert turns.start_turn(turn["id"], run_id=run["id"])
    with base.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO background_tasks (task_id, session_id, instance_id, status) "
            "VALUES (%s, %s, %s, 'running'), (%s, %s, %s, 'running')",
            (f"t1-{tag}", mine["session_id"], local, f"t2-{tag}", theirs["session_id"], sibling),
        )
    try:
        after = runs.activity_summary(local)
        delta = {key: after[key] - before[key] for key in COUNTS}
        assert delta == {
            "local_active_runs": 1, "other_active_runs": 1, "other_active_runners": 1,
            "local_live_turns": 1, "other_live_turns": 1, "stale_live_turns": 1,
            "local_running_background_tasks": 1, "other_running_background_tasks": 1,
            "unattributed_running_background_tasks": 0,
        }
        assert tag not in repr(after) and "never-collect-this-prompt" not in repr(after)
    finally:
        sessions = [run["session_id"] for run in (mine, theirs, lapsed)]
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM background_tasks WHERE session_id = ANY(%s)", (sessions,))
            cursor.execute("DELETE FROM turns WHERE session_id = ANY(%s)", (sessions,))
            cursor.execute("DELETE FROM runs WHERE session_id = ANY(%s)", (sessions,))
