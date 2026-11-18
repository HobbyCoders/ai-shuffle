"""Early fetch of a reply's reads and searches (NativeLoop._prefetch).

Only a handler's registered fetch runs early, and it writes nothing; each call
still settles in call order on the serial path. Overlap is shown by rendezvous, never by timing.
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
from fluid.tools.registry import STOPPED, ToolOutcome, ToolRegistry, ToolSpec, fetch_once

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
        self.on_cancel: dict[str, Any] = {}  # call id -> coroutine function

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
            if call in self.on_cancel:
                await self.on_cancel[call]()
            raise

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        registry.register_builtin(
            ToolSpec(name="Probe", description="read", risk="safe",
                     input_schema={"type": "object", "properties": {}}),
            lambda input, ctx: fetch_once(self.fetch, input, ctx), fetch=self.fetch,
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


async def _approving(loop: NativeLoop, sid: str, decision: str = "allow") -> list[str]:
    """Run the turn, answering each permission prompt; the prompted call ids."""
    asked: list[str] = []

    async def approve() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                asked.append(event.tool_use_id)
                await loop.submit(RespondPermission(request_id=event.request_id,
                                                    decision=decision))

    watcher = asyncio.create_task(approve())
    try:
        await _turn(loop)
    finally:
        watcher.cancel()
    return asked


async def test_call_the_gate_prompts_for_does_not_use_its_early_fetch() -> None:
    """The policy turns to always-ask after b's fetch started (a mode switch
    mid-reply): b's gate prompts, so its early result is dropped and, after
    approval, the fetch runs again on the base path."""
    probes, store = _Probes(), MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", "meet": "go"}),
                                  ("b", "Probe", {"id": "b"}))),
                 probes.register(ToolRegistry()), 4, store=store, sid=sid)

    async def switch() -> None:
        await asyncio.wait_for(probes.ev("b").wait(), _HANG)
        loop.permissions.approval_policy = "always_ask"
        probes.ev("go").set()

    switcher = asyncio.create_task(switch())
    assert await _approving(loop, sid) == ["b"]
    await switcher
    assert [r.output for r in _results(loop)] == ["probe a run1", "probe b run3"]


async def test_repeated_calls_are_not_fetched_early() -> None:
    """The repeat guard asks on the third identical call; a call it would ask
    for is never fetched before its gate (P4): c3 runs once, after approval."""
    probes, store = _Probes(), MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(_Provider(_reply(*[(c, "Probe", {"id": "same"}) for c in ("c1", "c2", "c3")])),
                 probes.register(ToolRegistry()), 4, policy="on_write", store=store, sid=sid)
    assert await _approving(loop, sid) == ["c3"]
    assert probes.runs == 3
    assert [r.output for r in _results(loop)][2] == "probe same run3"


def _guarded_reads(tmp_path, monkeypatch, threshold: int, *replies) -> tuple[NativeLoop, list]:
    """A never-ask loop at limit 4 whose repeat guard trips at ``threshold``
    (under never_ask it denies), and the files Read opens."""
    for f in "ab":
        (tmp_path / f"{f}.txt").write_text(f"{f}\n")
    loaded = _spy_load(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(*[_reply(*[(f"{n}{i}", "Read", {"file_path": f"{f}.txt"})
                                      for i, f in enumerate(files)])
                             for n, files in enumerate(replies)]),
                 registry, 4, tmp_path=tmp_path)
    loop.permissions = PermissionEngine("never_ask", doom_loop_threshold=threshold)
    return loop, loaded


async def test_threshold_one_guard_fetches_nothing_early(tmp_path, monkeypatch) -> None:
    """With ``doom_loop_threshold=1`` every call trips the guard, so no file
    is opened before (or after) the gate denies it."""
    loop, loaded = _guarded_reads(tmp_path, monkeypatch, 1, "ab")
    await _turn(loop)
    assert loaded == []
    assert all(r.is_error for r in _results(loop))


async def test_guard_streak_from_an_earlier_reply_counts(tmp_path, monkeypatch) -> None:
    """Reply 1 reads a.txt; reply 2 opens with the same Read, the guard's
    second in a row at threshold 2: it is denied, never fetched early."""
    loop, loaded = _guarded_reads(tmp_path, monkeypatch, 2, "a", "ab")
    await _turn(loop)
    assert loaded == ["a.txt", "b.txt"]
    assert [r.is_error for r in _results(loop)] == [False, True, False]


async def test_would_guard_flags_every_repeat_and_changes_nothing() -> None:
    """Conservative by construction: a call that repeats the guard's last call
    or any call in ``before`` is flagged, whatever lies between; so is every
    call at threshold 1. Whenever ``note_execution`` trips, ``would_guard``
    said so first, and asking never moves the streak."""
    engine = PermissionEngine("on_write", doom_loop_threshold=2)
    calls = [("Read", {"p": 1}), ("Grep", {"q": 1}), ("Read", {"p": 1}), ("Read", {"p": 1})]
    for n, (name, input) in enumerate(calls):
        flagged = engine.would_guard(name, input)
        assert engine.would_guard(name, input) == flagged  # asking changes nothing
        assert flagged == (n >= 3), n  # only the call right after its twin repeats the last
        assert engine.note_execution(name, input) <= flagged, n
    fresh = PermissionEngine("on_write", doom_loop_threshold=2)
    assert fresh.would_guard("Read", {"p": 1}, before=[("Read", {"p": 1}), ("Grep", {"q": 1})])
    assert not fresh.would_guard("Read", {"p": 2}, before=[("Read", {"p": 1})])
    assert PermissionEngine("on_write", doom_loop_threshold=1).would_guard("Read", {"p": 9})


async def test_failing_fetch_surfaces_at_its_own_position() -> None:
    """b's early fetch raised; its exception is not reused, b fetches again
    at its turn, as at limit 1, and only that result is reported."""
    probes = _Probes()
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", "meet": "b"}),
                                  ("b", "Probe", {"id": "b", "boom": True}),
                                  ("c", "Probe", {"id": "c"}))),
                 probes.register(ToolRegistry()), 3)
    await _turn(loop)
    assert probes.log.index("raise b") < probes.log.index("end a")  # raised early
    assert probes.log.count("raise b") == 2  # and again at its turn
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


def _spy_load(monkeypatch) -> list[str]:
    """Record each file Read's ``_load`` opens (inline or early)."""
    loaded: list[str] = []
    real = read_tool._load
    monkeypatch.setattr(read_tool, "_load", lambda i, c: (loaded.append(i["file_path"]),
                                                          real(i, c))[1])
    return loaded


