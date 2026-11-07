"""Keep admission-owned test tasks inside their event-loop fixture's lifetime."""
import asyncio
import os
import sys
import threading
from contextlib import contextmanager
from functools import partial

import pytest

_CLEANUP_SECONDS = 5.0
_deadlines = {}
_current_item = None


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


def _start_deadline(key, loop, nodeid, initial, capture):
    deadline = threading.Timer(_CLEANUP_SECONDS, _abort_cleanup,
                               args=(loop, nodeid, initial, _CLEANUP_SECONDS, capture))
    deadline.daemon = True
    _deadlines[key] = deadline
    deadline.start()


def _cancel_deadline(key):
    deadline = _deadlines.pop(key, None)
    if deadline is not None:
        deadline.cancel()


@contextmanager
def cleanup_deadline(loop):
    """Bound an explicit release wait, including non-fixture/TestClient loops."""
    item = _current_item
    nodeid = item.nodeid if item is not None else os.environ.get(
        "PYTEST_CURRENT_TEST", "explicit admission cleanup",
    ).rsplit(" (", 1)[0]
    capture = item.config.pluginmanager.getplugin("capturemanager") if item is not None else None
    key = object()
    _start_deadline(key, loop, nodeid, (), capture)
    try:
        yield
    finally:
        _cancel_deadline(key)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    global _current_item
    previous = _current_item
    _current_item = item
    try:
        return (yield)
    finally:
        _current_item = previous


async def wait_for_lease_release(lease):
    from app.core.provider import session_admission

    with cleanup_deadline(asyncio.get_running_loop()):
        await session_admission.close_and_wait(lease)


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
    _start_deadline(fixturedef, loop, nodeid, owned, capture)
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
    _cancel_deadline(fixturedef)


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
