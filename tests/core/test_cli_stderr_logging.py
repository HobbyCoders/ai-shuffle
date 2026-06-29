"""
Tests for ``log_captured_cli_stderr`` (app/core/query_persistence.py) — the Flow A
diagnostic that surfaces a crashed CLI subprocess's captured stderr at ERROR when
the subprocess dies MID-STREAM (the common case behind a background task that
silently vanishes).

The connect-time handlers already route captured stderr through
``_extract_connection_error``; this helper covers the mid-stream path where the
connect-scoped ``cli_stderr_lines`` was previously out of scope, so the real exit
reason was only ever logged at DEBUG (or leaked as "Task exception was never
retrieved"). The helper is best-effort and must never raise.
"""

import logging

import pytest

from app.core.query_persistence import log_captured_cli_stderr

_LOGGER_NAME = "app.core.query_persistence"


class TestLogCapturedCliStderr:
    def test_logs_captured_lines_at_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr(
                "sess-1",
                ["Traceback (most recent call last):", "RuntimeError: boom"],
                context="background mid-stream",
            )
        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(records) == 1
        msg = records[0].getMessage()
        # Carries the context tag, the session id, and the captured tail.
        assert "[Captured CLI stderr]" in msg
        assert "[background mid-stream]" in msg
        assert "session=sess-1" in msg
        assert "RuntimeError: boom" in msg
        # Multiple lines are joined with the ' | ' separator.
        assert "Traceback (most recent call last): | RuntimeError: boom" in msg

    def test_noop_for_empty_list(self, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr("sess-1", [], context="ws mid-stream")
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []

    def test_noop_for_none(self, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr("sess-1", None, context="rest-stream mid-stream")
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []

    def test_noop_when_only_blank_lines(self, caplog):
        # Lines that are all whitespace carry no signal — nothing is logged.
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr("sess-1", ["   ", "\n", ""], context="x")
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []

    def test_keeps_only_the_tail(self, caplog):
        # A long-lived pooled subprocess accumulates stderr across turns; only the
        # last 40 lines (nearest the crash) are the most diagnostic.
        lines = [f"line-{i}" for i in range(100)]
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr("sess-1", lines, context="")
        msg = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert "line-99" in msg  # newest kept
        assert "line-60" in msg  # 100 - 40 == start of the tail
        assert "line-59" not in msg  # older than the 40-line tail -> dropped

    def test_omits_context_tag_when_blank(self, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            log_captured_cli_stderr("sess-1", ["oops"], context="")
        msg = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        # No empty "[]" context tag when no context is supplied.
        assert "[Captured CLI stderr] session=sess-1" in msg

    def test_never_raises_on_bad_input(self):
        # Diagnostics must not mask the original error: a non-iterable / exploding
        # input is swallowed rather than propagated.
        class Boom:
            def __bool__(self):
                raise ValueError("nope")

        # Must not raise despite the truthiness check exploding.
        log_captured_cli_stderr("sess-1", Boom(), context="x")
