"""A mirror log recheck must survive both bounded gateway coalescing layers."""
import asyncio
import json

import pytest

from app.core.event_gateway import EventGateway, LOG_RECHECK


@pytest.mark.parametrize("order", ["before", "after"])
async def test_pending_mirror_recheck_survives_sequence_ticks(monkeypatch, order):
    gateway = EventGateway()
    monkeypatch.setattr(gateway, "_ensure_listener", lambda: None)
    queue = gateway.subscribe("mirror")
    check = {"session_id": "mirror", "max_seq": 1, "recheck_log": True}
    ordinary = {"session_id": "mirror", "max_seq": 999}
    payloads = [check, ordinary] if order == "before" else [ordinary, check]
    try:
        for payload in payloads:
            gateway._handle_payload(json.dumps(payload))
        await asyncio.sleep(0.01)
        assert queue.get_nowait() is LOG_RECHECK
        assert queue.empty()
    finally:
        gateway.unsubscribe("mirror", queue)


@pytest.mark.parametrize("order", ["before", "after"])
async def test_already_queued_mirror_recheck_survives_sequence_ticks(monkeypatch, order):
    gateway = EventGateway()
    monkeypatch.setattr(gateway, "_ensure_listener", lambda: None)
    queue = gateway.subscribe("mirror")
    first, second = (LOG_RECHECK, 999) if order == "before" else (999, LOG_RECHECK)
    try:
        gateway._dispatch("mirror", first)
        await asyncio.sleep(0.01)
        gateway._dispatch("mirror", second)
        await asyncio.sleep(0.01)
        assert queue.get_nowait() is LOG_RECHECK
        assert queue.empty()
    finally:
        gateway.unsubscribe("mirror", queue)
