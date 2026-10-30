from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.db import transcript_projections as store


def state():
    store.initialize()
    result = store.ProjectionState("synthetic-session", "generation-a", "pipeline", "epoch-a", 10, "tail",
                                   ready=1, stage="ready", preview_json="opaque-checkpoint")
    store.commit(result, None)
    return result


def record(index):
    return {"id": f"message-{index}", "order": index, "origin": index + 1,
            "foldedThrough": index + 1, "aliases": [f"message-{index}", f"tool-{index}"],
            "body": 'opaque-' + str(index)}


def test_generation_revision_cas_and_alias_updates_are_atomic():
    before = state()
    current = replace(before)
    store.commit(current, before, upserts=[record(0)])
    assert store.referenced_records(current, ["tool-0"])[0]["id"] == "message-0"
    with pytest.raises(store.ProjectionStoreUnavailable, match="stale_projection"):
        store.commit(replace(before), before, upserts=[record(1)])
    assert store.referenced_records(current, ["tool-1"]) == []
    row = record(0)
    row["aliases"] = ["new-alias"]
    latest = replace(current)
    store.commit(latest, current, upserts=[row])
    assert store.referenced_records(latest, ["tool-0"]) == []
    assert store.referenced_records(latest, ["new-alias"])[0]["id"] == "message-0"


@pytest.mark.parametrize("count", [1000, 10000])
def test_logical_page_query_count_is_bounded_independent_of_history(monkeypatch, count):
    current = state()
    for start in range(0, count, 128):
        previous = current
        current = replace(previous)
        store.commit(current, previous, upserts=[record(i) for i in range(start, min(start + 128, count))])
    original = store.get_db
    statements = []

    class Cursor:
        def __init__(self, cursor):
            self.inner = cursor
        def execute(self, sql, params=()):
            statements.append(sql)
            return self.inner.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self.inner, name)

    class Connection:
        def __init__(self, connection):
            self.inner = connection
        def cursor(self):
            return Cursor(self.inner.cursor())

    @contextmanager
    def counted():
        with original() as connection:
            yield Connection(connection)

    monkeypatch.setattr(store, "get_db", counted)
    result = store.page(current.session_id, current.generation, current.revision, before=count)
    assert len(result["records"]) == store.MAX_PAGE_ROWS
    assert result["hasOlder"] is True
    assert result["before"] == count - store.MAX_PAGE_ROWS
    assert len(statements) == 4
    assert not any("session_events" in sql for sql in statements)
    assert [row["order"] for row in result["records"]] == list(range(count - 96, count))


def test_epoch_generation_change_rejects_old_cursor_and_never_mixes_records():
    previous = state()
    current = replace(previous)
    store.commit(current, previous, upserts=[record(0)])
    newer = replace(current, generation="generation-b", epoch="epoch-b")
    store.commit(newer, current, upserts=[{**record(0), "body": "replacement"}])
    with pytest.raises(store.ProjectionStoreUnavailable, match="stale_projection"):
        store.page(current.session_id, current.generation, current.revision, before=1)
    result = store.page(newer.session_id, newer.generation, newer.revision, before=1)
    assert [row["body"] for row in result["records"]] == ["replacement"]


def test_corrupt_opaque_record_fails_closed():
    previous = state()
    current = replace(previous)
    store.commit(current, previous, upserts=[record(0)])
    with store.get_db() as connection:
        connection.cursor().execute("UPDATE session_transcript_projection_records SET body=%s", ("changed",))
    with pytest.raises(store.ProjectionStoreUnavailable, match="record_changed"):
        store.referenced_records(current, ["tool-0"])
