"""Tests for Phase 2.6 — Fatal-error diagnosability.

Three changes under test:
(a) ``_wrap_stderr_capture`` returns a bounded ``deque(maxlen=50)`` (was an
    unbounded list that leaked over a long-lived pooled subprocess), and the
    existing consumers (``_extract_connection_error``, ``log_captured_cli_stderr``)
    still format a deque correctly.
(b) ``ErrorLogDBHandler.emit`` attributes a record to the current session via the
    ``current_session_id`` ContextVar — set → error_log row carries it; unset →
    NULL; an explicit record.session_id is never overwritten.
(c) A "Fatal error in message reader" record with a pooled state carrying stderr
    lines gets that tail appended to ``detail``; with no pooled state it never
    raises and the row is still written.

Plus the ContextVar inheritance seam: a task created after ``set()`` sees the
value (plain asyncio).
"""

import asyncio
import collections
import logging

import pytest

from app.core.query_persistence import (
    _wrap_stderr_capture,
    _extract_connection_error,
    log_captured_cli_stderr,
    _STDERR_CAPTURE_MAXLEN,
)
from app.core import session_context
from app.core.session_context import (
    set_current_session_id,
    get_current_session_id,
    current_session_id,
)
from app.core import error_log_handler
from app.core.error_log_handler import ErrorLogDBHandler


# =============================================================================
# (a) Bounded stderr ring buffer
# =============================================================================

class TestStderrDequeBound:
    def test_wrap_returns_bounded_deque(self):
        cb, captured = _wrap_stderr_capture(None)
        assert isinstance(captured, collections.deque)
        assert captured.maxlen == 50 == _STDERR_CAPTURE_MAXLEN

    def test_push_60_keeps_last_50_oldest_dropped(self):
        cb, captured = _wrap_stderr_capture(None)
        for i in range(60):
            cb(f"line-{i}")
        assert len(captured) == 50
        # Oldest 10 dropped, newest kept.
        assert "line-9" not in captured
        assert list(captured)[0] == "line-10"
        assert list(captured)[-1] == "line-59"

    def test_original_callback_still_invoked(self):
        seen = []
        cb, captured = _wrap_stderr_capture(seen.append)
        cb("passthrough")
        assert seen == ["passthrough"]
        assert list(captured) == ["passthrough"]

    def test_extract_connection_error_formats_deque(self):
        _cb, captured = _wrap_stderr_capture(None)
        captured.append("  boom  ")
        captured.append("RuntimeError: kaboom")
        # A plain Exception (not ProcessError) -> stderr lines win.
        msg = _extract_connection_error(RuntimeError("x"), captured)
        assert msg == "boom RuntimeError: kaboom"

    def test_log_captured_cli_stderr_formats_deque_tail(self, caplog):
        _cb, captured = _wrap_stderr_capture(None)
        for i in range(60):  # ring keeps last 50
            captured.append(f"line-{i}")
        with caplog.at_level(logging.ERROR, logger="app.core.query_persistence"):
            log_captured_cli_stderr("sess-a", captured, context="ws mid-stream")
        rec = next(r for r in caplog.records if r.levelno == logging.ERROR)
        m = rec.getMessage()
        assert "session=sess-a" in m
        assert "line-59" in m  # newest
        # log_captured keeps only a 40-line tail of the (already 50-line) ring.
        assert "line-20" in m
        assert "line-19" not in m


# =============================================================================
# ContextVar seam + inheritance
# =============================================================================

class TestSessionContextVar:
    def test_set_get_roundtrip(self):
        token = current_session_id.set(None)
        try:
            assert get_current_session_id() is None
            set_current_session_id("sess-ctx")
            assert get_current_session_id() == "sess-ctx"
        finally:
            current_session_id.reset(token)

    async def test_task_created_after_set_inherits_value(self):
        # asyncio.create_task snapshots the current context, so a task created
        # AFTER set() sees the value — this is the seam the SDK reader task uses.
        set_current_session_id("sess-inherited")
        seen = {}

        async def child():
            seen["value"] = get_current_session_id()

        await asyncio.create_task(child())
        assert seen["value"] == "sess-inherited"


# =============================================================================
# (b)+(c) emit() attribution + fatal stderr enrichment
# =============================================================================

