"""One scratch owner per pytest controller; xdist workers inherit its basetemp.

A user-supplied --basetemp remains user-owned. SIGKILL cannot run finalizers;
normal failures, collection errors, SIGTERM and pytest's Ctrl-C path run them.
Transient cleanup refills warn after recovery; persistent leftovers still fail.
"""
from contextlib import ExitStack, contextmanager
import errno
from itertools import islice
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Iterator

import pytest

from app.core.temp_scratch import MARKER, cleanup_on_sigterm, scratch_directory


def _retry_refilled_scratch(directory: Path, receipt: bytes | None, error: OSError) -> None:
    # Only a transient concurrent refill is recoverable here. Persistent refills
    # and all other cleanup errors still fail rather than conceal a real leak.
    for delay in (0.05, 0.1, 0.2):
        time.sleep(delay)
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            if directory.exists():
                raise
        except OSError as retry_error:
            if retry_error.errno != errno.ENOTEMPTY:
                raise
            continue
        print(f"WARNING: pytest scratch refilled during cleanup: {directory} "
              f"({error}); removed after retry", file=sys.stderr)
        return

    # rmtree may have removed the receipt before a child refilled basetemp.
    # Keep abandoned owned scratch eligible for the conservative app reaper.
    if receipt is not None:
        try:
            (directory / MARKER).write_bytes(receipt)
        except OSError as diagnostic_error:
            print(f"NOTE: pytest scratch receipt restore failed: {diagnostic_error}",
                  file=sys.stderr)
    entries = []
    for parent in (directory, directory / "pytest"):
        try:
            entries.extend(str(path.relative_to(directory))
                           for path in islice(parent.iterdir(), 5))
        except OSError as diagnostic_error:
            print(f"NOTE: pytest scratch listing failed for {parent}: {diagnostic_error}",
                  file=sys.stderr)
    # This runs after pytest unconfigure, outside the warnings summary. Report
    # leftovers explicitly, then preserve the original failure: a persistent
    # leaking writer must still make CI red, even if diagnostics also fail.
    print(f"WARNING: pytest scratch refilled during cleanup: {directory} "
          f"({error}); leftover after 3 retries; entries: {entries!r}", file=sys.stderr)
    raise error


@contextmanager
def _session_scratch() -> Iterator[str]:
    stack = ExitStack()
    receipt = None
    try:
        directory = stack.enter_context(scratch_directory(
            "ai-shuffle-pytest-", root=os.environ.get("AI_SHUFFLE_TEST_SCRATCH_ROOT")))
        marker = Path(directory) / MARKER
        if marker.exists():
            receipt = marker.read_bytes()
        yield directory
    finally:
        try:
            stack.close()
        except OSError as error:
            if error.errno != errno.ENOTEMPTY:
                raise
            _retry_refilled_scratch(Path(directory), receipt, error)


def _stop_session(signum, frame):
    pytest.exit("SIGTERM", returncode=128 + signum)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if hasattr(config, "workerinput") or config.option.basetemp is not None:
        return
    stack = ExitStack()
    config.add_cleanup(stack.close)
    stack.enter_context(cleanup_on_sigterm(handler=_stop_session))
    directory = stack.enter_context(_session_scratch())
    # Keep ownership alive through all fixture/worker finalizers.
    config._shuffle_scratch = stack
    config.option.basetemp = directory + "/pytest"
