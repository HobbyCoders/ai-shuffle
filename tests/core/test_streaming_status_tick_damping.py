"""Damping of the event-gateway busy re-check (audit W5 / F8).

``streaming_status`` registers a listener that fires on EVERY notify tick — ~7
per second per streaming session (the recorder's 150 ms flush). Each tick used
to pop the guard TTL cache and run an uncached ``runs`` + ``turns`` read that
almost always diffed to "no change". These tests pin the two dampers and the
transitions they must NOT break.
"""

import asyncio

import pytest

from app.core import streaming_status
from app.core.event_gateway import EventGateway


@pytest.fixture(autouse=True)
def _reset():
    streaming_status._reset_state()
    yield
    streaming_status._reset_state()


@pytest.fixture
def busy_reads(monkeypatch):
    """Count uncached busy reads; report the session as idle."""
    calls = []

    def _read(session_id):
        calls.append(session_id)
        return False

    monkeypatch.setattr(streaming_status, "_has_active_run_db", _read)
    return calls


async def test_tick_on_an_already_busy_session_does_no_db_work(
    mock_db, monkeypatch, busy_reads
):
    """An append tick can only ever confirm "still busy".

    The busy->idle edge rides the run-lifecycle notify (release/abort) and the
    15 s sweep, both of which stay intact — see the tests below.
    """
    streaming_status._last_broadcast["s1"] = {"user_id": "admin"}
    streaming_status._cache["s1"] = (asyncio.get_running_loop().time(), True)

    for _ in range(20):
        streaming_status.notify_gateway_tick("s1")
    await asyncio.sleep(0.1)

    assert busy_reads == []
    # ...and the guard cache other busy-guards read through is left alone.
    assert "s1" in streaming_status._cache


async def test_ticks_are_throttled_per_session_with_a_trailing_recheck(
    mock_db, monkeypatch, busy_reads
):
    monkeypatch.setattr(streaming_status, "GATEWAY_TICK_COOLDOWN_SECONDS", 0.2)

    for _ in range(25):
        streaming_status.notify_gateway_tick("s1")
    await asyncio.sleep(0.05)
    # Leading edge: the first tick fires at once, so an idle->busy edge keeps
    # its latency; the other 24 collapse.
    assert len(busy_reads) == 1

    # Trailing edge: the burst ended inside the window, so exactly one more.
    await asyncio.sleep(0.35)
    assert len(busy_reads) == 2

    await asyncio.sleep(0.1)
    assert len(busy_reads) == 2  # nothing keeps firing on its own


async def test_throttle_is_per_session(mock_db, monkeypatch, busy_reads):
    monkeypatch.setattr(streaming_status, "GATEWAY_TICK_COOLDOWN_SECONDS", 5.0)

    streaming_status.notify_gateway_tick("s1")
    streaming_status.notify_gateway_tick("s2")
    streaming_status.notify_gateway_tick("s1")
    await asyncio.sleep(0.1)

    assert sorted(busy_reads) == ["s1", "s2"]


async def test_lifecycle_notify_is_not_damped(mock_db, monkeypatch, busy_reads):
    """The authoritative path (claim/release) must stay undamped."""
    monkeypatch.setattr(streaming_status, "GATEWAY_TICK_COOLDOWN_SECONDS", 5.0)

    for _ in range(3):
        streaming_status.notify("s1")
    await asyncio.sleep(0.1)

    assert len(busy_reads) == 3


async def test_idle_to_busy_edge_still_broadcasts_through_a_tick(
    mock_db, monkeypatch
):
    sent = []

    async def _send(session_id, is_streaming, info):
        sent.append((session_id, is_streaming))

    monkeypatch.setattr(streaming_status, "_send_streaming_changed", _send)
    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: True)
    monkeypatch.setattr(
        streaming_status, "_broadcast_info", lambda sid: {"user_id": "admin"}
    )

    streaming_status.notify_gateway_tick("s1")
    for _ in range(20):
        streaming_status.notify_gateway_tick("s1")
    await asyncio.sleep(0.2)

    assert sent == [("s1", True)]  # exactly one spinner-on frame


async def test_sweep_still_clears_a_stale_busy_entry(mock_db, monkeypatch):
    """The backstop the already-busy skip relies on must keep working."""
    sent = []

    async def _send(session_id, is_streaming, info):
        sent.append((session_id, is_streaming))

    monkeypatch.setattr(streaming_status, "_send_streaming_changed", _send)
    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: False)
    monkeypatch.setattr(streaming_status, "_reap_and_kick_turns", _noop_reap)
    streaming_status._last_broadcast["s1"] = {"user_id": "admin"}

    cleared = await streaming_status.sweep_once()

    assert cleared == 1
    assert sent == [("s1", False)]
    assert "s1" not in streaming_status._last_broadcast


async def _noop_reap():
    return 0


def test_install_registers_the_damped_listener(monkeypatch):
    gateway = EventGateway()
    monkeypatch.setattr(gateway, "_ensure_listener", lambda: None)
    monkeypatch.setattr(
        "app.core.event_gateway.get_gateway", lambda: gateway
    )

    streaming_status.install_gateway_listener()

    assert streaming_status.notify_gateway_tick in gateway._global_listeners
    assert streaming_status.notify not in gateway._global_listeners


def test_tick_from_a_foreign_thread_without_a_loop_is_a_noop(monkeypatch):
    """The listener thread has no running loop and may see none captured."""
    streaming_status._loop = None
    streaming_status.notify_gateway_tick("s1")  # must not raise
    assert streaming_status._tick_pending == {}


async def test_tick_state_stays_bounded(mock_db, monkeypatch, busy_reads):
    monkeypatch.setattr(streaming_status, "_TICK_STATE_MAX", 8)
    monkeypatch.setattr(streaming_status, "GATEWAY_TICK_COOLDOWN_SECONDS", 0.001)

    for i in range(50):
        streaming_status.notify_gateway_tick(f"s{i}")
        await asyncio.sleep(0.005)

    assert len(streaming_status._tick_last_fired) <= 50
    # The prune actually runs once the cap is exceeded and entries age out.
    await asyncio.sleep(0.05)
    streaming_status.notify_gateway_tick("s-final")
    await asyncio.sleep(0.05)
    assert len(streaming_status._tick_last_fired) < 50