async def test_a_replacement_handler_does_not_inherit_the_fetch(tmp_path, monkeypatch) -> None:
    """The app's Chat lane swaps Read's handler for an upload broker exactly
    like this (``register_builtin(replace(spec, ...), broker)``): the broker's
    own checks must run before any file is opened (P1)."""
    from dataclasses import replace

    (tmp_path / "f.txt").write_text("hello\n")
    loaded = _spy_load(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)

    async def broker(input, ctx):
        return ToolOutcome(text="refused: not an upload", is_error=True)

    registry.register_builtin(replace(read_tool.SPEC, name="Read"), broker)
    assert registry.fetch_of("Read") is None and registry.fetch_of("Grep") is not None
    loop = _loop(_Provider(_reply(("r1", "Read", {"file_path": "f.txt"}),
                                  ("r2", "Read", {"file_path": "f.txt", "offset": 1}))),
                 registry, 4, tmp_path=tmp_path)
    await _turn(loop)
    assert loaded == []
    assert [r.output for r in _results(loop)] == ["refused: not an upload"] * 2


async def test_look_ahead_authorizes_the_raw_input_the_gate_sees(tmp_path, monkeypatch) -> None:
    """A deny rule on ``offset: "01"`` matches the raw call but not its
    coerced ``1``: the call is denied and its file never opened (P4)."""
    from fluid.permissions.rules import Rule

    (tmp_path / "f.txt").write_text("hello\n")
    loaded = _spy_load(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("r", "Read", {"file_path": "f.txt", "offset": "01"}))),
                 registry, 4, policy="on_write", tmp_path=tmp_path)
    loop.permissions.rules = [Rule(tool="Read", decision="deny", input_globs={"offset": "01"})]
    await _turn(loop)
    assert loaded == []
    assert _results(loop)[0].is_error


def _glob_world(tmp_path):
    """A tree whose full Glob listing is over the spill cap, and the loop's
    state root, so spills land under ``tmp_path / "state"``."""
    import os

    tree = tmp_path / "tree"
    if not tree.exists():
        tree.mkdir()
        for n in range(400):
            (tree / f"{'x' * 60}-{n:04}.txt").write_text("")
            os.utime(tree / f"{'x' * 60}-{n:04}.txt", (1e9, 1e9))  # ties sort by path
    return tree, tmp_path / "state"


