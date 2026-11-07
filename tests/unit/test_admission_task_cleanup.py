"""Exercise the real shared teardown guard in isolated pytest subprocesses."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("kind", ["owned", "release", "cancelled_release", "completed", "fixture", "rewind_helper"])
def test_guard_drains_and_names_source_without_poisoning_next_test(pytester, monkeypatch, kind):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    pytester.makeconftest('pytest_plugins = ["tests._admission_cleanup"]')
    pytester.makepyfile(f"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from app.core.provider import session_admission as admission
from app.db import async_database as adb

state = {{}}


@pytest.fixture(autouse=True)
async def managed_work():
    yield
    if state.pop("release_in_fixture", False):
        state["release"].set()
        await admission.close_and_wait(state["lease"])


async def test_source(monkeypatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    kind = {kind!r}
    loop = asyncio.get_running_loop()
    if kind in ("owned", "completed"):
        async def work():
            if kind == "owned":
                await asyncio.sleep(0.02)
            state["settled"] = True
        task = admission._own(work())
        if kind == "completed":
            await task
        return

    lease = await (admission.begin_rewind("source") if kind == "rewind_helper"
                   else admission.acquire("source"))
    state["lease"] = lease
    started, release = threading.Event(), threading.Event()
    def write():
        started.set()
        assert release.wait(2)
        state["settled"] = True
    issuing = asyncio.create_task(adb.run_sync(write))
    assert await asyncio.to_thread(started.wait, 2)
    issuing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await issuing
    admission.close(lease)
    if kind == "cancelled_release":
        owner, = admission._unattended
        owner.cancel()  # Its done callback creates another owner during drain.
    # Only a still-open loop can release the real thread. The guard must drain
    # before asyncio's fixture closes it, then fail this source, not the victim.
    if kind == "fixture":
        state["release_in_fixture"] = True
        state["release"] = release
    else:
        loop.call_later(0.02, release.set)
    if kind == "rewind_helper":
        from tests.rewind_helpers import wait_for_rewind_release
        owners = tuple(admission._unattended)
        await wait_for_rewind_release("source")
        assert state["settled"] and lease.exited.is_set()
        assert all(not task.cancelled() for task in owners)


async def test_next_test_is_clean():
    from tests import _admission_cleanup as cleanup
    assert not cleanup._deadlines, "the previous Runner finished within its deadline"
    assert state["settled"]
    assert not admission._unattended
    if "lease" in state:
        assert state["lease"].exited.is_set()
    lease = await admission.acquire("source")
    await admission.close_and_wait(lease)
""")
    result = pytester.runpytest_subprocess("-q", "--tb=short", timeout=20)
    result.assert_outcomes(passed=2, errors=0 if kind in ("completed", "fixture", "rewind_helper") else 1)
    if kind not in ("completed", "fixture", "rewind_helper"):
        result.stdout.fnmatch_lines(["*test_source leaked unfinished admission tasks (drained):*"])
    assert "Event loop is closed" not in result.stdout.str()


@pytest.mark.parametrize("kind", ["owner", "shielded_thread", "runner_shutdown"])
def test_stalled_cleanup_exits_with_source_and_tasks(pytester, monkeypatch, kind):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    pytester.makeconftest("""
from tests import _admission_cleanup as cleanup
cleanup._CLEANUP_SECONDS = 0.2
pytest_plugins = ["tests._admission_cleanup"]
""")
    pytester.makepyfile(f"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from app.core.provider import session_admission as admission
from app.db import async_database as adb


async def test_stalled_source(monkeypatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    loop = asyncio.get_running_loop()
    kind = {kind!r}
    if kind == "owner":
        async def stalled_owner():
            await asyncio.Event().wait()
        admission._own(stalled_owner())
        return

    started, never = threading.Event(), threading.Event()
    def stalled_write():
        started.set()
        never.wait()  # Only exiting the child process can stop this thread.

    if kind == "runner_shutdown":
        async def issued_thread():
            loop.run_in_executor(None, stalled_write)
            await asyncio.sleep(0.02)
        admission._own(issued_thread())
        return  # Owner drains, but Runner.close() cannot join the executor.

    lease = await admission.acquire("stalled")
    issuing = asyncio.create_task(adb.run_sync(stalled_write))
    assert await asyncio.to_thread(started.wait, 2)
    issuing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await issuing
    admission.close(lease)
""")
    # This outer cap is an assertion, not cleanup: the child must stop itself
    # after its single 0.2s teardown deadline, even with an immortal thread.
    result = pytester.runpytest_subprocess("-q", "--tb=short", timeout=8)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stderr.fnmatch_lines([
        "*test_stalled_source*admission cleanup exceeded 0.2s*",
        "*<Task*",
    ])


@pytest.mark.parametrize("kind", ["body", "fixture", "portal", "short_lived_loop"])
def test_rewind_helper_stalled_write_exits_with_source_and_tasks(pytester, monkeypatch, kind):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    pytester.makeconftest("""
from tests import _admission_cleanup as cleanup
cleanup._CLEANUP_SECONDS = 0.2
pytest_plugins = ["tests._admission_cleanup"]
""")
    pytester.makepyfile(f"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from app.core.provider import session_admission as admission
from app.db import async_database as adb
from tests.rewind_helpers import wait_for_rewind_release


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))


async def start_stalled_rewind():
    lease = await admission.begin_rewind("stalled")
    started, never = threading.Event(), threading.Event()
    def stalled_write():
        started.set()
        never.wait()
    issuing = asyncio.create_task(adb.run_sync(stalled_write))
    assert await asyncio.to_thread(started.wait, 2)
    issuing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await issuing
    admission.close(lease)


@pytest.fixture
async def managed_rewind():
    await start_stalled_rewind()
    yield
    await wait_for_rewind_release("stalled")


kind = {kind!r}
if kind == "body":
    async def test_stalled_rewind_source():
        await start_stalled_rewind()
        await wait_for_rewind_release("stalled")
elif kind == "fixture":
    async def test_stalled_rewind_source(managed_rewind):
        pass
elif kind == "portal":
    def test_stalled_rewind_source():
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        with TestClient(Starlette()) as client:
            client.portal.call(start_stalled_rewind)
            client.portal.call(wait_for_rewind_release, "stalled")
else:
    def test_stalled_rewind_source():
        async def run():
            await start_stalled_rewind()
            await wait_for_rewind_release("stalled")
        asyncio.run(run())
""")
    result = pytester.runpytest_subprocess("-q", "--tb=short", timeout=8)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stderr.fnmatch_lines([
        "*test_stalled_rewind_source*admission cleanup exceeded 0.2s*",
        "*_close_after_writes*",
    ])
