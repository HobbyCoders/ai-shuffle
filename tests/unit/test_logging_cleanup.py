"""App logging listeners must not persist across tests on the same worker."""
from pathlib import Path

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