def _with_state(loop: NativeLoop, state) -> NativeLoop:
    loop.ctx.extra["harness_state_root"] = str(state)
    return loop


async def test_a_discarded_glob_writes_no_spill(tmp_path) -> None:
    """The oversized Glob's fetch has finished (its worker thread included)
    when the interrupt drops it: no spill file, no counter (P3)."""
    tree, state = _glob_world(tmp_path)
    probes, store = _Probes(), MemoryStore()
    sid = (await store.create_session()).session_id
    registry = probes.register(ToolRegistry())
    register_builtins(registry)
    loop = _with_state(_loop(_Provider(_reply(
        ("a", "Probe", {"id": "a", "hold": True}),
        ("l", "Glob", {"pattern": "*.txt", "path": str(tree), "limit": 1000}),
    )), registry, 4, store=store, sid=sid), state)
    await loop.submit(UserTurn(content="go"))
    await _until(lambda: "l" in loop._prefetches and loop._prefetches["l"][1].done())
    assert "saved to" in loop._prefetches["l"][1].result()[0][1].text  # it was oversized
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), _HANG)
    assert not state.exists() or not any(p.is_file() for p in state.rglob("*"))


async def test_an_early_glob_writes_its_spill_at_its_turn(tmp_path, monkeypatch) -> None:
    """Used, the early Glob's result, spill file and counter are main's, and
    each listing ran once: the early results were consumed, not refetched."""
    from fluid.tools.builtins import glob as glob_tool
    from fluid.tools.registry import deferred_writes

    early_flags: list[bool] = []
    real = glob_tool._glob
    monkeypatch.setattr(glob_tool, "_glob", lambda *a: (
        early_flags.append(deferred_writes.get() is not None), real(*a))[1])

    async def run(limit: int) -> tuple:
        tree, _ = _glob_world(tmp_path)
        state = tmp_path / f"state{limit}"
        store = MemoryStore()
        sid = (await store.create_session()).session_id
        registry = ToolRegistry()
        register_builtins(registry)
        loop = _with_state(_loop(_Provider(_reply(
            ("g", "Glob", {"pattern": "*-0000.txt", "path": str(tree)}),
            ("l", "Glob", {"pattern": "*.txt", "path": str(tree), "limit": 1000}),
        )), registry, limit, store=store, sid=sid), state)
        early_flags.clear()
        await _turn(loop)
        out = _results(loop)[1].output.replace(str(state), "<state>").replace(sid, "<sid>")
        stats = [json.loads(p.read_text())["spilled"] for p in state.rglob("spill-stats/*")]
        files = sorted(p.name for p in state.rglob("tool-output/*"))
        return (out, files, stats), list(early_flags)

    (serial, serial_runs), (early, early_runs) = await run(1), await run(4)
    assert early == serial and "saved to" in early[0] and early[1]
    assert early[2] == [{"Glob": 1}]
    assert serial_runs == [False, False] and early_runs == [True, True]


async def test_a_read_of_the_spill_stats_sees_the_new_count(tmp_path) -> None:
    """An oversized Glob, then a Read of the session's spill-stats file: at
    limit 4 the Read sees the count the Glob just wrote, as at limit 1 (P3)."""
    from fluid.tools.spill import _stats_path

    async def run(limit: int) -> str:
        tree, _ = _glob_world(tmp_path)
        state = tmp_path / f"stats{limit}"
        store = MemoryStore()
        sid = (await store.create_session()).session_id
        registry = ToolRegistry()
        register_builtins(registry)
        loop = _with_state(_loop(_Provider(), registry, limit, store=store, sid=sid), state)
        loop.ctx.session_id = sid  # as the turn sets it
        stats = str(_stats_path(loop.ctx))
        loop.provider.replies.insert(0, _reply(
            ("l", "Glob", {"pattern": "*.txt", "path": str(tree), "limit": 1000}),
            ("r", "Read", {"file_path": stats}),
        ))
        await _turn(loop)
        return _results(loop)[1].output

    serial, early = await run(1), await run(4)
    assert '"Glob": 1' in serial and '"Glob": 1' in early