def _emit(record_msg, *, logger_name="app.core.somewhere", exc_info=None,
          session_id_attr=None):
    """Push one record through a fresh ErrorLogDBHandler.emit and return nothing."""
    handler = ErrorLogDBHandler()
    record = logging.LogRecord(
        name=logger_name,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=record_msg,
        args=(),
        exc_info=exc_info,
    )
    if session_id_attr is not None:
        record.session_id = session_id_attr
    handler.emit(record)


def _rows(test_db, session_id):
    return test_db.execute(
        "SELECT message, detail, session_id FROM error_log WHERE session_id = ?",
        (session_id,),
    ).fetchall()


def _all_rows(test_db, message_like):
    return test_db.execute(
        "SELECT message, detail, session_id FROM error_log WHERE message LIKE ?",
        (message_like,),
    ).fetchall()


class TestEmitSessionAttribution:
    def test_contextvar_set_lands_in_error_log(self, mock_db, test_db):
        token = current_session_id.set("sess-attr-1")
        try:
            _emit("boom via context")
        finally:
            current_session_id.reset(token)
        rows = _rows(test_db, "sess-attr-1")
        assert len(rows) == 1
        assert rows[0]["message"] == "boom via context"
        assert rows[0]["session_id"] == "sess-attr-1"

    def test_contextvar_unset_leaves_null(self, mock_db, test_db):
        token = current_session_id.set(None)
        try:
            _emit("boom no context")
        finally:
            current_session_id.reset(token)
        rows = _all_rows(test_db, "boom no context")
        assert len(rows) == 1
        assert rows[0]["session_id"] is None

    def test_explicit_record_session_id_not_overwritten(self, mock_db, test_db):
        token = current_session_id.set("sess-context")
        try:
            _emit("boom explicit", session_id_attr="sess-explicit")
        finally:
            current_session_id.reset(token)
        # The record's own session_id wins; the ContextVar does not clobber it.
        assert _rows(test_db, "sess-context") == []
        rows = _rows(test_db, "sess-explicit")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-explicit"


class TestFatalStderrEnrichment:
    def test_fatal_with_pooled_state_appends_tail(self, mock_db, test_db,
                                                  monkeypatch):
        # Build a fake pooled state carrying a stderr ring.
        _cb, captured = _wrap_stderr_capture(None)
        captured.append("Traceback (most recent call last):")
        captured.append("RuntimeError: subprocess died")

        class _State:
            cli_stderr_lines = captured

        class _Pool:
            def peek(self, sid):
                return _State() if sid == "sess-fatal" else None

        import app.core.session_pool as sp
        monkeypatch.setattr(sp, "_session_pool", _Pool(), raising=False)

        token = current_session_id.set("sess-fatal")
        try:
            _emit("Fatal error in message reader")
        finally:
            current_session_id.reset(token)

        rows = _rows(test_db, "sess-fatal")
        assert len(rows) == 1
        assert "[Captured CLI stderr]" in (rows[0]["detail"] or "")
        assert "RuntimeError: subprocess died" in rows[0]["detail"]

    def test_fatal_without_pooled_state_no_raise_row_written(self, mock_db,
                                                             test_db, monkeypatch):
        import app.core.session_pool as sp
        monkeypatch.setattr(sp, "_session_pool", None, raising=False)

        token = current_session_id.set("sess-fatal-2")
        try:
            _emit("Fatal error in message reader")  # must not raise
        finally:
            current_session_id.reset(token)

        rows = _rows(test_db, "sess-fatal-2")
        assert len(rows) == 1
        # No pooled state -> no tail appended, but the row is still recorded.
        assert "[Captured CLI stderr]" not in (rows[0]["detail"] or "")

    def test_non_fatal_message_never_enriched(self, mock_db, test_db, monkeypatch):
        # Even with a pooled state present, a non-fatal message is not enriched.
        _cb, captured = _wrap_stderr_capture(None)
        captured.append("some stderr")

        class _State:
            cli_stderr_lines = captured

        class _Pool:
            def peek(self, sid):
                return _State()

        import app.core.session_pool as sp
        monkeypatch.setattr(sp, "_session_pool", _Pool(), raising=False)

        token = current_session_id.set("sess-plain")
        try:
            _emit("just a regular error")
        finally:
            current_session_id.reset(token)

        rows = _rows(test_db, "sess-plain")
        assert len(rows) == 1
        assert "[Captured CLI stderr]" not in (rows[0]["detail"] or "")
