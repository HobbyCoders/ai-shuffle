"""Orphan reconciler: one aggregation per sweep + the inconclusive memo (F6).

The reconciler used to run a GROUP-BY over the whole ``session_store_entries``
table ONCE PER NULL-LINK SESSION, every five minutes, for fourteen days — and an
ordinary session that was opened and never used stays NULL-link forever, so it
re-triggered that aggregation on every single sweep.

Two changes are pinned here:

  * one aggregation per sweep for ALL examined sessions (their per-session
    windows are applied in Python afterwards), and
  * a per-process memo of ids that came back inconclusive, so those sessions are
    not re-examined until it ages out — while a session that later BECOMES
    healable is still healed.

The database is faked at the ``get_db``/``get_cursor`` seam so the queries the
reconciler issues can be counted exactly.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.core import orphan_reconciler as orphan


class FakeCursor:
    """Cursor that answers the reconciler's two query shapes from fixtures."""

    def __init__(self, sessions, transcripts):
        self.sessions = sessions          # list of session row dicts
        self.transcripts = transcripts    # list of transcript aggregate dicts
        self.session_queries = 0
        self.aggregations = 0
        self._rows = []

    def execute(self, sql, params=None):
        if "FROM sessions" in sql and "sdk_session_id IS NULL" in sql:
            self.session_queries += 1
            self._rows = list(self.sessions)
        elif "FROM session_store_entries" in sql:
            self.aggregations += 1
            self._rows = list(self.transcripts)
        else:  # the UPDATE
            self._rows = []
        self.rowcount = 1

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture(autouse=True)
def clean_memo():
    orphan.reset_memo()
    yield
    orphan.reset_memo()


def _install(monkeypatch, sessions, transcripts):
    cur = FakeCursor(sessions, transcripts)

    @contextmanager
    def _get_db():
        yield object()

    monkeypatch.setattr(orphan, "get_db", _get_db)
    monkeypatch.setattr(orphan, "get_cursor", lambda _conn: cur)
    return cur


def _session(sid, created):
    return {
        "id": sid, "created_at": created, "status": "active",
        "turn_count": 0, "project_id": None, "worktree_id": None,
    }


def _transcript(sdk_id, first_at, n=5):
    return {
        "session_id": sdk_id, "n": n,
        "first_at": first_at, "last_at": first_at,
    }


NOW = datetime.now(timezone.utc)


# ─── One aggregation, not N ───────────────────────────────────────────────


def test_one_aggregation_for_many_null_sessions(monkeypatch):
    sessions = [
        _session(f"s{i}", NOW - timedelta(minutes=10 + i)) for i in range(25)
    ]
    cur = _install(monkeypatch, sessions, [])

    orphan.find_orphan_candidates()

    assert cur.session_queries == 1
    assert cur.aggregations == 1, (
        f"{cur.aggregations} aggregations for {len(sessions)} sessions — the "
        f"per-session GROUP-BY is back"
    )


def test_per_session_window_still_selects_the_right_transcript(monkeypatch):
    """The Python window filter must reproduce the old per-session HAVING."""
    a_created = NOW - timedelta(minutes=30)
    b_created = NOW - timedelta(minutes=10)
    sessions = [_session("a", a_created), _session("b", b_created)]
    transcripts = [
        _transcript("sdk-a", a_created + timedelta(seconds=1)),
        _transcript("sdk-b", b_created + timedelta(seconds=2)),
    ]
    _install(monkeypatch, sessions, transcripts)

    records = {r["session_id"]: r for r in orphan.find_orphan_candidates()}

    assert records["a"]["confidence"] == "confident"
    assert records["a"]["candidates"][0]["session_id"] == "sdk-a"
    assert records["b"]["confidence"] == "confident"
    assert records["b"]["candidates"][0]["session_id"] == "sdk-b"


def test_transcript_outside_the_window_is_not_a_candidate(monkeypatch):
    created = NOW - timedelta(minutes=30)
    sessions = [_session("a", created)]
    # 10 minutes after creation — far outside the 120s window.
    transcripts = [_transcript("sdk-late", created + timedelta(minutes=10))]
    _install(monkeypatch, sessions, transcripts)

    record = orphan.find_orphan_candidates()[0]

    assert record["confidence"] == "none"
    assert record["candidates"] == []


def test_two_transcripts_in_one_window_stay_ambiguous(monkeypatch):
    created = NOW - timedelta(minutes=30)
    sessions = [_session("a", created)]
    transcripts = [
        _transcript("sdk-1", created + timedelta(seconds=1)),
        _transcript("sdk-2", created + timedelta(seconds=3)),
    ]
    _install(monkeypatch, sessions, transcripts)

    assert orphan.find_orphan_candidates()[0]["confidence"] == "ambiguous"


