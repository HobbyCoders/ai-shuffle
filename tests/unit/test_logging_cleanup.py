"""App logging listeners must not persist across tests on the same worker."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def test_interrupted_lifespan_logging_is_cleaned_before_next_test(pytester, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    # tests.conftest adds this checkout's Fluid path on every platform.
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    # Load the real shared teardown, not a copy of the cleanup implementation.
    pytester.makeconftest('pytest_plugins = ["tests.conftest"]')
    pytester.makepyfile("""
import logging

from app.core import error_log_handler

state = {}


def test_interrupted_logging_owner():
    root = logging.getLogger()
    unrelated = logging.NullHandler()
    root.addHandler(unrelated)
    state["unrelated"] = unrelated
    # This is the install that an interrupted app lifespan leaves behind.
    error_log_handler.install_error_log_handler()
    handler = error_log_handler._handler
    state["handler"] = handler
    state["thread"] = handler.listener._thread
    assert handler in root.handlers
    assert state["thread"].is_alive()


def test_next_test_has_no_app_db_logging_listener(caplog):
    root = logging.getLogger()
    handler = state["handler"]
    try:
        assert handler not in root.handlers
        assert error_log_handler._handler is None
        assert not state["thread"].is_alive()
        assert handler.listener._thread is None
        # Cleanup must preserve pytest/caplog and any unrelated logging sinks.
        assert state["unrelated"] in root.handlers
        with caplog.at_level(logging.WARNING):
            logging.getLogger("fluid.providers._transport").warning("next test warning")
        assert [(r.name, r.message) for r in caplog.records] == [
            ("fluid.providers._transport", "next test warning")]
        assert handler.queue.empty()
    finally:
        error_log_handler.uninstall_error_log_handler()
        root.removeHandler(state["unrelated"])
        state["unrelated"].close()
""")
    result = pytester.runpytest_subprocess("-q", "-p", "no:randomly", timeout=30)
    result.assert_outcomes(passed=2)


@pytest.mark.parametrize(("blocked_at", "backlog", "release_on_join"), [
    ("persist", 1, True),
    ("format", 1, True),
    ("persist", 1024, True),
    ("format", 1024, False),
])
def test_blocked_listener_cannot_reach_next_tests_mocks(
    pytester, monkeypatch, blocked_at, backlog, release_on_join,
):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", str(root))
    pytester.makeini("[pytest]\nasyncio_mode=auto")
    pytester.makeconftest('pytest_plugins = ["tests.conftest"]')
    pytester.makepyfile(f"""
import logging
from pathlib import Path
import threading

from app.core import error_log_handler as elh
from app.db import error_logs

state = {{}}


def test_blocked_source(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    state["release"] = release
    state["old_rows"] = []

    def persist(**row):
        if {blocked_at!r} == "persist":
            entered.set()
            release.wait()
        state["old_rows"].append(row)

    def format_exception(info):
        # emit() has entered but has NOT resolved record_error yet.
        entered.set()
        release.wait()
        return "source traceback"

    monkeypatch.setattr(error_logs, "record_error", persist)
    if {blocked_at!r} == "format":
        monkeypatch.setattr(elh._formatter, "formatException", format_exception)
    elh.install_error_log_handler()
    handler = elh._handler
    thread = handler.listener._thread
    state["handler"], state["thread"] = handler, thread
    record = logging.LogRecord("test.source", logging.ERROR, __file__, 1, "source", (), None)
    if {blocked_at!r} == "format":
        record.exc_info = (RuntimeError, RuntimeError("source"), None)
    handler.handle(record)
    assert entered.wait(2)
    for _ in range({backlog}):
        handler.handle(logging.LogRecord("test.source", logging.ERROR, __file__, 1,
                                        "backlog", (), None))
    assert handler.queue.qsize() == {backlog}

    real_join = thread.join
    def join(timeout=None):
        # Production close's 2s join expires. Only the teardown's separate,
        # short final join can release this record before the next test.
        if {release_on_join!r} and timeout is not None and timeout < 2:
            release.set()
        return real_join(timeout)
    monkeypatch.setattr(thread, "join", join)


def test_next_test_uses_fresh_mocks(monkeypatch):
    Path("next-test-ran").touch()
    rows = []
    monkeypatch.setattr(error_logs, "record_error", lambda **row: rows.append(row))
    try:
        state["release"].set()
        state["thread"].join(timeout=2)
        assert not state["thread"].is_alive()
        assert rows == []
        assert elh._handler is None
        assert state["handler"] not in logging.getLogger().handlers
        assert state["handler"].listener.handlers == ()
        assert len(state["old_rows"]) == 1
    finally:
        state["release"].set()
""")
    result = pytester.runpytest_subprocess("-q", "-p", "no:randomly", timeout=15)
    if release_on_join:
        result.assert_outcomes(passed=2)
    else:
        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.stderr.fnmatch_lines([
            "*test_blocked_source*app error-log listener still alive*refusing to run another test*",
        ])
        assert not (pytester.path / "next-test-ran").exists()
