"""Early fetch of a reply's reads and searches (NativeLoop._prefetch).

Only a tool's ``ToolSpec.fetch`` runs early; each call still settles in call
order on the serial path. Overlap is shown by rendezvous, never by timing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fluid.engine.native import NativeLoop
from fluid.events import (
    Interrupt,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    UserTurn,
)
from fluid.hooks.bus import POST_TOOL_USE, HookBus
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import read as read_tool
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec, fetch_once

_HANG = 10  # only to turn a hang into a failure


def _reply(*calls: tuple[str, str, dict]) -> list[StreamChunk]:
    chunks = [StreamChunk("message_start", {"usage": {"input_tokens": 10}})]
    for i, (tuid, name, args) in enumerate(calls):
        chunks += [
            StreamChunk("block_start", {"index": i, "block_type": "tool_use",
                                        "tool_name": name, "tool_use_id": tuid}),
            StreamChunk("tool_args_delta", {"index": i, "partial_json": json.dumps(args)}),
            StreamChunk("block_stop", {"index": i}),
        ]
    stop = "tool_use" if calls else "end_turn"
    return chunks + [StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": stop}),
                     StreamChunk("message_stop", {})]


class _Provider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, *replies: list[StreamChunk]) -> None:
        self.replies, self.requests = list(replies) + [_reply()], []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.replies[min(len(self.requests), len(self.replies)) - 1]:
            yield chunk


class _Probes:
    """``Probe``: a fetchable read. Input ``id``; ``meet`` waits until that
    call's fetch has started, ``hold`` never returns, ``boom`` raises."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.started: dict[str, asyncio.Event] = {}
        self.runs = 0

    def ev(self, call: str) -> asyncio.Event:
        return self.started.setdefault(call, asyncio.Event())

    async def fetch(self, input: dict[str, Any], ctx: Any) -> ToolOutcome:
        call = input["id"]
        self.runs += 1
        run = self.runs
        self.log.append(f"start {call}")
        self.ev(call).set()
        try:
            if input.get("meet"):
                await asyncio.wait_for(self.ev(input["meet"]).wait(), _HANG)
            if input.get("hold"):
                await asyncio.Event().wait()
            if input.get("boom"):
                self.log.append(f"raise {call}")
                raise RuntimeError(f"boom {call}")
            self.log.append(f"end {call}")
            return ToolOutcome(text=f"probe {call} run{run}")
        except asyncio.CancelledError:
            self.log.append(f"cancelled {call}")
            raise

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        registry.register_builtin(
            ToolSpec(name="Probe", description="read", risk="safe", fetch=self.fetch,
                     input_schema={"type": "object", "properties": {}}),
            lambda input, ctx: fetch_once(self.fetch, input, ctx),
        )
        return registry


def _loop(provider, registry, limit, policy="never_ask", tmp_path=None, store=None,
          sid=None, hooks=None) -> NativeLoop:
    loop = NativeLoop(provider=provider, store=store or MemoryStore(), session_id=sid,
                      model="claude-sonnet-5", registry=registry,
                      permissions=PermissionEngine(policy), max_concurrent_tools=limit,
                      hooks=hooks)
    if tmp_path is not None:
        loop.ctx.cwd = str(tmp_path)
    return loop


async def _turn(loop: NativeLoop) -> None:
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), _HANG)


def _results(loop: NativeLoop) -> list[ToolResult]:
    return [e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult)]


async def _until(cond) -> None:
    async def spin() -> None:
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(spin(), _HANG)


@pytest.mark.parametrize("limit", [1, 2])
async def test_two_fetches_overlap_only_above_limit_one(limit) -> None:
    probes = _Probes()
    meet = {"meet": "b"} if limit == 2 else {}  # would hang at limit 1
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", **meet}),
                                  ("b", "Probe", {"id": "b"}))),
                 probes.register(ToolRegistry()), limit)
    await _turn(loop)
    if limit == 2:  # a was still waiting for b's fetch: they overlapped
        assert probes.log.index("start b") < probes.log.index("end a")
    else:
        assert probes.log == ["start a", "end a", "start b", "end b"]
    assert [(r.tool_use_id, r.output) for r in _results(loop)] == [
        ("a", "probe a run1"), ("b", "probe b run2")]


async def _mixed(tmp_path, limit: int) -> tuple[list, list]:
    (tmp_path / "f.txt").write_text("hello\nworld\n")
    registry = ToolRegistry()
    register_builtins(registry)
    provider = _Provider(_reply(
        ("r", "Read", {"file_path": "f.txt"}), ("g", "Grep", {"pattern": "wor"}),
        ("b", "Bash", {"command": "echo hi"}), ("l", "Glob", {"pattern": "*.txt"}),
    ))
    loop = _loop(provider, registry, limit, tmp_path=tmp_path)
    await _turn(loop)
    stored = await loop.store.events_since(loop.session_id, 0)
    shape = [(type(e).__name__, getattr(e, "tool_use_id", None), getattr(e, "output", None),
              getattr(e, "is_error", None)) for e in stored]
    return shape, [m.content for m in provider.requests[1].messages]


