"""Per-connection writer-queue tests (performance review W2 / W3 / W9).

W2: ``broadcast_event`` used to await each device's ``send_text`` SERIALLY,
bounded only by a 10 s timeout. The caller is the turn pump, so one half-open
client (locked phone on cellular) froze SDK event consumption, the TurnRecorder,
and therefore SSE delivery to EVERY device — for up to 10 s per frame per dead
device.

The fix gives every connection a bounded queue plus a dedicated writer task:
producers only ENQUEUE (synchronously, no awaits), and the single bounded await
per connection happens on that connection's own writer task.

These tests pin the properties that make that fix correct:
  * a stalled consumer never delays the producer,
  * frames for one connection stay in order,
  * the overflow policy (coalesce whole-state snapshots, otherwise disconnect),
  * writer tasks are cancelled AND awaited on disconnect / shutdown,
  * a writer that dies reaps itself out of the registry,
  * one bad connection cannot kill the writer loop.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from app.core import sync_engine as se
from app.core.sync_engine import SyncEngine, SyncEvent


# ─── doubles ────────────────────────────────────────────────────────────────


class _HungWebSocket:
    """A half-open socket: sends never complete and never raise."""

    def __init__(self):
        self.sent: list = []
        self.close = AsyncMock()

    async def send_text(self, text: str):
        await asyncio.Event().wait()  # parks forever

    async def send_json(self, data):
        await asyncio.Event().wait()


class _GatedWebSocket:
    """A socket that only completes sends while its gate is open."""

    def __init__(self, open_: bool = True):
        self.sent: list = []
        self.gate = asyncio.Event()
        if open_:
            self.gate.set()
        self.close = AsyncMock()

    async def send_text(self, text: str):
        await self.gate.wait()
        self.sent.append(text)

    async def send_json(self, data):
        await self.gate.wait()
        self.sent.append(data)


class _HealthyWebSocket(_GatedWebSocket):
    def __init__(self):
        super().__init__(open_=True)


class _ExplodingWebSocket:
    """Every send raises — the connection must be dropped, not the loop."""

    def __init__(self):
        self.close = AsyncMock()
        self.attempts = 0

    async def send_text(self, text: str):
        self.attempts += 1
        raise RuntimeError("socket exploded")

    async def send_json(self, data):
        await self.send_text(json.dumps(data))


async def _park_writer():
    """Let a freshly started writer task reach its idle wait.

    Producers never yield to the loop, so without this the writer would not
    have started at all and the test would be measuring a different (still
    correct, but less interesting) interleaving.
    """
    await asyncio.sleep(0)


async def _until(predicate, timeout: float = 2.0, interval: float = 0.005):
    """Poll ``predicate`` until true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
async def engine():
    eng = SyncEngine()
    try:
        yield eng
    finally:
        await eng.shutdown()


# ─── W2: a slow consumer must not block the producer ────────────────────────


@pytest.mark.asyncio
async def test_broadcast_does_not_block_on_stalled_consumer(engine):
    """The whole point of W2: with a device whose socket never completes a
    send, the producer (the turn pump) must still finish its fan-out
    immediately — not after _SEND_TIMEOUT_SECONDS per frame."""
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_device("dev-hung", "sess-1", hung)
    await engine.register_device("dev-ok", "sess-1", healthy)

    frames = 50
    started = time.perf_counter()
    for i in range(frames):
        await engine.broadcast_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1",
                      data={"type": "tool_use", "id": f"t{i}"})
        )
    elapsed = time.perf_counter() - started

    # Serial-await behaviour would cost frames * _SEND_TIMEOUT_SECONDS here.
    assert elapsed < 0.5, f"producer stalled for {elapsed:.2f}s behind a dead client"
    assert elapsed < se._SEND_TIMEOUT_SECONDS

    # And the healthy device still gets every frame, in order.
    assert await _until(lambda: len(healthy.sent) == frames)
    ids = [json.loads(t)["data"]["id"] for t in healthy.sent]
    assert ids == [f"t{i}" for i in range(frames)]


