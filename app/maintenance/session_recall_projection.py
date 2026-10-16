"""Remove SessionSearch's text projection (``app.db.session_recall``).

Only needed after going back to an app version that predates the projection:
older versions never read it, but its triggers keep maintaining it on every
transcript write. Run from the installed source with the worker's
interpreter:

    python -m app.maintenance.session_recall_projection --remove

A current version reinstalls and backfills the projection on its own after
startup, so running this on one only costs a rebuild.
"""
import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.maintenance.session_recall_projection", description=(
        "Drop SessionSearch's text projection: its triggers (one table at a time, waiting at most "
        "--lock-timeout-ms for each), its table and its functions. Without --remove no database is opened."
    ))
    parser.add_argument("--remove", action="store_true", help="Explicitly approve dropping the projection")
    parser.add_argument("--lock-timeout-ms", type=int, default=2000)
    args = parser.parse_args(argv)
    if not args.remove:
        parser.print_help()
        return 0
    if args.lock_timeout_ms <= 0:
        parser.error("--lock-timeout-ms must be positive")
    from app.db import base, session_recall

    try:
        base.init_pool(min_conn=1, max_conn=1)
        session_recall.uninstall(lock_timeout_ms=args.lock_timeout_ms)
        print("SessionSearch projection removed.")
        return 0
    except Exception as exc:  # noqa: BLE001 — do not print connection details
        print(f"Removal failed ({type(exc).__name__}); retry when the transcript tables are less busy.")
        return 1
    finally:
        base.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
