"""Content-free isolated memory lab. Never connects to a live worker/DB.

Run from the repo root with PYTHONPATH=.:fluid. --allocator uses real run_sync
thread workers to decode JSON, with/without glibc arena limits and malloc_trim.
--retention exercises notification fanout, reader-token churn, and Git polling.
Numbers are lab evidence, not attribution of production RSS.
"""
import argparse
import asyncio
from collections import Counter
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch


def rss():
    import psutil
    return round(psutil.Process().memory_info().rss / 2**20, 3)


def snapshot(label, initial):
    gc.collect()
    current = tracemalloc.take_snapshot()
    counts = Counter(type(obj).__name__ for obj in gc.get_objects())
    print(json.dumps({"label": label, "rss_mib": rss(),
        "traced_mib": round(tracemalloc.get_traced_memory()[0] / 2**20, 3),
        "objects": {k: counts[k] for k in ("Task", "Lock", "Handle", "dict")},
        "top_diffs": [str(x) for x in current.compare_to(initial, "lineno")[:4]]}), flush=True)


async def retention(rounds):
    from app.core.event_gateway import EventGateway
    from app.core.session_runtime import get_reader_lock, _reader_locks
    from app.core.git_cache import _git_cache
    from app.core.git_service import git_service
    import app.core.git_service as git_module
    gateway = EventGateway()
    gateway._ensure_listener = lambda: None
    tracemalloc.start()
    initial = tracemalloc.take_snapshot()
    snapshot("warm", initial)
    class NativeFixture:
        result = None
        def get_status(self, _path):
            return self.result
    native = NativeFixture()
    with patch.object(git_module, "_native_reader", native):
        for turn in range(rounds):
            queue = gateway.subscribe("slow-reader")
            start = time.perf_counter()
            # The real listener posts these while the loop or SSE reader is busy.
            for seq in range(20000):
                gateway._dispatch("slow-reader", seq)
            scheduled = len(asyncio.get_running_loop()._ready)
            await asyncio.sleep(0)
            depth = queue.qsize()
            gateway.unsubscribe("slow-reader", queue)
            del queue
            for i in range(1000):
                get_reader_lock(f"session-{turn}-{i}")
            # Real status -> native boundary -> cache path; synthetic git output.
            for i in range(100):
                native.result = {"untracked": [
                    f"{turn}-{i}-{j}-" + "x" * 500 for j in range(100)], "is_clean": False}
                git_service.get_status(f"/synthetic/worktree-{turn}-{i}")
            print(json.dumps({"round": turn, "scheduled_callbacks": scheduled,
                "subscriber_depth": depth, "reader_locks": len(_reader_locks),
                "git_entries": len(_git_cache._data),
                "elapsed_s": round(time.perf_counter() - start, 4)}), flush=True)
            snapshot(f"round-{turn}", initial)
    # Inspect ownership, without printing payloads.
    probe = get_reader_lock("referrer-probe")
    print(json.dumps({"reader_lock_registry_owns_probe": any(
        ref is _reader_locks for ref in gc.get_referrers(probe))}), flush=True)