@pytest.mark.asyncio
async def test_user_message_fanout_does_not_block_on_stalled_consumer(engine):
    """Same guarantee on the user-level leg (_broadcast_user_message)."""
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_user_device("u1", "dev-hung", hung)
    await engine.register_user_device("u1", "dev-ok", healthy)

    started = time.perf_counter()
    for i in range(25):
        await engine.broadcast_notification("u1", {"id": i})
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert await _until(lambda: len(healthy.sent) == 25)


@pytest.mark.asyncio
async def test_usage_snapshot_fanout_does_not_block_on_stalled_consumer(engine):
    """broadcast_usage_snapshot fans out cluster-wide; it must not await sockets."""
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_user_device("u1", "dev-hung", hung)
    await engine.register_user_device("u2", "dev-ok", healthy)

    started = time.perf_counter()
    await engine.broadcast_usage_snapshot({"windows": [], "generation": 1})
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert await _until(lambda: len(healthy.sent) == 1)
    assert json.loads(healthy.sent[0])["type"] == "usage_snapshot"


# ─── ordering ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frames_stay_in_order_for_one_connection(engine):
    """One writer task per connection draining one FIFO: enqueue order is
    delivery order even when the socket is stalled part-way through."""
    ws = _GatedWebSocket(open_=False)  # nothing drains until we open the gate
    await engine.register_device("dev-1", "sess-1", ws)

    for i in range(40):
        await engine.broadcast_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1",
                      data={"type": "tool_use", "id": f"t{i}"})
        )
    assert ws.sent == []  # producer ran ahead; nothing delivered yet

    ws.gate.set()
    assert await _until(lambda: len(ws.sent) == 40)
    ids = [json.loads(t)["data"]["id"] for t in ws.sent]
    assert ids == [f"t{i}" for i in range(40)]


# ─── overflow policy ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overflow_of_undroppable_frames_disconnects_the_client(engine):
    """Nothing on this leg is safe to silently drop (a lost `done` hangs the
    spinner forever), so the policy is: disconnect and let the client resync
    from the SSE feed's cursor resume."""
    ws = _GatedWebSocket(open_=False)
    conn = await engine.register_device("dev-1", "sess-1", ws)

    # One extra beyond the queue depth: `done`/`tool_use` are not coalescable.
    for i in range(se._OUTBOUND_QUEUE_MAXSIZE + 5):
        await engine.broadcast_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1",
                      data={"type": "tool_use", "id": f"t{i}"})
        )

    assert conn.writer.alive is False
    assert conn.writer.death_reason == "queue_overflow"
    # Pruned from the registry so later broadcasts skip it entirely...
    assert "dev-1" not in engine._connections.get("sess-1", {})
    # ...and the zombie socket is closed so the client reconnects and resyncs.
    assert await _until(lambda: ws.close.await_count > 0)


@pytest.mark.asyncio
async def test_coalescable_snapshot_frames_collapse_instead_of_overflowing(engine):
    """`queue_updated` carries the COMPLETE queue, so a newer one fully
    supersedes an older one: it is collapsed in place rather than piling up,
    and the connection survives far past the queue depth."""
    ws = _GatedWebSocket(open_=False)
    conn = await engine.register_device("dev-1", "sess-1", ws)
    await _park_writer()

    total = se._OUTBOUND_QUEUE_MAXSIZE * 3
    for i in range(total):
        await engine.broadcast_queue_updated("sess-1", [{"id": f"q{i}"}])

    assert conn.writer.alive is True
    assert conn.writer.depth == 1
    assert conn.writer.coalesced == total - 1

    ws.gate.set()
    assert await _until(lambda: len(ws.sent) == 1)
    payload = json.loads(ws.sent[0])
    # The newest snapshot is what lands — never a stale one.
    assert payload["data"]["queued_messages"] == [{"id": f"q{total - 1}"}]


