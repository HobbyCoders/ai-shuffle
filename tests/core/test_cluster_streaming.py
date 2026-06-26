"""Unit tests for cross-install streaming-state fan-out (Phase E).

Streaming indicators are per-install in-memory state. These tests cover the
machinery that propagates a streaming transition to peer installs so a roaming
user sees remote sessions stream like local ones:

- ``cluster_streaming._peer_targets`` / ``_fanout`` — who gets notified and how.
- ``SyncEngine.apply_remote_streaming_change`` — the peer receive path.
- ``SyncEngine.sweep_offline_remote_streaming`` — stale-spinner reconciliation.

Everything is mocked at the registry / network boundary — no DB, no sockets.
"""

from unittest.mock import patch

import pytest

from app.core import cluster_streaming
from app.core.sync_engine import SyncEngine


class _FakeSettings:
    effective_instance_id = "me"


async def _fake_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# _peer_targets — who to notify
# ---------------------------------------------------------------------------

def test_peer_targets_filters_self_offline_and_view_only(monkeypatch):
    rows = [
        {"id": "me", "online": True, "base_url": "http://me:1455"},   # self -> skip
        {"id": "a", "online": True, "base_url": "http://a:1455/"},    # ok (slash stripped)
        {"id": "b", "online": False, "base_url": "http://b:1455"},    # offline -> skip
        {"id": "c", "online": True, "base_url": ""},                  # view-only -> skip
        {"id": "d", "online": True, "base_url": "http://d:1455"},     # ok
    ]
    monkeypatch.setattr(cluster_streaming.database, "get_installs", lambda: rows)
    monkeypatch.setattr(cluster_streaming, "settings", _FakeSettings())
    assert cluster_streaming._peer_targets() == ["http://a:1455", "http://d:1455"]


def test_peer_targets_empty_for_single_install(monkeypatch):
    monkeypatch.setattr(
        cluster_streaming.database, "get_installs",
        lambda: [{"id": "me", "online": True, "base_url": "http://me:1455"}],
    )
    monkeypatch.setattr(cluster_streaming, "settings", _FakeSettings())
    assert cluster_streaming._peer_targets() == []


# ---------------------------------------------------------------------------
# _fanout — POSTs to each online peer
# ---------------------------------------------------------------------------

async def test_fanout_posts_to_each_online_peer(monkeypatch):
    rows = [
        {"id": "me", "online": True, "base_url": "http://me:1455"},
        {"id": "a", "online": True, "base_url": "http://a:1455"},
        {"id": "d", "online": True, "base_url": "http://d:1455"},
    ]
    monkeypatch.setattr(cluster_streaming.database, "get_installs", lambda: rows)
    monkeypatch.setattr(cluster_streaming, "settings", _FakeSettings())
    monkeypatch.setattr(cluster_streaming, "run_sync", _fake_run_sync)
    monkeypatch.setattr(
        cluster_streaming.cluster, "cluster_headers", lambda: {"Authorization": "Bearer x"}
    )

    posts = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posts.append((url, json, headers))

    monkeypatch.setattr(cluster_streaming.httpx, "AsyncClient", FakeClient)

    payload = {"session_id": "s1", "is_streaming": True, "user_id": "u1"}
    await cluster_streaming._fanout(payload)

    urls = sorted(p[0] for p in posts)
    assert urls == [
        "http://a:1455/api/v1/cluster/streaming",
        "http://d:1455/api/v1/cluster/streaming",
    ]
    # payload + auth header forwarded verbatim
    assert all(p[1] == payload for p in posts)
    assert all(p[2] == {"Authorization": "Bearer x"} for p in posts)


async def test_fanout_noop_without_peers(monkeypatch):
    monkeypatch.setattr(cluster_streaming.database, "get_installs", lambda: [])
    monkeypatch.setattr(cluster_streaming, "settings", _FakeSettings())
    monkeypatch.setattr(cluster_streaming, "run_sync", _fake_run_sync)

    def _boom(*a, **k):
        raise AssertionError("should not construct a client when there are no peers")

    monkeypatch.setattr(cluster_streaming.httpx, "AsyncClient", _boom)
    await cluster_streaming._fanout({"session_id": "s1"})  # must not raise


def test_schedule_fanout_noop_without_running_loop(monkeypatch):
    # In a sync test there is no running loop, so asyncio.create_task raises
    # RuntimeError, which schedule_fanout swallows rather than propagating.
    monkeypatch.setattr(cluster_streaming, "settings", _FakeSettings())
    cluster_streaming.schedule_fanout(
        user_id="u1", session_id="s1", is_streaming=True,
        project_id=None, worktree_id=None,
    )


# ---------------------------------------------------------------------------
# SyncEngine.apply_remote_streaming_change — peer receive path
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    return SyncEngine()


@pytest.fixture
def sent(engine):
    """Capture user-level broadcasts instead of touching real sockets."""
    messages = []

    async def fake_broadcast(user_id, message, source_device_id=None):
        messages.append((user_id, message))

    engine._broadcast_user_message = fake_broadcast
    return messages


async def test_apply_remote_start_records_and_broadcasts(engine, sent):
    await engine.apply_remote_streaming_change(
        user_id="u1", session_id="s1", is_streaming=True,
        owner_instance_id="box", project_id="p1", worktree_id="w1",
    )

    assert engine.is_session_streaming("s1")
    assert engine._remote_streaming_owner["s1"] == "box"

    # The connect-time snapshot now includes the remote session.
    snap = engine.get_user_streaming_sessions("u1")
    assert snap == [{"session_id": "s1", "project_id": "p1", "worktree_id": "w1"}]

    user_id, msg = sent[-1]
    assert user_id == "u1"
    assert msg["type"] == "user_streaming_changed"
    assert msg["session_id"] == "s1"
    assert msg["is_streaming"] is True


async def test_apply_remote_end_clears_everything(engine, sent):
    await engine.apply_remote_streaming_change(
        "u1", "s1", True, "box", "p1", "w1",
    )
    await engine.apply_remote_streaming_change("u1", "s1", False, "box")

    assert not engine.is_session_streaming("s1")
    assert "s1" not in engine._remote_streaming_owner
    assert engine.get_user_streaming_sessions("u1") == []

    user_id, msg = sent[-1]
    assert msg["is_streaming"] is False
    assert msg["session_id"] == "s1"
    # project/worktree are recovered from the stored maps on end
    assert msg["project_id"] == "p1"
    assert msg["worktree_id"] == "w1"


# ---------------------------------------------------------------------------
# SyncEngine.sweep_offline_remote_streaming — stale-spinner reconciliation
# ---------------------------------------------------------------------------

async def test_sweep_clears_only_offline_owners(engine, sent, monkeypatch):
    from app.db import database

    await engine.apply_remote_streaming_change("u1", "s1", True, "boxA", "p1", None)
    await engine.apply_remote_streaming_change("u1", "s2", True, "boxB", "p2", None)

    liveness = {"boxA": {"online": True}, "boxB": {"online": False}}
    monkeypatch.setattr(database, "get_install", lambda iid: liveness.get(iid))

    cleared = await engine.sweep_offline_remote_streaming()

    assert cleared == 1
    assert engine.is_session_streaming("s1")        # owner online -> kept
    assert not engine.is_session_streaming("s2")    # owner offline -> cleared
    # a stream-end was broadcast for the cleared session only
    user_id, msg = sent[-1]
    assert msg["session_id"] == "s2"
    assert msg["is_streaming"] is False


async def test_sweep_is_noop_when_no_remote_sessions(engine, sent):
    assert await engine.sweep_offline_remote_streaming() == 0
    assert sent == []
