"""#1407 reconciliation: duplicates are proven from execution identity only.

Each fixture builds the evidence a cancelled native worker leaves behind on the
SQLite shim: the engine store (``fluid_session_events``), the app ``turns`` row,
the recorder's per-call ``turn.usage`` records, the ``usage_log`` rows and the
session counters the old code double-incremented.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import database
from app.db.analytics import log_usage, settle_turn_usage
from scripts import dedupe_cancelled_usage as d


T0 = datetime(2026, 9, 14, 22, 40, 0, tzinfo=timezone.utc)
CALLS = ((1200, 40, 3000, 0), (800, 25, 4200, 64), (500, 10, 0, 0))
SUMS = tuple(sum(c[i] for c in CALLS) for i in range(4))
COST = 0.25


def _iso(moment):
    return moment.isoformat()


def _engine_attempt(db, store, turn_id, start, *, terminal="Interrupted",
                    calls=CALLS, terminal_calls=None, terminal_sums=None):
    """Append one fluid turn to the engine store; return the next seq."""
    seq = 1 + (db.execute(
        "SELECT MAX(seq) FROM fluid_session_events WHERE session_id=?", (store,),
    ).fetchone()[0] or 0)

    def put(event):
        nonlocal seq
        event.setdefault("session_id", store)
        event["seq"] = seq
        db.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) VALUES (?,?,?)",
            (store, seq, json.dumps(event)),
        )
        seq += 1

    put({"_cls": "TurnStarted", "turn_id": turn_id, "ts": start.timestamp(),
         "model": "gpt-test", "provider": "openai"})
    for index, (fresh, out, cache_r, cache_c) in enumerate(calls, start=1):
        put({"_cls": "Usage", "turn_id": turn_id,
             "ts": (start + timedelta(seconds=index)).timestamp(),
             "input_tokens": fresh, "output_tokens": out,
             "cache_read_tokens": cache_r, "cache_creation_tokens": cache_c,
             "raw": {"model_call_index": index}})
    sums = terminal_sums or tuple(sum(c[i] for c in calls) for i in range(4))
    put({"_cls": terminal, "turn_id": turn_id,
         "ts": (start + timedelta(minutes=10)).timestamp(),
         "usage": {"input_tokens": sums[0], "output_tokens": sums[1],
                   "cache_read_input_tokens": sums[2],
                   "cache_creation_input_tokens": sums[3]},
         "cost_usd": COST,
         "num_model_calls": len(calls) if terminal_calls is None else terminal_calls})
    return seq


def _app_turn(db, sid, turn_id, start, *, record_calls=True, calls=len(CALLS)):
    db.execute(
        "INSERT INTO turns (id, session_id, state, source, stop_reason, started_at, "
        "ended_at) VALUES (?,?,?,?,?,?,?)",
        (turn_id, sid, "aborted", "provider_agent", "parent_cancelled",
         _iso(start - timedelta(seconds=1)), _iso(start + timedelta(minutes=10))),
    )
    if record_calls:
        for index in range(1, calls + 1):
            db.execute(
                "INSERT INTO session_events (id, session_id, seq, turn_id, event_type, "
                "payload) VALUES (?,?,?,?,?,?)",
                (f"{turn_id}-{index}", sid, 1000 + index + hash(turn_id) % 1000,
                 turn_id, "turn.usage",
                 json.dumps({"usage": {"model_call_index": index}})),
            )


def _usage_row(db, sid, created, sums=SUMS, key=None, cost=COST):
    db.execute(
        "INSERT INTO usage_log (settlement_key, session_id, model, tokens_in, "
        "tokens_out, cache_read_tokens, cache_creation_tokens, cost_usd, "
        "duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, sid, "gpt-test", *sums, cost, 0, _iso(created)),
    )


def _session(db, sid, store, *, booked_times=2, calls=len(CALLS)):
    database.create_session(session_id=sid, profile_id="default")
    db.execute(
        "UPDATE sessions SET sdk_session_id=?, total_tokens_in=?, total_tokens_out=?, "
        "total_cache_read_tokens=?, total_cache_creation_tokens=?, total_cost_usd=?, "
        "total_model_calls=?, turn_count=? WHERE id=?",
        (store, *(booked_times * v for v in SUMS), booked_times * COST,
         booked_times * calls, 1, sid),
    )
    db.commit()


def _seed_duplicate(db, sid="app-dup", store="ses_dup", turn_id="turn-1",
                    **turn_kwargs):
    _session(db, sid, store)
    _engine_attempt(db, store, "ft-1", T0)
    _app_turn(db, sid, turn_id, T0, **turn_kwargs)
    killed_at = T0 + timedelta(minutes=10)
    _usage_row(db, sid, killed_at)
    _usage_row(db, sid, killed_at + timedelta(milliseconds=4))
    db.commit()
    return sid


def _ledger(db, sid):
    return [r[0] for r in db.execute(
        "SELECT id FROM usage_log WHERE session_id=? ORDER BY id", (sid,)).fetchall()]


def _counters(sid):
    row = database.get_session(sid)
    return (row["total_tokens_in"], row["total_tokens_out"],
            row["total_cache_read_tokens"], row["total_cache_creation_tokens"],
            round(row["total_cost_usd"], 6), row["total_model_calls"],
            row["turn_count"])


def test_read_only_run_reports_the_group_and_writes_nothing(mock_db, capsys):
    sid = _seed_duplicate(mock_db)
    before_rows, before_counters = _ledger(mock_db, sid), _counters(sid)

    assert d.reconcile() == 0

    out = capsys.readouterr().out
    assert "DUPLICATE  engine store ses_dup, turn ft-1" in out
    assert "3 per-call Usage events sum to in=2500 out=75" in out
    assert "app turn turn-1 state=aborted stop_reason=parent_cancelled" in out
    assert f"keep   usage_log id={before_rows[0]}" in out
    assert f"remove usage_log id={before_rows[1]}" in out
    assert "(+4.0 ms after kept row)" in out
    assert "total_model_calls -3" in out
    assert _ledger(mock_db, sid) == before_rows
    assert _counters(sid) == before_counters


def test_apply_removes_the_duplicate_and_corrects_counters_once(mock_db, capsys):
    sid = _seed_duplicate(mock_db)
    keep = _ledger(mock_db, sid)[0]

    assert d.reconcile(apply=True) == 0
    assert _ledger(mock_db, sid) == [keep]
    assert _counters(sid) == (*SUMS, COST, len(CALLS), 1)

    # Idempotent: the second run finds one booking and changes nothing.
    capsys.readouterr()
    assert d.reconcile(apply=True) == 0
    assert "DUPLICATE" not in capsys.readouterr().out
    assert _ledger(mock_db, sid) == [keep]
    assert _counters(sid) == (*SUMS, COST, len(CALLS), 1)


def test_model_calls_stay_when_the_kill_paths_count_is_unproven(mock_db, capsys):
    sid = _seed_duplicate(mock_db, record_calls=False)

    assert d.reconcile(apply=True) == 0
    assert "model-call counter left unchanged" in capsys.readouterr().out
    assert _counters(sid) == (*SUMS, COST, 2 * len(CALLS), 1)


def test_equal_rows_without_engine_evidence_are_never_touched(mock_db, capsys):
    sid = "app-no-store"
    database.create_session(session_id=sid, profile_id="default")
    for _ in range(2):
        log_usage(session_id=sid, profile_id="p", model="m", tokens_in=100,
                  tokens_out=5, cost_usd=0.01, duration_ms=1)
    rows = _ledger(mock_db, sid)

    assert d.reconcile(apply=True) == 0
    out = capsys.readouterr().out
    assert "NOT ASSESSABLE  no engine store linked" in out
    assert "DUPLICATE" not in out
    assert _ledger(mock_db, sid) == rows


def test_a_retry_with_identical_totals_is_a_separate_attempt(mock_db, capsys):
    sid, store = "app-retry", "ses_retry"
    _session(mock_db, sid, store)
    second = T0 + timedelta(minutes=20)
    _engine_attempt(mock_db, store, "ft-1", T0)
    _engine_attempt(mock_db, store, "ft-2", second)
    _app_turn(mock_db, sid, "turn-1", T0)
    _app_turn(mock_db, sid, "turn-2", second)
    _usage_row(mock_db, sid, T0 + timedelta(minutes=10))
    _usage_row(mock_db, sid, second + timedelta(minutes=10))
    mock_db.commit()
    rows, counters = _ledger(mock_db, sid), _counters(sid)

    assert d.reconcile(apply=True) == 0
    assert "DUPLICATE" not in capsys.readouterr().out
    assert _ledger(mock_db, sid) == rows
    assert _counters(sid) == counters


@pytest.mark.parametrize("breakage, reason", [
    ("row_differs", "differ from the per-call Usage sum"),
    ("terminal_disagrees", "disagrees with the per-call Usage sum"),
    ("call_count", "terminal num_model_calls=2 but 3 Usage events"),
    ("two_starts", "2 TurnStarted events"),
    ("app_indices", "recorded 2 call indices, the engine 3 Usage events"),
    ("keyed_row", "holds a keyed (post-fix) row"),
])
def test_inconsistent_evidence_is_not_assessable(mock_db, capsys, breakage, reason):
    sid, store = "app-broken", "ses_broken"
    _session(mock_db, sid, store)
    kwargs = {}
    if breakage == "terminal_disagrees":
        kwargs["terminal_sums"] = (SUMS[0] + 1, *SUMS[1:])
    if breakage == "call_count":
        kwargs["terminal_calls"] = 2
    seq = _engine_attempt(mock_db, store, "ft-1", T0, **kwargs)
    if breakage == "two_starts":
        mock_db.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) VALUES (?,?,?)",
            (store, seq, json.dumps({"_cls": "TurnStarted", "turn_id": "ft-1",
                                     "ts": T0.timestamp(), "model": "m",
                                     "provider": "p"})),
        )
    _app_turn(mock_db, sid, "turn-1", T0,
              calls=2 if breakage == "app_indices" else len(CALLS))
    killed_at = T0 + timedelta(minutes=10)
    _usage_row(mock_db, sid, killed_at)
    second = (SUMS[0] - 1, *SUMS[1:]) if breakage == "row_differs" else SUMS
    _usage_row(mock_db, sid, killed_at + timedelta(milliseconds=4), sums=second)
    if breakage == "keyed_row":
        _usage_row(mock_db, sid, killed_at + timedelta(seconds=1), key="attempt-x")
    mock_db.commit()
    rows, counters = _ledger(mock_db, sid), _counters(sid)

    assert d.reconcile(apply=True) == 0
    out = capsys.readouterr().out
    assert "NOT ASSESSABLE" in out and reason in out, out
    assert "DUPLICATE" not in out
    assert _ledger(mock_db, sid) == rows
    assert _counters(sid) == counters


def test_apply_refuses_when_counters_do_not_hold_the_duplicate(mock_db, caplog):
    sid = _seed_duplicate(mock_db)
    mock_db.execute("UPDATE sessions SET total_tokens_in=10 WHERE id=?", (sid,))
    mock_db.commit()
    rows = _ledger(mock_db, sid)

    assert d.reconcile(apply=True) == 1
    assert "Nothing was written" in caplog.text
    assert _ledger(mock_db, sid) == rows
    assert database.get_session(sid)["total_tokens_in"] == 10


def test_session_filter_and_keyed_rows(mock_db, capsys):
    sid = _seed_duplicate(mock_db)
    other = _seed_duplicate(mock_db, sid="app-other", store="ses_other",
                            turn_id="turn-other")
    # Rows booked through the settlement claim are never candidates.
    database.create_session(session_id="app-keyed", profile_id="default")
    assert settle_turn_usage("app-keyed", "attempt-1", tokens_in=5)
    assert settle_turn_usage("app-keyed", "attempt-2", tokens_in=5)

    assert d.reconcile(session_id=sid, apply=True) == 0
    assert "app-keyed" not in capsys.readouterr().out
    assert len(_ledger(mock_db, sid)) == 1
    assert len(_ledger(mock_db, other)) == 2
    assert len(_ledger(mock_db, "app-keyed")) == 2


def test_cli_defaults_to_read_only(mock_db, monkeypatch):
    sid = _seed_duplicate(mock_db)
    rows = _ledger(mock_db, sid)
    import app.db.base as base
    monkeypatch.setattr(base, "init_pool", lambda **kw: None)
    monkeypatch.setattr(base, "close_pool", lambda: None)

    assert d.main(["--session-id", sid]) == 0
    assert _ledger(mock_db, sid) == rows
    assert d.main(["--session-id", sid, "--apply"]) == 0
    assert len(_ledger(mock_db, sid)) == 1