# ─── The memo ─────────────────────────────────────────────────────────────


def test_memo_skips_the_aggregation_on_the_next_sweep(monkeypatch):
    """A never-used session must not re-trigger the aggregation forever."""
    sessions = [_session("never-used", NOW - timedelta(hours=2))]
    cur = _install(monkeypatch, sessions, [])

    orphan.reconcile_orphaned_sessions()
    assert cur.aggregations == 1

    for _ in range(5):
        orphan.reconcile_orphaned_sessions()

    assert cur.aggregations == 1, (
        f"{cur.aggregations} aggregations — the memo is not suppressing the "
        f"repeat scan of an already-inconclusive session"
    )
    assert cur.session_queries == 6  # the cheap outer read still runs


def test_memo_does_not_suppress_a_session_that_gains_entries(monkeypatch):
    """The memo may DELAY a heal; it must never prevent one."""
    created = NOW - timedelta(hours=2)
    sessions = [_session("orphan", created)]
    cur = _install(monkeypatch, sessions, [])

    # Sweep 1: no transcripts exist yet -> inconclusive, memoized.
    assert orphan.reconcile_orphaned_sessions() == []
    assert cur.aggregations == 1

    # The session's transcript shows up (a crash left the link NULL).
    cur.transcripts = [_transcript("sdk-orphan", created + timedelta(seconds=1))]

    # Still inside the TTL: skipped, as designed.
    assert orphan.reconcile_orphaned_sessions() == []
    assert cur.aggregations == 1

    # TTL elapses (advance the reconciler's clock rather than sleeping).
    base = orphan._now()
    monkeypatch.setattr(
        orphan, "_now", lambda: base + orphan.MEMO_TTL_SECONDS + 1
    )

    healed = orphan.reconcile_orphaned_sessions()

    assert cur.aggregations == 2
    assert [h["sdk_session_id"] for h in healed] == ["sdk-orphan"]


def test_confident_results_are_never_memoized(monkeypatch):
    """Only inconclusive answers are cached — a healable session is never skipped."""
    created = NOW - timedelta(hours=2)
    sessions = [_session("orphan", created)]
    _install(
        monkeypatch, sessions,
        [_transcript("sdk-orphan", created + timedelta(seconds=1))],
    )

    orphan.find_orphan_candidates(skip_memoized=True)

    assert "orphan" not in orphan._inconclusive_until


def test_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(orphan, "MEMO_MAX_ENTRIES", 10)
    for i in range(50):
        orphan._memo_record(f"s{i}")

    assert len(orphan._inconclusive_until) <= 10


def test_reset_memo_forces_a_full_re_examination(monkeypatch):
    sessions = [_session("s", NOW - timedelta(hours=2))]
    cur = _install(monkeypatch, sessions, [])

    orphan.reconcile_orphaned_sessions()
    orphan.reconcile_orphaned_sessions()
    assert cur.aggregations == 1

    orphan.reset_memo()
    orphan.reconcile_orphaned_sessions()
    assert cur.aggregations == 2


def test_direct_inspection_ignores_the_memo(monkeypatch):
    """Operator tooling must always see the real answer."""
    sessions = [_session("s", NOW - timedelta(hours=2))]
    cur = _install(monkeypatch, sessions, [])

    orphan.find_orphan_candidates(skip_memoized=True)
    orphan.find_orphan_candidates()  # default: no skipping

    assert cur.aggregations == 2


def test_no_null_sessions_means_no_aggregation_at_all(monkeypatch):
    cur = _install(monkeypatch, [], [])

    assert orphan.find_orphan_candidates() == []
    assert cur.aggregations == 0


# ─── Monitor cadence ──────────────────────────────────────────────────────


def test_monitor_gates_the_reconcile_to_its_own_interval(monkeypatch):
    from app.core import resource_monitor as rm

    monkeypatch.setattr(rm, "_last_orphan_reconcile", 0.0)
    monkeypatch.setattr(rm, "_orphan_reconcile_interval", lambda: 3600.0)

    assert rm._orphan_reconcile_due(now=1000.0) is True     # first tick
    assert rm._orphan_reconcile_due(now=1300.0) is False    # +5 min
    assert rm._orphan_reconcile_due(now=1900.0) is False    # +15 min
    assert rm._orphan_reconcile_due(now=4700.0) is True     # +1 h


def test_monitor_reconcile_can_be_disabled(monkeypatch):
    from app.core import resource_monitor as rm

    monkeypatch.setattr(rm, "_last_orphan_reconcile", 0.0)
    monkeypatch.setattr(rm, "_orphan_reconcile_interval", lambda: 0.0)

    assert rm._orphan_reconcile_due(now=1000.0) is False