async def test_mixed_reply_events_and_transcript_match_serial(tmp_path) -> None:
    serial = await _mixed(tmp_path, 1)
    early = await _mixed(tmp_path, 4)
    assert json.dumps(early, default=str) == json.dumps(serial, default=str)
    assert [s[1] for s in serial[0] if s[0] == "ToolResult"] == ["r", "g", "b", "l"]


@pytest.mark.parametrize("how", ["interrupt", "close"])
async def test_exit_mid_reply_discards_early_fetches(how) -> None:
    async def run(limit: int) -> tuple[list, _Probes, NativeLoop]:
        probes = _Probes()
        loop = _loop(_Provider(_reply(*[(c, "Probe", {"id": c, "hold": True})
                                        for c in "abc"])),
                     probes.register(ToolRegistry()), limit)
        await loop.submit(UserTurn(content="go"))
        await asyncio.wait_for(probes.ev("a").wait(), _HANG)
        if limit > 1:
            await asyncio.wait_for(probes.ev("c").wait(), _HANG)
        if how == "interrupt":
            await loop.submit(Interrupt())
            await asyncio.wait_for(loop.wait_turn(), _HANG)
        else:
            await asyncio.wait_for(loop.aclose(), _HANG)
        events = [(type(e).__name__, getattr(e, "tool_use_id", None),
                   getattr(e, "output", None)) for e in loop.bus.log(loop.session_id)]
        return events, probes, loop

    serial, _, _ = await run(1)
    events, probes, loop = await run(3)
    assert events == serial
    # b and c were fetched early, then cancelled and joined with the turn.
    assert sorted(probes.log) == ["cancelled a", "cancelled b", "cancelled c",
                                  "start a", "start b", "start c"]
    assert loop._prefetches == {}


async def test_call_the_gate_prompts_for_does_not_use_its_early_fetch() -> None:
    """The third identical call trips the doom-loop prompt, which the dry
    check cannot foresee: after approval its fetch runs again, on base."""
    probes, store = _Probes(), MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(_Provider(_reply(*[(c, "Probe", {"id": "same"}) for c in ("c1", "c2", "c3")])),
                 probes.register(ToolRegistry()), 4, policy="on_write", store=store, sid=sid)

    async def approve() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                await loop.submit(RespondPermission(request_id=event.request_id,
                                                    decision="allow"))
                return

    watcher = asyncio.create_task(approve())
    await _turn(loop)
    await asyncio.wait_for(watcher, _HANG)
    assert [r.output for r in _results(loop)] == [
        "probe same run1", "probe same run2", "probe same run4"]


async def test_failing_fetch_surfaces_at_its_own_position() -> None:
    probes = _Probes()
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", "meet": "b"}),
                                  ("b", "Probe", {"id": "b", "boom": True}),
                                  ("c", "Probe", {"id": "c"}))),
                 probes.register(ToolRegistry()), 3)
    await _turn(loop)
    assert probes.log.index("raise b") < probes.log.index("end a")  # raised early
    results = _results(loop)
    assert [(r.tool_use_id, r.is_error) for r in results] == [
        ("a", False), ("b", True), ("c", False)]
    assert "boom b" in results[1].output


async def test_discarded_read_fetch_leaves_the_read_ledger_unchanged(tmp_path, monkeypatch) -> None:
    (tmp_path / "f.txt").write_text("hello\n")
    loaded: list[str] = []
    real_load = read_tool._load
    monkeypatch.setattr(read_tool, "_load", lambda i, c: (loaded.append(i["file_path"]),
                                                          real_load(i, c))[1])
    probes = _Probes()
    registry = probes.register(ToolRegistry())
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", "hold": True}),
                                  ("r", "Read", {"file_path": "f.txt"}))),
                 registry, 4, tmp_path=tmp_path)
    await loop.submit(UserTurn(content="go"))
    await _until(lambda: loaded and loop._prefetches["r"][1].done())
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), _HANG)
    assert not loop.ctx.read_files and not loop.ctx.read_seen and not loop.ctx.read_windows
    edit = await registry.dispatch("Edit", {"file_path": "f.txt", "old_string": "hello",
                                            "new_string": "bye"}, loop.ctx)
    assert edit.is_error and (tmp_path / "f.txt").read_text() == "hello\n"


@pytest.mark.parametrize("tool", ["Probe", "Other"])
async def test_a_hook_matching_the_tool_keeps_it_serial(tool) -> None:
    """A PostToolUse hook whose matcher covers Probe makes it ineligible
    (HookBus.may_act); one scoped to another tool does not."""
    async def hook(event, payload):
        return None

    hook.matches = lambda name: name == tool  # as CommandHook exposes it
    bus = HookBus()
    bus.on(POST_TOOL_USE, hook)
    probes = _Probes()
    meet = {"meet": "b"} if tool == "Other" else {}
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", **meet}),
                                  ("b", "Probe", {"id": "b"}))),
                 probes.register(ToolRegistry()), 4, hooks=bus)
    await _turn(loop)
    serial = ["start a", "end a", "start b", "end b"]
    assert (probes.log == serial) == (tool == "Probe")