@pytest.mark.asyncio
async def test_coalescing_preserves_position_relative_to_other_frames(engine):
    """Collapsing happens IN PLACE, so a coalesced snapshot keeps the queue
    position of the frame it replaced — ordering against other frame types is
    unchanged."""
    ws = _GatedWebSocket(open_=False)
    await engine.register_device("dev-1", "sess-1", ws)
    await _park_writer()

    await engine.broadcast_queue_updated("sess-1", [{"id": "q0"}])
    await engine.broadcast_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={"type": "done"})
    )
    await engine.broadcast_queue_updated("sess-1", [{"id": "q1"}])  # collapses into q0's slot

    ws.gate.set()
    assert await _until(lambda: len(ws.sent) == 2)
    kinds = [json.loads(t)["event_type"] for t in ws.sent]
    assert kinds == ["queue_updated", "stream_frame"]
    assert json.loads(ws.sent[0])["data"]["queued_messages"] == [{"id": "q1"}]


@pytest.mark.asyncio
async def test_user_snapshot_frames_are_coalescable(engine):
    """usage_snapshot / tabs_sync / session_attention are whole-state frames."""
    assert se._user_coalesce_key({"type": "usage_snapshot"}) == ("user", "usage_snapshot")
    assert se._user_coalesce_key({"type": "tabs_sync"}) == ("user", "tabs_sync")
    assert se._user_coalesce_key({"type": "session_attention", "session_id": "s"}) == (
        "user", "session_attention", "s",
    )
    # Correctness-bearing frames must never be droppable.
    for mtype in ("notification", "user_question", "permission_request",
                  "user_session_updated", "user_session_created"):
        assert se._user_coalesce_key({"type": mtype}) is None
    assert se._session_coalesce_key("stream_frame", "s") is None
    assert se._session_coalesce_key("session_rewound", "s") is None


# ─── lifecycle ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writer_task_is_cancelled_and_awaited_on_unregister(engine):
    conn = await engine.register_device("dev-1", "sess-1", _HealthyWebSocket())
    task = conn.writer._task
    assert task is not None and not task.done()

    await engine.unregister_device("dev-1", "sess-1")

    # Already finished by the time unregister returns — no orphan task.
    assert task.done()
    assert conn.writer.alive is False
    assert engine._connections.get("sess-1") is None


@pytest.mark.asyncio
async def test_unregister_with_mismatched_websocket_keeps_the_new_writer(engine):
    """Reconnect race: the old socket's cleanup must not kill the new
    connection's writer."""
    await engine.register_device("dev-1", "sess-1", _HealthyWebSocket())
    stale_ws = _HealthyWebSocket()
    new_conn = engine._connections["sess-1"]["dev-1"]

    await engine.unregister_device("dev-1", "sess-1", websocket=stale_ws)

    assert engine._connections["sess-1"]["dev-1"] is new_conn
    assert new_conn.writer.alive is True


@pytest.mark.asyncio
async def test_session_sibling_sockets_are_idempotent_ordered_and_cleanup_is_scoped(engine):
    first_ws, second_ws = _HealthyWebSocket(), _HealthyWebSocket()
    first = await engine.register_device("dev-1", "sess-1", first_ws)
    second = await engine.register_device("dev-1", "sess-1", second_ws)
    assert await engine.register_device("dev-1", "sess-1", first_ws) is first
    assert await engine.register_device("dev-1", "sess-1", second_ws) is second
    assert engine.get_device_count("sess-1") == 2
    assert engine.get_connected_devices("sess-1") == ["dev-1"]
    first_ws.close.assert_not_called()
    second_ws.close.assert_not_called()

    # Ambiguous legacy cleanup cannot remove either tab.
    await engine.unregister_device("dev-1", "sess-1")
    assert engine.get_device_count("sess-1") == 2
    for i in range(3):
        await engine.broadcast_event(SyncEvent(
            event_type="stream_frame", session_id="sess-1", data={"i": i}))
    assert await _until(lambda: len(first_ws.sent) == len(second_ws.sent) == 3)
    assert [json.loads(t)["data"]["i"] for t in first_ws.sent] == [0, 1, 2]
    assert first_ws.sent == second_ws.sent

    first_task = first.writer._task
    await engine.unregister_device("dev-1", "sess-1", first_ws)
    assert first_task.done()
    assert second.writer.alive
    assert engine.is_device_watching_session("dev-1", "sess-1")
    assert engine.get_connected_devices("sess-1") == ["dev-1"]
    # Late cleanup and writer callbacks from the removed socket are harmless.
    await engine.unregister_device("dev-1", "sess-1", first_ws)
    await engine._reap_device("dev-1", "sess-1", first)
    await engine.broadcast_event(SyncEvent(
        event_type="stream_frame", session_id="sess-1", data={"i": 3}))
    assert await _until(lambda: len(second_ws.sent) == 4)
    assert len(first_ws.sent) == 3


