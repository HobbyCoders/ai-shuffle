"""Coalesced session-store summary fold (finding B4).

The SDK mirrors the transcript into Postgres roughly every 100ms while a turn
streams. Folding the ``session_store_summaries`` sidecar on every one of those
appends meant 4-5 statements + a commit ~10x/second per streaming session, each
one rewriting the whole summary JSONB row (a dead tuple per append) for a
sidecar nothing reads mid-turn.

The fold now runs at most once per ``_SUMMARY_FOLD_INTERVAL_S`` inside the
append path, plus on demand from the readers that consume summaries. What these
tests pin down:

* the entries are ALWAYS inserted, whether or not this append folds;
* a burst of appends produces ONE fold, not one per append;
* the folded summary is identical to folding the whole transcript in one go —
  the watermark (``folded_through_id``) makes the deferred fold pick up exactly
  the entries the previous one stopped at;
* a reader flushes what is still owed, so no reader sees a permanently stale
  summary;
* a fold never RE-CREATES a summary row that a delete/rewind removed (the row
  is a tombstone the lazy JSONL backfill depends on);
* a failed fold is retried rather than lost.

Runs WITHOUT Postgres: ``get_db`` is replaced by a small in-memory fake that
implements exactly the statements this module issues. The fold itself (the
SDK's ``fold_session_summary``) is the real one, so the summary contents
asserted here are the real contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from app.core.cli_sessions import fold_session_summary

import app.core.pg_session_store as pss


PROJECT_KEY = "-workspace-proj"
SID = "11111111-2222-3333-4444-555555555555"
KEY = {"project_key": PROJECT_KEY, "session_id": SID}


def _entry(i: int, text: str) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": f"u{i}",
        "timestamp": f"2024-01-01T00:00:{i:02d}.000Z",
        "sessionId": SID,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


class FakeStore:
    """In-memory stand-in for the two session_store_* tables."""

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []  # {id, project_key, session_id, subpath, entry}
        self.summaries: Dict[tuple, Dict[str, Any]] = {}
        self._next_id = 1
        self.folds = 0        # summary UPDATEs issued
        self.fail_folds = False

    # -- helpers ---------------------------------------------------------
    def main_entries(self) -> List[Dict[str, Any]]:
        return [
            r["entry"] for r in self.entries
            if r["session_id"] == SID and r["subpath"] is None
        ]

    def summary(self, session_id: str = SID) -> Optional[Dict[str, Any]]:
        return self.summaries.get((PROJECT_KEY, session_id))


class FakeCursor:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self._result: List[Dict[str, Any]] = []
        self.rowcount = 0

    # -- psycopg2-ish surface -------------------------------------------
    def executemany(self, sql: str, seq) -> None:
        for params in seq:
            self.execute(sql, params)

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        p = params or ()
        st = self.store
        self._result = []

        if s.startswith("INSERT INTO session_store_entries"):
            project_key, session_id, subpath, entry = p
            st.entries.append({
                "id": st._next_id,
                "project_key": project_key,
                "session_id": session_id,
                "subpath": subpath,
                # psycopg2 Json wrapper -> the dict it adapts
                "entry": getattr(entry, "adapted", entry),
            })
            st._next_id += 1
            return

        if s.startswith("INSERT INTO session_store_summaries"):
            project_key, session_id, data = p
            st.summaries.setdefault(
                (project_key, session_id),
                {
                    "data": getattr(data, "adapted", data),
                    "mtime_ms": 0,
                    "folded_through_id": 0,
                },
            )
            return

        if s.startswith("SELECT data, mtime_ms, folded_through_id"):
            project_key, session_id = p
            row = st.summaries.get((project_key, session_id))
            self._result = [dict(row)] if row is not None else []
            return

        if s.startswith("SELECT id, entry FROM session_store_entries"):
            session_id, watermark = p
            self._result = [
                {"id": r["id"], "entry": r["entry"]}
                for r in st.entries
                if r["session_id"] == session_id
                and r["subpath"] is None
                and r["id"] > watermark
            ]
            return

        if s.startswith("UPDATE session_store_summaries"):
            if st.fail_folds:
                raise RuntimeError("fold write failed")
            data, last_id, project_key, session_id = p
            row = st.summaries[(project_key, session_id)]
            row["data"] = getattr(data, "adapted", data)
            row["mtime_ms"] = 1_700_000_000_000 + last_id
            row["folded_through_id"] = last_id
            st.folds += 1
            return

        if s.startswith("SELECT s.session_id FROM session_store_summaries"):
            project_key, limit = p
            stale = []
            for (pk, sid), row in st.summaries.items():
                if pk != project_key:
                    continue
                if any(
                    r["session_id"] == sid
                    and r["subpath"] is None
                    and r["id"] > row["folded_through_id"]
                    for r in st.entries
                ):
                    stale.append({"session_id": sid})
            self._result = stale[:limit]
            return

        if s.startswith("SELECT session_id, data, mtime_ms"):
            project_key = p[0]
            self._result = [
                {"session_id": sid, "data": row["data"], "mtime_ms": row["mtime_ms"]}
                for (pk, sid), row in st.summaries.items()
                if pk == project_key
            ]
            return

        if s.startswith("SELECT session_id,"):  # _list_sessions_sync
            self._result = []
            return

        if s.startswith("DELETE FROM session_store_entries"):
            self.store.entries = [
                r for r in st.entries if r["session_id"] != p[0]
            ]
            self.rowcount = 1
            self._result = []
            return

        if s.startswith("DELETE FROM session_store_summaries"):
            for k in [k for k in st.summaries if k[1] == p[0]]:
                st.summaries.pop(k)
            self.rowcount = 1
            self._result = []
            return

        raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()

    @contextmanager
    def _get_db():
        yield FakeConn(fake)

    monkeypatch.setattr(pss, "get_db", _get_db)
    pss._reset_fold_state()
    yield fake
    pss._reset_fold_state()


def _expected_summary(entries) -> Dict[str, Any]:
    """What a single fold over the WHOLE transcript produces."""
    return fold_session_summary(None, KEY, list(entries))["data"]


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------

def test_burst_of_appends_folds_once_but_stores_everything(store, monkeypatch):
    """~10 appends/second during streaming must not mean ~10 folds/second."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)

    for i in range(20):
        pss._append_sync(KEY, [_entry(i, f"chunk {i}")])

    assert len(store.main_entries()) == 20, "entries must never be deferred"
    assert store.folds == 1, f"expected one fold for the burst, got {store.folds}"
    assert store.summary() is not None, "the tombstone row is created immediately"


