"""Request experimental online history indexing on a disposable candidate.

No database is opened without --apply. Tail latency is not yet accepted for
production: this is deliberately opt-in until the follow-up load test passes.
The running worker performs the migration and resumes it after a restart.
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Opt this worker into experimental deferred indexing")
    args = parser.parse_args(argv)
    if not args.apply:
        parser.print_help()
        return 0
    from app.db import base, history_indexing, session_recall

    try:
        base.init_pool(min_conn=1, max_conn=1)
        with base.get_db() as conn:
            cur = conn.cursor()
            if not session_recall.ready(cur):
                print("Wait for the recall projection to finish installing, then retry.")
                return 1
            cur.execute("INSERT INTO system_settings(key,value) VALUES (%s,'building') "
                        "ON CONFLICT (key) DO NOTHING", (history_indexing.KEY,))
        print("Experimental opt-in saved. The running worker will migrate and catch up "
              "between foreground turns; restart recovery is automatic.")
        return 0
    except Exception as exc:  # noqa: BLE001 — never expose connection details
        print(f"Opt-in failed ({type(exc).__name__}); no connection details are printed.")
        return 1
    finally:
        base.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
