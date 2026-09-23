"""#1407 reconciliation transaction contracts on disposable PostgreSQL."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                reason="requires disposable TEST_POSTGRES_DSN")

T0 = datetime(2026, 9, 14, 22, 40, 0, tzinfo=timezone.utc)
CALLS = ((1200, 40, 3000, 0), (800, 25, 4200, 64))
SUMS = tuple(sum(c[i] for c in CALLS) for i in range(4))
COST = 0.25


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    assert os.environ.get("TEST_POSTGRES_DSN")
    base.init_pool(min_conn=1, max_conn=6)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()


@pytest.fixture
def duplicate():
    """A cancelled worker booked by both historical paths."""
    from app.db import base
    sid, store, turn_id = (f"pg-dedupe-{uuid4().hex}", f"ses_{uuid4().hex[:12]}",
                           uuid4().hex)
    events = [{"_cls": "TurnStarted", "turn_id": "ft-1", "ts": T0.timestamp(),
               "model": "m", "provider": "p"}]
    for index, (fresh, out, cache_r, cache_c) in enumerate(CALLS, start=1):
        events.append({"_cls": "Usage", "turn_id": "ft-1",
                       "ts": T0.timestamp() + index, "input_tokens": fresh,
                       "output_tokens": out, "cache_read_tokens": cache_r,
                       "cache_creation_tokens": cache_c})
    events.append({"_cls": "Interrupted", "turn_id": "ft-1",
                   "ts": (T0 + timedelta(minutes=10)).timestamp(),
                   "usage": {"input_tokens": SUMS[0], "output_tokens": SUMS[1],
                             "cache_read_input_tokens": SUMS[2],
                             "cache_creation_input_tokens": SUMS[3]},
                   "num_model_calls": len(CALLS)})
    killed_at = T0 + timedelta(minutes=10)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (id, sdk_session_id, total_tokens_in, "
            "total_tokens_out, total_cache_read_tokens, total_cache_creation_tokens, "
            "total_cost_usd, total_model_calls, turn_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)",
            (sid, store, *(2 * v for v in SUMS), 2 * COST, 2 * len(CALLS)),
        )
        for seq, event in enumerate(events, start=1):
            event["session_id"], event["seq"] = store, seq
            cursor.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s,%s,%s)", (store, seq, json.dumps(event)),
            )
        cursor.execute(
            "INSERT INTO turns (id, session_id, state, source, stop_reason, "
            "started_at, ended_at) VALUES (%s,%s,'aborted','provider_agent',"
            "'parent_cancelled',%s,%s)",
            (turn_id, sid, T0 - timedelta(seconds=1), killed_at),
        )
        for index in range(1, len(CALLS) + 1):
            cursor.execute(
                "INSERT INTO session_events (id, session_id, seq, turn_id, "
                "event_type, payload) VALUES (%s,%s,%s,%s,'turn.usage',%s)",
                (uuid4().hex, sid, index, turn_id,
                 json.dumps({"usage": {"model_call_index": index}})),
            )
        for offset_ms in (0, 4):
            cursor.execute(
                "INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, "
                "cache_read_tokens, cache_creation_tokens, cost_usd, duration_ms, "
                "created_at) VALUES (%s,'m',%s,%s,%s,%s,%s,0,%s)",
                (sid, *SUMS, COST, killed_at + timedelta(milliseconds=offset_ms)),
            )
    return sid


def _state(sid):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) AS n FROM usage_log WHERE session_id=%s", (sid,))
        rows = cursor.fetchone()["n"]
        cursor.execute(
            "SELECT total_tokens_in, total_tokens_out, total_cache_read_tokens, "
            "total_cache_creation_tokens, total_cost_usd, total_model_calls "
            "FROM sessions WHERE id=%s", (sid,),
        )
        row = cursor.fetchone()
    return rows, (row["total_tokens_in"], row["total_tokens_out"],
                  row["total_cache_read_tokens"], row["total_cache_creation_tokens"],
                  round(row["total_cost_usd"], 6), row["total_model_calls"])


def test_read_only_then_apply_then_idempotent(duplicate):
    from scripts.dedupe_cancelled_usage import reconcile
    doubled = (*(2 * v for v in SUMS), round(2 * COST, 6), 2 * len(CALLS))
    assert reconcile(session_id=duplicate) == 0
    assert _state(duplicate) == (2, doubled)
    assert reconcile(session_id=duplicate, apply=True) == 0
    once = (*SUMS, COST, len(CALLS))
    assert _state(duplicate) == (1, once)
    assert reconcile(session_id=duplicate, apply=True) == 0
    assert _state(duplicate) == (1, once)


def test_concurrent_applies_remove_the_duplicate_once(duplicate):
    from scripts.dedupe_cancelled_usage import reconcile
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: reconcile(session_id=duplicate, apply=True), range(2)))
    # The row lock serializes the runs: the loser re-plans and finds one row,
    # or its guarded delete finds the row gone and rolls back. Either way the
    # counters move once.
    assert set(results) <= {0, 1}
    assert _state(duplicate) == (1, (*SUMS, COST, len(CALLS)))
