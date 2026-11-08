"""Real production server teardown with an HTTP event stream left open."""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVER = r"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from app import serve

root = Path(sys.argv[1])
# Use real ingress/server configuration, but let the OS select private ports.
original_bind = serve.bind_listener
ports = []
def bind(host, port):
    sock = original_bind(host, 0)
    ports.append(sock.getsockname()[1])
    return sock
serve.bind_listener = bind

@asynccontextmanager
async def lifespan(app):
    stop = asyncio.Event()
    async def running_turn():
        try:
            await stop.wait()
            (root / 'turn-settled').write_text('normal shutdown')
        except asyncio.CancelledError:
            (root / 'turn-cancelled').touch()
            raise
    turn = asyncio.create_task(running_turn())
    (root / 'port').write_text(str(ports[0]))
    yield
    # A connection drain must not cancel independent work before lifespan.
    (root / 'cleanup-entered').touch()
    stop.set()
    await turn
    (root / 'cleanup-completed').touch()

app = FastAPI(lifespan=lifespan)
@app.get('/ready')
async def ready():
    return {'ready': True}

@app.get('/events')
async def stream():
    async def events():
        try:
            yield ': connected\n\n'
            await asyncio.Event().wait()
        finally:
            (root / 'stream-closed').touch()
    return StreamingResponse(events(), media_type='text/event-stream')

serve.run(app, SimpleNamespace(host='127.0.0.1', port=18000, log_level='critical'))
"""


def wait_for_ready(root, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, 'server exited before readiness'
        try:
            port = int((root / 'port').read_text())
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                conn.request('GET', '/ready')
                if conn.getresponse().status == 200:
                    return port
            finally:
                conn.close()
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    pytest.fail('production server did not become ready')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX SIGTERM contract; Windows stop is platform-specific')
@pytest.mark.parametrize('open_stream', [False, True])
def test_single_sigterm_drains_connections_then_reaches_lifespan(tmp_path, open_stream):
    script = tmp_path / 'server.py'
    script.write_text(SERVER)
    env = dict(os.environ, SHUFFLE_PREVIEW_PORT='off', PYTHONPATH=str(ROOT))
    log = tmp_path / 'server.log'
    connection = None
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                                   cwd=ROOT, env=env, stdout=output, stderr=output)
        try:
            port = wait_for_ready(tmp_path, process)
            if open_stream:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                connection.request('GET', '/events')
                response = connection.getresponse()
                assert response.status == 200
                assert response.read(1) == b':'  # A real response stays open during SIGTERM.
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                pytest.fail('open connection prevented lifespan shutdown within 12 seconds')
            elapsed = time.monotonic() - started
            assert elapsed < 12, log.read_text()
            assert process.returncode in (0, -signal.SIGTERM), log.read_text()
            assert (tmp_path / 'cleanup-completed').exists(), log.read_text()
            assert (tmp_path / 'turn-settled').read_text() == 'normal shutdown'
            assert not (tmp_path / 'turn-cancelled').exists()
            if open_stream:
                assert (tmp_path / 'stream-closed').exists()
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
