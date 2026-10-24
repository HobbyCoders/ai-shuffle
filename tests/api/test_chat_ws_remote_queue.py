"""Owner-aware queue hydration and late peer reply regressions (#524)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.network import control
from tests.api.test_chat_ws_handler_watch import _HandlerHarness, _queue_frames, WATCH


@pytest.mark.parametrize("rewatch", [False, True])
async def test_remote_watch_reads_owner_not_viewer_queue(rewatch):
    snapshot = {"queue_status": "available", "queued_messages": [
        {"id": "owner-turn", "command_id": "client-" + "a" * 32,
         "client_message_id": "client-" + "a" * 32, "content": "still pending"}],
        "pending_peer_messages": []}
    with _HandlerHarness() as h, patch.object(control, "owner_of", return_value="owner"), patch.object(
        control, "call", new=AsyncMock(return_value=snapshot)) as relay:
        handler = h.make_handler()
        if rewatch:
            handler.current_session_id = "sess-1"
        await handler.handle_watch_session(dict(WATCH))
    relay.assert_awaited_once_with("owner", "queue-view", {"session_id": "sess-1"})
    h.queue_view.assert_not_called()
    h.peer_queue_view.assert_not_called()
    assert _queue_frames(h.ws)[0]["data"] == {**snapshot, "queue_owner_device_id": "owner"}


@pytest.mark.parametrize("failure", [control.RunnerUnreachable("offline"),
                                     control.ControlRefused("old peer"), ValueError("bad response")])
async def test_remote_queue_unavailable_does_not_send_empty_arrays(failure):
    with _HandlerHarness() as h, patch.object(control, "owner_of", return_value="owner"), patch.object(
        control, "call", new=AsyncMock(side_effect=failure)):
        await h.make_handler().handle_watch_session(dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"] == {
        "queue_status": "unavailable", "queue_owner_device_id": "owner"}
    h.queue_view.assert_not_called()


@pytest.mark.parametrize("next_session", ["sess-1", "sess-2"])
async def test_late_queue_response_cannot_overwrite_new_watch(next_session):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return {"queue_status": "available", "queued_messages": [{"id": "old"}], "pending_peer_messages": []}
        return {"queue_status": "available", "queued_messages": [], "pending_peer_messages": []}

    with _HandlerHarness() as h, patch.object(control, "owner_of", return_value="owner"), patch.object(
        control, "call", new=AsyncMock(side_effect=read)):
        handler = h.make_handler()
        first = asyncio.create_task(handler.handle_watch_session(dict(WATCH)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await handler.handle_watch_session({"session_id": next_session})
        finally:
            release.set()
            await first
    frames = _queue_frames(h.ws)
    assert len(frames) == 1
    assert frames[0]["session_id"] == next_session
    assert frames[0]["data"]["queued_messages"] == []


@pytest.mark.parametrize("reply", [{}, {"queue_status": "available"},
                                  {"queue_status": "available", "queued_messages": None,
                                   "pending_peer_messages": []}])
async def test_incomplete_owner_reply_is_unavailable(reply):
    with _HandlerHarness() as h, patch.object(control, "owner_of", return_value="owner"), patch.object(
        control, "call", new=AsyncMock(return_value=reply)):
        await h.make_handler().handle_watch_session(dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"] == {
        "queue_status": "unavailable", "queue_owner_device_id": "owner"}
    h.queue_view.assert_not_called()


async def test_owner_change_during_read_is_unavailable():
    with _HandlerHarness() as h, patch.object(control, "owner_of", side_effect=["owner", "new-owner"]), patch.object(
        control, "call", new=AsyncMock(return_value={"queue_status": "available", "queued_messages": [], "pending_peer_messages": []})):
        await h.make_handler().handle_watch_session(dict(WATCH))
    assert _queue_frames(h.ws)[0]["data"]["queue_status"] == "unavailable"
