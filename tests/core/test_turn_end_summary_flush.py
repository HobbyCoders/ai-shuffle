"""Turn end brings the ``session_store_summaries`` sidecar current.

The fold is coalesced (finding B4): while a turn streams it runs at most once
every ``_SUMMARY_FOLD_INTERVAL_S``, so when the turn ends the sidecar is
normally a little behind and stays behind until some reader happens to flush it.
The list of sessions is very likely to be looked at right after a turn finishes,
so ``persist_turn_end_state`` flushes it there.

What these pin down:

* the flush actually happens at turn end, and the summary it leaves behind is
  identical to folding the whole transcript in one go;
* it runs AFTER the durable parked marker — the write a caller awaiting this
  coroutine is actually waiting for — and off the event loop;
* a flush that fails does not fail the turn. The watermark lives in Postgres, so
  the work is picked up by the next append or reader either way.

Reuses the in-memory Postgres stand-in from the fold tests, so the fold asserted
here is the real ``fold_session_summary``.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import pytest
from app.core.cli_sessions import fold_session_summary

import app.core.pg_session_store as pss
from app.core.turn_finalize import persist_turn_end_state

from tests.core.test_pg_session_store_summary_fold import (
    KEY,
    SID,
    FakeConn,
    FakeStore,
    _entry,
)


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


@pytest.fixture
def quiet_parked_marker(monkeypatch):
    """Stub the parked-marker half of turn end; record when it ran."""
    from app.core import task_registry as task_registry_mod
    from app.db import database

    calls = []
    monkeypatch.setattr(
        task_registry_mod.task_registry,
        "get_active_background_work",
        lambda _sid: [],
    )
    monkeypatch.setattr(
        database,
        "set_session_parked_task_kind",
        lambda sid, kind: calls.append(("parked", sid, kind)),
    )
    return calls


def test_turn_end_leaves_the_summary_current(store, quiet_parked_marker, monkeypatch):
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    entries = [_entry(i, f"chunk {i}") for i in range(6)]
    for e in entries:
        pss._append_sync(KEY, [e])

    # Coalesced: one fold for the burst, so the sidecar is behind right now.
    assert store.folds == 1
    assert store.summary()["folded_through_id"] == 1

    asyncio.run(persist_turn_end_state(SID))

    assert store.summary()["folded_through_id"] == 6
    assert store.summary()["data"] == fold_session_summary(None, KEY, entries)["data"]


def test_turn_end_flush_is_a_no_op_when_nothing_is_owed(
    store, quiet_parked_marker, monkeypatch
):
    """Nothing pending means no DB work at all — turn end stays cheap."""
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 0.0)  # fold every append
    pss._append_sync(KEY, [_entry(0, "a")])
    folds_before = store.folds

    asyncio.run(persist_turn_end_state(SID))

    assert store.folds == folds_before


def test_turn_end_flush_only_touches_this_session(
    store, quiet_parked_marker, monkeypatch
):
    monkeypatch.setattr(pss, "_SUMMARY_FOLD_INTERVAL_S", 60.0)
    other_key = {"project_key": KEY["project_key"], "session_id": "other-session"}
    for i in range(3):
        pss._append_sync(KEY, [_entry(i, f"mine {i}")])
        pss._append_sync(other_key, [_entry(i, f"theirs {i}")])

    asyncio.run(persist_turn_end_state(SID))

    assert store.summaries[(KEY["project_key"], SID)]["folded_through_id"] == 5
    # The other session is still owed a fold; turn end must not do its work.
    assert store.summaries[(KEY["project_key"], "other-session")][
        "folded_through_id"
    ] == 2


def test_a_failing_flush_does_not_fail_the_turn(store, quiet_parked_marker, monkeypatch):
    """A persistence bug must never break turn teardown."""
    def _boom(_session_id=None):
        raise RuntimeError("summary fold exploded")

    monkeypatch.setattr(pss, "flush_summary_folds", _boom)

    asyncio.run(persist_turn_end_state(SID))  # must not raise

    assert quiet_parked_marker == [("parked", SID, None)]


def test_the_flush_runs_after_the_parked_marker_and_off_the_loop(
    store, quiet_parked_marker, monkeypatch
):
    """Ordering and placement, both load-bearing.

    The parked marker is what an awaiting caller needs, so it goes first; the
    flush is synchronous psycopg2 work, so it goes to a worker thread rather
    than blocking the event loop the way an in-line fold would.
    """
    order = quiet_parked_marker
    loop_thread = threading.get_ident()
    flush_thread = {}

    def _record(session_id=None):
        flush_thread["id"] = threading.get_ident()
        order.append(("flush", session_id))
        return 0

    monkeypatch.setattr(pss, "flush_summary_folds", _record)

    asyncio.run(persist_turn_end_state(SID))

    assert [c[0] for c in order] == ["parked", "flush"]
    assert order[-1] == ("flush", SID)
    assert flush_thread["id"] != loop_thread
