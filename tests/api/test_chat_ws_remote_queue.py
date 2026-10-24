"""Owner-aware queue hydration and late peer reply regressions (#524)."""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
from app.core.sync_engine import SyncEngine
from app.db import turns
from app.network import control
from tests.api.test_chat_ws_dispatch_contract import (
    _connect,
    _live_turn,
    _make_session,
    ws_env,  # noqa: F401 — fixture for the real serialized dispatcher
)
from tests.api.test_chat_ws_handler_watch import (
    WATCH,
    _HandlerHarness,
    _queue_frames,
    _watch_and_hydrate,
)


@pytest.mark.parametrize("rewatch", [False, True])
async def test_remote_watch_reads_owner_not_viewer_queue(rewatch: bool) -> None:
    snapshot = {
        "queue_status": "available",
        "queued_messages": [{
            "id": "owner-turn", "command_id": "client-" + "a" * 32,
            "client_message_id": "client-" + "a" * 32, "content": "still pending",
        }],
        "pending_peer_messages": [],
    }
    with (
        _HandlerHarness() as h,
        patch.object(control, "owner_of", return_value="owner"),
        patch.object(control, "call", new=AsyncMock(return_value=snapshot)) as relay,
    ):
        handler = h.make_handler()
        if rewatch:
            handler.current_session_id = "sess-1"
        await _watch_and_hydrate(handler, dict(WATCH))
    relay.assert_awaited_once_with("owner", "queue-view", {"session_id": "sess-1"})
    h.queue_view.assert_not_called()
    h.peer_queue_view.assert_not_called()
    assert _queue_frames(h.ws)[0]["data"] == {**snapshot, "queue_owner_device_id": "owner"}


@pytest.mark.parametrize("failure", [
    control.RunnerUnreachable("offline"), control.ControlRefused("old peer"),
    ValueError("bad response"),
])
async def test_remote_queue_unavailable_does_not_send_empty_arrays(failure: Exception) -> None:
    with (
        _HandlerHarness() as h,
        patch.object(control, "owner_of", return_value="owner"),
        patch.object(control, "call", new=AsyncMock(side_effect=failure)),
    ):
        await _watch_and_hydrate(h.make_handler(), dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"] == {
        "queue_status": "unavailable", "queue_owner_device_id": "owner",
    }
    h.queue_view.assert_not_called()


@pytest.mark.parametrize("next_session", ["sess-1", "sess-2"])
async def test_late_queue_response_cannot_overwrite_new_watch(next_session: str) -> None:
    entered = asyncio.Event()
    calls = 0

    async def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Even if a relay completes as cancellation arrives, generation
                # invalidation must prevent this stale snapshot reaching the wire.
                return {
                    "queue_status": "available", "queued_messages": [{"id": "old"}],
                    "pending_peer_messages": [],
                }
        return {
            "queue_status": "available", "queued_messages": [], "pending_peer_messages": [],
        }

    with (
        _HandlerHarness() as h,
        patch.object(control, "owner_of", return_value="owner"),
        patch.object(control, "call", new=AsyncMock(side_effect=read)),
    ):
        handler = h.make_handler()
        await handler.handle_watch_session(dict(WATCH))
        await asyncio.wait_for(entered.wait(), 1)
        await _watch_and_hydrate(handler, {"session_id": next_session})
    frames = _queue_frames(h.ws)
    assert len(frames) == 1
    assert frames[0]["session_id"] == next_session
    assert frames[0]["data"]["queued_messages"] == []


@pytest.mark.parametrize("reply", [
    {}, {"queue_status": "available"},
    {"queue_status": "available", "queued_messages": None, "pending_peer_messages": []},
])
async def test_incomplete_owner_reply_is_unavailable(reply: dict[str, Any]) -> None:
    with (
        _HandlerHarness() as h,
        patch.object(control, "owner_of", return_value="owner"),
        patch.object(control, "call", new=AsyncMock(return_value=reply)),
    ):
        await _watch_and_hydrate(h.make_handler(), dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"] == {
        "queue_status": "unavailable", "queue_owner_device_id": "owner",
    }
    h.queue_view.assert_not_called()


