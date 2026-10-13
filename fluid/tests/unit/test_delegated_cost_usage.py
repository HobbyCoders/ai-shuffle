"""What the delegated leg bills per turn, and the HTTP status of a failed turn.

The CLI's ``result.total_cost_usd`` is the running total of its process (its
2.1.285 schema: read the latest result rather than summing), while the
result's ``usage`` and ``num_turns`` are per turn. Production session
42595afe reported 0.761, 2.405, 2.659 and 2.768 on its four turns and was
booked 8.593 for a real spend of 2.768. These tests drive the scripted fake
CLI (``fixtures/fake_claude_script.py``), whose ``result`` step ``cost`` is
that cumulative total.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.events import ErrorEvent, TextDelta, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.events.ops import Interrupt
from fluid.providers.pricing import estimate_cost
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
TURN_BUDGET = 20.0
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)
PRICED_MODEL = "claude-opus-5-5"
USAGE = {"input_tokens": 1200, "output_tokens": 800,
         "cache_read_input_tokens": 40000, "cache_creation_input_tokens": 2000}


class Scripted:
    """A DelegatedLoop wired to the scripted fake CLI."""

    def __init__(self, tmp_path: Path, script: dict, **loop_kwargs: Any) -> None:
        script_path = tmp_path / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")
        self.record_path = tmp_path / "record.jsonl"
        self.store = MemoryStore()
        self.loop = DelegatedLoop(
            store=self.store, model="claude-fake",
            claude_cmd=[sys.executable, str(FAKE)],
            env={"FAKE_CLAUDE_SCRIPT": str(script_path),
                 "FAKE_CLAUDE_RECORD": str(self.record_path)},
            **loop_kwargs,
        )
        self.sid: str | None = None

    def starts(self) -> list[dict]:
        if not self.record_path.exists():
            return []
        rows = [json.loads(line) for line in self.record_path.read_text().splitlines() if line]
        return [r for r in rows if r["kind"] == "start"]

    async def turn(
        self, text: str, *, on_event: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Any:
        """Run one user turn; its terminal event."""
        if self.sid is None:
            self.sid = (await self.store.create_session()).session_id
            self.loop._session_id = self.sid
        start = self.loop.bus.last_seq(self.sid)
        seen: list = []

        async def watch() -> None:
            async for event in self.loop.bus.subscribe(self.sid, since_seq=start):
                seen.append(event)
                if on_event is not None:
                    await on_event(event)
                if isinstance(event, _TERMINALS):
                    return

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        await self.loop.submit(UserTurn(content=text))
        await asyncio.wait_for(watcher, TURN_BUDGET)
        await asyncio.wait_for(self.loop.wait_turn(), TURN_BUDGET)
        return seen[-1]

    async def aclose(self) -> None:
        await self.loop.aclose()


def _turn(cost: float, *, usage: dict | None = None, **result: Any) -> list[dict]:
    return [
        {"op": "text", "text": "done", "model": PRICED_MODEL, "usage": usage or USAGE},
        {"op": "result", "cost": cost, "usage": usage or USAGE, **result},
    ]


# ─── The turn's cost is its share of the CLI's running total ───────────────


async def test_each_turn_bills_its_increase_of_the_running_total(tmp_path) -> None:
    run = Scripted(tmp_path, {"turns": [_turn(0.5), _turn(0.8)]})
    try:
        first = await run.turn("one")
        second = await run.turn("two")
    finally:
        await run.aclose()
    assert isinstance(first, TurnComplete) and isinstance(second, TurnComplete)
    assert first.cost_usd == pytest.approx(0.5)
    assert second.cost_usd == pytest.approx(0.3)
    assert second.usage["cli_total_cost_usd"] == pytest.approx(0.8)
    assert "cost_from_table" not in second.usage
    # The per-turn usage is untouched.
    assert second.usage["output_tokens"] == 800


async def test_an_interrupted_turn_bills_its_increase(tmp_path) -> None:
    interrupted = [
        {"op": "stream_text", "text": "Working on it", "chunks": 2},
        {"op": "wait_interrupt"},
        {"op": "result", "subtype": "error_during_execution", "cost": 0.9, "usage": USAGE},
    ]
    run = Scripted(tmp_path, {"turns": [_turn(0.5), interrupted]}, partial=True)

    async def stop_on_text(event: Any) -> None:
        if isinstance(event, TextDelta):
            await run.loop.submit(Interrupt())

    try:
        await run.turn("one")
        stopped = await run.turn("two", on_event=stop_on_text)
    finally:
        await run.aclose()
    assert isinstance(stopped, Interrupted)
    assert stopped.cost_usd == pytest.approx(0.4)


async def test_a_failed_turn_bills_its_increase(tmp_path) -> None:
    failed = _turn(1.0, subtype="error_during_execution", errors=["tool crashed"])
    run = Scripted(tmp_path, {"turns": [_turn(0.5), failed]})
    try:
        await run.turn("one")
        error = await run.turn("two")
    finally:
        await run.aclose()
    assert isinstance(error, ErrorEvent)
    assert error.cost_usd == pytest.approx(0.5)
    assert error.usage["cli_total_cost_usd"] == pytest.approx(1.0)


async def test_a_total_below_the_baseline_is_a_restarted_counter(tmp_path) -> None:
    run = Scripted(tmp_path, {"turns": [_turn(0.8), _turn(0.2)]})
    try:
        await run.turn("one")
        second = await run.turn("two")
    finally:
        await run.aclose()
    assert second.cost_usd == pytest.approx(0.2)


async def test_a_resumed_cli_prices_its_first_turn_from_the_table(tmp_path) -> None:
    """Whether --resume restores the saved total is unverified, so the first
    result of a resumed process is not trusted as a baseline."""
    run = Scripted(tmp_path, {"turns": [_turn(1.25), _turn(1.40)]}, cli_session_id="cli-prev")
    try:
        first = await run.turn("one")
        second = await run.turn("two")
    finally:
        await run.aclose()
    assert "--resume" in run.starts()[0]["argv"]
    expected = estimate_cost(PRICED_MODEL, USAGE)
    assert expected is not None and expected > 0
    assert first.cost_usd == pytest.approx(expected)
    assert first.usage["cost_from_table"] is True
    assert first.usage["cli_total_cost_usd"] == pytest.approx(1.25)
    assert second.cost_usd == pytest.approx(0.15)
    assert "cost_from_table" not in second.usage


async def test_a_rejected_resume_handle_retries_on_a_fresh_total(tmp_path) -> None:
    rejected = [{"op": "result", "subtype": "error_during_execution", "cost": 0.3,
                 "result": "No conversation found with session ID: cli-gone"}]
    run = Scripted(
        tmp_path, {"turns": [_turn(0.7)], "resume_turns": [rejected]},
        cli_session_id="cli-gone",
    )
    try:
        done = await run.turn("hello")
    finally:
        await run.aclose()
    assert len(run.starts()) == 2  # retried on a process without --resume
    assert isinstance(done, TurnComplete)
    assert done.cost_usd == pytest.approx(0.7)
    assert "cost_from_table" not in done.usage


# ─── A failed turn's HTTP status ────────────────────────────────────────────


def _failed_frame(**extra: Any) -> dict:
    frame = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
             "duration_ms": 5, "total_cost_usd": 0.0, "usage": {"input_tokens": 0}}
    frame.update(extra)
    return {"op": "frame", "frame": frame}


async def test_a_usage_limit_result_carries_429(tmp_path) -> None:
    turn = [_failed_frame(api_error_status=429,
                          result="Claude AI usage limit reached|1790812800")]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        error = await run.turn("hi")
    finally:
        await run.aclose()
    assert isinstance(error, ErrorEvent)
    assert error.status == 429
    assert "usage limit" in error.provider_message
    assert "|1790812800" not in error.provider_message
    assert "usage limit" in error.message


async def test_an_auth_failure_carries_401(tmp_path) -> None:
    turn = [_failed_frame(api_error_status=401, result="Invalid API key · Please run /login")]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        error = await run.turn("hi")
    finally:
        await run.aclose()
    assert error.status == 401
    assert "/login" in error.provider_message


async def test_an_authentication_failed_frame_reads_as_401(tmp_path) -> None:
    turn = [
        {"op": "frame", "frame": {"type": "assistant", "error": "authentication_failed",
                                  "message": {"id": "m1", "role": "assistant", "model": "x",
                                              "content": [{"type": "text",
                                                           "text": "Please run /login"}]}}},
        _failed_frame(result="Please run /login"),
    ]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        error = await run.turn("hi")
    finally:
        await run.aclose()
    assert error.status == 401


async def test_a_failure_without_a_status_keeps_none(tmp_path) -> None:
    turn = [_failed_frame(api_error_status=True, errors=["tool crashed"])]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        error = await run.turn("hi")
    finally:
        await run.aclose()
    assert isinstance(error, ErrorEvent)
    assert error.status is None
    assert error.provider_message == "tool crashed"


# ─── A read cut short still books the call it was holding ─────────────────


def _held_reader(tail: Callable[[], Awaitable[bytes]]):
    """A loop whose reader maps one assistant frame (its call's Usage held
    for the final counts), then gets ``tail()`` as the next stdout line."""
    from types import SimpleNamespace

    from fluid.engine.delegated_proto import StreamJsonMapper
    from fluid.events import Usage
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    store = MemoryStore()
    loop = DelegatedLoop(store=store, model="claude-fake", claude_cmd=[sys.executable, str(FAKE)])
    loop._proc = SimpleNamespace(stdout=object())
    emitted: list = []

    async def emit(event: Any) -> Any:
        emitted.append(event)
        return event

    async def no_snapshot(*_: Any, **__: Any) -> None:
        return None

    frame = {"type": "assistant", "session_id": "cli", "message": {
        "id": "msg_1", "role": "assistant", "model": PRICED_MODEL,
        "content": [{"type": "text", "text": "Working"}], "usage": USAGE,
    }}
    lines = [json.dumps(frame).encode()]

    async def next_line() -> bytes:
        if lines:
            return lines.pop(0)
        return await tail()

    loop._emit = emit  # type: ignore[method-assign]
    loop._ask_usage_snapshot = no_snapshot  # type: ignore[method-assign]
    loop._next_stdout_line = next_line  # type: ignore[method-assign]

    async def read() -> str | None:
        sid = (await store.create_session()).session_id
        user = SessionNode(node_id="n0", parent_id=None,
                           message=Message(role="user", content=[Text("hi")]))
        await store.append(sid, user)
        mapper = StreamJsonMapper(session_id=sid, turn_id="t1", model="claude-fake",
                                  user_node_id="n0")
        return await loop._read_until_result(sid, "t1", mapper, user)

    def usages() -> list:
        return [e for e in emitted if isinstance(e, Usage) and not e.context_usage]

    return read, usages


async def test_a_reader_that_raises_still_books_the_held_call() -> None:
    async def boom() -> bytes:
        raise RuntimeError("pipe broke")

    read, usages = _held_reader(boom)
    with pytest.raises(RuntimeError):
        await read()
    [usage] = usages()
    assert usage.input_tokens == USAGE["input_tokens"]
    assert usage.cache_read_tokens == USAGE["cache_read_input_tokens"]


async def test_a_cancelled_reader_still_books_the_held_call() -> None:
    reached = asyncio.Event()

    async def hang() -> bytes:
        reached.set()
        await asyncio.Event().wait()
        return b""

    read, usages = _held_reader(hang)
    task = asyncio.create_task(read())
    await asyncio.wait_for(reached.wait(), TURN_BUDGET)
    assert usages() == []  # still held for its final counts
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [usage] = usages()
    assert usage.cache_creation_tokens == USAGE["cache_creation_input_tokens"]
