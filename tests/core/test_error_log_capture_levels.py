"""WARNING records reach the durable error log, bounded.

``GetAppErrors`` can only ever show what ``error_log`` holds, and the handler
persisted ERROR+ only. Whole classes of real failure — provider polls, auth
refusals, degraded fallbacks — live at WARNING, so an agent looking straight at
one was told ``{"errors": [], "count": 0}``. Warnings are now captured too, with
their own retention ceiling so they cannot evict the errors.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.core import error_log_handler as elh
from app.core.config import settings


@pytest.fixture
def recorded(monkeypatch):
    """Capture what the handler would persist, without touching a DB."""
    calls = []

    class _FakeErrorLogs:
        @staticmethod
        def record_error(**kwargs):
            calls.append(kwargs)
            return "row-1"

    import app.db.error_logs as real

    monkeypatch.setattr(real, "record_error", _FakeErrorLogs.record_error)
    return calls


def _record(level: int, message: str = "poll failed: HTTP 403") -> logging.LogRecord:
    return logging.LogRecord(
        name="app.core.provider_usage",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_warning_is_persisted_by_default(recorded):
    elh.ErrorLogDBHandler().emit(_record(logging.WARNING))

    assert len(recorded) == 1
    assert recorded[0]["level"] == "WARNING"
    assert recorded[0]["message"] == "poll failed: HTTP 403"


def test_error_is_still_persisted(recorded):
    elh.ErrorLogDBHandler().emit(_record(logging.ERROR))

    assert [c["level"] for c in recorded] == ["ERROR"]


def test_info_is_never_persisted(recorded):
    elh.ErrorLogDBHandler().emit(_record(logging.INFO))

    assert recorded == []


def test_setting_off_restores_error_only_capture(recorded, monkeypatch):
    monkeypatch.setattr(settings, "error_log_capture_warnings", False)
    handler = elh.ErrorLogDBHandler()

    handler.emit(_record(logging.WARNING))
    assert recorded == []

    handler.emit(_record(logging.ERROR))
    assert len(recorded) == 1


def test_db_layer_records_are_still_skipped(recorded):
    """A DB error logging into the DB is the feedback loop this guard exists
    for — widening the level must not widen that hole."""
    record = _record(logging.WARNING)
    record.name = "app.db.error_logs"

    elh.ErrorLogDBHandler().emit(record)

    assert recorded == []


def test_capture_level_falls_back_to_error_when_config_is_unreadable():
    """Logging must not depend on config loading cleanly."""

    class _Exploding:
        def __getattr__(self, name):
            raise RuntimeError("settings unavailable")

    with patch("app.core.config.settings", _Exploding()):
        assert elh._capture_level() == logging.ERROR


# ─── the read side ───────────────────────────────────────────────────────────


def test_levels_filter_builds_an_in_clause(mock_db):
    """``min_level`` reads as a level SET, because the column stores names."""
    from app.db import error_logs

    seen = {}

    class _Cursor:
        def execute(self, query, params=None):
            seen["query"] = query
            seen["params"] = params

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("app.db.error_logs.get_db", return_value=_Conn()):
        error_logs.get_recent_errors(levels=["WARNING", "ERROR", "CRITICAL"])

    assert "level IN (%s, %s, %s)" in seen["query"]
    assert seen["params"][:3] == ["WARNING", "ERROR", "CRITICAL"]


def test_prune_trims_warnings_to_their_own_ceiling(monkeypatch):
    """Without this, a chatty warning would push errors out of the table-wide
    5,000-row trim."""
    from app.db import error_logs

    monkeypatch.setattr(settings, "error_log_max_warning_rows", 250)
    statements = []

    class _Cursor:
        rowcount = 0

        def execute(self, query, params=None):
            statements.append((" ".join(query.split()), params))

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("app.db.error_logs.get_db", return_value=_Conn()):
        error_logs.prune_errors()

    warning_trims = [
        (q, p) for q, p in statements if "level = 'WARNING'" in q
    ]
    assert len(warning_trims) == 1
    assert warning_trims[0][1] == (250,)
    # And it runs BEFORE the table-wide trim, so the surviving errors are the
    # ones that fill the general budget.
    warning_idx = next(
        i for i, (q, _) in enumerate(statements) if "level = 'WARNING'" in q
    )
    global_idx = next(
        i
        for i, (q, _) in enumerate(statements)
        if "ORDER BY updated_at DESC OFFSET" in q and "WARNING" not in q
    )
    assert warning_idx < global_idx


def test_warning_ceiling_of_zero_disables_the_trim(monkeypatch):
    from app.db import error_logs

    monkeypatch.setattr(settings, "error_log_max_warning_rows", 0)
    statements = []

    class _Cursor:
        rowcount = 0

        def execute(self, query, params=None):
            statements.append(" ".join(query.split()))

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("app.db.error_logs.get_db", return_value=_Conn()):
        error_logs.prune_errors()

    assert not [q for q in statements if "level = 'WARNING'" in q]
