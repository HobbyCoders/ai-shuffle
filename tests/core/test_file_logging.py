"""Tests for persistent, crash-surviving file logging (app.core.file_logging).

Covers:
- setup_file_logging attaches a RotatingFileHandler with the configured level,
  rotation sizing, and UTF-8 encoding, under <data_dir>/logs/app.log.
- the startup banner (pid/version/instance_id) is written to the file.
- the fatal.log boot-check rotates a non-empty previous trace to fatal.log.prev,
  overwrites an older .prev, and surfaces a WARNING (pending → error_log).
- FILE_LOGGING_ENABLED=false disables the handler entirely.

The module reads ``app.core.config.settings`` lazily inside each function, so we
patch that singleton with a lightweight fake pointed at a tmp data dir. Each test
detaches any handler it attaches so the root logger is left clean.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app.core import file_logging


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
        return "ai-shuffle-testinst"


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Patch config.settings and reset module-level singletons around each test."""
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

    assert path == tmp_path / "logs" / "app.log"
    handler = file_logging._file_handler
    assert isinstance(handler, RotatingFileHandler)
    assert handler.level == logging.WARNING
    assert handler.maxBytes == 1234
    assert handler.backupCount == 3
    assert handler.encoding == "utf-8"
    # Attached to the ROOT logger so all app/uvicorn/third-party records land here.
    assert handler in logging.getLogger().handlers
    assert (tmp_path / "logs").is_dir()


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

    content = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "=== AI Shuffle starting" in content
    assert "version=9.9.9-test" in content
    assert "instance_id=ai-shuffle-testinst" in content
    assert "a distinctive test log line" in content


def test_disabled_via_env_flag(fake_settings, tmp_path):
    fake_settings(file_logging_enabled=False)
    path = file_logging.setup_file_logging()
    assert path is None
    assert file_logging._file_handler is None
    # No RotatingFileHandler attached, no logs dir created.
    assert not any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers)
    assert not (tmp_path / "logs").exists()


def test_faulthandler_fatal_log_opened(fake_settings, tmp_path):
    fake_settings()
    file_logging.setup_file_logging()
    # fatal.log is opened and kept referenced for the process lifetime.
    assert (tmp_path / "logs" / "fatal.log").exists()
    assert file_logging._fatal_file is not None
    assert not file_logging._fatal_file.closed


def test_boot_check_rotates_nonempty_previous_fatal_and_warns(fake_settings, tmp_path, caplog):
    fake_settings()
    log_dir = tmp_path / "logs"
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
    log_dir = tmp_path / "logs"
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
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "fatal.log").write_text("", encoding="utf-8")  # clean prior shutdown

    file_logging.setup_file_logging()

    # An empty prior fatal.log is not a crash → no .prev, no pending warning.
    assert not (log_dir / "fatal.log.prev").exists()
    assert file_logging._pending_fatal_warning is None


def test_emit_pending_fatal_warning_reemits_once(fake_settings, tmp_path, caplog):
    fake_settings()
    file_logging._pending_fatal_warning = ("boom crash trace 42 bytes", 42, tmp_path / "logs" / "fatal.log.prev")

    with caplog.at_level(logging.WARNING, logger=file_logging.logger.name):
        first = file_logging.emit_pending_fatal_warning()
        second = file_logging.emit_pending_fatal_warning()

    assert first is True
    assert second is False  # cleared after first emit — no duplicate error_log rows
    assert sum("boom crash trace" in r.getMessage() for r in caplog.records) == 1
