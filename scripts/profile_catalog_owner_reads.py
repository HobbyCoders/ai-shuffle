"""Compare catalog SQL ownership filtering in a disposable loopback PostgreSQL DB.

Set ALLOW_REAL_PG=1 and TEST_POSTGRES_DSN. Creates and retains a random schema;
existing tables are never read or changed. The baseline broadens only the
payload SELECTs, leaving the original Python ownership checks in place.
"""
import argparse
import json
import os
import statistics
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool

from app.db.schema_postgres import create_postgres_schema
from app.network import catalog, engine, policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=10000)
    parser.add_argument("--owned", type=int, default=100)
    args = parser.parse_args()
    if not 0 < args.owned <= args.sessions:
        parser.error("Require 0 < owned <= sessions")
    if os.environ.get("ALLOW_REAL_PG") != "1":
        parser.error("Set ALLOW_REAL_PG=1 for a disposable test database")
    params = parse_dsn(os.environ["TEST_POSTGRES_DSN"])
    if params.get("host") not in {"localhost", "127.0.0.1"}:
        parser.error("Only explicit loopback databases are accepted")
    params.update(host="127.0.0.1", hostaddr="127.0.0.1", sslmode="disable", connect_timeout=5)
    schema = "catalog_profile_" + uuid4().hex
    with closing(psycopg2.connect(**params)) as admin, admin:
        admin.cursor().execute("CREATE SCHEMA " + schema)
    params["options"] = f"-c search_path={schema} -c statement_timeout=60000"
    collections = ["sessions", "session_tags", "pending_interactions", "session_runtime"]
    with closing(psycopg2.connect(**params, cursor_factory=RealDictCursor)) as setup:
        cursor = setup.cursor()
        create_postgres_schema(cursor)
        setup.commit()
        setup.autocommit = True
        cursor.execute("SELECT version() AS version")
        version = cursor.fetchone()["version"]
        cursor.execute("""INSERT INTO sessions(id,owner_device_id,instance_id,title)
            SELECT 's-'||n,CASE WHEN n<=%s THEN 'owner' ELSE 'peer' END,
            'profile-instance',repeat(md5(n::text),4) FROM generate_series(1,%s) n""", (args.owned, args.sessions))
        cursor.execute("INSERT INTO tags(id,name) VALUES ('tag','Tag')")
        cursor.execute("INSERT INTO session_tags(session_id,tag_id) SELECT id,'tag' FROM sessions")
        cursor.execute("""INSERT INTO pending_interactions(id,session_id,kind,payload)
            SELECT id,id,'question',jsonb_build_object('question',repeat(md5(id),64)) FROM sessions""")
        cursor.execute("""INSERT INTO network_session_runtime(session_id,owner_device_id,state)
            SELECT id,owner_device_id,'idle' FROM sessions""")
        for name in collections:
            coll = policy.collection(name)
            cursor.execute(f"SELECT * FROM {coll.table}")
            hashes = []
            for row in cursor.fetchall():
                record = engine.serialize_row(coll, row)
                sid = row.get("session_id", row.get("id"))
                origin = "owner" if int(sid.removeprefix("s-")) <= args.owned else "peer"
                hashes.append((name, coll.record_id(record), 1, origin, engine.content_hash(coll, record), False))
            execute_values(cursor, """INSERT INTO sync_records
                (collection,record_id,clock,origin_device_id,content_hash,deleted) VALUES %s""", hashes)
            cursor.execute(f"VACUUM ANALYZE {coll.table}")
        cursor.execute("VACUUM ANALYZE sync_records")

    metrics = {}
    captured = {}
    baseline = False

    class Cursor(RealDictCursor):
        def execute(self, query, values=None):
            if query.startswith("SELECT * FROM sessions WHERE"):
                captured["sessions"] = (query, values)
                if baseline:
                    query, values = "SELECT * FROM sessions", None
            elif query.startswith("SELECT * FROM network_session_runtime WHERE"):
                captured["session_runtime"] = (query, values)
                if baseline:
                    query, values = "SELECT * FROM network_session_runtime", None
            elif query.startswith("SELECT child.* FROM"):
                table = query.split()[3]
                captured[table] = (query, values)
                if baseline:
                    query, values = f"SELECT * FROM {table}", None
            started = time.perf_counter()
            try:
                return super().execute(query, values)
            finally:
                metrics["statements"] = metrics.get("statements", 0) + 1
                metrics["execute_ms"] = metrics.get("execute_ms", 0) + (time.perf_counter() - started) * 1000

        def fetchall(self):
            rows = super().fetchall()
            metrics["returned_rows"] = metrics.get("returned_rows", 0) + len(rows)
            return rows

    pool = ThreadedConnectionPool(1, 1, **params, cursor_factory=Cursor)

    @contextmanager
    def get_db():
        conn = pool.getconn()
        metrics["acquisitions"] = metrics.get("acquisitions", 0) + 1
        started = time.perf_counter()
        try:
            with conn:
                yield conn
        finally:
            metrics["hold_ms"] = metrics.get("hold_ms", 0) + (time.perf_counter() - started) * 1000
            pool.putconn(conn)

    ctx = SimpleNamespace(device_id="owner", bootstrap_state=engine.BOOTSTRAP_READY)
    originals = (engine.get_db, catalog.get_db, engine._my_instance_id, catalog._my_instance_id)
    engine.get_db = catalog.get_db = get_db
    engine._my_instance_id = catalog._my_instance_id = lambda: "profile-instance"
    report = {"schema": schema, "postgres": version, "sessions": args.sessions,
              "owned_sessions": args.owned, "measurements": {}}
    try:
        for name in collections:
            report["measurements"][name] = {}
            for baseline in (True, False):
                samples = []
                for _ in range(7):
                    metrics.clear()
                    started = time.perf_counter()
                    if name in {"sessions", "session_runtime"}:
                        count = engine.scan_collection(name, ctx=ctx, owner_only=True)
                    else:
                        count = catalog._publish_child_rows(name, ctx)
                    assert count == 0, "Quiet fixture unexpectedly published a change"
                    samples.append({**metrics, "wall_ms": (time.perf_counter() - started) * 1000})
                report["measurements"][name]["baseline" if baseline else "candidate"] = {
                    "median": {key: statistics.median(s[key] for s in samples) for key in samples[0]},
                    "samples": samples}
        report["plans"] = {}
        with closing(psycopg2.connect(**params, cursor_factory=RealDictCursor)) as conn, conn:
            cursor = conn.cursor()
            for name, (query, values) in captured.items():
                table = policy.collection(name).table
                report["plans"][name] = {}
                for label, sql, args_ in [("baseline", f"SELECT * FROM {table}", None),
                                           ("candidate", query, values)]:
                    cursor.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, args_)
                    report["plans"][name][label] = cursor.fetchone()["QUERY PLAN"][0]
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({name: {label: data["median"] for label, data in measurements.items()}
                          for name, measurements in report["measurements"].items()}), flush=True)
    finally:
        engine.get_db, catalog.get_db, engine._my_instance_id, catalog._my_instance_id = originals
        pool.closeall()


if __name__ == "__main__":
    main()
