"""
Regression tests: the orphan reconciler must never double-relink.

``reconcile_orphaned_sessions`` heals sessions whose ``sdk_session_id`` was left
NULL by a mid-turn crash by matching them to an unlinked transcript in the store
whose first entry landed just after the session was created. ``find_orphan_
candidates`` computes every match in ONE read pass before any write happens, so
it cannot know that an earlier iteration of the same sweep already took a
transcript. Two orphaned sessions created seconds apart can each list the SAME
lone transcript as their single "confident" candidate. The old per-row
``sdk_session_id IS NULL`` guard protected each TARGET row but did nothing to
stop one transcript being written onto two different rows.

Live evidence (2026-07-02): a sweep HEALED two different sessions to the SAME
``sdk_session_id`` — both chats then rendered the same transcript. It also
"healed" a session that was merely mid-first-turn (+1.1s after creation), i.e.
a healthy running session, not a crashed orphan.

These tests pin: (1) two sessions matching one transcript heal AT MOST ONE of
them (in-sweep uniqueness); (2) the write is guarded so a transcript already
owned by another session is never re-assigned (DB-level uniqueness); (3) the
find query excludes sessions younger than the min-age floor so live
mid-first-turn sessions are not touched.
"""

from datetime import datetime, timedelta, timezone


import app.core.orphan_reconciler as orphan


