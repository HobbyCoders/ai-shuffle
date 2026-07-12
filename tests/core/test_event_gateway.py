"""Unit tests for app/core/event_gateway.py — the LISTEN/NOTIFY fan-out.

No real PostgreSQL: the :class:`EventGateway` is instantiated directly and its
listener thread is stubbed out, so only the in-memory registry + dispatch logic
is exercised.
"""

import asyncio

import pytest

from app.core.event_gateway import EventGateway


@pytest.fixture
def gw(monkeypatch):
    g = EventGateway()
    # Never start the real listener thread (which would try to reach Postgres).
    monkeypatch.setattr(g, "_ensure_listener", lambda: None)
    return g


async def test_dispatch_only_reaches_matching_session(gw):
    q1 = gw.subscribe("s1")
    q2 = gw.subscribe("s1")
    q3 = gw.subscribe("s2")

    gw._dispatch("s1", 5)
    await asyncio.sleep(0.01)  # let call_soon_threadsafe callbacks run

    assert q1.get_nowait() == 5
    assert q2.get_nowait() == 5
    assert q3.empty()


async def test_unsubscribe_removes_delivery_and_empties_key(gw):
    q1 = gw.subscribe("s1")
    q2 = gw.subscribe("s1")

    gw.unsubscribe("s1", q1)
    assert "s1" in gw._subscribers  # q2 still there
    gw.unsubscribe("s1", q2)
    assert "s1" not in gw._subscribers  # key dropped once empty

    gw._dispatch("s1", 9)
    await asyncio.sleep(0.01)
    assert q1.empty()
    assert q2.empty()


def test_dispatch_to_closed_loop_does_not_raise(gw):
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    loop.close()
    with gw._lock:
        gw._subscribers["s3"] = {(loop, queue)}

    # Must not raise; the dead subscriber is dropped.
    gw._dispatch("s3", 1)
    assert "s3" not in gw._subscribers
