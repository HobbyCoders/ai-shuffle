"""Fork and Rewind follow UserTurn's rule: refused while a turn is running.

The running loop chains each node it stores under the node it stored before,
not under the session head, and every append moves the head. A head move made
mid-turn was therefore overwritten by the turn's next append: the op returned
success and the fork was gone. The REPL reports the refusal and keeps going.

A head move whose record landed but could not be read back leaves the engine's
session meta unverified; the engine reads the session again before using it.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from fluid.cli.repl import Repl
from fluid.engine.base import SessionBusy
from fluid.events import Fork, Rewind, UserTurn
from fluid.store import jsonl
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from tests.unit.test_background_tasks import _loop
from tests.unit.test_tool_loop import ScriptedProvider, _text


class GatedProvider(ScriptedProvider):
    """Holds the second model call until ``gate`` is set."""

    def __init__(self, scripts) -> None:
        super().__init__(scripts)
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def stream(self, request):
        if self.calls == 1:
            self.waiting.set()
            await self.gate.wait()
        async for chunk in super().stream(request):
            yield chunk


async def _no_dispatch(input, ctx):
    raise AssertionError("not used")


@pytest.mark.parametrize("op_for", [
    lambda node_id: Fork(from_node_id=node_id),
    lambda node_id: Rewind(to_node_id=node_id),
], ids=["fork", "rewind"])
async def test_head_move_during_a_running_turn_is_refused_not_lost(op_for) -> None:
    store = MemoryStore()
    provider = GatedProvider([_text("one"), _text("two"), _text("three")])
    loop = _loop(provider, store, _no_dispatch, session_id="ses_fork")
    await loop.submit(UserTurn(content="first"))
    await loop.wait_turn()
    first_reply = (await store.load_path("ses_fork"))[-1].node_id

    await loop.submit(UserTurn(content="second"))
    await provider.waiting.wait()
    with pytest.raises(SessionBusy, match="turn is already running"):
        await loop.submit(op_for(first_reply))
    provider.gate.set()
    await loop.wait_turn()

    # Idle again: the same op is accepted and the next turn branches from it.
    await loop.submit(op_for(first_reply))
    await loop.submit(UserTurn(content="third"))
    await loop.wait_turn()
    path = [n.message.text() for n in await store.load_path("ses_fork")]
    assert path == ["first", "one", "third", "three"]


@pytest.mark.parametrize("command", ["/fork", "/rewind"])
async def test_the_repl_reports_a_busy_session_and_keeps_the_prompt(command) -> None:
    store = MemoryStore()
    provider = GatedProvider([_text("one"), _text("two")])
    loop = _loop(provider, store, _no_dispatch, session_id="ses_busy")
    await loop.submit(UserTurn(content="first"))
    await loop.wait_turn()
    first_reply = (await store.load_path("ses_busy"))[-1].node_id

    await loop.submit(UserTurn(content="second"))  # e.g. a background wake turn
    await provider.waiting.wait()
    out = io.StringIO()
    repl = Repl(loop, "ses_busy", input_fn=lambda _: "", out=out)
    assert await repl.dispatch_slash(f"{command} {first_reply}") is True
    assert "busy" in out.getvalue()
    provider.gate.set()
    await loop.wait_turn()


async def test_a_head_move_that_could_not_be_read_back_is_reread_before_use(
    tmp_path, monkeypatch
) -> None:
    store = JsonlStore(tmp_path)
    loop = _loop(ScriptedProvider([_text("one"), _text("two")]), store, _no_dispatch,
                 session_id="ses_unread")
    await loop.submit(UserTurn(content="first"))
    await loop.wait_turn()
    first = (await store.load_path("ses_unread"))[0].node_id

    real_write, real_parse = jsonl._write_record, JsonlStore._parse_log

    def landed(path, record, *, fsync):
        real_write(path, record, fsync=fsync)
        raise OSError(5, "fsync failed")

    def unreadable(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(jsonl, "_write_record", landed)
    monkeypatch.setattr(JsonlStore, "_parse_log", unreadable)
    with pytest.raises(OSError):
        await loop.submit(Fork(from_node_id=first))
    monkeypatch.setattr(jsonl, "_write_record", real_write)
    # Still unreadable: the engine refuses to use what it holds.
    with pytest.raises(OSError):
        await loop._ensure_session()

    monkeypatch.setattr(JsonlStore, "_parse_log", real_parse)
    meta = await loop._ensure_session()
    assert meta.head_node_id == first  # what landed, not the stale copy
    assert (await JsonlStore(tmp_path, lock=False).get_session("ses_unread")).head_node_id == first
