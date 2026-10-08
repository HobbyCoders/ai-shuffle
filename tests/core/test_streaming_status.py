"""Unit tests for app/core/streaming_status.py — the durable session-busy view.

Busy truth comes from REAL runs-table writer sequences (``runs.claim_run`` /
``release_run``, plus the negative-TTL trick from tests/db/test_runs.py for an
already-expired lease) — never from planting state into the module's private
dicts. Broadcast assertions capture the module's send hook (or, for the
wire-shape test, the sync engine's ``_broadcast_user_message`` helper itself).
"""

import asyncio
import time

import pytest

from app.core import streaming_status
from app.db import database
from app.db import runs


@pytest.fixture(autouse=True)
def _fresh_status_state():
    """The guard cache / last-broadcast map are module-global — isolate tests."""
    streaming_status._reset_state()
    yield
    streaming_status._reset_state()


class _Recorder:
    """Async stand-in for streaming_status._send_streaming_changed."""

    def __init__(self):
        self.calls = []

    async def __call__(self, session_id, is_streaming, info):
        self.calls.append((session_id, is_streaming, info))


async def _wait_until(pred, timeout=2.0):
    """Poll ``pred`` until true or timeout (notify schedules background tasks)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# =============================================================================
# has_active_run — lease-valid busy reads
# =============================================================================

def test_live_lease_is_busy(mock_db):
    runs.claim_run("s1", "runnerA", 60)
    assert streaming_status.has_active_run("s1") is True
    assert streaming_status.has_active_run("s-other") is False
    assert streaming_status.has_active_run(None) is False
    assert streaming_status.has_active_run("") is False


def test_expired_lease_is_not_busy(mock_db):
    # Negative ttl -> the active row's lease is already lapsed on insert
    # (deterministic without freezegun — see tests/db/test_runs.py).
    runs.claim_run("s1", "runnerA", -1)
    assert streaming_status.has_active_run("s1") is False


def test_released_run_is_not_busy(mock_db):
    r = runs.claim_run("s1", "runnerA", 60)
    runs.release_run(r["id"], r["fence_token"])
    assert streaming_status.has_active_run("s1") is False


def test_ttl_cache_serves_cached_value_then_expires(mock_db):
    r = runs.claim_run("s1", "runnerA", 60)
    assert streaming_status.has_active_run("s1") is True  # caches True

    # The DB flips under the cache...
    runs.release_run(r["id"], r["fence_token"])
    # ...but within the TTL the cached value is served (no DB re-read).
    assert streaming_status.has_active_run("s1") is True

    # Age the entry past the TTL -> the next read goes back to the DB.
    with streaming_status._cache_lock:
        checked_at, busy = streaming_status._cache["s1"]
        streaming_status._cache["s1"] = (
            checked_at - (streaming_status.CACHE_TTL_SECONDS + 1),
            busy,
        )
    assert streaming_status.has_active_run("s1") is False


def test_notify_drops_the_guard_cache(mock_db):
    """notify() must pop the cache entry so guards see transitions promptly.

    Called here with no running loop and no captured loop, so the async
    re-check is skipped — the synchronous cache drop is the part under test.
    """
    r = runs.claim_run("s1", "runnerA", 60)
    assert streaming_status.has_active_run("s1") is True
    runs.release_run(r["id"], r["fence_token"])
    assert streaming_status.has_active_run("s1") is True  # still cached

    streaming_status.notify("s1")

    assert streaming_status.has_active_run("s1") is False


def test_has_any_active_run(mock_db):
    assert streaming_status.has_any_active_run() is False
    runs.claim_run("s-expired", "runnerA", -1)  # lapsed lease -> not live
    assert streaming_status.has_any_active_run() is False
    runs.claim_run("s-live", "runnerB", 60)
    assert streaming_status.has_any_active_run() is True


# =============================================================================
# notify -> uncached re-check -> diff -> broadcast
# =============================================================================

async def test_notify_broadcasts_once_per_transition(mock_db, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(streaming_status, "_send_streaming_changed", rec)
    database.create_session(session_id="s1", profile_id="default")
    r = runs.claim_run("s1", "runnerA", 60)

    # idle -> busy: exactly one spinner-on broadcast.
    streaming_status.notify("s1")
    assert await _wait_until(lambda: len(rec.calls) == 1)
    sid, busy, info = rec.calls[0]
    assert (sid, busy) == ("s1", True)
    assert info["user_id"] == "admin"  # api_user_id NULL -> admin routing

    # Still busy: the diff says unchanged -> NO second broadcast.
    streaming_status.notify("s1")
    await asyncio.sleep(0.2)
    assert len(rec.calls) == 1

    # busy -> idle: exactly one spinner-off broadcast.
    runs.release_run(r["id"], r["fence_token"])
    streaming_status.notify("s1")
    assert await _wait_until(lambda: len(rec.calls) == 2)
    assert rec.calls[1][:2] == ("s1", False)

    # Already idle: unchanged again -> no extra broadcast.
    streaming_status.notify("s1")
    await asyncio.sleep(0.2)
    assert len(rec.calls) == 2


async def test_broadcast_uses_exact_legacy_wire_shape(mock_db, monkeypatch):
    """The frame must be byte-compatible with the legacy sync_engine emission —
    the frontend's user_streaming_changed handler needs ZERO changes."""
    from app.core.sync_engine import sync_engine

    sent = []

    async def _capture(user_id, message, source_device_id=None):
        sent.append((user_id, message))

    monkeypatch.setattr(sync_engine, "_broadcast_user_message", _capture)

    mock_db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'P', '/tmp/p')"
    )
    mock_db.commit()
    database.create_session(
        session_id="s1", profile_id="default", project_id="p1"
    )
    runs.claim_run("s1", "runnerA", 60)

    streaming_status.notify("s1")

    assert await _wait_until(lambda: len(sent) == 1)
    user_id, message = sent[0]
    assert user_id == "admin"
    # worktree_branch joins the legacy shape (additive): v2 branch sessions
    # carry their logical branch there so consumers can tell own-checkout
    # streaming apart from main-checkout streaming.
    assert message == {
        "type": "user_streaming_changed",
        "session_id": "s1",
        "is_streaming": True,
        "project_id": "p1",
        "worktree_id": None,
        "worktree_branch": None,
    }