# ---------------------------------------------------------------------------
# In-memory fake DB — faithful to the reconciler's UPDATE guards.
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Interprets exactly the UPDATE the reconciler issues, over a dict of rows.

    ``sessions`` maps app-session-id -> current sdk_session_id (None = unlinked).
    """

    def __init__(self, sessions: dict):
        self._sessions = sessions
        self.rowcount = 0

    def execute(self, sql, params):
        assert sql.strip().upper().startswith("UPDATE SESSIONS"), sql
        # Tolerate both the pre-fix 2-param form (new_sdk_id, target_id) and the
        # fixed 3-param form (..., guard_sdk_id) so the RED run exercises the
        # actual double-relink DAMAGE rather than a param-unpack error.
        new_sdk_id, target_id = params[0], params[1]
        has_not_exists_guard = "NOT EXISTS" in sql.upper()
        guard_sdk_id = params[2] if len(params) > 2 else None
        self.rowcount = 0
        # WHERE id = target_id AND sdk_session_id IS NULL
        current = self._sessions.get(target_id, "__missing__")
        if current == "__missing__":
            return
        if current is not None:
            return  # target already linked — IS NULL guard fails
        # AND NOT EXISTS (another session already owns guard_sdk_id)  [fixed only]
        if has_not_exists_guard and any(
            v == guard_sdk_id for v in self._sessions.values()
        ):
            return  # transcript already owned — NOT EXISTS guard fails
        # Passed the guards: perform the link.
        self._sessions[target_id] = new_sdk_id
        self.rowcount = 1


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def commit(self):
        pass

    def rollback(self):
        pass


def _install_fake_db(monkeypatch, sessions: dict) -> _FakeCursor:
    """Patch get_db/get_cursor in the reconciler to hit the in-memory store."""
    from contextlib import contextmanager

    cursor = _FakeCursor(sessions)
    conn = _FakeConn(cursor)

    @contextmanager
    def _get_db():
        yield conn

    monkeypatch.setattr(orphan, "get_db", _get_db)
    monkeypatch.setattr(orphan, "get_cursor", lambda c: cursor)
    return cursor


def _confident_record(session_id, sdk_id, created_at, first_at, n=5):
    return {
        "session_id": session_id,
        "created_at": created_at,
        "status": "active",
        "turn_count": 0,
        "confidence": "confident",
        "candidates": [
            {
                "session_id": sdk_id,  # this is the SDK transcript id
                "n": n,
                "first_at": first_at,
                "last_at": first_at,
            }
        ],
    }


# ---------------------------------------------------------------------------
# (1) Two sessions, one transcript — must NOT double-link.
# ---------------------------------------------------------------------------
def test_two_sessions_one_transcript_heals_at_most_one(monkeypatch):
    """The core double-relink bug: two NULL sessions matching one transcript."""
    now = datetime.now(timezone.utc)
    created = now - timedelta(minutes=5)
    first_at = created + timedelta(seconds=1)
    shared_sdk_id = "e3903661-shared-transcript"

    # Both sessions start unlinked; both match the SAME transcript.
    sessions = {"a8c0e274": None, "4a8fb02b": None}
    _install_fake_db(monkeypatch, sessions)

    monkeypatch.setattr(
        orphan,
        "find_orphan_candidates",
        lambda *a, **k: [
            _confident_record("a8c0e274", shared_sdk_id, created, first_at),
            _confident_record("4a8fb02b", shared_sdk_id, created, first_at),
        ],
    )

    healed = orphan.reconcile_orphaned_sessions()

    # Exactly one session ends up owning the transcript — never both.
    owners = [sid for sid, sdk in sessions.items() if sdk == shared_sdk_id]
    assert len(owners) == 1, (
        f"double-relink: {owners} both point at {shared_sdk_id}"
    )
    assert len(healed) == 1
    # The other session stays unlinked (left for manual review), not clobbered.
    other = [sid for sid, sdk in sessions.items() if sdk is None]
    assert len(other) == 1


# ---------------------------------------------------------------------------
# (2) Distinct transcripts still both heal — the guard is not over-eager.
# ---------------------------------------------------------------------------
def test_distinct_transcripts_both_heal(monkeypatch):
    now = datetime.now(timezone.utc)
    created = now - timedelta(minutes=5)
    first_at = created + timedelta(seconds=1)

    sessions = {"sess-A": None, "sess-B": None}
    _install_fake_db(monkeypatch, sessions)

    monkeypatch.setattr(
        orphan,
        "find_orphan_candidates",
        lambda *a, **k: [
            _confident_record("sess-A", "sdk-A", created, first_at),
            _confident_record("sess-B", "sdk-B", created, first_at),
        ],
    )

    healed = orphan.reconcile_orphaned_sessions()

    assert sessions == {"sess-A": "sdk-A", "sess-B": "sdk-B"}
    assert len(healed) == 2


# ---------------------------------------------------------------------------
# (3) DB write guard: a transcript already owned by another session (e.g. a
# concurrent reconcile pass wrote it) is never re-assigned.
# ---------------------------------------------------------------------------
def test_write_guard_refuses_already_owned_transcript(monkeypatch):
    now = datetime.now(timezone.utc)
    created = now - timedelta(minutes=5)
    first_at = created + timedelta(seconds=1)
    sdk_id = "sdk-owned-elsewhere"

    # Another session already owns the transcript before this sweep writes.
    sessions = {"target": None, "already-owner": sdk_id}
    _install_fake_db(monkeypatch, sessions)

    monkeypatch.setattr(
        orphan,
        "find_orphan_candidates",
        lambda *a, **k: [_confident_record("target", sdk_id, created, first_at)],
    )

    healed = orphan.reconcile_orphaned_sessions()

    assert sessions["target"] is None, "re-assigned an already-owned transcript"
    assert healed == []


# ---------------------------------------------------------------------------
# (4) In-sweep dry-run also refuses the second claim (no writes, but the plan
# must not report two heals of one transcript).
# ---------------------------------------------------------------------------
def test_dry_run_does_not_plan_double_link(monkeypatch):
    now = datetime.now(timezone.utc)
    created = now - timedelta(minutes=5)
    first_at = created + timedelta(seconds=1)
    shared = "sdk-shared"

    sessions = {"s1": None, "s2": None}
    _install_fake_db(monkeypatch, sessions)
    monkeypatch.setattr(
        orphan,
        "find_orphan_candidates",
        lambda *a, **k: [
            _confident_record("s1", shared, created, first_at),
            _confident_record("s2", shared, created, first_at),
        ],
    )

    healed = orphan.reconcile_orphaned_sessions(dry_run=True)

    # Dry-run performs no writes...
    assert sessions == {"s1": None, "s2": None}
    # ...and plans to heal the transcript exactly once.
    planned = [h for h in healed if h["sdk_session_id"] == shared]
    assert len(planned) == 1


# ---------------------------------------------------------------------------
# (5) Min-age floor: find_orphan_candidates must build a SQL clause that
# excludes sessions younger than MIN_SESSION_AGE_SECONDS (the mid-first-turn
# case the live incident wrongly "healed").
# ---------------------------------------------------------------------------
def test_find_candidates_applies_min_age_floor(monkeypatch):
    captured = {}

    class _RecordingCursor:
        def execute(self, sql, params):
            # First execute is the session-selection query.
            if "FROM sessions" in sql and "sdk_session_id IS NULL" in sql:
                captured["sql"] = sql
                captured["params"] = list(params)
            self._rows = []

        def fetchall(self):
            return []

    from contextlib import contextmanager

    cur = _RecordingCursor()

    @contextmanager
    def _get_db():
        yield object()

    monkeypatch.setattr(orphan, "get_db", _get_db)
    monkeypatch.setattr(orphan, "get_cursor", lambda c: cur)

    orphan.find_orphan_candidates(min_age_seconds=orphan.MIN_SESSION_AGE_SECONDS)

    assert "created_at <" in captured["sql"], (
        "no min-age exclusion — young mid-first-turn sessions can be healed"
    )
    assert str(orphan.MIN_SESSION_AGE_SECONDS) in captured["params"]
