"""Real LISTEN driver controls and isolation, with an external process deadline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from app.core import event_listener
from tests.core.network.test_transcript_op_retention_postgres import cluster  # noqa: F401
from tests.db.schema_lifecycle_helpers import pg_connect

pytestmark = pytest.mark.skipif(os.name == "nt", reason="raw socket descriptor replacement")

# This is the removed in-process select/poll path, run ONLY in a disposable
# child. Readiness is real; reuse occurs after select, not a simulated driver.
_REUSE = textwrap.dedent("""
    import os, select, socket
    real_select = select.select
    sockets = []
    def ready(read, write, error, timeout):
        conn = read[0]
        with psycopg2.connect(**kwargs) as sender:
            sender.cursor().execute("SELECT pg_notify('session_events', '{}')")
        assert real_select(read, write, error, 2)[0]
        if size is not None:
            stranger, peer = socket.socketpair()
            sockets.extend([stranger, peer])
            stranger.setblocking(True)
            os.dup2(stranger.fileno(), conn.fileno())
            peer.sendall(b"A" * size)
        print("readable", flush=True)
        return read, [], []
    select.select = ready
""")
_CONTROL = textwrap.dedent("""
    import json, sys, threading, time
    import psycopg2
    request = json.load(sys.stdin)
    kwargs, size = request["kwargs"], request["bytes"]
""") + _REUSE + textwrap.dedent("""
    def listen():
        conn = psycopg2.connect(**kwargs)
        conn.autocommit = True
        conn.cursor().execute("LISTEN session_events")
        select.select([conn], [], [], 5)
        conn.poll()
        print("returned", flush=True)
    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    while thread.is_alive():
        time.sleep(.05)
        print("tick", flush=True)
""")


def kwargs_for(cluster):
    conn = pg_connect(cluster)
    try:
        return {**conn.get_dsn_parameters(), "password": cluster["password"]}
    finally:
        conn.close()


def run_child(script, request, timeout=5):
    env = {key: value for key, value in os.environ.items() if key not in ("AI_SHUFFLE_ENV_FILE", "DATABASE_URL")}
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    expired = False
    try:
        output, errors = child.communicate(json.dumps(request), timeout=timeout)
    except subprocess.TimeoutExpired:
        expired = True
        child.kill()
        output, errors = child.communicate(timeout=2)
    return expired, child.returncode, output, errors


@pytest.mark.parametrize("size", [None, 0, 1, 8192])
def test_removed_in_process_listener_real_driver_control(cluster, size):
    expired, code, output, errors = run_child(_CONTROL, {"kwargs": kwargs_for(cluster), "bytes": size})
    assert "readable" in output, errors
    if size == 0:
        # No Python timeout or heartbeat runs while this poll owns the GIL.
        assert expired and "returned" not in output and "tick" not in output
    else:
        assert not expired and code == 0 and "returned" in output, (output, errors)


def test_frozen_driver_is_reaped_without_freezing_the_gateway(cluster, tmp_path):
    # Execute the real isolated listener body, injecting the same FD reuse at
    # its actual select boundary. Only its path is replaced, never its driver.
    child_source = Path(event_listener.__file__).read_text()
    injection = "\nimport psycopg2\nkwargs = request['kwargs']\nsize = 0\n" + _REUSE
    child_source = child_source.replace('    sys.path.extend(request["path"])',
        '    sys.path.extend(request["path"])\n' + textwrap.indent(injection, "    "))
    child = tmp_path / "frozen_listener.py"
    child.write_text(child_source)
    script = textwrap.dedent("""
        import json, sys, time, threading
        from pathlib import Path
        from app.core import event_listener as listener, event_gateway as eg
        request = json.load(sys.stdin)
        listener._CHILD = Path(request["child"])
        listener._HEARTBEAT_SECONDS = .5
        type(eg.settings).get_database_connection = lambda self: request["kwargs"]
        gateway = eg.EventGateway()
        gateway._sleep = lambda _: setattr(gateway, "_stop", True)
        started = time.monotonic()
        thread = threading.Thread(target=gateway._listen_loop, daemon=True)
        thread.start()
        ticks = 0
        while thread.is_alive():
            time.sleep(.01)
            ticks += 1
        print(json.dumps({"ticks": ticks, "elapsed": time.monotonic() - started,
                          "connected": gateway.is_connected()}))
    """)
    expired, code, output, errors = run_child(script, {"kwargs": kwargs_for(cluster), "child": str(child)})
    assert not expired and code == 0, (output, errors)
    result = json.loads(output)
    assert result["ticks"] >= 20 and result["elapsed"] < 4 and not result["connected"]
    assert "heartbeat expired" in errors


def test_real_listener_delivers_then_recovers_from_child_exit(cluster):
    listener = event_listener.NotificationListener(kwargs_for(cluster))
    try:
        import time
        deadline = time.monotonic() + 5
        while not listener.connected:
            listener.read()
            assert time.monotonic() < deadline
            time.sleep(.01)
        sender = pg_connect(cluster)
        try:
            sender.cursor().execute("SELECT pg_notify('session_events', %s)", ('{"session_id":"chat","max_seq":8}',))
        finally:
            sender.close()
        payloads = []
        while not payloads:
            payloads, overflow = listener.read()
            assert not overflow and time.monotonic() < deadline
            time.sleep(.01)
        assert json.loads(payloads[0]) == {"session_id": "chat", "max_seq": 8}
        listener._child.kill()
        listener._child.wait(timeout=2)
        with pytest.raises(RuntimeError, match="exited"):
            listener.read()
    finally:
        listener.close()


def test_mailbox_overflow_forces_reconcile_without_unbounded_growth():
    import mmap
    import time
    from unittest.mock import Mock

    listener = object.__new__(event_listener.NotificationListener)
    listener._memory = mmap.mmap(-1, event_listener._SIZE)
    listener._child = Mock()
    listener._child.poll.return_value = None
    listener._seen = 0
    listener._started = time.monotonic()
    try:
        event_listener._store(listener._memory, 8, time.monotonic_ns())
        for sequence in range(1, event_listener._SLOTS + 10):
            event_listener._publish(listener._memory, sequence, str(sequence))
        payloads, overflow = listener.read()
        assert overflow and len(payloads) == event_listener._SLOTS
        assert payloads[-1] == str(event_listener._SLOTS + 9)
        assert listener.read() == ([], False)
    finally:
        listener._memory.close()
