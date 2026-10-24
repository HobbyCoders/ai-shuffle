"""Upgrade/backfill coverage for immutable per-session turn insertion order."""

from app.db import database, turns
from app.db.schema_postgres import create_postgres_schema
from tests.pg_shim import route_db_to


def test_upgrade_backfills_history_and_keeps_ordinals_stable(test_db):
    first = turns.enqueue_turn("history-upgrade", "user")
    front = turns.enqueue_turn("history-upgrade", "queue", at_front=True)
    adhoc = turns.record_adhoc_turn("history-upgrade", "api")
    test_db.execute(
        "UPDATE turns SET created_at=? WHERE id=?", ("2026-01-01 00:00:01", first["id"]),
    )
    test_db.execute(
        "UPDATE turns SET created_at=? WHERE id=?", ("2026-01-01 00:00:02", front["id"]),
    )
    test_db.execute(
        "UPDATE turns SET created_at=? WHERE id=?", ("2026-01-01 00:00:03", adhoc["id"]),
    )
    # Recreate the pre-upgrade shape in this disposable fixture. Bootstrap runs
    # before the additive migration and must not index the missing column.
    test_db.execute("DROP INDEX idx_turns_session_insert_order")
    test_db.execute("ALTER TABLE turns DROP COLUMN insert_order")
    with route_db_to(test_db) as conn:
        create_postgres_schema(conn.cursor())
        database._run_column_migrations(conn.cursor())
        assert [turns.get_turn(row["id"])["insert_order"] for row in (first, front, adhoc)] == [1, 2, 3]
        assert turns.get_latest_turn("history-upgrade")["id"] == adhoc["id"]
        newer = turns.enqueue_turn("history-upgrade", "queue", at_front=True)
        assert newer["insert_order"] == 4
        assert newer["position"] < front["position"]
        assert turns.get_latest_turn("history-upgrade")["id"] == newer["id"]
        database._run_column_migrations(conn.cursor())
        assert [turns.get_turn(row["id"])["insert_order"] for row in (first, front, adhoc, newer)] == [
            1, 2, 3, 4,
        ]
        turns.start_turn(newer["id"])
        turns.requeue_turn(newer["id"])
        assert turns.get_turn(newer["id"])["insert_order"] == 4


def test_backfill_imported_rows_after_existing_assigned_history(test_db):
    test_db.execute("INSERT INTO sessions (id, instance_id) VALUES (?, ?)", ("legacy-import", ""))
    existing = turns.enqueue_turn("legacy-import", "user")
    with route_db_to(test_db) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO queued_messages
                (id, session_id, content, position, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, ("legacy-message", "legacy-import", "legacy pending message", 2, "2026-01-01 00:00:00"))
        database._run_column_migrations(cur)
        cur.execute("SELECT * FROM turns WHERE session_id=%s AND source='queue'", ("legacy-import",))
        imported = cur.fetchone()
        assert imported["insert_order"] > existing["insert_order"]
        assert turns.get_latest_turn("legacy-import")["id"] == imported["id"]
        database._run_column_migrations(cur)
        assert turns.get_turn(imported["id"])["insert_order"] == imported["insert_order"]
