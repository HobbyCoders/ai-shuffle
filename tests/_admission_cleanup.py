"""Keep admission-owned test tasks inside their event-loop fixture's lifetime."""
import asyncio
import os
import sys
import threading
from functools import partial

import pytest

_CLEANUP_SECONDS = 5.0
_deadlines = {}


def _abort_cleanup(loop, nodeid, initial, seconds, capture):
    # pytest.fail/exit still run Runner.close(), whose cancellation and executor
    # shutdown can wait forever. A genuine immortal thread requires terminating
    # this test process; do not hide it by removing executor/thread registrations.
    try:
        # FD capture otherwise takes this diagnostic into a buffer that the
        # emergency exit cannot replay. No resume: this process is terminating.
        if capture is not None:
            capture.suspend(in_=True)
        os.write(2, (f"\nERROR {nodeid}: admission cleanup exceeded {seconds:g}s "
                     "(drain + Runner cleanup)\n").encode())
        admission = sys.modules.get("app.core.provider.session_admission")
        owned = tuple(admission._unattended) if admission is not None else ()
        tasks = set(initial) | {task for task in owned if task.get_loop() is loop}
        tasks.update(asyncio.all_tasks(loop))
        for task in sorted(tasks, key=repr):
            os.write(2, ("  " + repr(task) + "\n").encode(errors="replace"))
    finally:
        os._exit(int(pytest.ExitCode.TESTS_FAILED))


async def _drain(admission, loop):
    leaked = set()
    while owned := {task for task in tuple(admission._unattended) if task.get_loop() is loop}:
        leaked.update(task for task in owned if not task.done())
        # Do not cancel: release owners deliberately shield real write threads.
        # Include replacement owners spawned by cancellation callbacks, too.
        await asyncio.gather(*owned, return_exceptions=True)
        await asyncio.sleep(0)
    return leaked


def _check_loop(loop, nodeid, fixturedef, capture):
    admission = sys.modules.get("app.core.provider.session_admission")
    owned = tuple(task for task in tuple(admission._unattended)
                  if task.get_loop() is loop) if admission else ()
    if not owned:
        return
    # One wall-clock budget, not a fresh timeout per owner or cancellation. Keep
    # it armed until the fixture's Runner/loop cleanup has also finished.
    deadline = threading.Timer(_CLEANUP_SECONDS, _abort_cleanup,
                               args=(loop, nodeid, owned, _CLEANUP_SECONDS, capture))
    deadline.daemon = True
    _deadlines[fixturedef] = deadline
    deadline.start()
    leaked = loop.run_until_complete(_drain(admission, loop))
    if leaked:
        names = sorted(task.get_coro().__qualname__ for task in leaked)
        pytest.fail(f"{nodeid} leaked unfinished admission tasks (drained): " + ", ".join(names),
                    pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    value = yield
    if isinstance(value, asyncio.Runner):
        loop = value.get_loop()
    elif isinstance(value, asyncio.AbstractEventLoop):
        loop = value
    else:
        return value
    # Add the check AFTER the loop fixture's own yield-finalizer was registered:
    # LIFO makes it run before Runner.close(), but after dependent fixtures have
    # cleaned up. Do not add an async fixture/extra loop to synchronous tests or
    # touch TestClient loops still running their independently owned lifespan.
    capture = request.config.pluginmanager.getplugin("capturemanager")
    request.addfinalizer(partial(_check_loop, loop, request.node.nodeid, fixturedef, capture))
    return value


@pytest.hookimpl(trylast=True)
def pytest_fixture_post_finalizer(fixturedef, request):
    deadline = _deadlines.pop(fixturedef, None)
    if deadline is not None:
        deadline.cancel()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    try:
        return (yield)
    finally:
        # Also attribute explicitly closed, non-fixture loops to their source.
        # They cannot be drained now; fail visibly and discard the stale registry
        # entries so a later TestClient shutdown cannot try to cancel them.
        admission = sys.modules.get("app.core.provider.session_admission")
        if admission is not None:
            closed = {task for task in tuple(admission._unattended) if task.get_loop().is_closed()}
            admission._unattended.difference_update(closed)
            if any(not task.done() for task in closed):
                pytest.fail(f"{item.nodeid} leaked admission tasks on an already-closed loop",
                            pytrace=False)
