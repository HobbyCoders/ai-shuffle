"""Keep admission-owned test tasks inside their event-loop fixture's lifetime."""
import asyncio
import sys
from functools import partial

import pytest


async def _drain(admission, loop):
    leaked = set()
    while owned := {task for task in tuple(admission._unattended) if task.get_loop() is loop}:
        leaked.update(task for task in owned if not task.done())
        # Do not cancel: release owners deliberately shield real write threads.
        # Include replacement owners spawned by cancellation callbacks, too.
        await asyncio.gather(*owned, return_exceptions=True)
        await asyncio.sleep(0)
    return leaked


def _check_loop(loop, nodeid):
    admission = sys.modules.get("app.core.provider.session_admission")
    if admission is None or not admission._unattended:
        return
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
    request.addfinalizer(partial(_check_loop, loop, request.node.nodeid))
    return value


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
