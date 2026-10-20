"""Real turn/store/adapter/pool-eviction lab; no live app or provider access.

Run with pytest -s for tracemalloc and gc ownership evidence. Store/engine
handles are weak: only the adapter and actual lifecycle code may retain them.
"""
import asyncio
import gc
import json
import tracemalloc
import weakref

import pytest

from app.core.provider import harness_adapter as ha
from app.core.provider.fluid_pg_store import PgFluidStore
from app.core.provider.types import SessionOptions
from app.core.session_pool import SessionPool, SessionState
from fluid.events.model import ErrorEvent, TurnComplete
from fluid.events.ops import UserTurn
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import SessionNode


class LabProvider:
    name = "retention-lab"
    caps = ProviderCaps()

    async def stream(self, request):
        text = "reply:" + request.messages[-1].text()
        yield StreamChunk("message_start", {})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": text})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"stop_reason": "end_turn"})


def retained(weak_stores):
    stats = {"live_stores": 0, "cached_nodes": 0, "cached_text_bytes": 0}
    owners = set()
    for ref in weak_stores:
        store = ref()
        if store is None:
            continue
        stats["live_stores"] += 1
        for tree in store._nodes.values():
            stats["cached_nodes"] += len(tree)
            stats["cached_text_bytes"] += sum(len(n.message.text().encode()) for n in tree.values())
        owners.update(type(r).__name__ for r in gc.get_referrers(store))
    stats["store_referrers"] = sorted(owners)
    return stats


@pytest.mark.asyncio
async def test_turn_store_adapter_eviction_retention_lab(mock_db, monkeypatch, tmp_path):
    import fluid.context.plugins as plugins

    async def provider(*args):
        return LabProvider(), None

    monkeypatch.setattr(ha, "_build_provider_client", provider)
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _: PgFluidStore()))
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **kw: [])
    pool = SessionPool()
    # Adapters may have real external owners (e.g. readers) after eviction;
    # stores and engines must NOT be artificially owned by the lab.
    adapters, weak_stores, weak_engines, weak_adapters = [], [], [], []
    cached_nodes_before_eviction = 0
    gc.collect()
    tracemalloc.start(10)
    baseline = tracemalloc.take_snapshot()
    try:
        for i in range(8):
            adapter = ha.HarnessAdapter()
            await adapter.connect(SessionOptions(
                model="gpt-4o", cwd=str(tmp_path), session_id=f"lab-{i}",
                setting_sources=[], extra={"fluid_provider": "openai_responses"},
            ))
            adapters.append(adapter)
            weak_adapters.append(weakref.ref(adapter))
            weak_stores.append(weakref.ref(adapter._store))
            engine = adapter._engine
            weak_engines.append(weakref.ref(engine))
            sid = engine.session_id
            for turn in range(8):
                since = engine.bus.last_seq(sid)
                await engine.submit(UserTurn(content=f"session {i} turn {turn}:" + "x" * 32768))
                async with asyncio.timeout(15):
                    async for event in engine.events(since_seq=since):
                        assert not isinstance(event, ErrorEvent), event
                        if isinstance(event, TurnComplete):
                            break
                await engine._turn_task
            del event
            cached_nodes_before_eviction += sum(len(tree) for tree in adapter._store._nodes.values())
            state = SessionState(adapter=adapter, is_connected=True, sdk_session_id=sid)
            pool.put(sid, state)
            await pool._do_evict(sid, state, "retention-lab")
            assert adapter._engine is None
            del engine, state, adapter
        gc.collect()
        snapshot = tracemalloc.take_snapshot()
        stats = retained(weak_stores)
        stats["cached_nodes_before_eviction"] = cached_nodes_before_eviction
        stats["adapters_holding_store_after_eviction"] = sum(a._store is not None for a in adapters)
        stats["live_engines_after_eviction"] = sum(r() is not None for r in weak_engines)
        stats["traced_growth_bytes"] = sum(s.size_diff for s in snapshot.compare_to(baseline, "lineno"))
        stats["top_allocations"] = [str(s) for s in snapshot.compare_to(baseline, "lineno")[:5]]
        print("RETENTION_LAB " + json.dumps(stats, sort_keys=True))
        assert len(pool) == 0
        # Durable history survives collection of the entire original store.
        cold = PgFluidStore()
        path = await cold.load_path("lab-0")
        assert len(path) == 16
        assert path[0].message.text().startswith("session 0 turn 0:")
        assert (await cold.events_since("lab-0", 0))
        assert (await cold.get_node("lab-0", path[0].node_id)).node_id == path[0].node_id
        del path, cold
        adapters.clear()
        gc.collect()
        print("RETENTION_LAB live_adapters_after_release=" + str(sum(r() is not None for r in weak_adapters)))
        assert stats["live_stores"] == 0
        assert stats["live_engines_after_eviction"] == 0
        assert stats["adapters_holding_store_after_eviction"] == 0
        assert not any(r() is not None for r in weak_stores)
        assert not any(r() is not None for r in weak_adapters)
    finally:
        tracemalloc.stop()
        for adapter in adapters:
            await adapter.disconnect_transport()


