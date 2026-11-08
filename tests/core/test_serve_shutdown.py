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
from app.api.sse_utils import PassiveEventResponse
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
    return PassiveEventResponse(events(), media_type='text/event-stream')

serve.run(app, SimpleNamespace(host='127.0.0.1', port=18000, log_level='error'))
"""


QUERY_SETUP = r"""
from app.api import query

# Exercise the actual non-SSE query route with controlled provider work. The
# independent lifespan task above is not responsible for settling this request.
async def api_user():
    return {'id': 'test-user'}
async def ready_leg(*args, **kwargs):
    return None
async def finite_query(**kwargs):
    (root / 'query-started').touch()
    try:
        while not (root / 'shutdown-requested').exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(6)
        (root / 'query-settled').write_text('completed before response')
        return {'response': 'complete', 'session_id': 'test-session', 'metadata': {}}
    except asyncio.CancelledError:
        (root / 'query-cancelled').touch()
        raise
async def finite_query_stream(**kwargs):
    result = await finite_query(**kwargs)
    yield {'type': 'done', **result}
app.dependency_overrides[query.require_api_key] = api_user
query._stream_query_with_failover = finite_query_stream
query.resolve_api_user_profile = lambda *args: 'test-agent'
query._refuse_unready_leg = ready_leg
query._execute_query_with_failover = finite_query
app.include_router(query.router)
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
            assert 'timeout graceful shutdown exceeded' not in log.read_text()
            if open_stream:
                assert (tmp_path / 'stream-closed').exists()
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX SIGTERM contract')
@pytest.mark.parametrize('open_stream', [False, True])
@pytest.mark.parametrize('query_path', ['/api/v1/query', '/api/v1/query/stream'])
def test_finite_query_settles_after_sigterm(tmp_path, open_stream, query_path):
    script = tmp_path / 'server.py'
    script.write_text(SERVER.replace('serve.run(app,', QUERY_SETUP + '\nserve.run(app,'))
    env = dict(os.environ, SHUFFLE_PREVIEW_PORT='off', PYTHONPATH=str(ROOT))
    connection = stream = None
    log = tmp_path / 'server.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                                   cwd=ROOT, env=env, stdout=output, stderr=output)
        try:
            port = wait_for_ready(tmp_path, process)
            if open_stream:
                stream = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                stream.request('GET', '/events')
                assert stream.getresponse().read(1) == b':'
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
            connection.request('POST', query_path, '{"prompt":"finish this turn"}',
                               {'Content-Type': 'application/json'})
            deadline = time.monotonic() + 5
            while not (tmp_path / 'query-started').exists():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            (tmp_path / 'shutdown-requested').touch()
            process.send_signal(signal.SIGTERM)
            response = connection.getresponse()
            assert response.status == 200, log.read_text()
            assert b'complete' in response.read()
            process.wait(timeout=12)
            assert (tmp_path / 'query-settled').exists(), log.read_text()
            assert not (tmp_path / 'query-cancelled').exists(), log.read_text()
            assert (tmp_path / 'cleanup-completed').exists(), log.read_text()
            if open_stream:
                assert (tmp_path / 'stream-closed').exists()
        finally:
            if connection:
                connection.close()
            if stream:
                stream.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