async def test_force_notify_rebroadcasts_on_despite_stale_busy_entry(
    mock_db, monkeypatch
):
    """A run CLAIM must emit an ON frame even when _last_broadcast still holds
    the PREVIOUS turn's busy entry (the frontend already consumed that turn's
    spinner-off via its done-frame cleanup — the stale entry suppresses the
    next turn's ON frame, leaving the board spinner dark until the session is
    selected or the page refreshed)."""
    rec = _Recorder()
    monkeypatch.setattr(streaming_status, "_send_streaming_changed", rec)
    database.create_session(session_id="s1", profile_id="default")
    r1 = runs.claim_run("s1", "runnerA", 60)

    # Turn 1: plain notify (gateway-tick path) broadcasts ON once.
    streaming_status.notify("s1")
    assert await _wait_until(lambda: len(rec.calls) == 1)
    assert rec.calls[0][:2] == ("s1", True)

    # Turn 1's lease releases with NO notify reaching the module (pre-fix
    # release path / sweep hasn't run): _last_broadcast still says busy.
    runs.release_run(r1["id"], r1["fence_token"])

    # Turn 2 claims. A plain re-notify (the gateway tick its first event
    # append produces) reads busy-again and is SUPPRESSED as "unchanged"...
    r2 = runs.claim_run("s1", "runnerA", 60)
    streaming_status.notify("s1")
    await asyncio.sleep(0.15)
    assert len(rec.calls) == 1

    # ...but the CLAIM-time forced notify pushes the ON frame through.
    streaming_status.notify("s1", force=True)
    assert await _wait_until(lambda: len(rec.calls) == 2)
    assert rec.calls[1][:2] == ("s1", True)

    # Cleanup for the autouse state reset.
    runs.release_run(r2["id"], r2["fence_token"])