@pytest.mark.asyncio
async def test_warm_store_reads_and_rewinds_without_database_round_trips(mock_db, monkeypatch):
    from app.core.provider import fluid_pg_store as pg
    from fluid.messages import Message, Text

    store = PgFluidStore()
    await store.create_session("branches")
    await store.append("branches", SessionNode(node_id="root", parent_id=None,
                       message=Message(role="user", content=[Text("root")])))
    for i in range(40):
        await store.append("branches", SessionNode(
            node_id=f"branch-{i}", parent_id="root",
            message=Message(role="assistant", content=[Text("x" * 8192)]),
        ))
    resumed = PgFluidStore()
    assert await resumed.get_session("branches")

    async def no_read(*args):
        pytest.fail("warm transcript read attempted database I/O")

    with monkeypatch.context() as patch:
        patch.setattr(pg, "run_sync", no_read)
        for _ in range(30):
            assert [n.node_id for n in await resumed.load_path("branches")] == ["root", "branch-39"]
        assert (await resumed.get_node("branches", "branch-0")).node_id == "branch-0"
        assert await resumed.get_node("branches", "absent") is None
        assert [n.node_id for n in await resumed.load_path("branches", "branch-0")] == ["root", "branch-0"]
    await resumed.set_head("branches", "branch-2")
    with monkeypatch.context() as patch:
        patch.setattr(pg, "run_sync", no_read)
        assert [n.node_id for n in await resumed.load_path("branches")] == ["root", "branch-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "error", "timeout", "no-engine"])
async def test_disconnect_releases_store_and_registry_even_on_incomplete_close(mode):
    from unittest.mock import AsyncMock

    adapter = ha.HarnessAdapter()
    store = PgFluidStore()
    adapter._store = store
    registry = AsyncMock()
    adapter._registry = registry
    if mode != "no-engine":
        engine = AsyncMock()
        if mode == "error":
            engine.aclose.side_effect = RuntimeError("synthetic close error")
        elif mode == "timeout":
            async def wait():
                await asyncio.Event().wait()
            engine.aclose.side_effect = wait
        adapter._engine = engine
    await adapter.disconnect_transport(grace_period=0.01)
    assert adapter._store is None
    assert adapter._registry is None
    assert adapter._engine is None
    registry.aclose.assert_awaited_once()
    # Repeated teardown does not close the registry twice.
    await adapter.disconnect_transport()
    registry.aclose.assert_awaited_once()
    # Never mutate a store an engine could still be using after a close timeout.
    assert store._sessions == {}


@pytest.mark.asyncio
async def test_resource_monitor_counts_adapters_without_owning_them(monkeypatch):
    from app.core import resource_monitor

    monkeypatch.setattr(ha.HarnessAdapter, "_instances", weakref.WeakSet())
    adapter = ha.HarnessAdapter()
    adapter._store = PgFluidStore()
    from fluid.messages import Message, Text
    await adapter._store.create_session("metrics")
    await adapter._store.append("metrics", SessionNode(
        node_id="root", parent_id=None,
        message=Message(role="user", content=[Text("retained node")]),
    ))
    snapshot = await resource_monitor._collect_metrics()
    assert snapshot["harness_adapters"] == 1
    assert snapshot["harness_adapters_with_store"] == 1
    assert snapshot["harness_retained_nodes"] == 1
    await adapter.disconnect_transport()
    snapshot = await resource_monitor._collect_metrics()
    assert snapshot["harness_adapters"] == 1
    assert snapshot["harness_adapters_with_store"] == 0
    assert snapshot["harness_retained_nodes"] == 0
    ref = weakref.ref(adapter)
    del adapter
    gc.collect()
    assert ref() is None
    assert (await resource_monitor._collect_metrics())["harness_adapters"] == 0