async def journeys(rounds, duration_seconds=0):
    # Actual engine turns and sync apply, scripted provider/in-memory DB only.
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn, TurnComplete
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore
    from app.core.sync_engine import SyncEngine, SyncEvent
    from app.network import engine
    from tests.core.network.harness import Worker, join, set_owner_pref, exchange, owner_pref

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        async def stream(self, _request):
            for kind, data in [
                ("message_start", {"usage": {"input_tokens": 10}}),
                ("block_start", {"index": 0, "block_type": "text"}),
                ("text_delta", {"index": 0, "text": "Synthetic reply " + "x" * 4096}),
                ("block_stop", {"index": 0}),
                ("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
                ("message_stop", {}),
            ]:
                yield StreamChunk(kind, data)

    class Socket:
        async def send_text(self, _text):
            await asyncio.sleep(0)
        async def close(self):
            pass

    sync = SyncEngine()
    with tempfile.TemporaryDirectory(prefix="shuffle-sync-lab-") as root:
        first, second = Worker("first", Path(root)), Worker("second", Path(root))
        try:
            with first.active():
                engine.create_network("Synthetic lab")
            join(first, second)
            tracemalloc.start()
            initial = tracemalloc.take_snapshot()
            snapshot("warm", initial)
            deadline = time.monotonic() + duration_seconds
            for turn in range(rounds if not duration_seconds else 100000):
                for session in range(4):
                    loop = NativeLoop(provider=Provider(), store=MemoryStore(), model="synthetic")
                    for _ in range(8):
                        await loop.submit(UserTurn(content="Synthetic input " + "x" * 2048))
                        await loop.wait_turn()
                        assert any(isinstance(e, TurnComplete) for e in loop.bus.log(loop.session_id))
                    await loop.bus.close()
                    del loop
                for i in range(40):
                    socket = Socket()
                    await sync.register_device("device", "session", socket)
                    for _ in range(8):
                        await sync.broadcast_event(SyncEvent("session_updated", "session", {"title": "x" * 4096}))
                    await sync.unregister_device("device", "session", socket)
                value = {"round": turn, "content": "x" * 65536}
                set_owner_pref(first, "synthetic-memory-lab", value)
                applied = exchange(first, second)
                assert applied.retry == applied.pending == applied.rejected == 0, applied.reasons
                assert owner_pref(second, "synthetic-memory-lab") == value
                print(json.dumps({"round": turn, "turns": 32, "websocket_reconnects": 40,
                    "sync_pending": applied.pending, "sync_retry": applied.retry,
                    "remaining_connections": len(sync._connections),
                    "tasks_by_coro": dict(Counter(t.get_coro().__qualname__ for t in asyncio.all_tasks()))}), flush=True)
                snapshot(f"round-{turn}", initial)
                if duration_seconds and time.monotonic() >= deadline:
                    break
        finally:
            first.close()
            second.close()
            engine.reset_for_tests()


async def events(rounds):
    from fluid.events.bus import EventBus
    from fluid.events.model import ToolResult
    bus = EventBus()
    tracemalloc.start()
    initial = tracemalloc.take_snapshot()
    snapshot("warm", initial)
    for turn in range(rounds):
        for i in range(64):
            await bus.publish(ToolResult(session_id="session", tool_use_id=f"{turn}-{i}",
                name="SyntheticTool", output=f"{turn}-{i}:" + "x" * (256 * 1024)))
        print(json.dumps({"round": turn, "event_ring_entries": len(bus.log("session")),
            "event_ring_payload_chars": sum(len(e.output) for e in bus.log("session"))}), flush=True)
        snapshot(f"round-{turn}", initial)


async def allocator(rounds, trim):
    from app.db.async_database import run_sync
    from concurrent.futures import ThreadPoolExecutor
    # Warm the exact pool size before measuring steady state.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8))
    payload = json.dumps([{"id": i, "content": "x" * (300 + i % 900),
        "parts": list(range(32))} for i in range(6000)])
    def decode():
        data = json.loads(payload)
        return len(data)
    await asyncio.gather(*(run_sync(decode) for _ in range(8)))
    tracemalloc.start()
    initial = tracemalloc.take_snapshot()
    snapshot("warm", initial)
    for turn in range(rounds):
        start = time.perf_counter()
        for _ in range(4):
            await asyncio.gather(*(run_sync(decode) for _ in range(8)))
        gc.collect()
        before_trim = rss()
        if trim and sys.platform == "linux":
            import ctypes
            libc = ctypes.CDLL(None)
            if hasattr(libc, "malloc_trim"):
                libc.malloc_trim(0)
        print(json.dumps({"round": turn, "rss_before_trim_mib": before_trim,
            "elapsed_s": round(time.perf_counter() - start, 4)}), flush=True)
        snapshot(f"round-{turn}", initial)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--allocator", action="store_true")
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--journeys", action="store_true")
    parser.add_argument("--trim", action="store_true")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--duration-seconds", type=int, default=0,
                        help="Run --journeys for this duration instead of a fixed round count")
    args = parser.parse_args()
    os.environ.pop("AI_SHUFFLE_ENV_FILE", None)
    os.environ["TESTING"] = "1"
    with tempfile.TemporaryDirectory(prefix="shuffle-memory-lab-") as root:
        os.environ["DATA_DIR"] = root
        asyncio.run(allocator(args.rounds, args.trim) if args.allocator else
                    events(args.rounds) if args.events else journeys(args.rounds, args.duration_seconds)
                    if args.journeys else retention(args.rounds))
