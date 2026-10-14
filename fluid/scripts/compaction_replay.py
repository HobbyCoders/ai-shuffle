"""Replay a session's event log through the compactor, sweep off against on.

Reads an ``events.jsonl`` dump (one ``{"seq": ..., "event": {...}}`` row per
line, the shape the session store and the audit dumps write), rebuilds the
history path the model saw at every call, and drives ``Compactor.apply`` over
it once per sweep mode with the same cache signal each time. It reports the
summed prompt, the peak share of the window, how many calls committed a
batch, and the tokens those batches re-sent from their first changed message.

The rebuilt path is an approximation: assistant text and top-level tool calls
come from the stream events, each top-level result becomes its own node,
nested code-mode calls are left inside their cell, reasoning is not carried,
and a user turn is a fixed 1,200-char message. A calibration factor taken from
the calls before the first real compaction maps the chars/4 estimate to the
billed prompt, so the numbers read as measured tokens. The cache signal comes
from the recorded calls: their request times, their system and tool hashes,
their hit rates and the session's park and unpark events. Use it to compare
modes on one session, not as an exact cost model.

Usage::

    python scripts/compaction_replay.py path/to/events.jsonl --window 272000
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fluid.context.cache_signal import CacheSignal, update_p_cold  # noqa: E402
from fluid.context.compaction import (  # noqa: E402
    WAKE_TURN_SOURCES,
    CompactionConfig,
    Compactor,
)
from fluid.context.trim import estimate_tokens_of_messages  # noqa: E402
from fluid.messages import (  # noqa: E402
    Image,
    Message,
    Text,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.messages.content import SteerDelivery  # noqa: E402
from fluid.store.base import SessionNode  # noqa: E402

USER_TURN_CHARS = 1_200
CALIBRATION_CALLS = 25


@dataclass
class Call:
    """One recorded model call: the path it saw and its cache facts."""

    prefix: int  # len(nodes) at request time
    boundary: bool
    requested_at: float
    static: str
    overhead: int  # measured system + tools tokens
    billed: int
    hit_rate: float
    resumed: bool


@dataclass
class Session:
    nodes: list[SessionNode] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    wake_starts: set[str] = field(default_factory=set)
    first_compaction_call: int | None = None


def _node(nid: str, role: str, blocks: list[Any]) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _nested(tool_use_id: str) -> bool:
    """Calls a code-mode cell makes carry ``call_`` plus 12 hex chars; their
    output already sits inside the cell's own result."""
    tail = tool_use_id[5:]
    return (tool_use_id.startswith("call_") and len(tail) == 12
            and all(ch in "0123456789abcdef" for ch in tail))