async def test_no_fetch_starts_when_the_interrupt_lands_during_cleanup() -> None:
    """``a, d, e`` at limit 2: d's gate denies it after its fetch started (a
    rule added mid-reply), so its fetch is stale when the loop moves to e, and
    the interrupt arrives while that fetch is being cancelled. e's fetch is
    never scheduled and e gets main's cancellation result (P1, round 2)."""
    from fluid.permissions.rules import Rule

    probes, store = _Probes(), MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(_Provider(_reply(("a", "Probe", {"id": "a", "meet": "go"}),
                                  ("d", "Probe", {"id": "d", "hold": True}),
                                  ("e", "Probe", {"id": "e"}))),
                 probes.register(ToolRegistry()), 2, policy="on_write", store=store, sid=sid)

    async def interrupt() -> None:
        await loop.submit(Interrupt())

    probes.on_cancel["d"] = interrupt
    scheduled: list[str] = []

    class _Recording(dict):
        def __setitem__(self, key, value) -> None:
            scheduled.append(key)
            super().__setitem__(key, value)

    loop._prefetches = _Recording()

    async def deny_d() -> None:
        await asyncio.wait_for(probes.ev("d").wait(), _HANG)
        loop.permissions.rules = [Rule(tool="Probe", decision="deny", input_globs={"id": "d"})]
        probes.ev("go").set()

    denier = asyncio.create_task(deny_d())
    await _turn(loop)
    await denier
    assert scheduled == ["a", "d"] and "cancelled d" in probes.log
    assert "start e" not in probes.log
    assert [(r.tool_use_id, r.is_error) for r in _results(loop)] == [
        ("a", False), ("d", True), ("e", True)]
    assert "interrupted" in _results(loop)[2].output


async def test_no_fetch_starts_after_an_interrupt(tmp_path, monkeypatch) -> None:
    """``Read, Bash, Read, Read``, interrupted during the first Read: the
    trailing Reads get main's cancellation results and are never opened (P5)."""
    import threading

    for f in "abc":
        (tmp_path / f"{f}.txt").write_text(f"{f}\n")
    release, loaded = threading.Event(), []
    real = read_tool._load

    def slow(i, c):
        loaded.append(i["file_path"])
        if i["file_path"] == "a.txt":
            release.wait(_HANG)
        return real(i, c)

    monkeypatch.setattr(read_tool, "_load", slow)

    async def run(limit: int) -> list:
        loaded.clear()
        release.clear()
        registry = ToolRegistry()
        register_builtins(registry)
        loop = _loop(_Provider(_reply(
            ("r1", "Read", {"file_path": "a.txt"}), ("b", "Bash", {"command": "echo hi"}),
            ("r2", "Read", {"file_path": "b.txt"}), ("r3", "Read", {"file_path": "c.txt"}),
        )), registry, limit, tmp_path=tmp_path)
        await loop.submit(UserTurn(content="go"))
        if limit == 1:  # the inline Read holds the event loop until released
            release.set()
        else:
            await _until(lambda: loaded == ["a.txt"])
        await loop.submit(Interrupt())
        release.set()
        await asyncio.wait_for(loop.wait_turn(), _HANG)
        return [(r.tool_use_id, r.output) for r in _results(loop)]

    early = await run(4)
    assert loaded == ["a.txt"]
    assert [o for _, o in early] == ["Tool execution cancelled: the turn was interrupted."] * 4


_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJ"
        "RU5ErkJggg==")


async def _steer_lands_on(tmp_path, monkeypatch, limit: int, during: str) -> list[str]:
    """Which results carry a steer queued while the image Read ``during``
    its load or its conditioning (``Read i.png, Read t.txt``)."""
    import base64

    import fluid.engine.native as native
    from fluid.events.ops import Steer

    (tmp_path / "i.png").write_bytes(base64.b64decode(_PNG))
    (tmp_path / "t.txt").write_text("t\n")
    aloop, holder = asyncio.get_running_loop(), {}

    def queue_steer_soon(real):
        def wrapped(*args):
            if not holder.get("sent"):
                holder["sent"] = True
                aloop.call_soon_threadsafe(lambda: asyncio.ensure_future(
                    holder["loop"].submit(Steer(text="steer-q1", id="q1"))))
            return real(*args)
        return wrapped

    if during == "load":
        monkeypatch.setattr(read_tool, "_read_image", queue_steer_soon(read_tool._read_image))
    else:
        monkeypatch.setattr(native, "condition_image", queue_steer_soon(native.condition_image))
    registry = ToolRegistry()
    register_builtins(registry)
    kwargs = {} if limit == 1 else {"max_concurrent_tools": limit}
    loop = NativeLoop(provider=_Provider(_reply(("i", "Read", {"file_path": "i.png"}),
                                                ("t", "Read", {"file_path": "t.txt"}))),
                      store=MemoryStore(), model="claude-sonnet-5", registry=registry,
                      permissions=PermissionEngine("never_ask"), **kwargs)
    loop.ctx.cwd = str(tmp_path)
    holder["loop"] = loop
    await _turn(loop)
    return [r.tool_use_id for r in _results(loop) if "steer-q1" in r.output]


