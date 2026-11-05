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
from unittest.mock import Mock

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
class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            # Startup runs inside Uvicorn's installed signal-handler context.
            (root/'server-ready').write_text('startup and signal handlers ready')
with bounded_shutdown():
    server = ReadyServer(uvicorn.Config(wrapped, log_level='critical', access_log=False))
    if len(sys.argv) > 3:
        server.handle_exit = lambda *args: None  # Real hung-child/deadline case.
    server.run(sockets=[sock])
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
        wait_for(lambda: (tmp_path / "server-ready").exists())
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
        assert lease.commit_admission()
        assert lease.admission_closed(committed=True)
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



def test_real_guardian_ends_committed_child_after_launcher_sigkill(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    server = tmp_path / "server"
    server.mkdir(mode=0o700)
    launcher = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                start_new_session=True)
    process = subprocess.Popen([sys.executable, str(script), str(server), str(ROOT)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    connection = None
    try:
        wait_for(lambda: (server / "server-ready").exists())
        lease = idle.IdleLease(SimpleNamespace(directory=server,
            data={"app": {"pid": process.pid, "pid_identity": guardian.identity(process.pid)}},
            fake=SimpleNamespace(log_path=server / "fake.jsonl")), threading.Event(), seconds=0)
        wait_for(lease.idle)
        assert lease.close_admission() and lease.commit_admission()
        lease.open_admission()  # A committed gate refuses even if its command disappears.
        wait_for(lambda: not lease.gate_request.exists() and lease.admission_closed(committed=True))
        connection = http.client.HTTPConnection("127.0.0.1", int((server / "port").read_text()), timeout=3)
        connection.request("GET", "/late-finite-request")
        assert connection.getresponse().status == 503
        assert not (server / "finite-started").exists()
        status = tmp_path / "status.json"
        status.write_text(json.dumps({"pid": launcher.pid,
            "pid_identity": guardian.identity(launcher.pid), "state": "pausing",
            "app": {"pid": process.pid, "pid_identity": guardian.identity(process.pid)}}))
        guard = guardian.spawn_guardian(status, server, launcher_pid=launcher.pid)
        assert guard is not None
        launcher.kill()
        launcher.wait(timeout=3)
        wait_for(lambda: process.poll() is not None and not status.exists())
        assert process.returncode == -signal.SIGKILL
        wait_for(lambda: not guardian.alive(guard["pid"], guard["pid_identity"]))
    finally:
        if connection is not None:
            connection.close()
        for child in (launcher, process):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)



def test_real_committed_hung_child_keeps_refusing_in_pause_failed(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    children, failures, clock_offset = [], [], [0]
    stopped = threading.Event()
    # Do not spend a short shutdown deadline on startup/admission handshakes.
    # Only the supervisor clock jumps, synchronously after the real SIGTERM.
    monkeypatch.setattr(idle, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + clock_offset[0]))
    obj = SimpleNamespace(directory=tmp_path, data={"state": "starting", "app": {}},
        args=SimpleNamespace(ttl_seconds=60, host="127.0.0.1"), port=0, vite_port=None,
        fake=SimpleNamespace(log_path=tmp_path / "fake.jsonl"), record_postgres=Mock())
    original_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original_popen([sys.executable, str(script), str(tmp_path), str(ROOT), "hang"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        real_signal = process.send_signal

        def signal_and_expire(signum):
            real_signal(signum)
            if signum == signal.SIGTERM:
                clock_offset[0] += idle.PAUSE_SECONDS + 1

        process.send_signal = Mock(side_effect=signal_and_expire)
        children.append(process)
        wait_for(lambda: (tmp_path / "server-ready").exists())
        return process

    def write(**changes):
        obj.data.update(changes)
        if changes.get("state") == "pause-failed":
            record = json.loads((tmp_path / "activity.json").read_text())
            assert record["gate_state"] == "committed" and children[0].poll() is None
            children[0].send_signal.assert_called_once_with(signal.SIGTERM)
            connection = http.client.HTTPConnection("127.0.0.1", int((tmp_path / "port").read_text()), timeout=3)
            try:
                connection.request("POST", "/late-finite-request", body=b"must be refused")
                assert connection.getresponse().status == 503
                assert not (tmp_path / "finite-started").exists()
            finally:
                connection.close()
            assert "keeps refusing" in obj.data["pause_error"]
            failures.append("committed child refused")
            stopped.set()  # Exercise normal down/TTL final teardown next.

    def on_start(process):
        obj.write(app={"pid": process.pid, "pid_identity": guardian.identity(process.pid)}, state="ready")

    obj.write, obj.on_app_start = write, on_start
    obj.on_app_exit, obj.retire = Mock(), lambda: obj.write(state="stopping")
    db = SimpleNamespace(open=Mock(), publish_descriptor=Mock(), close=Mock(), process=Mock(),
                         query=Mock(return_value="f"))
    lease = idle.IdleLease(obj, stopped, seconds=0)
    monkeypatch.setattr(idle.subprocess, "Popen", spawn)
    try:
        assert idle.supervise(obj, {}, db, lease, threading.Event(), start_vite=Mock(), stop_vite=Mock()) == 1
        assert failures == ["committed child refused"]
        assert len(children) == 1 and children[0].returncode == -signal.SIGKILL
        db.open.assert_called_once()
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
