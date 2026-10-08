"""Tests for persistent, crash-surviving file logging (app.core.file_logging).

Covers:
- setup_file_logging attaches a RotatingFileHandler with the configured level,
  rotation sizing, and UTF-8 encoding, under <data_dir>/logs/<instance_id>/app.log
  (per-instance namespacing so installs sharing a data dir never fight over
  rotation).
- the startup banner (pid/version/instance_id) is written to the file.
- the fatal.log boot-check rotates a non-empty previous trace to fatal.log.prev,
  overwrites an older .prev, and surfaces a WARNING (pending → error_log).
- FILE_LOGGING_ENABLED=false disables the handler entirely.
- TESTING=1 makes setup a hard no-op (belt-and-braces: a test run must never
  create/truncate anything in the real data dir).
- importing app.main does NOT run setup (it moved to lifespan startup): a bare
  import at pytest collection previously created the real log dir and truncated
  a genuine production fatal.log.
- shutdown banner flushes to disk so the tail survives a cut-short teardown,
  and setup registers an atexit flush/close.
- the asyncio loop exception handler installs on the RUNNING loop and warns
  (not silently returns) when there is no loop.

The module reads ``app.core.config.settings`` lazily inside each function, so we
patch that singleton with a lightweight fake pointed at a tmp data dir. Each test
detaches any handler it attaches so the root logger is left clean. The conftest
sets TESTING=1 process-wide, which setup_file_logging treats as a no-op — the
fixture removes it so the code under test actually runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app.core import file_logging

_INSTANCE = "ai-shuffle-testinst"


class _FakeSettings:
    def __init__(self, tmp_path: Path, **overrides):
        self._data_dir = tmp_path
        self.file_logging_enabled = overrides.get("file_logging_enabled", True)
        self.file_log_level = overrides.get("file_log_level", "INFO")
        self.file_log_max_bytes = overrides.get("file_log_max_bytes", 10 * 1024 * 1024)
        self.file_log_backup_count = overrides.get("file_log_backup_count", 5)
        self.version = "9.9.9-test"
        self.service_name = "ai-shuffle-test"

    @property
    def effective_data_dir(self) -> Path:
        return self._data_dir

    @property
    def effective_instance_id(self) -> str:
        return _INSTANCE


def _log_dir(tmp_path: Path) -> Path:
    """The per-instance log dir the module should use for the fake settings."""
    return tmp_path / "logs" / _INSTANCE


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Patch config.settings and reset module-level singletons around each test.

    Also removes the conftest's process-wide TESTING=1 for the duration of the
    test: setup_file_logging deliberately no-ops under TESTING (so a test run
    can never touch the real data dir), but these tests point it at a tmp dir
    and need it to actually run.
    """
    monkeypatch.delenv("TESTING", raising=False)

    def _make(**overrides):
        settings = _FakeSettings(tmp_path, **overrides)
        monkeypatch.setattr("app.core.config.settings", settings, raising=True)
        return settings

    # Reset module singletons before AND clean up after so root logger stays clean.
    file_logging._file_handler = None
    file_logging._fatal_file = None
    file_logging._pending_fatal_warning = None
    yield _make

    root = logging.getLogger()
    if file_logging._file_handler is not None:
        root.removeHandler(file_logging._file_handler)
        try:
            file_logging._file_handler.close()
        except Exception:
            pass
    if file_logging._fatal_file is not None:
        try:
            file_logging._fatal_file.close()
        except Exception:
            pass
    file_logging._file_handler = None
    file_logging._fatal_file = None
    file_logging._pending_fatal_warning = None


def test_setup_attaches_rotating_file_handler_with_config(fake_settings, tmp_path):
    fake_settings(file_log_level="WARNING", file_log_max_bytes=1234, file_log_backup_count=3)

    path = file_logging.setup_file_logging()

    # Per-instance namespacing: logs/<instance_id>/app.log
    assert path == _log_dir(tmp_path) / "app.log"
    handler = file_logging._file_handler
    assert isinstance(handler, RotatingFileHandler)
    assert handler.level == logging.WARNING
    assert handler.maxBytes == 1234
    assert handler.backupCount == 3
    assert handler.encoding == "utf-8"
    # Attached to the ROOT logger so all app/uvicorn/third-party records land here.
    assert handler in logging.getLogger().handlers
    assert _log_dir(tmp_path).is_dir()


