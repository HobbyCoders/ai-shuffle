"""Real Uvicorn/SSE teardown: bounded cancellation still runs ASGI lifespan."""
from __future__ import annotations

import http.client
import json
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.devloop import guardian, idle
from scripts.devloop.__main__ import ROOT


SERVER = """
import asyncio, pathlib, socket, sqlite3, sys
sys.path.insert(0, sys.argv[2])
import uvicorn
from scripts.devloop.child import bounded_shutdown
from scripts.devloop.observer import ActivityObserver
root = pathlib.Path(sys.argv[1])
async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type':'lifespan.startup.complete'})
            else:
                (root/'lifespan-stopped').write_text('normal teardown completed')
                wrapped.stopped.set()
                await send({'type':'lifespan.shutdown.complete'})
                return
    else:
        if scope['path'] == '/late-finite-request':
            with sqlite3.connect(root/'durable.sqlite') as db:
                db.execute('CREATE TABLE turns (state TEXT)')
                db.execute("INSERT INTO turns VALUES ('running')")
        await send({'type':'http.response.start','status':200,
                    'headers':[(b'content-type',b'text/event-stream')]})
        await send({'type':'http.response.body','body':b': keepalive\\n\\n','more_body':True})
        await asyncio.Event().wait()
wrapped = ActivityObserver(app, root/'activity.json')
sock = socket.socket()
sock.bind(('127.0.0.1',0)); sock.listen(128); sock.setblocking(False)
(root/'port').write_text(str(sock.getsockname()[1]))
with bounded_shutdown():
    uvicorn.Server(uvicorn.Config(wrapped, log_level='critical', access_log=False)).run(sockets=[sock])
"""


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("real server condition timed out")


@pytest.mark.parametrize("late_finite", [False, True])
def test_cutting_passive_sse_runs_lifespan_and_retains_late_durable_write(tmp_path, late_finite):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    process = subprocess.Popen([sys.executable, str(script), str(tmp_path), str(ROOT)],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    connections = []
    try:
        wait_for(lambda: (tmp_path / "port").exists())
        port = int((tmp_path / "port").read_text())

        def connect(path):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connections.append(connection)
            connection.request("GET", path)
            assert connection.getresponse().status == 200
            return connection

        connect("/api/v1/sessions/settled/events")
        lease = idle.IdleLease(SimpleNamespace(directory=tmp_path,
            data={"app": {"pid": process.pid, "pid_identity": guardian.identity(process.pid)}},
            fake=SimpleNamespace(log_path=tmp_path / "fake.jsonl")), threading.Event(), seconds=0)
        wait_for(lease.idle)  # Real passive stream is open, no finite request pins it.
        if late_finite:
            # Worst-case arrival after final proof: committed work must survive cancellation.
            connect("/late-finite-request")
            wait_for(lambda: json.loads(lease.receipt.read_text())["active"] == 1)
            assert not lease.idle()  # Normal recheck would abort; intentionally simulate the gap.
        process.terminate()
        # Uvicorn re-raises its captured SIGTERM after normal lifespan teardown.
        assert process.wait(timeout=10) == -signal.SIGTERM
        assert (tmp_path / "lifespan-stopped").read_text() == "normal teardown completed"
        if late_finite:
            with sqlite3.connect(tmp_path / "durable.sqlite") as db:
                assert db.execute("SELECT state FROM turns").fetchall() == [("running",)]
    finally:
        for connection in connections:
            connection.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
