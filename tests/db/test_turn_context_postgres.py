"""Context/history ordering under PostgreSQL NULLs, transaction clocks and locks."""
from typing import Any
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_terminal_context_prefers_ended_turn_over_legacy_null() -> None:
    from app.core import turn_scheduler
    from app.db import base, turns

    sid = "pg-context-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    recent = turns.enqueue_turn(sid, "user", profile_id="recent-profile")
    turns.start_turn(recent["id"], None, "runner")
    turns.settle_turn(recent["id"], "completed")
    legacy = turns.enqueue_turn(sid, "agent_message")
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET state='completed', ended_at=NULL WHERE id=%s", (legacy["id"],),
        )
    context = turn_scheduler._latest_context_turn(sid)
    assert context["id"] == recent["id"]
    assert context["profile_id"] == "recent-profile"


def test_insert_order_survives_transaction_timestamp_tie_and_front_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.db import base, turns

    sid = "pg-insert-order-" + uuid4().hex
    ids = iter(["z-" + sid, "a-" + sid])
    monkeypatch.setattr(turns, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=next(ids)),
    ))
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
        first = turns.enqueue_turn(sid, "user", _conn=conn)
        front = turns.enqueue_turn(sid, "queue", at_front=True, _conn=conn)
        # NOW() is transaction-stable in PostgreSQL: this drives a real tie
        # through the producer without seeding the timestamp the reader uses.
        assert first["created_at"] == front["created_at"]
        assert first["id"] > front["id"]
        assert first["insert_order"] < front["insert_order"]
        assert front["position"] < first["position"]
    assert turns.get_latest_turn(sid)["id"] == front["id"]
    assert turns.get_latest_run_turn(sid)["id"] == front["id"]
    assert turns.pop_ready(sid)["id"] == front["id"]


def test_insert_order_serializes_adhoc_and_queued_writers() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.db import base, turns

    sid = "pg-concurrent-order-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    first = turns.enqueue_turn(sid, "user")
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE turns SET insert_order=0 WHERE id=%s", (first["id"],))
    barrier = Barrier(2, timeout=5)

    def insert(adhoc: bool) -> dict[str, Any]:
        barrier.wait()
        if adhoc:
            return turns.record_adhoc_turn(sid, "api")
        return turns.enqueue_turn(sid, "queue", at_front=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        others = list(pool.map(insert, [False, True]))
    ordered = sorted([first, *others], key=lambda row: row["insert_order"])
    assert [row["insert_order"] for row in ordered] == [1, 2, 3]
    assert turns.get_latest_turn(sid)["id"] == ordered[-1]["id"]
    assert turns.get_turn(first["id"])["insert_order"] == 1


def test_lazy_backfill_does_not_block_another_sessions_write() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.db import base, turns

    sid = "pg-lazy-order-" + uuid4().hex
    other_sid = "pg-independent-order-" + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        for session_id in (sid, other_sid):
            cur.execute("INSERT INTO sessions (id) VALUES (%s)", (session_id,))
    first = turns.enqueue_turn(sid, "user")
    other_first = turns.enqueue_turn(other_sid, "user")
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET insert_order=0 WHERE session_id IN (%s, %s)", (sid, other_sid),
        )
    # Keep the lazy allocation transaction open. A history-wide UPDATE would
    # lock other_sid's legacy row too and block its writer until this commits.
    # The pool is outside the transaction so a failed timeout still releases
    # that transaction before joining the thread (no teardown deadlock).
    with ThreadPoolExecutor(max_workers=1) as pool, base.get_db() as conn:
        newer = turns.enqueue_turn(sid, "queue", _conn=conn)
        assert newer["insert_order"] == 2
        independent = pool.submit(turns.record_adhoc_turn, other_sid, "api")
        assert independent.result(timeout=5)["insert_order"] == 2
    assert turns.get_turn(first["id"])["insert_order"] == 1
    assert turns.get_turn(other_first["id"])["insert_order"] == 1
