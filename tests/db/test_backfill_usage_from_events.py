"""``scripts/backfill_usage_from_events`` replays the fluid event log onto the
session row.

Sessions written before the accounting fix carry a distorted picture: cache
tokens were never accumulated anywhere, and ``turn_count`` was fed the model-call
count. ``fluid_session_events`` still holds the evidence — one ``Usage`` event per
model call, one ``TurnComplete`` per turn — so the numbers can be recomputed
exactly.

The properties that matter for an operator tool: it repairs what it can, it
leaves alone what it has no evidence for, ``--dry-run`` writes nothing, and a
second run is a no-op.
"""

import json

import pytest

from scripts import backfill_usage_from_events as backfill


@pytest.fixture(autouse=True)
def _foreign_keys_off(test_db):
    """Run on the shared conftest DB with foreign keys off: the backfill
    touches only sessions; the FK to projects is irrelevant here."""
    test_db.execute("PRAGMA foreign_keys = OFF")


@pytest.fixture
def shim(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db) as shim:
        yield shim


def _seed_session(shim, sid, sdk_session_id, **columns):
    columns.setdefault("total_tokens_in", 0)
    names = ["id", "sdk_session_id", *columns]
    values = [sid, sdk_session_id, *columns.values()]
    placeholders = ", ".join(["%s"] * len(names))
    shim.cursor().execute(
        f"INSERT INTO sessions ({', '.join(names)}) VALUES ({placeholders})",
        values,
    )


def _seed_events(shim, sdk_session_id, events):
    cur = shim.cursor()
    for seq, event in enumerate(events):
        cur.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) "
            "VALUES (%s, %s, %s)",
            (sdk_session_id, seq, json.dumps(event)),
        )


def _usage(input_tokens, output_tokens, cache_read, cache_creation):
    return {
        "_cls": "Usage",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }


def _turn_complete():
    return {"_cls": "TurnComplete", "stop_reason": "end_turn", "num_model_calls": 2}


def _row(shim, sid):
    cur = shim.cursor()
    cur.execute(
        "SELECT total_tokens_in, total_tokens_out, total_cache_read_tokens, "
        "total_cache_creation_tokens, total_model_calls, turn_count "
        "FROM sessions WHERE id = %s",
        (sid,),
    )
    return dict(cur.fetchone())


def test_the_hidden_cache_tokens_come_back(shim):
    """The audit's session: booked as 15 tokens, actually a quarter-million."""
    _seed_session(shim, "sess-1", "ses_abc", total_tokens_in=15, turn_count=16)
    _seed_events(shim, "ses_abc", [
        _usage(10, 400, 180_000, 12_000),
        _usage(5, 120, 73_000, 1_500),
        _turn_complete(),
    ])

    backfill.backfill(dry_run=False)

    row = _row(shim, "sess-1")
    assert row["total_tokens_in"] == 15
    assert row["total_tokens_out"] == 520
    assert row["total_cache_read_tokens"] == 253_000
    assert row["total_cache_creation_tokens"] == 13_500
    # Two API hops inside ONE turn — the distinction turn_count never made.
    assert row["total_model_calls"] == 2
    assert row["turn_count"] == 1


def test_dry_run_writes_nothing(shim):
    _seed_session(shim, "sess-2", "ses_dry", total_tokens_in=15, turn_count=16)
    _seed_events(shim, "ses_dry", [_usage(15, 20, 5_000, 100), _turn_complete()])

    backfill.backfill(dry_run=True)

    row = _row(shim, "sess-2")
    assert row["total_cache_read_tokens"] == 0
    assert row["turn_count"] == 16


def test_a_second_run_changes_nothing(shim):
    _seed_session(shim, "sess-3", "ses_idem", total_tokens_in=1, turn_count=9)
    _seed_events(shim, "ses_idem", [_usage(10, 20, 5_000, 100), _turn_complete()])

    backfill.backfill(dry_run=False)
    first = _row(shim, "sess-3")
    backfill.backfill(dry_run=False)

    assert _row(shim, "sess-3") == first


def test_a_session_without_an_event_log_is_left_alone(shim):
    """No evidence is not the same as zero — a delegated-leg or pre-fluid row
    keeps whatever the old path managed to record."""
    _seed_session(shim, "sess-4", "ses_none", total_tokens_in=1234, turn_count=7)

    backfill.backfill(dry_run=False)

    row = _row(shim, "sess-4")
    assert row["total_tokens_in"] == 1234
    assert row["turn_count"] == 7


def test_a_partial_event_log_is_left_alone(shim):
    """``sdk_session_id`` names only the LATEST fluid log. A re-keyed or
    mixed-leg session has booked usage the log never saw; recomputing absolute
    totals from the fragment would erase it. The tell: recomputed fresh in/out
    below what is already booked (the repair only ever adds unbooked usage)."""
    _seed_session(
        shim, "sess-7", "ses_frag",
        total_tokens_in=242, total_tokens_out=94_652, turn_count=41,
    )
    # The surviving fragment: a handful of tiny late turns.
    _seed_events(shim, "ses_frag", [_usage(25, 39, 85_000, 120_000), _turn_complete()])

    backfill.backfill(dry_run=False)

    row = _row(shim, "sess-7")
    assert row["total_tokens_in"] == 242
    assert row["total_tokens_out"] == 94_652
    assert row["turn_count"] == 41
    assert row["total_cache_read_tokens"] == 0  # untouched, not "repaired"


def test_a_single_session_can_be_targeted(shim):
    _seed_session(shim, "sess-5", "ses_a", total_tokens_in=1, turn_count=5)
    _seed_session(shim, "sess-6", "ses_b", total_tokens_in=1, turn_count=5)
    _seed_events(shim, "ses_a", [_usage(2, 3, 40, 5), _turn_complete()])
    _seed_events(shim, "ses_b", [_usage(2, 3, 40, 5), _turn_complete()])

    backfill.backfill(dry_run=False, session_id="sess-5")

    assert _row(shim, "sess-5")["turn_count"] == 1
    assert _row(shim, "sess-6")["turn_count"] == 5
