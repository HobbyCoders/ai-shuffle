"""Metadata-only upgrade and lazy per-session insertion-order allocation."""

from sqlite3 import Connection

from app.db import database, turns
from app.db.schema_postgres import create_postgres_schema
from tests.pg_shim import route_db_to


def test_upgrade_defers_history_allocation_until_first_insert(test_db: Connection) -> None:
    first = turns.enqueue_turn("history-upgrade", "user")
    front = turns.enqueue_turn("history-upgrade", "queue", at_front=True)
    adhoc = turns.record_adhoc_turn("history-upgrade", "api")
    for row, timestamp in (
        (first, "2026-01-01 00:00:01"), (front, "2026-01-01 00:00:02"),
        (adhoc, "2026-01-01 00:00:03"),
    ):
        test_db.execute("UPDATE turns SET created_at=? WHERE id=?", (timestamp, row["id"]))
    # Recreate the pre-upgrade shape in this disposable fixture. Startup must
    # neither build a new index nor rewrite this history under the DDL lock.
    test_db.execute("ALTER TABLE turns DROP COLUMN insert_order")
    with route_db_to(test_db) as conn:
        create_postgres_schema(conn.cursor())
        database._run_column_migrations(conn.cursor())
        legacy = (first, front, adhoc)
        assert [turns.get_turn(row["id"])["insert_order"] for row in legacy] == [0, 0, 0]
        assert turns.get_latest_turn("history-upgrade")["id"] == adhoc["id"]
        database._run_column_migrations(conn.cursor())
        assert [turns.get_turn(row["id"])["insert_order"] for row in legacy] == [0, 0, 0]
        newer = turns.enqueue_turn("history-upgrade", "queue", at_front=True)
        assert [turns.get_turn(row["id"])["insert_order"] for row in legacy] == [1, 2, 3]
        assert newer["insert_order"] == 4
        assert newer["position"] < front["position"]
        assert turns.get_latest_turn("history-upgrade")["id"] == newer["id"]
        database._run_column_migrations(conn.cursor())
        assert [turns.get_turn(row["id"])["insert_order"] for row in (*legacy, newer)] == [
            1, 2, 3, 4,
        ]
        turns.start_turn(newer["id"])
        turns.requeue_turn(newer["id"])
        assert turns.get_turn(newer["id"])["insert_order"] == 4


def test_imported_legacy_rows_stay_below_assigned_history(test_db: Connection) -> None:
    test_db.execute("INSERT INTO sessions (id, instance_id) VALUES (?, ?)", ("legacy-import", ""))
    existing = turns.enqueue_turn("legacy-import", "user")
    with route_db_to(test_db) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO queued_messages
                (id, session_id, content, position, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            "legacy-message", "legacy-import", "legacy pending message", 2,
            "2026-01-01 00:00:00",
        ))
        database._run_column_migrations(cur)
        cur.execute(
            "SELECT * FROM turns WHERE session_id=%s AND source='queue'", ("legacy-import",),
        )
        imported = cur.fetchone()
        assert imported["insert_order"] == 0
        assert turns.get_latest_turn("legacy-import")["id"] == existing["id"]
        newer = turns.enqueue_turn("legacy-import", "user")
        assert newer["insert_order"] == existing["insert_order"] + 1
        database._run_column_migrations(cur)
        assert turns.get_turn(imported["id"])["insert_order"] == 0
        assert turns.get_latest_turn("legacy-import")["id"] == newer["id"]