def load(path: Path) -> Session:
    s = Session()
    uses: set[str] = set()
    text: list[str] = []
    calls: list[ToolUseBlock] = []
    steers: list[SteerDelivery] = []
    boundary = False
    resumed = True
    seq = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)["event"]
            cls = event["_cls"]
            seq += 1
            if cls == "TurnStarted":
                nid = event.get("node_id") or f"node_{seq:012x}"
                s.nodes.append(_node(nid, "user", [Text("u" * USER_TURN_CHARS)]))
                if event.get("source") in WAKE_TURN_SOURCES:
                    s.wake_starts.add(nid)
                if event.get("source") == "unpark":
                    resumed = True
                boundary = True
            elif cls == "SessionParked":
                resumed = True
            elif cls == "TextDelta":
                text.append(event.get("text") or "")
            elif cls == "ToolUse":
                tid = event.get("tool_use_id") or f"t{seq}"
                if event.get("name") == "run_python" or not _nested(tid):
                    uses.add(tid)
                    calls.append(ToolUseBlock(id=tid, name=event.get("name") or "",
                                              input=event.get("input") or {}))
            elif cls == "Usage":
                raw = event.get("raw") or {}
                hashes = raw.get("prefix_hashes") or {}
                uncached = int(event.get("input_tokens") or raw.get("input_tokens") or 0)
                cached = int(event.get("cache_read_tokens") or 0)
                billed = uncached + cached
                s.calls.append(Call(
                    prefix=len(s.nodes), boundary=boundary,
                    requested_at=float(hashes.get("_requested_at") or event.get("ts") or 0),
                    static=f"{hashes.get('system')}:{hashes.get('tools')}",
                    overhead=int(hashes.get("_tokens_tools") or 0),
                    billed=billed,
                    hit_rate=cached / billed if billed else 0.0,
                    resumed=resumed,
                ))
                boundary = resumed = False
                blocks: list[Any] = []
                if text:
                    blocks.append(Text("".join(text)))
                blocks.extend(calls)
                s.nodes.append(_node(f"node_{seq:012x}", "assistant",
                                     blocks or [Text("")]))
                text, calls = [], []
            elif cls == "SteerDelivered":
                for steer in event.get("steers") or []:
                    steers.append(SteerDelivery(
                        text=steer.get("text") or "",
                        origin=steer.get("origin") or "user",
                        sender=steer.get("from"),
                    ))
            elif cls == "ToolResult" and (event.get("tool_use_id") or "") in uses:
                content: list[Any] = [Text(event.get("output") or "")]
                content.extend(Image(media_type="image/png", data="x")
                               for _ in event.get("images") or [])
                s.nodes.append(_node(f"node_{seq:012x}", "user", [ToolResultBlock(
                    tool_use_id=event.get("tool_use_id") or "",
                    content=content, is_error=bool(event.get("is_error")),
                    delivered_steers=steers, delivery_metadata_version=1,
                )]))
                steers = []
            elif cls == "ContextCompaction" and s.first_compaction_call is None:
                s.first_compaction_call = len(s.calls)
    return s


def calibrate(s: Session) -> float:
    """Billed history tokens over the chars/4 estimate of the raw path, from
    the calls before the first real compaction, clamped like apply's."""
    end = s.first_compaction_call or len(s.calls)
    ratios = []
    for call in s.calls[:end][:CALIBRATION_CALLS]:
        est = estimate_tokens_of_messages([n.message for n in s.nodes[:call.prefix]])
        if est > 10_000 and call.billed > call.overhead:
            ratios.append((call.billed - call.overhead) / est)
    if not ratios:
        return 1.0
    return min(1.0, max(0.6, statistics.median(ratios)))


@dataclass
class Result:
    mode: str
    prompts: list[float] = field(default_factory=list)
    batches: int = 0
    sweeps: int = 0
    rewrite_tail: float = 0.0
    seconds: float = 0.0
    post_batch: list[float] = field(default_factory=list)
    summary_batches: int = 0
    summary_after: list[int] = field(default_factory=list)
    manual_ladder: int = 0
    manual_before: int = 0
    manual_after: int = 0


