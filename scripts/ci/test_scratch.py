"""One scratch owner per pytest controller; xdist workers inherit its basetemp.

A user-supplied --basetemp remains user-owned. SIGKILL cannot run finalizers;
normal failures, collection errors, SIGTERM and pytest's Ctrl-C path run them.
"""
from contextlib import ExitStack
import os

import pytest

from app.core.temp_scratch import cleanup_on_sigterm, scratch_directory


def _stop_session(signum, frame):
    pytest.exit("SIGTERM", returncode=128 + signum)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput") or config.option.basetemp is not None:
        return
    stack = ExitStack()
    config.add_cleanup(stack.close)
    stack.enter_context(cleanup_on_sigterm(handler=_stop_session))
    directory = stack.enter_context(scratch_directory(
        "ai-shuffle-pytest-", root=os.environ.get("AI_SHUFFLE_TEST_SCRATCH_ROOT"),
        warn_on_cleanup_race=True))
    # Keep ownership alive through all fixture/worker finalizers.
    config._shuffle_scratch = stack
    config.option.basetemp = directory + "/pytest"
