"""Deterministic lab: native live-session transcript retention (issue #264).

Drives one synthetic long native session through the real ``NativeLoop``, the
app's compaction config (``harness_adapter._compaction_config``) and the real
``PgFluidStore`` over the test suite's in-memory PostgreSQL-on-SQLite shim.
It never connects to a live worker or database.

Each turn is a user message, ``--calls-per-turn`` tool calls with synthetic
results and a final reply. Every ``--compact-every`` turns the owner sends
``/compact``, the real manual path that writes a checkpoint summary (automatic
background checkpoints fire on their own schedule as well).

The same session runs twice in one process, each phase on a fresh database
with the same seeded ids:

- ``base``: the engine reads the whole path every call and the store keeps
  every node (the behaviour before the live window);
- ``windowed``: the engine reads ``load_window`` and the store evicts the
  checkpointed prefix.

Content-free JSON lines report, per sample: path nodes (durable), nodes the
store retains in memory and their JSON bytes, the traced Python heap above
the phase's own starting point, and process RSS (a high-water mark: the
windowed phase runs first so its RSS is not inflated by the base phase).
Each phase ends with a SHA-256 over every main-model request (system,
rendered history, tool names): equal digests mean byte-identical prompts.

``--phase windowed`` / ``--phase base`` run one phase per process (a clean
RSS baseline each); equal ``request_sha256`` across the two runs is the
byte-identical check. The base phase slows as its path grows (1000 turns
takes well over an hour).

Run from the repo root, ONE copy at a time, under a hard memory cap and a
timeout (the script also caps its own address space, ``--max-memory-gib``):
    PYTHONPATH=.:fluid prlimit --as=3221225472 timeout 5400 \
        python scripts/measure_live_transcript_retention.py --turns 1000 --phase windowed
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections import Counter


def rss_mib() -> float:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 2**20, 1)
    except Exception:  # noqa: BLE001 - optional
        return -1.0


class SeededIds:
    """``uuid.uuid4`` stand-in for fluid's ``new_id``: same ids every phase.

    ``new_id`` keeps only ``uuid4().hex[:12]``, the HIGH 48 bits. The counter
    therefore goes there (``n << 80``): ``UUID(int=n)`` puts it in the low bits,
    every id becomes ``<prefix>_000000000000``, each node is its own parent
    and the leaf→root walk never ends (this ate 15 GB+ before the fix).
    """

    SHIFT = 128 - 48

    def __init__(self) -> None:
        self.n = 0

    def uuid4(self) -> uuid.UUID:
        self.n += 1
        if self.n >= 1 << 48:
            raise OverflowError("seeded id space exhausted")
        return uuid.UUID(int=self.n << self.SHIFT)


async def phase(name: str, args: argparse.Namespace) -> dict:
    import fluid.store.base as store_base
    from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM
    from fluid.engine.native import NativeLoop
    from fluid.events import ContextCompaction, ErrorEvent, TurnComplete, UserTurn
    from fluid.permissions.engine import PermissionEngine
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import iter_events
    from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.provider.harness_adapter import _compaction_config

    store_base.uuid = SeededIds()
    probe = [store_base.new_id("probe") for _ in range(64)]
    if len(set(probe)) != len(probe):
        raise SystemExit("seeded ids collide in new_id's 12 hex digits; refusing to run")
    store_base.uuid = SeededIds()
    calls = args.calls_per_turn
    digest = hashlib.sha256()
    counts = {"main": 0, "checkpoint": 0}

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()

        def __init__(self) -> None:
            self.step = 0

        def estimate_cost(self, _model, _usage):
            return 0.0

        async def stream(self, request):
            if request.system == CHECKPOINT_SYSTEM:
                counts["checkpoint"] += 1
                chunks = [("text", "Checkpoint: the synthetic work so far. " + "s" * 1500)]
                stop = "end_turn"
            else:
                counts["main"] += 1
                digest.update(json.dumps(
                    [request.system, [m.to_dict() for m in request.messages],
                     [getattr(t, "name", str(t)) for t in (request.tools or [])]],
                    sort_keys=True, default=str).encode())
                position = self.step % (calls + 1)
                self.step += 1
                if position < calls:
                    chunks = [("tool", json.dumps({"text": f"call {self.step}"}))]
                    stop = "tool_use"
                else:
                    chunks = [("text", f"Done with turn step {self.step}. " + "r" * 300)]
                    stop = "end_turn"
            prompt = sum(len(json.dumps(m.to_dict())) for m in request.messages) // 4
            yield StreamChunk("message_start", {"usage": {"input_tokens": prompt, "output_tokens": 1}})
            for kind, payload in chunks:
                if kind == "text":
                    yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                    yield StreamChunk("text_delta", {"index": 0, "text": payload})
                else:
                    yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                                      "tool_name": "Echo",
                                                      "tool_use_id": f"toolu_{self.step:08d}"})
                    yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": payload})
                yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 50}, "stop_reason": stop})
            yield StreamChunk("message_stop", {"stop_reason": stop})

    registry = ToolRegistry()

    async def echo(inp, _ctx):
        seed = str(inp.get("text", ""))
        return ToolOutcome(text=(seed + " ") * (args.result_chars // (len(seed) + 1)))

    registry.register_builtin(ToolSpec(
        name="Echo", description="Echo.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    ), echo)

    store = PgFluidStore()
    if name == "base":
        # The behaviour before the window: every call reads the whole path,
        # and the store has nothing to evict.
        async def whole_path(self, sid, leaf_node_id=None):
            return await self.store.load_path(sid, leaf_node_id)
        NativeLoop._context_path = whole_path
        store.load_window = None
        store.evict_prefix = None
    loop = NativeLoop(provider=Provider(), store=store, model="synthetic-model",
                      registry=registry, permissions=PermissionEngine("never_ask"),
                      session_id="ses_retention_lab", context_window=args.context_window,
                      compaction=_compaction_config())
    gc.collect()
    floor = tracemalloc.get_traced_memory()[0]
    started = time.perf_counter()
    samples = []

    async def sample(turn: int) -> None:
        gc.collect()
        stats = store.cache_stats()
        row = {
            "phase": name, "turn": turn,
            "retained_nodes": stats["retained_nodes"],
            "retained_message_json_bytes": stats["message_json_bytes"],
            "traced_mib_above_start": round((tracemalloc.get_traced_memory()[0] - floor) / 2**20, 2),
            "rss_mib": rss_mib(),
        }
        row["path_nodes"] = len(await store.load_path(loop.session_id))
        row["main_calls"], row["checkpoint_calls"] = counts["main"], counts["checkpoint"]
        row["elapsed_s"] = round(time.perf_counter() - started, 1)
        samples.append(row)
        print(json.dumps(row), flush=True)

    for turn in range(1, args.turns + 1):
        prompts = [f"Request {turn}: continue the synthetic work. " + "u" * 400]
        if turn % args.compact_every == 0:
            prompts.append("/compact")
        for prompt in prompts:
            await loop.submit(UserTurn(content=prompt))
            await asyncio.wait_for(loop.wait_turn(), 300)
            task = getattr(loop, "_summary_task", None)
            if task is not None and not task.done():
                await task
            tail = loop.bus.log(loop.session_id)[-50:]
            if any(isinstance(e, ErrorEvent) for e in tail):
                raise SystemExit(f"{name}: turn {turn} raised an error event")
            if not any(isinstance(e, TurnComplete) for e in tail):
                raise SystemExit(f"{name}: turn {turn} did not complete")
        if turn % args.every == 0 or turn == args.turns:
            await sample(turn)

    sid = loop.session_id
    full = await store.load_path(sid)
    fresh = await PgFluidStore().load_path(sid)
    cached = store._nodes.get(sid, {})
    evicted = [n for n in full if n.node_id not in cached]
    read_back = all([await store.get_node(sid, n.node_id) == n for n in evicted[:20]])
    compactions = Counter()
    async for event in iter_events(store, sid, 0):
        if isinstance(event, ContextCompaction):
            compactions[f"tier{event.tier}:{event.reason}"] += 1
    final = {
        "phase": name, "final": True, "turns": args.turns, "path_nodes": len(full),
        "retained_nodes": store.cache_stats()["retained_nodes"],
        "peak_retained_nodes": max(s["retained_nodes"] for s in samples),
        "evicted_nodes": len(evicted), "evicted_read_back_exact": read_back,
        "fresh_store_path_equal": [n.node_id for n in fresh] == [n.node_id for n in full],
        "request_sha256": digest.hexdigest(), "main_calls": counts["main"],
        "checkpoint_calls": counts["checkpoint"], "compactions": dict(sorted(compactions.items())),
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    print(json.dumps(final), flush=True)
    await loop.aclose()
    return final


async def main_async(args: argparse.Namespace) -> None:
    from tests.pg_shim import new_sqlite_connection, route_db_to

    tracemalloc.start()
    results = {}
    names = ("windowed", "base") if args.phase == "both" else (args.phase,)
    for name in names:
        with route_db_to(new_sqlite_connection()):
            results[name] = await phase(name, args)
        gc.collect()
    if len(results) < 2:
        return  # compare request_sha256 across the two single-phase runs
    print(json.dumps({
        "summary": True,
        "prompts_byte_identical": results["base"]["request_sha256"] == results["windowed"]["request_sha256"],
        "retained_nodes": {k: v["retained_nodes"] for k, v in results.items()},
        "path_nodes": {k: v["path_nodes"] for k, v in results.items()},
    }), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turns", type=int, default=500)
    parser.add_argument("--calls-per-turn", type=int, default=2)
    parser.add_argument("--result-chars", type=int, default=2000)
    parser.add_argument("--context-window", type=int, default=200_000)
    parser.add_argument("--compact-every", type=int, default=50)
    parser.add_argument("--every", type=int, default=50)
    parser.add_argument("--phase", choices=("both", "windowed", "base"), default="both",
                        help="one phase per process gives each its own RSS baseline")
    parser.add_argument("--max-memory-gib", type=float, default=3.0,
                        help="RLIMIT_AS cap for this process (0 = none); still run it "
                             "under prlimit/ulimit and a timeout, one copy at a time")
    args = parser.parse_args()
    if args.max_memory_gib > 0:
        import resource
        cap = int(args.max_memory_gib * 2**30)
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        if soft == resource.RLIM_INFINITY or soft > cap:
            resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    os.environ.pop("AI_SHUFFLE_ENV_FILE", None)
    os.environ["TESTING"] = "1"
    with tempfile.TemporaryDirectory(prefix="shuffle-retention-lab-") as root:
        os.environ["DATA_DIR"] = root
        os.environ["HOME"] = root
        sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), "fluid")]
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