@pytest.mark.asyncio
async def test_session_stale_cleanup_cannot_evict_reused_registration_key(engine):
    stale_ws, replacement_ws = _HealthyWebSocket(), _HealthyWebSocket()
    stale = await engine.register_device("dev-1", "sess-1", stale_ws)
    await engine.unregister_device("dev-1", "sess-1", stale_ws)
    replacement = await engine.register_device("dev-1", "sess-1", replacement_ws)
    await engine.unregister_device("dev-1", "sess-1", stale_ws)
    await engine._reap_device("dev-1", "sess-1", stale)
    assert engine._connections["sess-1"]["dev-1"] is replacement
    assert replacement.writer.alive


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [True, False])
async def test_session_failed_sibling_is_pruned_without_evicting_healthy_socket(engine, bad_first):
    healthy, bad = _HealthyWebSocket(), _ExplodingWebSocket()
    for ws in ([bad, healthy] if bad_first else [healthy, bad]):
        await engine.register_device("dev-1", "sess-1", ws)
    await engine.broadcast_event(SyncEvent(
        event_type="stream_frame", session_id="sess-1", data={"i": 0}))
    assert await _until(lambda: engine.get_device_count("sess-1") == 1)
    assert await _until(lambda: len(healthy.sent) == 1)
    assert engine.get_connected_devices("sess-1") == ["dev-1"]
    assert engine.is_device_watching_session("dev-1", "sess-1")


@pytest.mark.asyncio
async def test_session_exclusion_uses_persistent_device_not_registration_key(engine):
    tabs = [_HealthyWebSocket(), _HealthyWebSocket()]
    for ws in tabs:
        await engine.register_device("dev-1", "sess-1", ws)
    other = _HealthyWebSocket()
    await engine.register_device("dev-2", "sess-1", other)
    await engine.broadcast_event(SyncEvent(
        event_type="stream_frame", session_id="sess-1", data={}),
        exclude_device_id="dev-1")
    assert await _until(lambda: len(other.sent) == 1)
    assert all(ws.sent == [] for ws in tabs)


@pytest.mark.asyncio
async def test_user_writer_task_cancelled_on_unregister(engine):
    conn = await engine.register_user_device("u1", "dev-1", _HealthyWebSocket())
    task = conn.writer._task
    await engine.unregister_user_device("u1", "dev-1")
    assert task.done()
    assert engine._user_connections.get("u1") is None


@pytest.mark.asyncio
async def test_shutdown_cancels_every_writer():
    """Server shutdown must not leave writer tasks pending on a closing loop."""
    eng = SyncEngine()
    a = await eng.register_device("dev-a", "sess-1", _HungWebSocket())
    b = await eng.register_device("dev-b", "sess-2", _GatedWebSocket(open_=False))
    c = await eng.register_user_device("u1", "dev-c", _HungWebSocket())
    tasks = [a.writer._task, b.writer._task, c.writer._task]

    await eng.shutdown()

    assert all(t.done() for t in tasks)
    assert eng._connections == {}
    assert eng._user_connections == {}
    await eng.shutdown()  # idempotent


