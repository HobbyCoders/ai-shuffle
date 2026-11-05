"""Real Uvicorn/SSE teardown: bounded cancellation still runs ASGI lifespan."""
from __future__ import annotations

import http.client
import json
import signal
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
import asyncio, pathlib, socket, sys
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
            (root/'finite-started').write_text('admitted')
            await asyncio.sleep(6)  # Longer than the shutdown grace: must NOT be cut.
            (root/'finite-completed').write_text('work finished')
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'completed'})
            return
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
def test_gate_refuses_late_work_or_aborts_and_allows_completion_before_sse_teardown(tmp_path, late_finite):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    process = subprocess.Popen([sys.executable, str(script), str(tmp_path), str(ROOT)],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    connections = []
    try:
        wait_for(lambda: (tmp_path / "port").exists())
        port = int((tmp_path / "port").read_text())

        def connect(path):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=9)
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
            response = []

            def late_request():
                connection = connect("/late-finite-request")
                # connect already read headers; the completion marker proves the
                # actual >5s operation, not merely a committed starting row.
                response.append((tmp_path / "finite-completed").read_text())

            worker = threading.Thread(target=late_request)
            worker.start()
            wait_for(lambda: (tmp_path / "finite-started").exists())
            assert not lease.close_admission()  # Arrival after decision aborts pause.
            assert not lease.admission_closed()
            worker.join(9)
            assert not worker.is_alive() and response == ["work finished"]
            wait_for(lease.idle)
        assert lease.close_admission()
        assert lease.admission_closed()
        refused = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connections.append(refused)
        refused.request("POST", "/late-finite-request", body=b"not admitted")
        reply = refused.getresponse()
        assert reply.status == 503 and reply.getheader("Retry-After") == "1"
        assert not late_finite or (tmp_path / "finite-completed").exists()
        if not late_finite:
            assert not (tmp_path / "finite-started").exists()
        process.terminate()
        # Uvicorn re-raises its captured SIGTERM after normal lifespan teardown.
        assert process.wait(timeout=10) == -signal.SIGTERM
        assert (tmp_path / "lifespan-stopped").read_text() == "normal teardown completed"
        assert not late_finite or (tmp_path / "finite-completed").read_text() == "work finished"
    finally:
        for connection in connections:
            connection.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