def test_diagnostic_dump_uses_managed_fatal_log(fake_settings, tmp_path):
    fake_settings()
    file_logging.setup_file_logging()
    file_logging.dump_diagnostic_traceback("Event loop stalled for 10.0s")
    text = (_log_dir(tmp_path) / "fatal.log").read_text(encoding="utf-8")
    assert "Event loop stalled for 10.0s" in text
    assert "test_diagnostic_dump_uses_managed_fatal_log" in text


def test_diagnostic_dump_without_fatal_stream(monkeypatch):
    monkeypatch.setattr(file_logging, "_fatal_file", None)
    file_logging.dump_diagnostic_traceback("disabled")


def test_diagnostic_dump_io_failure_is_nonfatal(monkeypatch):
    class BrokenStream:
        def write(self, text):
            raise OSError("diagnostic disk unavailable")

    monkeypatch.setattr(file_logging, "_fatal_file", BrokenStream())
    file_logging.dump_diagnostic_traceback("unwritable")


def test_setup_is_idempotent(fake_settings, tmp_path):
    fake_settings()
    first = file_logging.setup_file_logging()
    root = logging.getLogger()
    count_after_first = sum(isinstance(h, RotatingFileHandler) for h in root.handlers)
    second = file_logging.setup_file_logging()
    count_after_second = sum(isinstance(h, RotatingFileHandler) for h in root.handlers)
    assert first == second
    assert count_after_first == count_after_second == 1


def test_records_written_to_file_and_startup_banner(fake_settings, tmp_path):
    fake_settings(file_log_level="INFO")
    file_logging.setup_file_logging()

    file_logging.log_startup_banner()
    logging.getLogger("some.module").info("a distinctive test log line")
    file_logging._file_handler.flush()

    content = (_log_dir(tmp_path) / "app.log").read_text(encoding="utf-8")
    assert "=== AI Shuffle starting" in content
    assert "version=9.9.9-test" in content
    assert f"instance_id={_INSTANCE}" in content
    assert "a distinctive test log line" in content


def test_disabled_via_env_flag(fake_settings, tmp_path):
    fake_settings(file_logging_enabled=False)
    path = file_logging.setup_file_logging()
    assert path is None
    assert file_logging._file_handler is None
    # No RotatingFileHandler attached, no logs dir created.
    assert not any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers)
    assert not (tmp_path / "logs").exists()


def test_setup_is_noop_under_testing_env(fake_settings, tmp_path, monkeypatch):
    """TESTING=1 (set by conftest for every test run) makes setup a hard no-op.

    Belt-and-braces for the import-time pollution bug: even if something calls
    setup_file_logging during a test run with the REAL settings singleton, it
    must not create the log dir or truncate a previous crash's fatal.log.
    """
    fake_settings()
    monkeypatch.setenv("TESTING", "1")

    path = file_logging.setup_file_logging()

    assert path is None
    assert file_logging._file_handler is None
    assert not (tmp_path / "logs").exists()


