"""Request settlement must fit inside the production supervisor's stop budget."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.core.test_serve_shutdown import ROOT, SERVER, QUERY_SETUP, wait_for_ready


def stop_group(process, timeout):
    spec = importlib.util.spec_from_file_location("shutdown_runtime", ROOT / "docker/container_runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.stop_group(process, timeout)


REAL_QUERY_SETUP = r"""

from contextlib import ExitStack
import json
from unittest.mock import AsyncMock
from app.core import query_engine as qe, credential_gate
from app.core.provider.events import SystemMessage, AssistantMessage, TextBlock, ResultMessage
from app.core.provider.session_admission import raw_engine_events
from tests.core.query_engine_flow_harness import engine_patches, fresh_adapter
from app.core import sdk_options

patches = ExitStack()
adapter = fresh_adapter()
boundaries = engine_patches(patches, cwd=str(root), adapter=adapter)
sdk_options.build_session_options = lambda **kwargs: (boundaries.options, {})
credential_gate.refusal_for_options_async = AsyncMock(return_value=None)
@asynccontextmanager
async def no_plan(*args, **kwargs):
    yield None
qe.recorded_turn = no_plan
qe._record_query_message = AsyncMock()

original_sync = qe.run_sync
async def record_sync(fn, *args, **kwargs):
    result = await original_sync(fn, *args, **kwargs)
    if fn.__name__ == 'record_adhoc_turn':
        (root / 'terminal-turn.json').write_text(json.dumps(kwargs, default=str))
        (root / 'query-settled').touch()
    return result
qe.run_sync = record_sync

async def persist(**kwargs):
    from app.db.async_database import run_sync
    import time

    def issued_write():
        (root / 'persistence-started').touch()
        time.sleep(PERSIST_DELAY)
        (root / 'persisted.json').write_text(json.dumps(kwargs))
    await run_sync(issued_write)
qe._persist_turn_results = persist
async def memory(*args):
    # Controlled delay before replication; local memory was already written.
    (root / 'local-memory.md').write_text('durable local memory')
    await asyncio.sleep(MEMORY_DELAY)
    (root / 'memory-published').touch()
qe.push_memory = memory

async def raw_events():
    (root / 'query-started').touch()
    while not (root / 'shutdown-requested').exists():
        await asyncio.sleep(.01)
    if MODE == 'over-budget':
        await asyncio.sleep(35)
    elif MODE == 'finite-15':
        await asyncio.sleep(15)
    else:
        await asyncio.Event().wait()
    yield ResultMessage(subtype='success')
async def messages():
    yield SystemMessage(subtype='init', data={'session_id': 'sdk-shutdown'})
    yield AssistantMessage(content=[TextBlock(text='partial answer')], model='controlled')
    async for event in raw_engine_events(raw_events(), on_shutdown=lambda: (root / 'engine-fenced').touch()):
        yield event
adapter.receive_events = messages
# Keep the real query route and both engine entrypoints; isolate failover/config lookup.
async def real_query(**kwargs):
    try:
        return await qe.execute_query(**kwargs)
    finally:
        (root / 'query-owner-ended').touch()
async def real_stream(**kwargs):
    async for event in qe.stream_query(**kwargs):
        yield event
query._execute_query_with_failover = real_query
query._stream_query_with_failover = real_stream

"""


def run_query_shutdown_probe(tmp_path, mode, budget, query_path, *, container_supervised=False,
                             memory_delay=.15, expect_memory=True, persist_delay=.15):
    script = tmp_path / "server.py"
    setup = QUERY_SETUP + f"\nMODE = {mode!r}\nMEMORY_DELAY = {memory_delay!r}\nPERSIST_DELAY = {persist_delay!r}\n" + REAL_QUERY_SETUP
    script.write_text(SERVER.replace("serve.run(app,", setup + "\nserve.run(app,"))
    env = dict(os.environ, SHUFFLE_PREVIEW_PORT="off", PYTHONPATH=str(ROOT))
    env.pop("SHUFFLE_SUPERVISOR_STOP_SECONDS", None)
    connection = None
    log = tmp_path / "server.log"
    with log.open("w") as output:
        command = [sys.executable, str(script), str(tmp_path)]
        if container_supervised:
            # Drive the real budget producer too, not only a seeded env value.
            spec = importlib.util.spec_from_file_location("query_runtime", ROOT / "docker/container_runtime.py")
            runtime = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runtime)
            process = runtime.start_app(command, ROOT, env)
        else:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output,
                                       stderr=output, start_new_session=True)
        try:
            port = wait_for_ready(tmp_path, process)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
            connection.request("POST", query_path, '{"prompt":"finish this turn"}',
                               {"Content-Type": "application/json"})
            deadline = time.monotonic() + 5
            while not (tmp_path / "query-started").exists():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            (tmp_path / "shutdown-requested").touch()
            started = time.monotonic()
            stop_group(process, budget)
            elapsed = time.monotonic() - started
            assert process.returncode != -signal.SIGKILL, f"killed at {elapsed:.3f}s; {log.read_text()}"
            assert elapsed < budget, log.read_text()
            assert (tmp_path / "cleanup-completed").exists(), log.read_text()
            assert (tmp_path / "query-settled").exists(), log.read_text()
            assert (tmp_path / "engine-fenced").exists() is (mode != "finite-15"), log.read_text()
            assert (tmp_path / "memory-published").exists() is expect_memory, log.read_text()
            assert (tmp_path / "local-memory.md").read_text() == "durable local memory"
            if query_path == "/api/v1/query":
                assert (tmp_path / "query-owner-ended").exists(), log.read_text()
                if persist_delay == 2 and budget == 10:
                    # Uvicorn disconnected the request BEFORE the terminal.
                    # The query-owned finalizer still saved, published and exited.
                    assert ((tmp_path / "query-owner-ended").stat().st_mtime_ns
                            < (tmp_path / "terminal-turn.json").stat().st_mtime_ns)
            if not expect_memory:
                # Uvicorn may have already closed the connection before its
                # cancelled request task can send a 500; neither is success.
                try:
                    status = connection.getresponse().status
                except http.client.RemoteDisconnected:
                    status = "disconnected"
                assert status in (500, "disconnected")
                assert "exceeded the shutdown reserve" in log.read_text()
            terminal = json.loads((tmp_path / "terminal-turn.json").read_text())
            assert terminal["outcome"] == ("completed" if mode == "finite-15" else "aborted")
            if mode == "finite-15":
                assert elapsed >= 15  # It used the container's grace, not the default cutoff.
            saved = json.loads((tmp_path / "persisted.json").read_text())
            assert saved["sdk_session_id"] == "sdk-shutdown"
            assert (tmp_path / "turn-settled").exists()
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX production stop_group")
@pytest.mark.parametrize("mode,budget,query_path", [
    ("over-budget", 30, "/api/v1/query"),
    ("stalled", 10, "/api/v1/query"),
    ("over-budget", 30, "/api/v1/query/stream"),
    ("stalled", 10, "/api/v1/query/stream"),
])
def test_over_budget_query_settles_before_supervisor_kill(tmp_path, mode, budget, query_path):
    run_query_shutdown_probe(tmp_path, mode, budget, query_path)
