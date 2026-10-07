"""Performance receipts must be driven by the real query producer, not fixtures alone."""
import logging
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.startup_timing import TurnStartupTiming


def test_local_monotonic_spans(caplog):
    clock = iter([10.0, 10.125, 10.5])
    with caplog.at_level(logging.INFO, logger="app.core.startup_timing"):
        timing = TurnStartupTiming("session", "turn", clock=lambda: next(clock))
        timing.mark("options_built")
        timing.mark("submitted")
    assert "elapsed_ms=125.000 stage_ms=125.000" in caplog.messages[0]
    assert "elapsed_ms=500.000 stage_ms=375.000" in caplog.messages[1]


def test_default_clock_preserves_sub_millisecond_stages(monkeypatch, caplog):
    from app.core import startup_timing

    ticks = iter([10.0, 10.00025])
    monkeypatch.setattr(startup_timing, "time", SimpleNamespace(
        perf_counter=lambda: next(ticks), monotonic=lambda: 10.0,
    ))
    with caplog.at_level(logging.INFO, logger="app.core.startup_timing"):
        timing = TurnStartupTiming("session", "turn")
        timing.mark("submitted")
    assert "elapsed_ms=0.250 stage_ms=0.250" in caplog.messages[0]


@pytest.mark.asyncio
async def test_query_producer_reports_only_reached_stages(tmp_path, caplog):
    from app.core import query_engine as qe
    from app.core.provider.events import ResultMessage
    from tests.core.engine_harness import collect
    from tests.core.query_engine_flow_harness import engine_patches, fresh_adapter

    with caplog.at_level(logging.INFO, logger="app.core.startup_timing"), ExitStack() as stack:
        adapter = fresh_adapter([ResultMessage(subtype="success")])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        await collect(qe._stream_to_websocket_impl("private prompt marker", "timing-session", "claude-code"))
    records = [r.getMessage() for r in caplog.records if r.name == "app.core.startup_timing"]
    assert [r.split("stage=")[1].split()[0] for r in records] == [
        "options_built", "adapter_ready", "submitted", "first_event",
    ]
    assert all("private prompt marker" not in r for r in records)
    adapter.send_query.assert_awaited_once()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.core.startup_timing"), ExitStack() as stack:
        adapter = fresh_adapter([])
        adapter.send_query = AsyncMock(side_effect=RuntimeError("send failed"))
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        await collect(qe._stream_to_websocket_impl("hi", "timing-session", "claude-code"))
    records = [r.getMessage() for r in caplog.records if r.name == "app.core.startup_timing"]
    assert [r.split("stage=")[1].split()[0] for r in records] == ["options_built", "adapter_ready"]