async def test_rechecks_serialize_per_session_in_notify_order(
    mock_db, monkeypatch
):
    """Two transitions in quick succession (turn N releases, turn N+1 claims)
    must evaluate strictly in notify order: if the OFF recheck's DB read
    happens before the claim but its SEND lands after the ON's, the frontend
    map ends dark on a working session with no sweep recovery."""
    events = []

    class _Journal:
        async def __call__(self, session_id, is_streaming, info):
            events.append(("send", is_streaming))

    monkeypatch.setattr(streaming_status, "_send_streaming_changed", _Journal())

    import threading

    gate = threading.Event()
    reads = {"n": 0}

    # Real ON broadcast first, so both gated OFF evaluations have a busy entry
    # to pop (the exact race shape: release-OFF then another evaluation).
    database.create_session(session_id="s1", profile_id="default")
    r = runs.claim_run("s1", "runnerA", 60)
    streaming_status.notify("s1")
    assert await _wait_until(lambda: events == [("send", True)])

    def _gated_read(session_id):
        reads["n"] += 1
        n = reads["n"]
        events.append(("read", n))
        if n == 1:
            gate.wait(timeout=5)  # hold the FIRST recheck mid-evaluation
        return False  # both evaluations see idle

    monkeypatch.setattr(streaming_status, "_has_active_run_db", _gated_read)

    # First notify blocks inside its DB read; the second must queue behind it,
    # not interleave.
    streaming_status.notify("s1")
    await asyncio.sleep(0.05)
    assert ("read", 1) in events
    streaming_status.notify("s1")
    await asyncio.sleep(0.05)
    assert ("read", 2) not in events  # serialized behind the first

    gate.set()
    assert await _wait_until(lambda: ("read", 2) in events)
    await asyncio.sleep(0.1)

    # Strict order after the initial ON: read1 → send1(OFF) → read2; the
    # second OFF is diff-suppressed (the first already popped the busy entry)
    # — crucially it evaluated AFTER the first finished, never interleaved.
    assert [kind for kind, _ in events] == ["send", "read", "send", "read"]
    assert events[2][1] is False

    runs.release_run(r["id"], r["fence_token"])


# =============================================================================
# Sweep reconciler — lease expiry with no terminal event
# =============================================================================

async def test_sweep_broadcasts_spinner_off_after_lease_expiry(mock_db, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(streaming_status, "_send_streaming_changed", rec)
    database.create_session(session_id="s1", profile_id="default")
    runs.claim_run("s1", "runnerA", 60)

    streaming_status.notify("s1")
    assert await _wait_until(lambda: len(rec.calls) == 1)  # spinner on

    # Crash scenario: the lease lapses with NO terminal event — no release, no
    # notify. (Direct SQL because production reaches this state by a runner
    # dying, which no writer API can simulate.)
    mock_db.execute(
        "UPDATE runs SET lease_expires_at = datetime('now', '-5 seconds') "
        "WHERE session_id = 's1'"
    )
    mock_db.commit()

    cleared = await streaming_status.sweep_once()

    assert cleared == 1
    assert len(rec.calls) == 2
    assert rec.calls[1][:2] == ("s1", False)  # spinner off
    assert "s1" not in streaming_status._last_broadcast
    # The sweep also dropped the guard cache, so guards converge immediately.
    assert streaming_status.has_active_run("s1") is False

    # Idempotent: nothing left to clear, no duplicate broadcast.
    assert await streaming_status.sweep_once() == 0
    assert len(rec.calls) == 2


async def test_sweep_keeps_live_sessions(mock_db, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(streaming_status, "_send_streaming_changed", rec)
    database.create_session(session_id="s1", profile_id="default")
    runs.claim_run("s1", "runnerA", 60)

    streaming_status.notify("s1")
    assert await _wait_until(lambda: len(rec.calls) == 1)

    assert await streaming_status.sweep_once() == 0  # lease still valid
    assert len(rec.calls) == 1
    assert "s1" in streaming_status._last_broadcast


# =============================================================================
# V2 turns — live-turn busy union + reap-and-kick sweep (D3)
# =============================================================================

def test_unavailable_live_turn_read_is_busy(monkeypatch):
    def unavailable():
        raise RuntimeError("turn read unavailable")
    monkeypatch.setattr(streaming_status, "get_db", unavailable)
    assert streaming_status._has_live_turn_db("draining-session") is True


def test_draining_turn_reads_busy_without_active_run(mock_db):
    """A draining turn (escalated interrupt keeping the healthy client) keeps
    the session busy even with no lease-valid active run."""
    from app.db import turns as turns_db

    tid = turns_db.enqueue_turn("s1", "user", prompt="p")["id"]
    turns_db.start_turn(tid, run_id="run-x")  # no matching runs row
    turns_db.interrupt_turn("s1", "stop")
    turns_db.escalate_to_draining(tid)
    assert streaming_status.has_active_run("s1") is True


async def test_sweep_reaps_dead_lease_turns_and_kicks(mock_db, monkeypatch):
    from app.core import turn_scheduler
    from app.db import turns as turns_db

    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: kicked.append(sid))
    tid = turns_db.enqueue_turn("s1", "user", prompt="p")["id"]
    turns_db.start_turn(tid, run_id="run-missing")  # no runs row → dead lease

    await streaming_status.sweep_once()

    # User turn requeued at its position; the session was kicked to restart it.
    assert turns_db.get_turn(tid)["state"] == "queued"
    assert kicked == ["s1"]


# =============================================================================
# get_user_streaming_sessions — connect-time snapshot join
# =============================================================================

def test_get_user_streaming_sessions_join(mock_db):
    mock_db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'P', '/tmp/p')"
    )
    mock_db.execute(
        "INSERT INTO api_users (id, name, api_key_hash) VALUES ('u1', 'U', 'x')"
    )
    mock_db.commit()
    database.create_session(
        session_id="s-admin", profile_id="default", project_id="p1"
    )
    database.create_session(
        session_id="s-user", profile_id="default", api_user_id="u1"
    )
    database.create_session(session_id="s-admin-idle", profile_id="default")
    database.create_session(session_id="s-admin-expired", profile_id="default")

    runs.claim_run("s-admin", "runnerA", 60)
    runs.claim_run("s-user", "runnerB", 60)
    runs.claim_run("s-admin-expired", "runnerC", -1)  # lapsed lease -> excluded
    # s-admin-idle has no run at all -> excluded.

    # Admin sees only admin-owned (api_user_id NULL) lease-valid sessions.
    assert streaming_status.get_user_streaming_sessions("admin") == [
        {
            "session_id": "s-admin",
            "project_id": "p1",
            "worktree_id": None,
            "worktree_branch": None,
        }
    ]

    # An API user sees only their own.
    assert streaming_status.get_user_streaming_sessions("u1") == [
        {
            "session_id": "s-user",
            "project_id": None,
            "worktree_id": None,
            "worktree_branch": None,
        }
    ]

    # Unknown user -> nothing.
    assert streaming_status.get_user_streaming_sessions("nobody") == []


