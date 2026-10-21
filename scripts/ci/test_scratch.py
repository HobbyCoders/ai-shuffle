"""One scratch owner per pytest controller; xdist workers inherit its basetemp.

A user-supplied --basetemp remains user-owned. SIGKILL cannot run finalizers;
normal failures, collection errors and pytest's Ctrl-C path do run them.
"""
from contextlib import ExitStack

import pytest

from app.core.temp_scratch import scratch_directory


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if hasattr(config, "workerinput") or config.option.basetemp is not None:
        return
    stack = ExitStack()
    config.add_cleanup(stack.close)
    directory = stack.enter_context(scratch_directory("ai-shuffle-pytest-"))
    # Keep ownership alive through all fixture/worker finalizers.
    config._shuffle_scratch = stack
    config.option.basetemp = directory + "/pytest"
