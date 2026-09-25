"""Device command queue (app.db.mobile_tools) through the per-test SQLite shim.

The clock is pinned by monkeypatching ``mobile_tools.now`` so expiry and the
30 s heartbeat window are exercised deterministically.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import mobile_tools as queue
from app.db.base import get_db

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(mock_db, monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(queue, "now", lambda: state["now"])
    queue.init_schema()
    queue._reset_purge_throttle()
    with get_db() as conn:
        cur = conn.cursor()
        for dev in ("a", "b"):
            cur.execute(
                "INSERT INTO mobile_devices (id, device_id, device_name, platform, api_key_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (f"id-{dev}", dev, dev, "android", f"hash-{dev}"),
            )
    yield state
    queue._reset_purge_throttle()


def _online(device="a", tools=("open_url", "read_screen")):
    queue.capabilities(device, True, list(tools))
    queue.claim(device)  # records the heartbeat


def _enqueue(device="a", ttl=20, name="read_screen", args="{}"):
    return queue.enqueue(device, "sess", name, args, ttl)


def _row(command_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM mobile_tool_commands WHERE id=%s", (command_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def test_ts_coercion():
    assert queue._ts(None) is None
    assert queue._ts("2026-01-01T00:00:00").tzinfo == timezone.utc
    aware = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))
    assert queue._ts(aware) is aware


def test_state_and_capabilities(clock):
    assert queue.state("a") is None
    queue.capabilities("a", True, ["read_screen"])
    st = queue.state("a")
    assert bool(st["enabled"]) is True and json.loads(st["tools"]) == ["read_screen"]


def test_enqueue_refusals(clock):
    assert _enqueue() == {"error": "device_disabled"}
    queue.capabilities("a", False, ["read_screen"])
    assert _enqueue() == {"error": "device_disabled"}
    queue.capabilities("a", True, ["read_screen"])
    # Enabled but never polled: no heartbeat.
    assert _enqueue() == {"error": "device_offline"}
    queue.claim("a")
    assert _enqueue(name="open_url") == {"error": "unsupported_tool"}
    clock["now"] = T0 + timedelta(seconds=31)
    assert _enqueue() == {"error": "device_offline"}


def test_enqueue_bounds_queue_depth(clock):
    _online()
    ids = [_enqueue()["id"] for _ in range(8)]
    assert len(set(ids)) == 8
    assert _enqueue() == {"error": "device_busy"}
    # Another device has its own budget.
    _online("b")
    assert "id" in _enqueue("b")


def test_claim_returns_pending_once_and_parses_arguments(clock):
    assert queue.claim("a") == []  # not enabled
    _online()
    cid = _enqueue(name="open_url", args='{"url":"https://x"}')["id"]
    claimed = queue.claim("a")
    assert [c["id"] for c in claimed] == [cid]
    assert claimed[0]["arguments"] == {"url": "https://x"}
    assert claimed[0]["session_id"] == "sess"
    assert queue.claim("a") == []


def test_capability_change_cancels_outstanding_work(clock):
    _online()
    pending = _enqueue()["id"]
    queue.capabilities("a", True, ["open_url", "read_screen"])  # identical: no-op
    assert _row(pending)["status"] == "pending"
    assert queue.state("a")["heartbeat_at"] is not None
    queue.capabilities("a", True, ["read_screen"])  # tools changed
    row = _row(pending)
    assert row["status"] == "cancelled" and row["arguments"] == "{}"
    # A change also clears the heartbeat: the device must poll again first.
    assert queue.state("a")["heartbeat_at"] is None


def test_submit_result_state_machine(clock):
    _online()
    cid = _enqueue()["id"]
    assert queue.submit_result("b", cid, '{"ok":1}') == "not_found"  # wrong device
    assert queue.submit_result("a", "missing", "{}") == "not_found"
    assert queue.submit_result("a", cid, '{"ok":1}') == "not_claimed"
    queue.claim("a")
    assert queue.submit_result("a", cid, '{"ok":1}') is None
    row = _row(cid)
    assert row["status"] == "completed" and row["arguments"] == "{}"
    # Idempotent replay of the same result; a different one conflicts.
    assert queue.submit_result("a", cid, '{"ok":1}') is None
    assert queue.submit_result("a", cid, '{"ok":2}') == "result_conflict"
    assert queue.outcome(cid, "sess") == {"status": "completed", "result": '{"ok":1}'}


def test_submit_after_expiry_scrubs_payload(clock):
    _online()
    cid = _enqueue(ttl=5, name="open_url", args='{"url":"https://secret"}')["id"]
    queue.claim("a")
    clock["now"] = T0 + timedelta(seconds=6)
    assert queue.submit_result("a", cid, "{}") == "expired"
    row = _row(cid)
    assert row["status"] == "expired" and row["arguments"] == "{}"
    assert queue.submit_result("a", cid, "{}") == "expired"


def test_outcome_expiry_and_ownership(clock):
    _online()
    cid = _enqueue(ttl=5)["id"]
    assert queue.outcome(cid, "other-session") is None
    assert queue.outcome(cid, "sess") == {"status": "pending", "result": None}
    clock["now"] = T0 + timedelta(seconds=5)
    assert queue.outcome(cid, "sess") == {"status": "expired", "result": None}


def test_cancel_only_affects_live_commands_of_the_session(clock):
    _online()
    cid = _enqueue()["id"]
    queue.cancel(cid, "other-session")
    assert _row(cid)["status"] == "pending"
    queue.cancel(cid, "sess")
    assert _row(cid)["status"] == "cancelled"


def test_purge_sweeps_and_is_throttled(clock):
    _online()
    old = _enqueue(ttl=1)["id"]
    clock["now"] = T0 + timedelta(seconds=2)
    with get_db() as conn:
        cur = conn.cursor()
        queue._purge(cur, clock["now"], force=True)
    assert _row(old)["status"] == "expired"

    # A second expiry within the interval is left for the next sweep...
    fresh = _enqueue(ttl=1)["id"]
    clock["now"] = T0 + timedelta(seconds=10)
    with get_db() as conn:
        queue._purge(conn.cursor(), clock["now"])
    assert _row(fresh)["status"] == "pending"
    # ...though reads still never report it live.
    assert queue.outcome(fresh, "sess")["status"] == "expired"

    # Rows a day past expiry are deleted outright.
    clock["now"] = T0 + timedelta(days=2)
    with get_db() as conn:
        queue._purge(conn.cursor(), clock["now"], force=True)
    assert _row(old) is None and _row(fresh) is None
