"""One question to PostgreSQL, in a process of its own; see backend_probe.py.

Standard library and psycopg2 only. Started with -I -S, so nothing else loads.
Never prints a parameter, a connection detail or an exception's text.
"""
import json
import os
import signal
import sys
import threading

QUERY = (
    "SELECT pid, state, wait_event_type, wait_event, application_name, "
    "EXTRACT(EPOCH FROM clock_timestamp() - state_change)::float8 "
    "FROM pg_stat_activity WHERE pid = ANY(%s)"
)


def _give_up_by_itself(seconds: int) -> None:
    """The parent kills this process; this is for a parent that died first."""
    if hasattr(signal, "alarm"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)  # An ignored signal survives exec.
        signal.alarm(seconds)  # The kernel's clock: needs nothing from this interpreter.
    else:
        threading.Timer(seconds, os._exit, (3,)).start()


def main() -> int:
    _give_up_by_itself(int(sys.argv[1]))
    with open(sys.argv[2], encoding="utf-8") as source:
        request = json.load(source)
    sys.path.extend(request["path"])
    import psycopg2

    conn = psycopg2.connect(*request["args"], **request["kwargs"])
    conn.autocommit = True
    cursor = conn.cursor()
    cursor.execute(QUERY, (request["pids"],))
    _answer({"rows": cursor.fetchall()})
    return 0


def _answer(value) -> None:
    # The parent has mapped this fixed-size file. Never truncate it under the mapping.
    try:
        with open(sys.argv[3], "r+b") as result:
            result.write(json.dumps(value).encode())
    except OSError:
        pass  # A late launch can find that the timed-out parent already removed it.


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001 - the type is all the parent ever needs
        _answer({"error": type(exc).__name__})
        code = 1
    os._exit(code)  # Nothing left to tidy, and a timer thread must not delay this.