def test_importing_app_main_does_not_touch_log_dir(tmp_path):
    """Importing app.main must NOT create the log dir or touch fatal.log.

    Regression test for the import-time-setup blocker: pytest collection
    imports app.main with the real settings singleton; when setup ran at
    module import it created the real logs dir, TRUNCATED the real fatal.log
    (open "w"), and ran the boot-check os.replace — destroying a genuine
    production crash trace. Setup now runs only in the lifespan startup.

    Runs in a subprocess with DATA_DIR pointed at a tmp dir so the assertion
    is about a fresh interpreter's import side effects, not this process's
    accumulated state.
    """
    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import app.main\n"
        "from app.core import file_logging\n"
        "assert file_logging._file_handler is None, 'file handler attached at import time'\n"
        "assert file_logging._fatal_file is None, 'fatal.log opened at import time'\n"
    )
    env = {
        **os.environ,
        "TESTING": "1",
        "CORS_ORIGINS": "*",
        "DATA_DIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join((str(repo_root), str(repo_root / "fluid"))),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    # No logs directory materialized anywhere under the data dir.
    assert not (tmp_path / "logs").exists()


def test_faulthandler_fatal_log_opened(fake_settings, tmp_path):
    fake_settings()
    file_logging.setup_file_logging()
    # fatal.log is opened and kept referenced for the process lifetime.
    assert (_log_dir(tmp_path) / "fatal.log").exists()
    assert file_logging._fatal_file is not None
    assert not file_logging._fatal_file.closed


def test_boot_check_rotates_nonempty_previous_fatal_and_warns(fake_settings, tmp_path, caplog):
    fake_settings()
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    fatal = log_dir / "fatal.log"
    fatal.write_text("Fatal Python error: Segmentation fault\n\nThread 0x...\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=file_logging.logger.name):
        file_logging.setup_file_logging()

    prev = log_dir / "fatal.log.prev"
    assert prev.exists()
    assert "Segmentation fault" in prev.read_text(encoding="utf-8")
    # A fresh (empty) fatal.log was reopened for this run.
    assert fatal.exists()
    assert fatal.stat().st_size == 0
    # WARNING surfaced immediately (also stashed for error_log re-emit).
    assert any("fatal crash trace" in r.getMessage() for r in caplog.records)
    assert file_logging._pending_fatal_warning is not None


def test_boot_check_overwrites_older_prev(fake_settings, tmp_path):
    fake_settings()
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "fatal.log.prev").write_text("OLD stale prev trace", encoding="utf-8")
    (log_dir / "fatal.log").write_text("NEW crash trace here", encoding="utf-8")

    file_logging.setup_file_logging()

    prev = log_dir / "fatal.log.prev"
    text = prev.read_text(encoding="utf-8")
    assert "NEW crash trace here" in text
    assert "OLD stale prev" not in text  # exactly one generation preserved


def test_boot_check_ignores_empty_previous_fatal(fake_settings, tmp_path):
    fake_settings()
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "fatal.log").write_text("", encoding="utf-8")  # clean prior shutdown

    file_logging.setup_file_logging()

    # An empty prior fatal.log is not a crash → no .prev, no pending warning.
    assert not (log_dir / "fatal.log.prev").exists()
    assert file_logging._pending_fatal_warning is None


def test_emit_pending_fatal_warning_reemits_once(fake_settings, tmp_path, caplog):
    fake_settings()
    file_logging._pending_fatal_warning = (
        "boom crash trace 42 bytes", 42, _log_dir(tmp_path) / "fatal.log.prev",
    )

    with caplog.at_level(logging.WARNING, logger=file_logging.logger.name):
        first = file_logging.emit_pending_fatal_warning()
        second = file_logging.emit_pending_fatal_warning()

    assert first is True
    assert second is False  # cleared after first emit — no duplicate error_log rows
    assert sum("boom crash trace" in r.getMessage() for r in caplog.records) == 1


def test_shutdown_banner_flushes_and_survives_handler_close(fake_settings, tmp_path):
    """The shutdown banner must hit disk even when teardown is cut short.

    log_shutdown_banner flushes the handler itself (no reliance on a later
    logging.shutdown), and the atexit-registered _flush_and_close_file_handler
    closes it cleanly and idempotently.
    """
    fake_settings()
    file_logging.setup_file_logging()

    file_logging.log_shutdown_banner()
    # No manual flush here — the banner call itself must have flushed.
    content = (_log_dir(tmp_path) / "app.log").read_text(encoding="utf-8")
    assert "=== AI Shuffle shutting down" in content

    # The atexit hook closes without raising, and is idempotent.
    file_logging._flush_and_close_file_handler()
    file_logging._flush_and_close_file_handler()
    content = (_log_dir(tmp_path) / "app.log").read_text(encoding="utf-8")
    assert "=== AI Shuffle shutting down" in content


def test_setup_registers_atexit_flush(fake_settings, tmp_path, monkeypatch):
    registered = []
    monkeypatch.setattr(file_logging.atexit, "register", lambda fn, *a, **kw: registered.append(fn))

    fake_settings()
    file_logging.setup_file_logging()

    assert file_logging._flush_and_close_file_handler in registered


