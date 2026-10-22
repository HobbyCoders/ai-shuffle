"""Aggregate recent Bash requests, without printing commands or session content.

Run from the repository: PYTHONPATH=fluid:. python scripts/measure_bash_file_work.py
Uses the app's supervised database configuration; never initializes the schema.
Counts durable ToolUse events (including code-mode calls), not argument deltas.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import time

from fluid.tools.bash_file_guard import classify

CATEGORIES = ("file-read", "search", "file-write/redirect", "in-place edit", "legitimate shell")


def aggregate(rows, since, until, exclude_session=None):
    counts = defaultdict(Counter)
    seen = set()
    unknown_time = 0
    for sid, leg, event in rows:
        if sid == exclude_session:
            continue
        if isinstance(event, str):
            event = json.loads(event)
        ts = event.get("ts", 0)
        if not ts:
            unknown_time += 1
            continue
        if not since <= ts < until:
            continue
        key = (sid, event.get("tool_use_id"))
        if key in seen:
            continue
        seen.add(key)
        inp = event.get("input")
        if not isinstance(inp, dict) or not isinstance(inp.get("command"), str):
            counts[leg]["unclassifiable"] += 1
            continue
        work = classify(inp["command"])
        counts[leg][work.category if work else "legitimate shell"] += 1
    return {
        "capture_utc": datetime.fromtimestamp(until, timezone.utc).isoformat(),
        "since_utc": datetime.fromtimestamp(since, timezone.utc).isoformat(),
        "scope": "worker database; all projects and child sessions; observed requests, not executions",
        "attribution": "TurnStarted.provider per turn; claude=delegated, other recorded providers=native; missing=unknown",
        "limitations": "conservative lower bound; legitimate shell includes unknown syntax; retained events only; no execution/success or dedicated-tool availability inference",
        "unknown_timestamp_events": unknown_time,
        "legs": {leg: {"total": sum(c.values()), "categories": {
            category: {"count": c[category], "rate": round(c[category]/sum(c.values()), 6)}
            for category in (*CATEGORIES, "unclassifiable")}}
            for leg, c in sorted(counts.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--until", help="ISO UTC cutoff for a repeatable frozen window")
    parser.add_argument("--exclude-session", help="Exclude this audit's Fluid session id")
    args = parser.parse_args()
    import psycopg2
    from app.core.config import settings
    until = datetime.fromisoformat(args.until).timestamp() if args.until else time.time()
    since = until - args.days * 86400
    # Dedicated read-only, repeatable-read transaction; no pool init, writes,
    # DSNs in arguments, or exception messages that could expose credentials.
    conn = psycopg2.connect(**settings.get_database_connection())
    try:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            cur.execute("""
                WITH turn_legs AS (
                    SELECT session_id, event ->> 'turn_id' AS turn_id,
                           CASE WHEN count(DISTINCT event ->> 'provider') != 1 THEN 'unknown'
                                WHEN min(event ->> 'provider') = 'claude' THEN 'delegated'
                                ELSE 'native' END AS leg
                    FROM fluid_session_events
                    WHERE event ->> '_cls' = 'TurnStarted'
                    GROUP BY session_id, event ->> 'turn_id'
                )
                SELECT e.session_id, COALESCE(t.leg, 'unknown'), e.event
                FROM fluid_session_events e
                LEFT JOIN turn_legs t ON t.session_id = e.session_id
                    AND t.turn_id = e.event ->> 'turn_id' 
                WHERE e.event ->> '_cls' = 'ToolUse'
                  AND e.event ->> 'name' = 'Bash'
                  AND (COALESCE((e.event ->> 'ts')::double precision, 0) >= %s
                       OR COALESCE((e.event ->> 'ts')::double precision, 0) = 0)
            """, (since,))
            result = aggregate(cur, since, until, args.exclude_session)
        print(json.dumps(result, indent=2))
    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not echo a driver/config exception that might contain connection
        # details. A failed audit is not an empty/zero baseline.
        raise SystemExit(f"Read-only Bash audit unavailable ({type(exc).__name__})") from None
