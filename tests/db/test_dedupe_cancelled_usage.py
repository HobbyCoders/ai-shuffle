"""--dedupe-cancelled pairs rows only on settlement-identity evidence.

Equal token totals are never evidence: pre-fix rows carry no settlement key
and must stay unreported even when two of them match exactly.
"""
import pytest
from scripts import dedupe_cancelled_usage as d
from app.db import database
from app.db.analytics import log_usage, settle_turn_usage


def test_equal_tokens_without_identity_are_never_reported(mock_db, capsys):
    sid = "ses-equal"
    database.create_session(session_id=sid, profile_id="default")
    for _ in range(2):
        log_usage(session_id=sid, profile_id="p", model="m", tokens_in=100,
                  tokens_out=5, cost_usd=0.01, duration_ms=1)
    assert d.report_duplicate_cancelled_usage() == 0
    out = capsys.readouterr().out
    assert "NOT ASSESSABLE" in out
    assert "2 historical row(s)" in out
    assert "No deletion or counter correction proposed" in out
    assert "settlement_key=" not in out


def test_a_shared_settlement_key_is_reported(monkeypatch, capsys):
    rows = [
        {"id": 1, "session_id": "ses-keyed", "settlement_key": "attempt-1",
         "tokens_in": 10, "tokens_out": 1, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "cost_usd": 0.01, "created_at": "t1"},
        {"id": 2, "session_id": "ses-keyed", "settlement_key": "attempt-1",
         "tokens_in": 10, "tokens_out": 1, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "cost_usd": 0.01, "created_at": "t2"},
    ]

    class FakeCursor:
        def execute(self, *a): pass
        def fetchall(self): return rows

    class FakeConn:
        def cursor(self): return FakeCursor()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import app.db.base as base
    monkeypatch.setattr(base, "get_db", lambda: FakeConn())

    assert d.report_duplicate_cancelled_usage() == 0
    out = capsys.readouterr().out
    assert "2 rows" in out and "attempt-1" in out
