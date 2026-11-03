"""A probe frozen inside the real driver, with the GIL held, on real PostgreSQL."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.db import backend_probe
from tests.db.test_commit_deadline_postgres import private_db, private_pool  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="replaces a raw socket descriptor")

# ---------------------------------------------------------------------------
# The real driver, really blocking with the GIL held
# ---------------------------------------------------------------------------

# Real libpq, in a call that does not release the GIL, on a real blocking socket that
# replaced the connection's number: the failure no clock in the same process can bound.
_FREEZE = textwrap.dedent('''
    import os, select, socket, sys
    import psycopg2

    def freeze(kwargs, marker):
        conn = psycopg2.connect(**kwargs, async_=1)
        while True:
            state = conn.poll()
            if state == psycopg2.extensions.POLL_OK:
                break
            select.select(*(([conn], [], []) if state == psycopg2.extensions.POLL_READ
                            else ([], [conn], [])), 5)
        conn.cursor().execute("SELECT 1")
        stranger, peer = socket.socketpair()
        stranger.setblocking(True)
        os.dup2(stranger.fileno(), conn.fileno())
        open(marker, "w").close()
        conn.poll()
''')

# Runs in a process of its own so that a freeze here fails this test instead of hanging
# pytest: nothing inside a frozen interpreter can run, pytest-timeout included.
_HARNESS = _FREEZE + textwrap.dedent('''
    import json, threading, time
    sys.path.insert(0, os.getcwd())
    request = json.load(sys.stdin)
    finished = {}

    def run():
        from pathlib import Path
        from app.db import backend_probe
        import psycopg2
        # Whichever process would call the driver freezes. A probe in this process would
        # be frozen by this; the child is frozen by the script that replaces its body.
        real_connect = psycopg2.connect
        psycopg2.connect = lambda *a, **k: (
            setattr(psycopg2, "connect", real_connect), freeze(k, request["parent_marker"]))
        backend_probe._CHILD = Path(request["child"])
        probe = backend_probe.BackendProbe()
        started = time.monotonic()
        try:
            probe.read([1], (), request["kwargs"], request["seconds"])
            finished["outcome"] = "answered"
        except Exception as exc:
            finished["outcome"] = type(exc).__name__
        finished["seconds"] = time.monotonic() - started
        finished["left_running"] = probe._child is not None

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worst, last, ticks = 0.0, time.monotonic(), 0
    while worker.is_alive():
        time.sleep(0.01)
        now = time.monotonic()
        worst, last, ticks = max(worst, now - last), now, ticks + 1
    print(json.dumps({**finished, "worst_tick": worst, "ticks": ticks}))
''')

# Started like the real child, with -I -S, so it finds the driver the same way.
_FROZEN_CHILD_START = textwrap.dedent('''
    import json, sys
    request = json.load(sys.stdin)
    sys.path.extend(request["path"])
''')
_FROZEN_CHILD_END = 'freeze(request["kwargs"], {marker!r})\n'

# Control: the same call inside one interpreter stops its main thread.
_CONTROL = _FREEZE + textwrap.dedent('''
    import json, threading, time
    kwargs = json.load(sys.stdin)
    threading.Thread(target=freeze, args=(kwargs, sys.argv[1]), daemon=True).start()
    while True:
        time.sleep(0.01)
        print(time.monotonic(), flush=True)
''')


@pytest.fixture
def real_pg_kwargs(private_pool: Any) -> dict[str, Any]:  # noqa: F811
    return {k: v for k, v in private_pool._kwargs.items() if k != "application_name"}


def test_control_the_blocking_call_really_freezes_the_interpreter_that_makes_it(
    real_pg_kwargs: dict[str, Any], tmp_path: Path,
) -> None:
    """Without this the test below could pass on a call that releases the GIL."""
    marker = tmp_path / "frozen"
    process = subprocess.Popen(
        [sys.executable, "-c", _CONTROL, str(marker)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    process.stdin.write(json.dumps(real_pg_kwargs).encode())
    process.stdin.close()
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: [lines.append(line.decode()) for line in process.stdout], daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        time.sleep(0.2)
        before = len(lines)
        time.sleep(1.5)
        assert len(lines) - before <= 2, "the main thread kept running: this call is no proof"
    finally:
        process.kill()
        process.wait(5)


def test_a_probe_frozen_inside_the_real_driver_never_stops_the_worker(
    real_pg_kwargs: dict[str, Any], tmp_path: Path,
) -> None:
    """The review's freeze: the main loop keeps running, the kill lands, nothing is left."""
    child_marker, parent_marker = tmp_path / "child-frozen", tmp_path / "parent-frozen"
    child = tmp_path / "frozen_child.py"
    child.write_text(
        _FROZEN_CHILD_START + _FREEZE + _FROZEN_CHILD_END.format(marker=str(child_marker)))
    request = {"kwargs": real_pg_kwargs, "child": str(child), "seconds": 1.5,
               "parent_marker": str(parent_marker)}
    root = Path(backend_probe.__file__).parents[2]
    # A hard limit from outside: a freeze here must fail, and a frozen process cannot help.
    result = subprocess.run(
        [sys.executable, "-c", _HARNESS], input=json.dumps(request).encode(), cwd=root,
        capture_output=True, timeout=30, env={**os.environ, "PYTHONPATH": str(root)})
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    report = json.loads(result.stdout.decode().splitlines()[-1])
    assert child_marker.exists(), "the child never reached the blocking call"
    assert not parent_marker.exists()
    assert report["outcome"] == "TimeoutError"
    assert report["seconds"] < 3.5
    assert not report["left_running"]
    assert report["ticks"] > 50
    assert report["worst_tick"] < 0.5, f"main loop stopped for {report['worst_tick']:.2f}s"