def test_fold_runs_again_once_the_interval_elapses(store, monkeypatch):
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    now = [1000.0]
    monkeypatch.setattr(pss.time, "monotonic", lambda: now[0])

    pss._append_sync(KEY, [_entry(0, "a")])
    assert store.folds == 1

    pss._append_sync(KEY, [_entry(1, "b")])
    assert store.folds == 1  # still inside the interval

    now[0] += 61.0
    pss._append_sync(KEY, [_entry(2, "c")])
    assert store.folds == 2


def test_deferred_fold_is_equivalent_to_folding_everything_at_once(store, monkeypatch):
    """The watermark must make the coalesced fold produce EXACTLY the summary a
    per-append fold produced — that is what makes deferring it safe."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    entries = [_entry(i, f"chunk {i}") for i in range(6)]

    for e in entries:
        pss._append_sync(KEY, [e])
    pss.flush_summary_folds()

    assert store.summary()["folded_through_id"] == 6
    assert store.summary()["data"] == _expected_summary(entries)


def test_second_fold_does_not_re_fold_already_folded_entries(store, monkeypatch):
    """Entries at or below the watermark are never folded twice (a double fold
    would double-count anything the SDK accumulates)."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 0.0)  # fold every append
    entries = [_entry(i, f"chunk {i}") for i in range(4)]
    for e in entries:
        pss._append_sync(KEY, [e])

    assert store.folds == 4
    assert store.summary()["folded_through_id"] == 4

    # An append that adds nothing new leaves the summary untouched.
    before = dict(store.summary())
    pss.flush_summary_folds()
    assert store.summary() == before


def test_subagent_appends_never_touch_the_main_summary(store, monkeypatch):
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 0.0)
    sub_key = dict(KEY, subpath="subagents/agent-1")

    pss._append_sync(sub_key, [_entry(0, "sub")])

    assert store.summaries == {}
    assert store.folds == 0
    assert len(store.entries) == 1


# ---------------------------------------------------------------------------
# Flush on read / crash recovery
# ---------------------------------------------------------------------------

def test_summary_reader_flushes_what_is_still_pending(store, monkeypatch):
    """Requirement (a): no reader ever sees a permanently stale summary."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    entries = [_entry(i, f"chunk {i}") for i in range(3)]
    for e in entries:
        pss._append_sync(KEY, [e])
    assert store.summary()["folded_through_id"] == 1  # only the first fold ran

    summaries = pss._list_session_summaries_sync(PROJECT_KEY)

    assert store.summary()["folded_through_id"] == 3
    assert summaries[0]["data"] == _expected_summary(entries)


def test_stale_summary_from_a_crashed_process_is_repaired_on_read(store, monkeypatch):
    """Requirement (b): the in-process pending list dies with the process, so a
    reader also repairs rows whose watermark trails their entries."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    entries = [_entry(i, f"chunk {i}") for i in range(4)]
    for e in entries:
        pss._append_sync(KEY, [e])

    # Simulate the restart: this process no longer knows a fold is owed.
    pss._reset_fold_state()
    assert store.summary()["folded_through_id"] == 1

    summaries = pss._list_session_summaries_sync(PROJECT_KEY)

    assert store.summary()["folded_through_id"] == 4
    assert summaries[0]["data"] == _expected_summary(entries)


def test_flush_is_a_no_op_when_nothing_is_pending(store, monkeypatch):
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 0.0)
    pss._append_sync(KEY, [_entry(0, "a")])
    folds_before = store.folds

    assert pss.flush_summary_folds() == 0
    assert store.folds == folds_before


def test_failed_flush_is_retried(store, monkeypatch):
    """A fold that fails must stay owed — never silently dropped."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    pss._append_sync(KEY, [_entry(0, "a")])
    pss._append_sync(KEY, [_entry(1, "b")])  # pending

    store.fail_folds = True
    assert pss.flush_summary_folds() == 0

    store.fail_folds = False
    assert pss.flush_summary_folds() == 1
    assert store.summary()["folded_through_id"] == 2


# ---------------------------------------------------------------------------
# The summary row is a tombstone — a fold must never resurrect it
# ---------------------------------------------------------------------------

def test_fold_never_recreates_a_deleted_summary_row(store, monkeypatch):
    """The row means "this session WAS store-recorded"; the lazy JSONL backfill
    reads it to refuse resurrecting a deleted/rewound session."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    pss._append_sync(KEY, [_entry(0, "a")])
    pss._append_sync(KEY, [_entry(1, "b")])  # leaves a pending fold

    store.summaries.clear()  # the session was deleted meanwhile

    assert pss.flush_summary_folds() == 0
    assert store.summaries == {}


def test_delete_drops_the_pending_fold(store):
    pss._append_sync(KEY, [_entry(0, "a")])
    pss._append_sync(KEY, [_entry(1, "b")])
    pss._delete_session_data_by_session_id_sync(SID)

    assert SID not in pss._fold_state