def replay(s: Session, mode: str, *, window: int, reserve: int,
           scale: float, summary_tokens: int = 0) -> Result:
    started = time.monotonic()
    compactor = Compactor(config=CompactionConfig(
        sweep_mode=mode, summary_enabled=summary_tokens > 0,
        summary_max_output_tokens=summary_tokens or 4_000,
    ))
    compactor.wake_turn_starts = set(s.wake_starts)
    out = Result(mode=mode)
    p_cold: float | None = None
    previous: Call | None = None
    pending: tuple[str, str, str] | None = None
    stub = "s" * (int(summary_tokens / scale) * 4)
    for call in s.calls:
        nodes = s.nodes[:call.prefix]
        overhead = int(call.overhead / scale)
        before = overhead + estimate_tokens_of_messages(compactor.view(nodes))
        signal = CacheSignal(
            gap_s=None if previous is None else max(
                0.0, call.requested_at - previous.requested_at),
            static_changed=previous is not None and call.static != previous.static,
            resumed=call.resumed,
            p_cold=p_cold or 0.0,
            reuse_floor=overhead,
        )
        rendered, promotions = compactor.apply(
            nodes, overhead_tokens=overhead, context_window=window,
            output_reserve_tokens=reserve, turn_boundary=call.boundary,
            measured_prompt_tokens=int(before * scale), cache=signal,
        )
        threshold = compactor._threshold(compactor.config.trigger_tokens, window, reserve)
        if pending is not None and ((call.boundary and signal.cold_now)
                                    or promotions or before * scale >= threshold):
            promo = compactor.apply_summary(nodes, pending, overhead_tokens=overhead)
            pending = None
            if promo is not None:
                promotions.append(promo)
                rendered = compactor.view(nodes)
                out.summary_batches += 1
                out.summary_after.append(int(promo.after_tokens * scale))
        after = overhead + estimate_tokens_of_messages(rendered)
        out.prompts.append(after * scale)
        if promotions:
            out.batches += 1
            out.post_batch.append(after * scale)
            out.sweeps += any(p.reason == "sweep" for p in promotions)
            first = promotions[0].first_changed_message or 0
            out.rewrite_tail += estimate_tokens_of_messages(rendered[first:]) * scale
        p_cold = update_p_cold(p_cold, call.hit_rate)
        previous = call
        # The next boundary represents a finished turn. A zero-latency stub
        # is ready for the next eligible call; this is not provider latency.
        next_index = len(out.prompts)
        ended = next_index == len(s.calls) or s.calls[next_index].boundary
        if ended and pending is None and summary_tokens:
            region = compactor.summary_region(nodes, context_window=window, scale=scale, due_only=True)
            if region:
                pending = region[0], region[1], stub
    if s.calls:
        nodes = s.nodes  # include the last recorded assistant reply
        overhead = int(s.calls[-1].overhead / scale)
        out.manual_before = int(compactor.estimate_view_tokens(nodes, overhead) * scale)
        compactor.compact_now(nodes, overhead_tokens=overhead, context_window=window,
                              output_reserve_tokens=reserve, scale=scale)
        out.manual_ladder = int(compactor.estimate_view_tokens(nodes, overhead) * scale)
        region = compactor.summary_region(nodes, context_window=window, scale=scale)
        if region and summary_tokens:
            compactor.apply_summary(nodes, (region[0], region[1], stub), overhead_tokens=overhead,
                                    decision_reason="manual")
        out.manual_after = int(compactor.estimate_view_tokens(nodes, overhead) * scale)
    out.seconds = time.monotonic() - started
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("events", type=Path)
    parser.add_argument("--window", type=int, default=272_000)
    parser.add_argument("--reserve", type=int, default=32_000)
    parser.add_argument("--modes", default="off,on")
    parser.add_argument("--summary-tokens", type=int, default=0,
                        help="Enable tier 4 with a fixed-size stub; no model calls")
    args = parser.parse_args(argv)
    s = load(args.events)
    scale = calibrate(s)
    billed = [c.billed for c in s.calls]
    print(f"{args.events}: {len(s.calls)} calls, {len(s.nodes)} nodes, "
          f"window {args.window}, calibration {scale:.3f}")
    if billed:
        print(f"  recorded   sum={sum(billed) / 1e6:6.2f}M "
              f"peak={100 * max(billed) / args.window:5.1f}%")
    for mode in args.modes.split(","):
        r = replay(s, mode.strip(), window=args.window, reserve=args.reserve,
                   scale=scale, summary_tokens=args.summary_tokens)
        print(f"  checkpoint={r.summary_batches} post_batch_max={max(r.post_batch, default=0):.0f} "
              f"post_batch_last={r.post_batch[-1] if r.post_batch else 0:.0f} "
              f"manual={r.manual_before}->{r.manual_ladder}->{r.manual_after} "
              f"summary_after={r.summary_after}")
        over = sum(1 for p in r.prompts if p > 0.8 * args.window)
        print(f"  sweep {r.mode:6s} sum={sum(r.prompts) / 1e6:6.2f}M "
              f"peak={100 * max(r.prompts, default=0) / args.window:5.1f}% "
              f">80%={over:3d} batches={r.batches:3d} sweeps={r.sweeps:3d} "
              f"rewrite_tail={r.rewrite_tail / 1e6:5.2f}M ({r.seconds:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
