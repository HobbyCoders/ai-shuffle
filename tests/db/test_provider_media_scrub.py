"""Tests for the one-time scrub of media models out of provider rosters.

Filtering the fetch route alone does not help an existing install: the provider
panel deliberately re-surfaces stored ids the catalog no longer offers, so a
hidden model would come back as the first row, checked. The stored data has to
be corrected, once — that is what this migration does.

Driven through a fake cursor so the logic is tested without standing up a
database: what matters here is which UPDATEs are issued, not how they execute.
"""

import json

from app.db.database import _backfill_provider_media_models


class FakeCursor:
    """Enough DB-API surface for the backfill: one SELECT, then UPDATEs.

    Savepoint statements are recorded separately. The scrub fences its roster
    read and every row it touches with a SAVEPOINT so one bad row cannot abort
    the shared boot transaction on real Postgres; what these tests are about is
    which UPDATEs get issued, so the fencing is bookkeeping, not an update.
    """

    _SAVEPOINT_PREFIXES = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")

    def __init__(self, rows):
        self._rows = rows
        self.updates = []
        self.savepoints = []

    def execute(self, sql, params=None):
        self._last = sql
        upper = sql.strip().upper()
        if upper.startswith(self._SAVEPOINT_PREFIXES):
            self.savepoints.append(sql.strip())
        elif upper.startswith("SELECT"):
            self._pending = list(self._rows)
        else:
            self.updates.append((sql, params))

    def fetchall(self):
        return getattr(self, "_pending", [])


def _row(provider_id, model_ids, default_model=None, small_fast_model=None):
    return {
        "id": provider_id,
        "models_json": json.dumps([{"id": m, "label": m} for m in model_ids]),
        "default_model": default_model,
        "small_fast_model": small_fast_model,
    }


def test_media_ids_are_removed_and_text_ids_kept():
    cursor = FakeCursor([
        _row("p1", ["gpt-5.6-sol", "gpt-realtime-2.1", "whisper-1", "gpt-4o-audio-preview"])
    ])

    result = _backfill_provider_media_models(cursor)

    assert result == (2, 1)
    assert len(cursor.updates) == 1
    sql, params = cursor.updates[0]
    kept = [m["id"] for m in json.loads(params[0])]
    assert kept == ["gpt-5.6-sol", "gpt-4o-audio-preview"]


def test_default_model_pointing_at_a_removed_id_is_cleared():
    """Leaving it dangling is worse than the original problem: the row would
    resolve to a different model than it claims, silently."""
    cursor = FakeCursor([
        _row("p1", ["gpt-5.6-sol", "gpt-realtime-2.1"], default_model="gpt-realtime-2.1")
    ])

    _backfill_provider_media_models(cursor)

    sql, _params = cursor.updates[0]
    assert "default_model = NULL" in sql
    assert "small_fast_model = NULL" not in sql


def test_small_fast_model_is_cleared_too():
    cursor = FakeCursor([
        _row(
            "p1",
            ["gpt-5.6-sol", "tts-1"],
            default_model="gpt-5.6-sol",
            small_fast_model="tts-1",
        )
    ])

    _backfill_provider_media_models(cursor)

    sql, _params = cursor.updates[0]
    assert "small_fast_model = NULL" in sql
    # The default still points at a surviving model, so it must be left alone.
    assert "default_model = NULL" not in sql


def test_a_clean_row_is_not_written_at_all():
    cursor = FakeCursor([_row("p1", ["gpt-5.6-sol", "gpt-4o-audio-preview"])])

    assert _backfill_provider_media_models(cursor) is None
    assert cursor.updates == []


def test_second_run_is_a_no_op():
    """Idempotence: the migration pass can re-run without re-writing rows."""
    first = FakeCursor([_row("p1", ["gpt-5.6-sol", "whisper-1"])])
    _backfill_provider_media_models(first)
    kept = json.loads(first.updates[0][1][0])

    second = FakeCursor([
        {
            "id": "p1",
            "models_json": json.dumps(kept),
            "default_model": None,
            "small_fast_model": None,
        }
    ])

    assert _backfill_provider_media_models(second) is None
    assert second.updates == []


def test_counts_span_multiple_rows():
    cursor = FakeCursor([
        _row("p1", ["gpt-5.6-sol", "whisper-1"]),
        _row("p2", ["dall-e-3", "sora-2", "grok-4.6"]),
        _row("p3", ["gpt-5.4-mini"]),
    ])

    assert _backfill_provider_media_models(cursor) == (3, 2)
    assert len(cursor.updates) == 2


def test_a_malformed_row_does_not_stop_the_rest():
    cursor = FakeCursor([
        {
            "id": "bad",
            "models_json": "{not json",
            "default_model": None,
            "small_fast_model": None,
        },
        _row("good", ["gpt-5.6-sol", "whisper-1"]),
    ])

    assert _backfill_provider_media_models(cursor) == (1, 1)
    assert len(cursor.updates) == 1


def test_empty_and_missing_rosters_are_skipped():
    cursor = FakeCursor([
        {"id": "a", "models_json": None, "default_model": None, "small_fast_model": None},
        {"id": "b", "models_json": "[]", "default_model": None, "small_fast_model": None},
    ])

    assert _backfill_provider_media_models(cursor) is None
    assert cursor.updates == []
