"""Real peer control + isolated worker DBs + model-request queue regression."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.chat_ws_handler import ChatWebSocketHandler
from app.core import session_runtime, turn_manager
from app.db import database, turns
from app.network import control, engine
from app.db import network as store
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _served_client
from tests.core.test_session_runtime import _runtime_env
from tests.test_harness_adapter import FakeProvider, _adapter_with, _collect


async def test_remote_queued_turn_survives_switch_and_reconnect_and_reaches_model_once(workers, monkeypatch):
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import StreamChunk
    from fluid.store.base import MemoryStore
    from app.core.provider.events import ResultMessage

    owner, viewer = workers("queue-owner"), workers("queue-viewer")
    served = _served_client(monkeypatch, owner)
    with owner.active():
        engine.create_network("Queue regression")
    join(owner, viewer)
    sid = "remote-queue"
    command = "client-" + "a" * 32
    prompt = "unique queued instruction after switching"
    with owner.active():
        database.create_session(session_id=sid, profile_id="default", title="Busy remote chat")
        busy = turns.enqueue_turn(sid, "user", prompt="busy first turn")
        turns.start_turn(busy["id"])
        engine.scan_all()
    converge([owner, viewer])
    with owner.active():
        store.touch_member(viewer.device_id, endpoints=[])
    with viewer.active():
        store.touch_member(owner.device_id, endpoints=[served.base_url])
        database.create_session(session_id="other-chat", profile_id="default", title="Other")

    real_submit = turn_manager.submit_async
    async def submit_without_running(session_id, source, **kwargs):
        return await real_submit(session_id, source, no_kick=True, **kwargs)
    monkeypatch.setattr(turn_manager, "submit_async", submit_without_running)
    # Registration stays real handler code; socket transport/account binding is
    # outside this test. Peer HTTP authentication and queue reads are NOT mocked.
    import app.api.chat_ws_handler as cwh
    sync = MagicMock()
    sync.register_device = AsyncMock()
    sync.unregister_device = AsyncMock()
    monkeypatch.setattr(cwh, "sync_engine", sync)

    def handler():
        h = ChatWebSocketHandler(websocket=MagicMock(), device_id="viewer-tab", user_id="admin", api_user_id=None)
        h._may_act_on_session = AsyncMock(return_value=True)
        h._authorize_mobile_session = AsyncMock(return_value=True)
        h.send_json = AsyncMock()
        return h

    with viewer.active():
        accepted = await control.submit_turn(owner.device_id, sid, prompt=prompt,
                                             profile_id="default", command_id=command)
        first, sibling = handler(), handler()
        await first.handle_watch_session({"session_id": sid})
        await first.handle_watch_session({"session_id": "other-chat"})
        await first.handle_watch_session({"session_id": sid})
        # Reconnect creates a new socket and re-watches, as does another tab.
        reconnect = handler()
        for h in (first, sibling, reconnect):
            await h.handle_watch_session({"session_id": sid})
            snapshot = h.send_json.await_args.args[0]["data"]
            assert snapshot["queue_status"] == "available"
            assert snapshot["queue_owner_device_id"] == owner.device_id
            assert len(snapshot["queued_messages"]) == 1
            row = snapshot["queued_messages"][0]
            assert row["id"] == accepted["turn_id"]
            assert row["content"] == prompt
            assert row["command_id"] == row["client_message_id"] == command
        assert turns.get_queued_turns(sid) == []  # mirror never gains executable work
    assert any(path.endswith("/control/queue-view") for _, path in served.requests)

    requests = []
    class RecordingProvider(FakeProvider):
        async def stream(self, request):
            requests.append(request)
            async for chunk in super().stream(request):
                yield chunk

    chunks = [StreamChunk("message_start", {}),
              StreamChunk("block_start", {"index": 0, "block_type": "text"}),
              StreamChunk("text_delta", {"index": 0, "text": "Received"}),
              StreamChunk("block_stop", {"index": 0}),
              StreamChunk("usage", {"stop_reason": "end_turn"})]
    async def pump(**kwargs):
        # The real turn drain supplies this prompt. Drive it through the real
        # adapter + NativeLoop to the provider request, not a seeded display row.
        loop = NativeLoop(provider=RecordingProvider(chunks), store=MemoryStore(), model="fake")
        adapter = _adapter_with(loop)
        from app.core.provider.types import SessionOptions
        adapter._options = SessionOptions(model="fake")
        await loop._ensure_session()
        try:
            await adapter.send_query(kwargs["prompt"])
            events = await _collect(adapter)
            assert isinstance(events[-1], ResultMessage)
            yield {"type": "done", "metadata": {}}
        finally:
            await adapter.disconnect()

    with owner.active(), _runtime_env(pump=pump):
        assert turns.get_turn(accepted["turn_id"])["state"] == "queued"
        turns.settle_turn(busy["id"], "completed")
        ready = turns.pop_ready(sid)
        assert ready["id"] == accepted["turn_id"]
        assert await asyncio.wait_for(session_runtime.SessionRuntime(ready).execute(), 5) == "completed"
        # Re-running an already consumed turn loses the DB start race, and a
        # drain finds nothing. Neither path may issue a second model request.
        assert await session_runtime.SessionRuntime(ready).execute() is None
        assert turns.pop_ready(sid) is None
        assert turns.get_turn(ready["id"])["state"] == "completed"
    assert len(requests) == 1
    assert sum(block.text.count(prompt) for message in requests[0].messages
               for block in message.content if hasattr(block, "text")) == 1
    with viewer.active():
        duplicate = await control.submit_turn(owner.device_id, sid, prompt=prompt, command_id=command)
        assert duplicate["duplicate"] is True
        await reconnect.handle_watch_session({"session_id": sid})
        assert reconnect.send_json.await_args.args[0]["data"]["queued_messages"] == []
        assert turns.get_queued_turns(sid) == []


async def test_queue_view_refuses_a_mirror_and_unknown_session(workers):
    owner, viewer = workers("read-owner"), workers("read-viewer")
    with owner.active():
        engine.create_network("Queue authority")
        database.create_session(session_id="owned", profile_id="default", title="Owner")
        engine.scan_all()
    join(owner, viewer)
    with viewer.active():
        with pytest.raises(PermissionError):
            await control.handle("queue-view", {"session_id": "owned"}, from_device=owner.device_id)
    with owner.active():
        with pytest.raises(LookupError):
            await control.handle("queue-view", {"session_id": "missing"}, from_device=viewer.device_id)


async def test_owner_queue_view_includes_durable_held_messages_and_peer_queue(workers, monkeypatch):
    from app.core import session_handoff
    from app.db import session_moves

    owner = workers("held-owner")
    sid = "queue-with-move"
    command = "client-" + "b" * 32
    with owner.active():
        engine.create_network("Held queue")
        database.create_session(session_id=sid, profile_id="default", title="Moving")
        queued = turns.enqueue_turn(sid, "user", prompt="before move")
        turns.enqueue_turn(sid, "auto_continue", prompt="invisible internal turn")
        peer = turns.enqueue_turn(sid, "agent_message", prompt="peer note")
        pending = session_handoff.PendingHandoff(target_id="next-owner", label="Next", note="", requested_at=1)
        monkeypatch.setitem(session_handoff._pending, sid, pending)
        session_moves.save(sid, target_id=pending.target_id, label=pending.label, note="",
                           requested_by="owner", requested_at=1, expires_at=100)
        # Exercise the producing hold path, not a fake row inserted into a view.
        held = session_handoff.hold(sid, "user", {"prompt": "after move", "command_id": command})
        assert held is not None
        snapshot = await control.handle("queue-view", {"session_id": sid}, from_device="viewer")
        assert snapshot["queue_status"] == "available"
        assert [row["id"] for row in snapshot["queued_messages"]] == [queued["id"], held]
        assert [row["position"] for row in snapshot["queued_messages"]] == [0, 1]
        assert snapshot["queued_messages"][1]["command_id"] == command
        assert snapshot["queued_messages"][1]["client_message_id"] == command
        assert [row["id"] for row in snapshot["pending_peer_messages"]] == [peer["id"]]
