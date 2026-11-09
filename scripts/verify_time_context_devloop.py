"""Stage ONLY the disposable devloop DB for time-awareness.journey.mjs.

Never reads inherited DB settings or credentials. The private devloop receipt and
its owned socket must match this checkout. Does not import app startup/config.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    import psycopg2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--zone", choices=("America/New_York", "unset"))
    parser.add_argument("--session")
    args = parser.parse_args()
    status = json.loads(args.status.read_text())
    root = Path(__file__).resolve().parents[1]
    assert Path(status["checkout"]).resolve() == root and status["state"] == "ready"
    data = Path(status["data_dir"])
    assert data.parent.name.startswith("ai-shuffle-preview-") and data.parent.parent == Path("/tmp")
    runtime = json.loads((data / "postgres/runtime.json").read_text())
    assert runtime["socket"] == status["postgres"]["socket_dir"] and runtime["socket"].startswith("/tmp/shuffle-pg-")
    with psycopg2.connect(host=runtime["socket"], port=runtime["port"], user="shuffle_admin", dbname="ai_shuffle", connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='2s'")
            if args.zone:
                cur.execute("SELECT user_type, user_id FROM user_preferences WHERE key='timezone' LIMIT 1")
                identity = cur.fetchone()
                if identity is None:
                    cur.execute("SELECT value FROM system_settings WHERE key='owner_preference_identity'")
                    owner = cur.fetchone()
                    assert owner, "Open the private UI first so owner identity is initialized"
                    identity = ("admin", owner[0])
                    cur.execute("INSERT INTO user_preferences(user_type,user_id,key,value) VALUES(%s,%s,'timezone','\"local\"'::jsonb)", identity)
                # No invalidation required: the harness deliberately reads the
                # authoritative row per turn, not a cached browser/app value.
                value = "local" if args.zone == "unset" else args.zone
                cur.execute("UPDATE user_preferences SET value=%s::jsonb WHERE user_type=%s AND user_id=%s AND key='timezone'", (json.dumps(value), *identity))
                if args.zone == "unset":
                    cur.execute("UPDATE user_preferences SET value='null'::jsonb WHERE user_type=%s AND user_id=%s AND key='timezone_resolved'", identity)
            if args.session:
                previous = datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)
                previous = previous.replace(hour=21, minute=14, second=0, microsecond=0)
                cur.execute("UPDATE session_events SET created_at=%s WHERE session_id=%s AND seq=(SELECT MAX(seq) FROM session_events WHERE session_id=%s AND event_type='message.user' AND COALESCE(payload->>'source','user') IN ('user','queue'))", (previous, args.session, args.session))
                assert cur.rowcount == 1, "No real owner message was produced for this synthetic chat"
                print(json.dumps({"backdated_owner_at": previous.isoformat()}))


if __name__ == "__main__":
    main()
