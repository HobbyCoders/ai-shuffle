"""Sidebar status correctness: owner audience and durable attention.

The owner is one person on several chat-socket principals: the web app
registers as ``"admin"`` and a paired phone as ``"paired:<id>"``. Streaming and
attention frames addressed to either must reach both, and the Needs permission
/ Has question label must come from the durable ``pending_interactions`` rows,
not from a per-turn counter that a page load or reconnect never sees.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from app.core import streaming_status
from app.core import user_question_handler as uqh
from app.core.sync_engine import SyncEngine
from app.db import database, interactions, runs


class _Socket:
    def __init__(self):
        self.sent: list = []
        self.close = AsyncMock()

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))

    async def send_json(self, data):
        self.sent.append(data)


class _DeadSocket(_Socket):
    async def send_text(self, text: str):
        raise RuntimeError("socket gone")


async def _until(predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture
async def engine():
    eng = SyncEngine()
    try:
        yield eng
    finally:
        await eng.shutdown()


# ── A: owner audience ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["admin", "paired:phone-1"])
async def test_owner_frames_reach_web_and_paired_sockets(engine, sender):
    """A turn sent from either the web app or the phone lights up both."""
    web, phone, key = _Socket(), _Socket(), _Socket()
    await engine.register_user_device("admin", "dev-web", web)
    await engine.register_user_device("paired:phone-1", "dev-phone", phone)
    await engine.register_user_device("key-1", "dev-key", key)

    await engine.broadcast_session_attention(sender, "s1", 1, kind="question")
    # session_attention coalesces per session while queued; let ON land first.
    assert await _until(lambda: len(web.sent) == 1 and len(phone.sent) == 1)
    await engine.broadcast_session_attention(sender, "s1", 0, kind="question")

    assert await _until(lambda: len(web.sent) == 2 and len(phone.sent) == 2)
    for sock in (web, phone):
        assert [f["count"] for f in sock.sent] == [1, 0]
        assert all(f["type"] == "session_attention" for f in sock.sent)
    await asyncio.sleep(0.02)
    assert key.sent == []


@pytest.mark.asyncio
async def test_streaming_on_off_reaches_paired_socket(engine, monkeypatch):
    """user_streaming_changed is addressed to "admin" for owner sessions."""
    import app.core.sync_engine as se

    web, phone = _Socket(), _Socket()
    await engine.register_user_device("admin", "dev-web", web)
    await engine.register_user_device("paired:phone-1", "dev-phone", phone)
    monkeypatch.setattr(se, "sync_engine", engine)

    info = {"user_id": "admin", "project_id": None, "worktree_id": None, "worktree_branch": None}
    await streaming_status._send_streaming_changed("s1", True, info)
    await streaming_status._send_streaming_changed("s1", False, info)

    assert await _until(lambda: len(web.sent) == 2 and len(phone.sent) == 2)
    assert [f["is_streaming"] for f in phone.sent] == [True, False]
    assert [f["is_streaming"] for f in web.sent] == [True, False]


@pytest.mark.asyncio
async def test_api_key_frames_stay_in_the_keys_bucket(engine):
    web, key = _Socket(), _Socket()
    await engine.register_user_device("admin", "dev-web", web)
    await engine.register_user_device("key-1", "dev-key", key)

    await engine.broadcast_session_attention("key-1", "s1", 1)

    assert await _until(lambda: len(key.sent) == 1)
    await asyncio.sleep(0.02)
    assert web.sent == []


@pytest.mark.asyncio
async def test_source_device_is_excluded_across_owner_buckets(engine):
    web, phone = _Socket(), _Socket()
    await engine.register_user_device("admin", "dev-web", web)
    await engine.register_user_device("paired:phone-1", "dev-phone", phone)

    await engine.broadcast_session_attention("admin", "s1", 1, source_device_id="dev-phone")

    assert await _until(lambda: len(web.sent) == 1)
    await asyncio.sleep(0.02)
    assert phone.sent == []


@pytest.mark.asyncio
async def test_dead_device_is_pruned_from_its_own_bucket(engine):
    web, dead = _Socket(), _DeadSocket()
    await engine.register_user_device("admin", "dev-web", web)
    await engine.register_user_device("paired:phone-1", "dev-phone", dead)

    for count in (1, 2):
        await engine.broadcast_session_attention("admin", "s1", count)

    assert await _until(lambda: "paired:phone-1" not in engine._user_connections)
    assert "dev-web" in engine._user_connections["admin"]


def test_paired_client_gets_owner_streaming_snapshot(mock_db):
    mock_db.execute("INSERT INTO api_users (id, name, api_key_hash) VALUES ('u1', 'U', 'x')")
    mock_db.commit()
    database.create_session(session_id="s-owner", profile_id="default")
    database.create_session(session_id="s-key", profile_id="default", api_user_id="u1")
    runs.claim_run("s-owner", "runnerA", 60)
    runs.claim_run("s-key", "runnerB", 60)

    snapshot = streaming_status.get_user_streaming_sessions("paired:phone-1")
    assert [row["session_id"] for row in snapshot] == ["s-owner"]


# ── B: durable attention ────────────────────────────────────────────────────


def _owner_and_key_sessions(mock_db):
    mock_db.execute("INSERT INTO api_users (id, name, api_key_hash) VALUES ('u1', 'U', 'x')")
    mock_db.commit()
    database.create_session(session_id="s-q", profile_id="default")
    database.create_session(session_id="s-pq", profile_id="default")
    database.create_session(session_id="s-key", profile_id="default", api_user_id="u1")


def test_attention_snapshot_counts_pending_rows_per_owner(mock_db):
    _owner_and_key_sessions(mock_db)
    interactions.create_interaction("s-q", "question", {"request_id": "q1"}, None)
    interactions.create_interaction("s-pq", "question", {"request_id": "q2"}, None)
    interactions.create_interaction("s-pq", "permission", {"request_id": "p1"}, 600)
    interactions.create_interaction("s-key", "question", {"request_id": "q3"}, None)
    done = interactions.create_interaction("s-q", "question", {"request_id": "q4"}, None)
    interactions.cancel_interaction(done["id"])
    # Lapsed TTL: still 'pending' until the sweeper runs, but not answerable.
    interactions.create_interaction("s-q", "permission", {"request_id": "p2"}, -10)

    for user in ("admin", "paired:phone-1"):
        snapshot = sorted(uqh.get_user_attention_snapshot(user), key=lambda r: r["session_id"])
        assert snapshot == [
            {"session_id": "s-pq", "count": 2, "kind": "permission"},
            {"session_id": "s-q", "count": 1, "kind": "question"},
        ]
    assert uqh.get_user_attention_snapshot("u1") == [
        {"session_id": "s-key", "count": 1, "kind": "question"},
    ]
    assert uqh.get_user_attention_snapshot("nobody") == []


def test_session_attention_is_addressed_to_the_owner(mock_db):
    _owner_and_key_sessions(mock_db)
    interactions.create_interaction("s-key", "question", {"request_id": "q"}, None)

    assert uqh.get_session_attention("s-q") == ("admin", 0, "question")
    assert uqh.get_session_attention("s-key") == ("u1", 1, "question")
    assert uqh.get_session_attention("missing") is None


@pytest.fixture
def quiet_delivery(monkeypatch):
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr(
        "app.core.user_question_handler.UserQuestionHandler._retire_native_question",
        AsyncMock(),
    )
    attention = AsyncMock()
    monkeypatch.setattr(
        "app.core.sync_engine.sync_engine.broadcast_session_attention", attention
    )
    return attention


def _attention_calls(mock):
    return [(c.args[0], c.args[1], c.args[2], c.kwargs.get("kind")) for c in mock.call_args_list]


@pytest.mark.asyncio
async def test_posted_question_publishes_durable_attention(mock_db, quiet_delivery):
    """Nonblocking questions never enter the turn's counter, so the post
    itself must light the sidebar, and answering must clear it."""
    database.create_session(session_id="s1", profile_id="default")
    handler = uqh.UserQuestionHandler()
    kwargs = dict(tool_use_id="tu", session_id="s1", broadcast_func=AsyncMock())

    await handler.post_question(request_id="q1", questions=[{"question": "Color?"}], **kwargs)
    await handler.post_question(request_id="q2", questions=[{"question": "Size?"}], **kwargs)
    assert _attention_calls(quiet_delivery) == [
        ("admin", "s1", 1, "question"),
        ("admin", "s1", 2, "question"),
    ]

    quiet_delivery.reset_mock()
    assert await handler.respond("q1", "s1", {"Color?": "blue"})
    assert _attention_calls(quiet_delivery) == [("admin", "s1", 1, "question")]

    quiet_delivery.reset_mock()
    assert await handler.respond("q2", "s1", {}, dismissed=True)
    assert _attention_calls(quiet_delivery) == [("admin", "s1", 0, "question")]


@pytest.mark.asyncio
async def test_cancel_all_publishes_the_cleared_count(mock_db, quiet_delivery):
    database.create_session(session_id="s1", profile_id="default")
    handler = uqh.UserQuestionHandler()
    await handler.post_question(request_id="q1", tool_use_id="tu", session_id="s1",
                                questions=[{"question": "Color?"}], broadcast_func=AsyncMock())
    quiet_delivery.reset_mock()

    await handler.cancel_all("s1")

    assert _attention_calls(quiet_delivery) == [("admin", "s1", 0, "question")]


@pytest.mark.asyncio
async def test_retiring_a_timed_out_question_publishes_the_count(mock_db, quiet_delivery):
    database.create_session(session_id="s1", profile_id="default")
    row = interactions.create_interaction("s1", "question", {"request_id": "q"}, 600)

    await uqh.UserQuestionHandler._retire_unanswered("s1", row["id"], "q")

    assert _attention_calls(quiet_delivery) == [("admin", "s1", 0, "question")]