async def test_owner_change_during_read_is_unavailable() -> None:
    snapshot = {
        "queue_status": "available", "queued_messages": [], "pending_peer_messages": [],
    }
    with (
        _HandlerHarness() as h,
        patch.object(control, "owner_of", side_effect=["owner", "new-owner"]),
        patch.object(control, "call", new=AsyncMock(return_value=snapshot)),
    ):
        await _watch_and_hydrate(h.make_handler(), dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"]["queue_status"] == "unavailable"


@pytest.mark.parametrize("following_frame", ["remove_queued_message", "watch_session"])
async def test_slow_peer_does_not_block_serialized_dispatch(
    ws_env: Any, monkeypatch: pytest.MonkeyPatch, following_frame: str,
) -> None:
    remote = _make_session("slow-owner")
    local = _make_session("local-queue")
    _live_turn(local)
    queued = turns.enqueue_turn(local, "user", prompt="withdraw before delivery")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("slow relay should only finish through cancellation")

    monkeypatch.setattr(control, "call", read)
    monkeypatch.setattr(
        control, "owner_of", lambda row: "owner" if row["id"] == remote else None,
    )
    async with _connect(ws_env) as conn:
        conn.wire.push({"type": "watch_session", "session_id": remote})
        await asyncio.wait_for(entered.wait(), 1)
        hydration = conn.handler._queue_watch_task
        if following_frame == "remove_queued_message":
            # The real receive loop must dispatch the withdrawal while the peer
            # remains blocked, before a local drain could consume this turn.
            replies = await conn.roundtrip({
                "type": following_frame, "session_id": local, "message_id": queued["id"],
            }, timeout=1)
            assert any(f.get("outcome") == "cancelled" for f in replies)
            assert turns.get_turn(queued["id"])["state"] == "aborted"
            assert not hydration.done()
            assert not cancelled.is_set()
        conn.wire.push({"type": "watch_session", "session_id": local})
        await conn.wait_for(lambda fs: any(
            f.get("event_type") == "queue_updated" and f.get("session_id") == local
            and f["data"].get("queue_status") == "available" for f in fs
        ), timeout=1)
        assert hydration.cancelled()
        assert cancelled.is_set()
        frames = [f for f in conn.frames() if f.get("event_type") == "queue_updated"]
        assert all(f["session_id"] == local for f in frames)
        expected = [] if following_frame == "remove_queued_message" else [queued["id"]]
        assert [row["id"] for row in frames[-1]["data"]["queued_messages"]] == expected


async def test_disconnect_cancels_inflight_hydration(
    ws_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = _make_session("disconnect-during-hydration")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("slow relay should only finish through cancellation")

    monkeypatch.setattr(control, "owner_of", lambda row: "owner")
    monkeypatch.setattr(control, "call", read)
    async with _connect(ws_env) as conn:
        conn.wire.push({"type": "watch_session", "session_id": sid})
        await asyncio.wait_for(entered.wait(), 1)
        hydration = conn.handler._queue_watch_task
    assert hydration.cancelled()
    assert cancelled.is_set()
    assert conn.handler._queue_watch_task is None


@pytest.mark.parametrize("sibling", [False, True])
@pytest.mark.parametrize("mutation", [
    "queue_message", "remove_queued_message", "clear_queued_messages", "edit",
])
async def test_hydration_rereads_after_newer_queue_mutation(
    ws_env: Any, monkeypatch: pytest.MonkeyPatch, sibling: bool, mutation: str,
) -> None:
    """An older read cannot replace a newer mutation's authoritative snapshot."""
    sid = _make_session("mutation-during-hydration")
    _live_turn(sid)
    queued = None
    if mutation != "queue_message":
        queued = turns.enqueue_turn(sid, "queue", prompt="remove me")
    read_done, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def read(fn: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        result = fn(*args, **kwargs)
        if fn is cwh._v2_queue_view:
            reads += 1
            if reads == 1:
                # Capture the real DB projection, then let a real enqueue/remove
                # and its broadcast complete before this stale read resumes.
                read_done.set()
                await release.wait()
        return result

    sync = SyncEngine()
    monkeypatch.setattr(cwh, "sync_engine", sync)
    monkeypatch.setattr(cwh, "run_sync", read)
    try:
        async with _connect(ws_env) as conn:
            conn.wire.push({"type": "watch_session", "session_id": sid})
            await asyncio.wait_for(read_done.wait(), 1)
            frame = {"type": mutation, "session_id": sid, "content": "keep this queued"}
            if queued is not None:
                frame["message_id"] = queued["id"]
            if mutation == "edit":
                # Exercise an in-place queued edit and the shared broadcast path
                # independently of _v2_broadcast_queue's origin-socket guard.
                updated = turns.update_queued_turn_prompt(
                    queued["id"], "edited queued message", {},
                )
                assert updated is not None
                await sync.broadcast_queue_updated(
                    sid, await read(cwh._v2_queue_view, sid), pending_peer_messages=[],
                )
            elif sibling:
                producer = cwh.ChatWebSocketHandler(
                    websocket=MagicMock(), device_id="sibling-tab", user_id="admin",
                    api_user_id=None,
                )
                producer.send_json = AsyncMock()
                producer._authorize_mobile_session = AsyncMock(return_value=True)
                await producer._handlers[mutation](frame)
            else:
                await conn.roundtrip(frame)
            expected = {
                "queue_message": ["keep this queued"], "edit": ["edited queued message"],
            }.get(mutation, [])
            await conn.wait_for(lambda fs: any(
                f.get("event_type") == "queue_updated"
                and [row["content"] for row in f["data"]["queued_messages"]] == expected
                for f in fs
            ))
            assert [row["prompt"] for row in turns.get_queued_turns(sid)] == expected
            # The newer authoritative snapshot has reached the observer already.
            # Resume the earlier read; hydration must discard it and read again.
            release.set()
            await asyncio.wait_for(conn.handler._queue_watch_task, 1)
            await conn.wait_for(lambda fs: any(
                f.get("event_type") == "queue_updated"
                and f["data"].get("queue_status") == "available" for f in fs
            ))
            frames = [f for f in conn.frames() if f.get("event_type") == "queue_updated"]
            final_state = [row["content"] for row in frames[-1]["data"]["queued_messages"]]
            assert final_state == expected
            assert reads >= 3  # hydration read, mutation broadcast read, hydration re-read
    finally:
        release.set()
        await sync.shutdown()