@pytest.mark.parametrize("during", ["load", "conditioning"])
async def test_steer_placement_around_an_image_read(tmp_path, monkeypatch, during) -> None:
    """At limit 1 the Read is main's inline path, so a steer queued during the
    image load or its conditioning rides the next result, as on main (P6).
    Fetched early, the load runs before the call's turn, so a steer queued
    then rides the image result itself; conditioning is unchanged."""
    assert await _steer_lands_on(tmp_path, monkeypatch, 1, during) == ["t"]
    early = await _steer_lands_on(tmp_path, monkeypatch, 4, during)
    assert early == (["i"] if during == "load" else ["t"])


@pytest.mark.parametrize(("delegated", "limit", "claimed"), [
    (False, 1, False), (False, 4, True), (True, 4, False),
])
def test_prompt_claims_early_fetch_only_when_the_native_loop_does_it(delegated, limit, claimed):
    from fluid.cli.prompt import PromptProfile, compose_base_prompt

    prompt = compose_base_prompt(PromptProfile(
        tools=frozenset({"Read", "Grep", "Glob", "Bash"}), model="claude",
        delegated=delegated, concurrent_tools=limit > 1))
    assert "Make independent tool calls in the same reply" in prompt
    assert ("Reads and searches (Read, Grep, Glob) among them may be fetched concurrently"
            in prompt) == claimed


async def test_a_fetch_task_that_starts_after_the_interrupt_does_not_fetch() -> None:
    """Scheduled, then the turn is interrupted before the task first runs:
    the task returns without fetching (P1, round 2)."""
    probes = _Probes()
    loop = _loop(_Provider(), probes.register(ToolRegistry()), 2)
    calls = [{"tool_use_id": "p", "name": "Probe", "input": {"id": "p"}}]
    await loop._prefetch(calls, 0)
    task = loop._prefetches["p"][1]
    loop._interrupted.set()
    assert await asyncio.wait_for(task, _HANG) is STOPPED
    assert probes.log == []
    loop._interrupted.clear()
    await loop._prefetch([], 0)


@pytest.mark.parametrize("limit", [1, 4])
async def test_grep_and_glob_resolve_their_root_once(tmp_path, monkeypatch, limit) -> None:
    """The fetch hands its resolved arguments to the bookkeeping, so a
    relative root is resolved once per call, as on main (P2, round 2)."""
    from fluid.tools.builtins import glob as glob_tool
    from fluid.tools.builtins import grep as grep_tool

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.txt").write_text("needle\n")
    from fluid.tools.registry import deferred_writes

    resolved: list[tuple[str, bool]] = []  # (helper, inside an early fetch)
    for module, name in ((grep_tool, "_search_args"), (glob_tool, "_list_args")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda i, c, real=real, name=name: (
            resolved.append((name, deferred_writes.get() is not None)), real(i, c))[1])
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("g", "Grep", {"pattern": "needle", "path": "src"}),
                                  ("l", "Glob", {"pattern": "*.txt", "path": "src"}))),
                 registry, limit, tmp_path=tmp_path)
    await _turn(loop)
    assert sorted(resolved) == [("_list_args", limit > 1), ("_search_args", limit > 1)]
    assert [r.is_error for r in _results(loop)] == [False, False]