# ─── death / reaping ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dead_socket_writer_reaps_itself_from_the_registry(engine, monkeypatch):
    """A half-open device is evicted by its OWN writer's send timeout, without
    any producer having waited for it."""
    monkeypatch.setattr(se, "_SEND_TIMEOUT_SECONDS", 0.05)
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    conn = await engine.register_device("dev-hung", "sess-1", hung)
    await engine.register_device("dev-ok", "sess-1", healthy)

    await engine.broadcast_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={"type": "start"})
    )

    assert await _until(lambda: "dev-hung" not in engine._connections.get("sess-1", {}))
    assert conn.writer.death_reason == "send_failed"
    assert "dev-ok" in engine._connections["sess-1"]
    assert len(healthy.sent) == 1


@pytest.mark.asyncio
async def test_raising_socket_kills_only_its_own_connection(engine):
    """An exception inside a writer must not take down the loop or the siblings."""
    boom = _ExplodingWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_device("dev-boom", "sess-1", boom)
    await engine.register_device("dev-ok", "sess-1", healthy)

    for i in range(3):
        await engine.broadcast_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1", data={"i": i})
        )

    assert await _until(lambda: "dev-boom" not in engine._connections.get("sess-1", {}))
    assert await _until(lambda: len(healthy.sent) == 3)
    # It failed once and was retired — it did not spin retrying.
    assert boom.attempts == 1


@pytest.mark.asyncio
async def test_unserializable_event_drops_the_frame_not_the_connections(engine):
    """A payload bug is one message's problem; every connection stays alive."""
    healthy = _HealthyWebSocket()
    await engine.register_device("dev-ok", "sess-1", healthy)

    circular: dict = {}
    circular["self"] = circular  # json.dumps -> ValueError

    bad = SyncEvent(event_type="stream_frame", session_id="sess-1", data=circular)
    await engine.broadcast_event(bad)

    assert "dev-ok" in engine._connections["sess-1"]
    await engine.broadcast_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={"ok": True})
    )
    assert await _until(lambda: len(healthy.sent) == 1)


@pytest.mark.asyncio
async def test_source_device_is_still_excluded(engine):
    """Behaviour parity: the echo-suppression contract is unchanged."""
    origin = _HealthyWebSocket()
    other = _HealthyWebSocket()
    await engine.register_device("dev-origin", "sess-1", origin)
    await engine.register_device("dev-other", "sess-1", other)

    await engine.broadcast_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={"type": "x"}),
        exclude_device_id="dev-origin",
    )

    assert await _until(lambda: len(other.sent) == 1)
    await asyncio.sleep(0.02)
    assert origin.sent == []


@pytest.mark.asyncio
async def test_send_timeout_is_short_enough_to_be_an_eviction_deadline():
    """W2: the per-send timeout now only evicts a dead socket — it no longer
    governs producer progress, so it must be aggressive (~2-3 s)."""
    assert 1.0 <= se._SEND_TIMEOUT_SECONDS <= 3.0


# ─── end-to-end: the turn pump's broadcast adapter ──────────────────────────


@pytest.mark.asyncio
async def test_default_broadcast_adapter_is_not_stalled_by_a_dead_device():
    """The W2 symptom in situ: SessionRuntime._default_broadcast fans a frame
    out through the GLOBAL sync engine on every non-trimmed frame. With a
    half-open watcher, that call must still return immediately — otherwise the
    pump, the TurnRecorder and SSE delivery to every device stall behind it."""
    from app.core import session_runtime as sr
    from app.core.sync_engine import sync_engine as global_engine

    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await global_engine.register_device("dev-hung", "sess-w2", hung)
    await global_engine.register_device("dev-ok", "sess-w2", healthy)
    try:
        started = time.perf_counter()
        for i in range(30):
            await sr._default_broadcast(
                "sess-w2",
                {"type": "tool_use", "id": f"t{i}"},
                source="server",
                origin_send=None,
                origin_device_id=None,
            )
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, f"turn pump stalled {elapsed:.2f}s on a dead device"
        assert await _until(lambda: len(healthy.sent) == 30)
    finally:
        await global_engine.unregister_device("dev-hung", "sess-w2")
        await global_engine.unregister_device("dev-ok", "sess-w2")
