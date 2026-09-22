"""Profile stream heads in an owned schema of an explicit disposable loopback DB.

Set ALLOW_REAL_PG=1 and TEST_POSTGRES_DSN, then run with PYTHONPATH=.:fluid.
The schema is retained with the output for inspection. No existing table is
read, altered, or cleared. Never point this at a production database.
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.db import network as store

AGGREGATE = "SELECT origin_device_id, incarnation, MAX(seq) AS s FROM sync_ops GROUP BY origin_device_id, incarnation"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("ALLOW_REAL_PG") != "1":
        parser.error("Set ALLOW_REAL_PG=1 for a disposable test database")
    params = parse_dsn(os.environ["TEST_POSTGRES_DSN"])
    if params.get("host") not in {"localhost", "127.0.0.1"}:
        parser.error("Only an explicit loopback test database is accepted")
    params.update(host="127.0.0.1", hostaddr="127.0.0.1", connect_timeout=5, sslmode="disable")
    schema = "head_profile_" + uuid4().hex
    with closing(psycopg2.connect(**params)) as admin, admin:
        admin.cursor().execute("CREATE SCHEMA " + schema)
    params["options"] = f"-c search_path={schema} -c statement_timeout=60000"
    setup = psycopg2.connect(**params, cursor_factory=RealDictCursor)
    setup.autocommit = True
    cursor = setup.cursor()
    store.create_schema(cursor)
    metrics = {}

    class TimedCursor(RealDictCursor):
        def execute(self, query, parameters=None):
            started = time.perf_counter()
            try:
                return super().execute(query, parameters)
            finally:
                metrics["statements"] = metrics.get("statements", 0) + 1
                metrics["sql_ms"] = metrics.get("sql_ms", 0) + (time.perf_counter() - started) * 1000

    pool = ThreadedConnectionPool(1, 2, **params, cursor_factory=TimedCursor)

    @contextmanager
    def get_db():
        connection = pool.getconn()
        metrics["acquisitions"] = metrics.get("acquisitions", 0) + 1
        started = time.perf_counter()
        try:
            with connection:
                yield connection
        finally:
            metrics["hold_ms"] = metrics.get("hold_ms", 0) + (time.perf_counter() - started) * 1000
            pool.putconn(connection)

    def baseline():
        with get_db() as connection:
            cur = connection.cursor()
            cur.execute(AGGREGATE)
            heads = {}
            for row in cur.fetchall():
                heads.setdefault(row["origin_device_id"], {})[str(row["incarnation"])] = row["s"]
            cur.execute("SELECT origin_device_id, incarnation, last_seq FROM sync_watermarks")
            for row in cur.fetchall():
                bucket = heads.setdefault(row["origin_device_id"], {})
                inc = str(row["incarnation"])
                bucket[inc] = max(bucket.get(inc, 0), row["last_seq"])
            return heads

    def measure(function):
        samples = []
        answer = None
        for _ in range(7):
            metrics.clear()
            started = time.perf_counter()
            answer = function()
            samples.append({**metrics, "wall_ms": (time.perf_counter() - started) * 1000})
        return answer, {"median": {key: statistics.median(s[key] for s in samples) for key in samples[0]},
                        "samples": samples}

    async def loop_lag(function):
        delays = []
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                started = time.perf_counter()
                await asyncio.sleep(0.005)
                delays.append(max(0, (time.perf_counter() - started - 0.005) * 1000))

        task = asyncio.create_task(ticker())
        try:
            for _ in range(30):
                await asyncio.to_thread(function)
        finally:
            done.set()
            await task
        return {"samples": len(delays), "max_excess_ms": max(delays, default=0),
                "median_excess_ms": statistics.median(delays) if delays else 0}

    report = {"schema": schema, "head_probe_limit": store.HEAD_PROBE_LIMIT, "datasets": []}
    original = store.get_db
    store.get_db = get_db
    try:
        cursor.execute("SELECT version() AS version")
        report["postgres"] = cursor.fetchone()["version"]
        for rows, streams in [(10000, 8), (100000, 64), (1000000, 64), (1000000, 512)]:
            # Only tables created in our random schema are in this search path.
            cursor.execute("TRUNCATE sync_ops, sync_watermarks")
            cursor.execute("""INSERT INTO sync_ops(origin_device_id,incarnation,seq,collection,record_id,op,clock,lane,envelope)
                SELECT 'worker-'||lpad(((n-1) %% %s)::text,4,'0'),1,((n-1)/%s)+1,
                'session_events',n::text,'upsert',n,'history',
                jsonb_build_object('ciphertext',md5(n::text)||md5((n+1)::text)||md5((n+2)::text)||md5((n+3)::text))
                FROM generate_series(1,%s) n""", (streams, streams, rows))
            cursor.execute("VACUUM ANALYZE sync_ops")
            before, old_metrics = measure(baseline)
            after, new_metrics = measure(store.local_watermarks)
            assert before == after and len(after) == streams
            plans = {}
            for name, query, values in [("aggregate", AGGREGATE, None),
                                        ("bounded_probe", store._LOG_HEADS_QUERY, (store.HEAD_PROBE_LIMIT,))]:
                cursor.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, values)
                plans[name] = cursor.fetchone()["QUERY PLAN"][0]
            data = {"rows": rows, "streams": streams, "baseline": old_metrics, "candidate": new_metrics,
                    "plans": plans, "loop_lag": {"baseline": asyncio.run(loop_lag(baseline)),
                                                 "candidate": asyncio.run(loop_lag(store.local_watermarks))}}
            report["datasets"].append(data)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"rows": rows, "streams": streams, "baseline": old_metrics["median"],
                              "candidate": new_metrics["median"]}), flush=True)
    finally:
        store.get_db = original
        pool.closeall()
        cursor.close()
        setup.close()


if __name__ == "__main__":
    main()