async def test_a_repeat_is_not_fetched_early_even_after_a_mode_flip(tmp_path, monkeypatch) -> None:
    """``Read a, Read b, Read a`` at threshold 2, a rule allowing only a.txt,
    and the policy switched to always_ask while the first Read is loading: b
    prompts (denied) and never reaches the guard, so the third Read trips it.
    It repeats an earlier call of the reply, so it was never fetched early:
    a.txt is opened once (P1, round 3)."""
    import threading

    from fluid.permissions.rules import Rule

    for f in "ab":
        (tmp_path / f"{f}.txt").write_text(f"{f}\n")
    release, loaded = threading.Event(), []
    real = read_tool._load

    def slow(i, c):
        loaded.append(i["file_path"])
        if loaded.count("a.txt") == 1 and i["file_path"] == "a.txt":
            release.wait(_HANG)
        return real(i, c)

    monkeypatch.setattr(read_tool, "_load", slow)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("r0", "Read", {"file_path": "a.txt"}),
                                  ("r1", "Read", {"file_path": "b.txt"}),
                                  ("r2", "Read", {"file_path": "a.txt"}))),
                 registry, 4, policy="on_write", tmp_path=tmp_path, store=store, sid=sid)
    loop.permissions = PermissionEngine(
        "on_write", [Rule(tool="Read", decision="allow", input_globs={"file_path": "a.txt"})],
        doom_loop_threshold=2)

    early_at_flip: list[str] = []

    async def flip() -> None:
        try:
            await _until(lambda: "a.txt" in loaded)
            early_at_flip.extend(loop._prefetches)
            loop.permissions.approval_policy = "always_ask"
        finally:
            release.set()

    flipper = asyncio.create_task(flip())
    asked = await _approving(loop, sid, decision="deny")
    await flipper
    assert "r1" in early_at_flip and "r2" not in early_at_flip  # r0 may be in its gate
    assert asked == ["r1", "r2"]
    assert loaded.count("a.txt") == 1
    assert [r.is_error for r in _results(loop)] == [False, True, True]


async def test_a_queued_worker_job_checks_the_interrupt_before_any_io(tmp_path, monkeypatch) -> None:
    """The only worker thread is held, so the Read fetch's job queues behind
    it; the interrupt lands (no cancel yet) before the thread frees up. The
    job checks at its start and opens nothing (P2, round 3)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    (tmp_path / "f.txt").write_text("f\n")
    loaded = _spy_load(monkeypatch)
    pool, held, queued = ThreadPoolExecutor(max_workers=1), threading.Event(), []
    aloop = asyncio.get_running_loop()

    def to_thread(fn, *args):
        queued.append(fn)
        return aloop.run_in_executor(pool, fn, *args)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    pool.submit(held.wait, _HANG)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(), registry, 2, tmp_path=tmp_path)
    try:
        await loop._prefetch([{"tool_use_id": "r", "name": "Read",
                               "input": {"file_path": "f.txt"}}], 0)
        task = loop._prefetches["r"][1]
        await _until(lambda: queued)  # the job waits for the held thread
        loop._interrupted.set()
        held.set()
        assert await asyncio.wait_for(task, _HANG) is STOPPED
        assert loaded == []
    finally:
        held.set()
        loop._interrupted.clear()
        await loop._prefetch([], 0)
        pool.shutdown(wait=True)


async def test_grep_reports_a_missing_pattern_before_a_bad_path() -> None:
    """Main's error precedence at limit 1: ``{"path": 123}`` without a
    pattern names the pattern (P4, round 3)."""
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("g", "Grep", {"path": 123}))), registry, 1)
    await _turn(loop)
    result = _results(loop)[0]
    assert result.is_error and "pattern" in result.output and "TypeError" not in result.output


def test_note_execution_reads_the_streak_after_the_threshold_callback() -> None:
    """As on main: the threshold callback runs first, then the streak is
    read; a callback that notes another tool resets the streak, so the second
    Read does not trip (P5, round 3)."""
    calls: list[str] = []

    def threshold_for(name: str):
        calls.append(name)
        if calls.count("Read") == 2 and name == "Read":
            engine.note_execution("Other", {})
        return None

    engine = PermissionEngine("on_write", doom_loop_threshold=2,
                              doom_loop_threshold_for=threshold_for)
    assert engine.note_execution("Read", {"p": 1}) is False
    assert engine.note_execution("Read", {"p": 1}) is False


def test_default_settings_keep_main_prompt_and_config(tmp_path) -> None:
    """At the default (``max_concurrent_tools`` 1) the prompt bullet and the
    serialized config are main's, byte for byte (round 3, item 7)."""
    from dataclasses import replace

    from fluid.cli.prompt import compose_base_prompt, profile_for
    from fluid.config.model import HarnessConfig

    config = HarnessConfig()
    main_bullet = ("- Make independent tool calls in the same reply so they run together; "
                   "serialize only when one call's input depends on another's result.")
    profile = replace(profile_for(config, guidance_home=tmp_path),
                      tools=frozenset({"Read", "Grep", "Glob", "Bash"}), code_mode=False)
    assert profile.concurrent_tools is False
    prompt = compose_base_prompt(profile)
    assert main_bullet in prompt and "fetched concurrently" not in prompt
    on = compose_base_prompt(replace(profile, concurrent_tools=True))
    assert main_bullet not in on and "fetched concurrently" in on
    assert "max_concurrent_tools" not in config.to_dict()
    assert HarnessConfig.from_dict({"max_concurrent_tools": 3}).to_dict()[
        "max_concurrent_tools"] == 3


