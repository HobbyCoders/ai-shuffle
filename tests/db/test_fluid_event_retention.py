"""fluid_session_events retention (audit seam: 27,510 ToolArgsDelta rows).

The event log persists every bus event, so streaming deltas dominate it. Only
those are prunable, and only for sessions that have gone idle — everything else
in the log is evidence, and a live session's replay must not be disturbed.

Runs against pg_shim, so the SQL under test is the real Postgres statement the
app ships (the `->>` class filter included).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import get_db
from app.db.fluid_events import PRUNABLE_EVENT_CLASSES, prune_fluid_delta_events


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _mk_session(session_id, sdk_session_id, *, idle_days):
    """A sessions row whose updated_at puts it `idle_days` in the past."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions (id, sdk_session_id, title, status, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, sdk_session_id, session_id, "completed", _iso(idle_days)),
        )


def _seed_events(sdk_session_id, classes):
    """One event row per entry in `classes`, seq in order."""
    with get_db() as conn:
        cur = conn.cursor()
        for seq, cls in enumerate(classes):
            cur.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s, %s, %s)",
                (
                    sdk_session_id,
                    seq,
                    json.dumps(
                        {"_cls": cls, "session_id": sdk_session_id, "seq": seq}
                    ),
                ),
            )


def _remaining(sdk_session_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT event FROM fluid_session_events WHERE session_id = %s ORDER BY seq",
            (sdk_session_id,),
        )
        rows = cur.fetchall()
    out = []
    for row in rows:
        raw = row[0] if not isinstance(row, dict) else row["event"]
        out.append(json.loads(raw)["_cls"] if isinstance(raw, str) else raw["_cls"])
    return out


_MIXED = [
    "TurnStarted",
    "ToolArgsDelta",
    "ToolArgsDelta",
    "ToolUse",
    "ToolResult",
    "TextDelta",
    "ThinkingDelta",
    "ContextCompaction",
    "Usage",
    "TurnComplete",
]


# ─── pruning old deltas ──────────────────────────────────────────────────────


def test_prunes_delta_rows_from_an_idle_session(mock_db):
    _mk_session("s-old", "sdk-old", idle_days=30)
    _seed_events("sdk-old", _MIXED)

    deleted = prune_fluid_delta_events(7)

    assert deleted == 4  # 2x ToolArgsDelta + TextDelta + ThinkingDelta
    assert _remaining("sdk-old") == [
        "TurnStarted",
        "ToolUse",
        "ToolResult",
        "ContextCompaction",
        "Usage",
        "TurnComplete",
    ]


def test_keeps_the_accounting_and_transcript_evidence(mock_db):
    """Usage backs the backfill; ToolUse/ToolResult are code mode's only record."""
    _mk_session("s-keep", "sdk-keep", idle_days=90)
    _seed_events("sdk-keep", ["Usage", "ToolUse", "ToolResult", "TurnComplete"])

    assert prune_fluid_delta_events(7) == 0
    assert _remaining("sdk-keep") == ["Usage", "ToolUse", "ToolResult", "TurnComplete"]


def test_prunes_only_the_enumerated_delta_classes(mock_db):
    _mk_session("s-cls", "sdk-cls", idle_days=30)
    survivors = [
        "TurnStarted",
        "BlockStart",
        "BlockStop",
        "ToolUse",
        "ToolResult",
        "Usage",
        "TurnComplete",
        "ContextCompaction",
        "Interrupted",
        "ErrorEvent",
        "RateLimit",
        "HandoffCreated",
        "SubagentEvent",
    ]
    _seed_events("sdk-cls", survivors + list(PRUNABLE_EVENT_CLASSES))

    assert prune_fluid_delta_events(7) == len(PRUNABLE_EVENT_CLASSES)
    assert _remaining("sdk-cls") == survivors


# ─── the idle window ─────────────────────────────────────────────────────────


def test_never_touches_a_session_active_within_the_window(mock_db):
    """A streaming session bumps updated_at constantly, so it stays outside."""
    _mk_session("s-live", "sdk-live", idle_days=0)
    _seed_events("sdk-live", _MIXED)

    assert prune_fluid_delta_events(7) == 0
    assert _remaining("sdk-live") == _MIXED


def test_window_boundary_is_honored(mock_db):
    _mk_session("s-recent", "sdk-recent", idle_days=3)
    _mk_session("s-stale", "sdk-stale", idle_days=11)
    _seed_events("sdk-recent", ["ToolArgsDelta", "Usage"])
    _seed_events("sdk-stale", ["ToolArgsDelta", "Usage"])

    assert prune_fluid_delta_events(7) == 1
    assert _remaining("sdk-recent") == ["ToolArgsDelta", "Usage"]
    assert _remaining("sdk-stale") == ["Usage"]


def test_a_wider_window_spares_more(mock_db):
    _mk_session("s-mid", "sdk-mid", idle_days=20)
    _seed_events("sdk-mid", ["ToolArgsDelta", "Usage"])

    assert prune_fluid_delta_events(60) == 0
    assert prune_fluid_delta_events(7) == 1


def test_leaves_sessions_with_no_app_row_alone(mock_db):
    """Nothing to date them by, so they are never pruned."""
    _seed_events("sdk-orphan", _MIXED)

    assert prune_fluid_delta_events(7) == 0
    assert _remaining("sdk-orphan") == _MIXED


# ─── the disable switch ──────────────────────────────────────────────────────


@pytest.mark.parametrize("window", [0, -1])
def test_disabled_when_window_is_zero_or_negative(mock_db, window):
    _mk_session("s-off", "sdk-off", idle_days=365)
    _seed_events("sdk-off", _MIXED)

    assert prune_fluid_delta_events(window) == 0
    assert _remaining("sdk-off") == _MIXED


# ─── maintenance wiring ──────────────────────────────────────────────────────


def test_maintenance_pass_prunes_and_reports(mock_db, monkeypatch):
    from app.core.config import settings
    from app.db import database

    monkeypatch.setattr(settings, "fluid_event_retention_days", 7, raising=False)
    _mk_session("s-maint", "sdk-maint", idle_days=30)
    _seed_events("sdk-maint", _MIXED)

    results = database.run_database_maintenance(skip_vacuum=True)

    assert results["cleaned_fluid_delta_events"] == 4
    assert "ToolArgsDelta" not in _remaining("sdk-maint")
    assert "Usage" in _remaining("sdk-maint")


def test_maintenance_pass_reports_disabled_when_set_to_zero(mock_db, monkeypatch):
    from app.core.config import settings
    from app.db import database

    monkeypatch.setattr(settings, "fluid_event_retention_days", 0, raising=False)
    _mk_session("s-maint-off", "sdk-maint-off", idle_days=30)
    _seed_events("sdk-maint-off", _MIXED)

    results = database.run_database_maintenance(skip_vacuum=True)

    assert results["cleaned_fluid_delta_events"] == "disabled"
    assert _remaining("sdk-maint-off") == _MIXED


def test_retention_default_is_seven_days():
    from app.core.config import Settings

    assert Settings.model_fields["fluid_event_retention_days"].default == 7
