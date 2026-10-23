"""Read-only WAL/checkpoint deltas: python -m scripts.diagnostics.wal_sample --interval 10.

Uses the app's managed database settings. Statistics are never reset. PostgreSQL
14+ exposes pg_stat_wal; PostgreSQL 17+ moves checkpoints to pg_stat_checkpointer.
Sync times can remain zero when the server's I/O timing collection is disabled.
"""

from __future__ import annotations

import argparse
import json
import math
import time

COUNTERS = ("wal_bytes", "wal_fpi", "wal_sync_ms", "checkpoints_timed",
            "checkpoints_requested", "checkpoint_write_ms", "checkpoint_sync_ms")


def read_sample(connection) -> dict:
    """End each read transaction so the next sample sees fresh statistics."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wal_bytes, wal_fpi, wal_sync_time, stats_reset "
                "FROM pg_catalog.pg_stat_wal"
            )
            wal_bytes, wal_fpi, wal_sync, wal_reset = cursor.fetchone()
            if connection.server_version >= 170000:
                cursor.execute(
                    "SELECT num_timed, num_requested, write_time, sync_time, stats_reset "
                    "FROM pg_catalog.pg_stat_checkpointer"
                )
            else:
                cursor.execute(
                    "SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time, "
                    "checkpoint_sync_time, stats_reset FROM pg_catalog.pg_stat_bgwriter"
                )
            timed, requested, write, sync, checkpoint_reset = cursor.fetchone()
        return dict(zip(COUNTERS, (wal_bytes, wal_fpi, wal_sync, timed, requested, write, sync)),
                    wal_stats_reset=wal_reset, checkpoint_stats_reset=checkpoint_reset)
    finally:
        connection.rollback()


def compute_deltas(before: dict, after: dict) -> dict:
    for key in ("wal_stats_reset", "checkpoint_stats_reset"):
        if before[key] != after[key]:
            raise ValueError("statistics were reset during sampling; sample again")
    deltas = {key: after[key] - before[key] for key in COUNTERS}
    if any(value < 0 for value in deltas.values()):
        raise ValueError("statistics decreased during sampling; sample again")
    # WAL bytes are numeric in PostgreSQL: preserve exact integer byte counts.
    for key in ("wal_bytes", "wal_fpi", "checkpoints_timed", "checkpoints_requested"):
        deltas[key] = int(deltas[key])
    for key in ("wal_sync_ms", "checkpoint_write_ms", "checkpoint_sync_ms"):
        deltas[key] = float(deltas[key])
    deltas["checkpoints"] = deltas["checkpoints_timed"] + deltas["checkpoints_requested"]
    return deltas


def sample(connection, interval: float) -> dict:
    connection.set_session(readonly=True)
    started = time.monotonic()
    before = read_sample(connection)
    time.sleep(interval)
    after = read_sample(connection)
    return {"elapsed_s": time.monotonic() - started, **compute_deltas(before, after)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between samples (default: 10)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be finite and positive")

    import psycopg2

    from app.core.config import settings

    connection = None
    try:
        connection = psycopg2.connect(**settings.get_database_connection())
        print(json.dumps(sample(connection, args.interval), sort_keys=True))
    except ValueError:
        parser.exit(1, "Statistics changed discontinuously; sample again.\n")
    except (psycopg2.Error, OSError, RuntimeError):
        # Connection errors may contain private endpoint/credential details.
        parser.exit(1, "Could not sample managed PostgreSQL statistics.\n")
    finally:
        if connection is not None:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