async def test_a_read_through_a_link_chain_to_the_stats_sees_the_new_count(
    tmp_path, monkeypatch,
) -> None:
    """``alias.json -> stats/<session>.json -> prior.json``, after an oversized
    Glob: the Glob's count replaces the middle link, so at limit 1 the Read
    sees the new count. At 4 the Read was fetched early (prior.json), but the
    spill since then sends it back to load at its turn: the same result
    (P7/P11, round 4)."""
    from fluid.tools.registry import deferred_writes
    from fluid.tools.spill import _stats_path

    early_loads: list[bool] = []
    real = read_tool._load
    monkeypatch.setattr(read_tool, "_load", lambda i, c: (
        early_loads.append(deferred_writes.get() is not None), real(i, c))[1])

    async def run(limit: int) -> dict:
        tree, _ = _glob_world(tmp_path)
        state, work = tmp_path / f"chain{limit}", tmp_path / f"work{limit}"
        work.mkdir()
        store = MemoryStore()
        sid = (await store.create_session()).session_id
        registry = ToolRegistry()
        register_builtins(registry)
        loop = _with_state(_loop(_Provider(), registry, limit, store=store, sid=sid,
                                 tmp_path=work), state)
        loop.ctx.session_id = sid  # as the turn sets it
        stats = _stats_path(loop.ctx)
        stats.parent.mkdir(parents=True)
        (work / "prior.json").write_text('{"prior": true}')
        stats.symlink_to(work / "prior.json")
        (work / "alias.json").symlink_to(stats)
        loop.provider.replies.insert(0, _reply(
            ("l", "Glob", {"pattern": "*.txt", "path": str(tree), "limit": 1000}),
            ("r", "Read", {"file_path": "alias.json"}),
        ))
        early_loads.clear()
        await _turn(loop)
        seen = json.loads(_results(loop)[1].output.split("\t", 1)[1])
        return {k: v for k, v in seen.items() if k not in ("session_id", "updated_at")}

    serial = await run(1)
    assert early_loads == [False] and serial == {"prior": True, "spilled": {"Glob": 1}}
    early = await run(4)
    assert early_loads == [True, False]  # fetched early, then again at its turn
    assert early == serial


async def test_an_early_result_is_used_when_nothing_spilled_since(tmp_path, monkeypatch) -> None:
    """Two plain Reads at limit 4: each is loaded once, early, and that load
    is the one used (round 4)."""
    from fluid.tools.registry import deferred_writes

    for f in "ab":
        (tmp_path / f"{f}.txt").write_text(f"{f}\n")
    early_loads: list[tuple[str, bool]] = []
    real = read_tool._load
    monkeypatch.setattr(read_tool, "_load", lambda i, c: (
        early_loads.append((i["file_path"], deferred_writes.get() is not None)), real(i, c))[1])
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(_reply(("a", "Read", {"file_path": "a.txt"}),
                                  ("b", "Read", {"file_path": "b.txt"}))),
                 registry, 4, tmp_path=tmp_path)
    await _turn(loop)
    assert sorted(early_loads) == [("a.txt", True), ("b.txt", True)]
    assert [r.is_error for r in _results(loop)] == [False, False]