@pytest.mark.parametrize("state", ["running", "interrupting", "draining"])
@pytest.mark.parametrize("owner", [None, "u1"])
def test_reconnect_snapshot_matches_live_turn_truth(mock_db, state, owner):
    from app.db import turns

    mock_db.execute(
        "INSERT INTO api_users (id, name, api_key_hash) VALUES ('u1', 'U', 'x')"
    )
    mock_db.commit()
    database.create_session(session_id="s-live", profile_id="default", api_user_id=owner)
    run = runs.claim_run("s-live", "runner", 60)
    turn = turns.enqueue_turn("s-live", "user", prompt="keep working")
    assert turns.start_turn(turn["id"], run_id=run["id"])
    if state in ("interrupting", "draining"):
        assert turns.interrupt_turn("s-live", "stop")
    if state == "draining":
        assert turns.escalate_to_draining(turn["id"])

    user_id = owner or "admin"
    # A live run AND live turn must not duplicate the card.
    snapshot = streaming_status.get_user_streaming_sessions(user_id)
    assert [row["session_id"] for row in snapshot] == ["s-live"]
    assert runs.release_run(run["id"], run["fence_token"])
    # Production transitions, not hand-written rows: the lease is gone but
    # the live turn has not settled. Reconnect must agree with busy guards.
    assert streaming_status._has_active_run_db("s-live") is True
    assert streaming_status.get_user_streaming_sessions(user_id) == snapshot
    other_user = "admin" if owner else "u1"
    assert streaming_status.get_user_streaming_sessions(other_user) == []
    assert streaming_status.get_user_streaming_sessions("nobody") == []
    assert turns.settle_turn(turn["id"], "completed")
    assert streaming_status.get_user_streaming_sessions(user_id) == []


def test_get_user_streaming_sessions_reports_v2_branch(mock_db):
    """A v2 branch-intake session has NO legacy worktree row — its logical
    branch lives in sessions.branch. The snapshot must surface it as
    ``worktree_branch`` so consumers can tell it apart from a main-checkout
    session (it runs its own checkout and must not lock others' git actions)."""
    mock_db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'P', '/tmp/p')"
    )
    mock_db.commit()
    database.create_session(session_id="s-v2", profile_id="default", project_id="p1")
    database.set_session_branch("s-v2", "feat/login", None)
    runs.claim_run("s-v2", "runnerA", 60)

    assert streaming_status.get_user_streaming_sessions("admin") == [
        {
            "session_id": "s-v2",
            "project_id": "p1",
            "worktree_id": None,
            "worktree_branch": "feat/login",
        }
    ]
