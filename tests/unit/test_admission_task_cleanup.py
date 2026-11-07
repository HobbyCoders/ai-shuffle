"""Exercise the real shared teardown guard in isolated pytest subprocesses."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("kind", ["owned", "release", "cancelled_release", "completed", "fixture"])
def test_guard_drains_and_names_source_without_poisoning_next_test(pytester, monkeypatch, kind):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    pytester.makeconftest(
        "from tests._admission_cleanup import pytest_fixture_setup, pytest_runtest_teardown"
    )
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

    lease = await admission.acquire("source")
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


async def test_next_test_is_clean():
    assert state["settled"]
    assert not admission._unattended
    if "lease" in state:
        assert state["lease"].exited.is_set()
    lease = await admission.acquire("source")
    await admission.close_and_wait(lease)
""")
    result = pytester.runpytest_subprocess("-q", "--tb=short", timeout=20)
    result.assert_outcomes(passed=2, errors=0 if kind in ("completed", "fixture") else 1)
    if kind not in ("completed", "fixture"):
        result.stdout.fnmatch_lines(["*test_source leaked unfinished admission tasks (drained):*"])
    assert "Event loop is closed" not in result.stdout.str()