async def test_a_stopped_early_fetch_is_not_retried_inline(tmp_path, monkeypatch) -> None:
    """The reviewer's order: the Read's early fetch stopped for the interrupt
    and that completion reaches the handler before the interrupt watcher
    cancels dispatch. The handler raises ``FetchStopped`` instead of loading
    inline, and the loop answers it with main's interrupted result (P8,
    round 4)."""
    from fluid.tools.registry import FetchStopped, prefetched, spill_generation

    (tmp_path / "f.txt").write_text("f\n")
    loaded = _spy_load(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _loop(_Provider(), registry, 4, tmp_path=tmp_path)
    input = {"file_path": "f.txt"}

    async def stopped() -> Any:
        return STOPPED

    task = asyncio.ensure_future(stopped())
    await task
    token = prefetched.set([(input, task, spill_generation())])
    try:
        with pytest.raises(FetchStopped):
            await registry.dispatch("Read", input, loop.ctx)
    finally:
        prefetched.reset(token)
    assert loaded == []

    async def dispatch_stopped(sid, turn_id, tu):
        loop._interrupted.set()  # the flag is set; the watcher has not run yet
        raise FetchStopped

    monkeypatch.setattr(loop, "_dispatch_tool", dispatch_stopped)
    block = await loop._dispatch_tool_interruptible(
        "s", "t", {"tool_use_id": "r", "name": "Read", "input": input})
    assert block.is_error
    assert block.content[0].text == "Tool execution cancelled: the turn was interrupted."
    assert loaded == []


def test_would_guard_is_conservative_for_an_unmarked_threshold_callback() -> None:
    """A re-entrant callback (base 2; it notes the same Read inside and
    returns 3 there) trips the guard on a fresh call, so ``would_guard`` says
    True for any callback not marked pure; a marked one is ignored, as before
    (P9, round 4)."""
    from fluid.permissions.engine import PURE_THRESHOLD

    depth = {"n": 0}

    def reentrant(name: str):
        if depth["n"] == 0:
            depth["n"] += 1
            try:
                engine.note_execution(name, {"p": 1})
            finally:
                depth["n"] -= 1
            return None
        return 3

    engine = PermissionEngine("on_write", doom_loop_threshold=2,
                              doom_loop_threshold_for=reentrant)
    assert engine.would_guard("Read", {"p": 1}) is True
    assert engine.note_execution("Read", {"p": 1}) is True  # what it guards against

    def pure(name: str):
        return None

    setattr(pure, PURE_THRESHOLD, True)
    marked = PermissionEngine("on_write", doom_loop_threshold=2, doom_loop_threshold_for=pure)
    assert marked.would_guard("Read", {"p": 1}) is False


async def test_an_unmarked_threshold_callback_fetches_nothing_early(tmp_path) -> None:
    probes = _Probes()
    loop = _loop(_Provider(), probes.register(ToolRegistry()), 4)
    loop.permissions = PermissionEngine("never_ask", doom_loop_threshold_for=lambda n: None)
    await loop._prefetch([{"tool_use_id": "p", "name": "Probe", "input": {"id": "p"}}], 0)
    assert loop._prefetches == {}


async def test_an_early_globs_spill_commit_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The deferred spill write of an early oversized Glob is committed in a
    worker thread at its turn, as main writes it in the Glob worker: the
    event loop keeps running while the write is stalled (P10, round 4)."""
    import threading

    from fluid.tools import spill
    from fluid.tools.registry import deferred_writes

    entered, release, timed_out = threading.Event(), threading.Event(), []
    real = spill.write_spill

    def stalled(text, *, ctx, tool):
        if deferred_writes.get() is None:  # the commit, not the early naming
            entered.set()
            timed_out.append(not release.wait(2))
        return real(text, ctx=ctx, tool=tool)

    monkeypatch.setattr(spill, "write_spill", stalled)
    tree, state = _glob_world(tmp_path)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    register_builtins(registry)
    loop = _with_state(_loop(_Provider(_reply(
        ("l", "Glob", {"pattern": "*.txt", "path": str(tree), "limit": 1000}),
    )), registry, 4, store=store, sid=sid), state)
    ticks = {"n": 0}

    async def ticker() -> None:
        while not release.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.005)

    ticking = asyncio.create_task(ticker())
    await loop.submit(UserTurn(content="go"))
    await _until(entered.is_set)
    seen = ticks["n"]
    await _until(lambda: ticks["n"] >= seen + 3)  # the loop runs during the stall
    release.set()
    await asyncio.wait_for(loop.wait_turn(), _HANG)
    await ticking
    assert timed_out == [False]
    assert "saved to" in _results(loop)[0].output
    assert any(state.rglob("tool-output/*"))
