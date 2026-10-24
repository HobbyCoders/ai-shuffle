"""Owner-aware queue hydration and late peer reply regressions (#524)."""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

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