def test_asyncio_exception_handler_installed_on_running_loop(fake_settings):
    """install_asyncio_exception_handler uses the RUNNING loop and installs."""
    fake_settings()

    async def _run():
        loop = asyncio.get_running_loop()
        assert loop.get_exception_handler() is None  # fresh loop
        file_logging.install_asyncio_exception_handler()
        handler = loop.get_exception_handler()
        assert handler is not None
        # The installed handler logs the exception (smoke: it must not raise).
        handler(loop, {"message": "test-context", "exception": RuntimeError("boom")})

    asyncio.run(_run())


def test_asyncio_exception_handler_warns_without_running_loop(fake_settings, caplog):
    """No running loop → WARNING, not a silent no-op."""
    fake_settings()
    with caplog.at_level(logging.WARNING, logger=file_logging.logger.name):
        file_logging.install_asyncio_exception_handler()
    assert any("no running event loop" in r.getMessage() for r in caplog.records)


def _install_and_capture_handler(loop):
    """Install the loop handler on ``loop`` and return (handler, calls list).

    The fresh loop has no pre-existing handler, so ``_handler`` chains to
    ``loop.default_exception_handler``; we stub that to record chaining without
    the noisy default output. Returns the installed handler and the list its
    stub appends to.
    """
    calls = []
    loop.default_exception_handler = lambda ctx: calls.append(ctx)  # type: ignore[method-assign]
    file_logging.install_asyncio_exception_handler(loop)
    handler = loop.get_exception_handler()
    assert handler is not None
    return handler, calls


def test_proactor_connreset_winerror_10054_is_debug_not_chained(fake_settings, caplog):
    """The known Windows proactor teardown artifact: no ERROR, no chaining."""
    fake_settings()
    loop = asyncio.new_event_loop()
    try:
        handler, calls = _install_and_capture_handler(loop)
        exc = ConnectionResetError(10054, "An existing connection was forcibly closed")
        # On Linux CI ``winerror`` is not populated by the constructor; set it
        # explicitly to mirror the Windows attribute the predicate keys off.
        exc.winerror = 10054
        with caplog.at_level(logging.DEBUG, logger=file_logging.logger.name):
            handler(loop, {"message": "Fatal error on transport", "exception": exc})
        # Downgraded to DEBUG, never ERROR.
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any(r.levelno == logging.DEBUG for r in caplog.records)
        # RETURNED without chaining to the default handler (which would re-log
        # at ERROR and feed the error_log DB).
        assert calls == []
    finally:
        loop.close()


def test_proactor_marker_in_message_is_debug_not_chained(fake_settings, caplog):
    """The proactor source marker (no winerror) also downgrades + does not chain."""
    fake_settings()
    loop = asyncio.new_event_loop()
    try:
        handler, calls = _install_and_capture_handler(loop)
        exc = ConnectionResetError("Connection lost")  # no winerror set
        ctx = {
            "message": "Error on transport _ProactorBasePipeTransport._call_connection_lost",
            "exception": exc,
        }
        with caplog.at_level(logging.DEBUG, logger=file_logging.logger.name):
            handler(loop, ctx)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert calls == []
    finally:
        loop.close()


def test_plain_connreset_without_marker_still_errors_and_chains(fake_settings, caplog):
    """Regression guard: a ConnectionResetError from anywhere else keeps ERROR + chain."""
    fake_settings()
    loop = asyncio.new_event_loop()
    try:
        handler, calls = _install_and_capture_handler(loop)
        exc = ConnectionResetError("peer reset during app request")  # no winerror, no marker
        ctx = {"message": "Unhandled connection reset", "exception": exc}
        with caplog.at_level(logging.DEBUG, logger=file_logging.logger.name):
            handler(loop, ctx)
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        assert calls == [ctx]  # chained to the default handler
    finally:
        loop.close()


def test_other_exception_unchanged_errors_and_chains(fake_settings, caplog):
    """Any non-ConnectionResetError is unaffected: ERROR + chain as before."""
    fake_settings()
    loop = asyncio.new_event_loop()
    try:
        handler, calls = _install_and_capture_handler(loop)
        ctx = {"message": "boom", "exception": RuntimeError("kaboom")}
        with caplog.at_level(logging.DEBUG, logger=file_logging.logger.name):
            handler(loop, ctx)
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        assert calls == [ctx]
    finally:
        loop.close()
