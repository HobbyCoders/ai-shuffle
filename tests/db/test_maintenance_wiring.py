"""``run_database_maintenance`` wiring.

The maintenance routine is where the periodic, table-shrinking passes are
actually invoked from; a pass that exists but is never called does nothing at
runtime. These tests assert the call happens and SUCCEEDS (an exception inside
the routine is swallowed into an ``"error: ..."`` string, so "the key is
present" is not enough — it has to be a count).
"""

from __future__ import annotations

from app.db import database


def test_maintenance_runs_session_event_delta_compaction(mock_db):
    """Finding B6: session_events delta compaction is driven from maintenance."""
    results = database.run_database_maintenance(skip_vacuum=True)

    assert "compacted_session_event_deltas" in results
    value = results["compacted_session_event_deltas"]
    assert isinstance(value, int), f"compaction pass failed: {value!r}"


def test_maintenance_still_runs_the_neighbouring_passes(mock_db):
    """Guard against a new pass raising out of the shared transaction and
    taking the rest of the routine down with it."""
    results = database.run_database_maintenance(skip_vacuum=True)

    assert results.get("analyze") == "ok"
    assert not isinstance(results.get("cleaned_fluid_delta_events"), str) or results[
        "cleaned_fluid_delta_events"
    ] in ("disabled",)
