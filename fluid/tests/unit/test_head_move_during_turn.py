"""Fork and Rewind follow UserTurn's rule: refused while a turn is running.

The running loop chains each node it stores under the node it stored before,
not under the session head, and every append moves the head. A head move made
mid-turn was therefore overwritten by the turn's next append: the op returned
success and the fork was gone.
"""

from __future__ import annotations

import asyncio

import pytest

from fluid.events import Fork, Rewind, UserTurn
from fluid.store.base import MemoryStore
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
    with pytest.raises(RuntimeError, match="turn is already running"):
        await loop.submit(op_for(first_reply))
    provider.gate.set()
    await loop.wait_turn()

    # Idle again: the same op is accepted and the next turn branches from it.
    await loop.submit(op_for(first_reply))
    await loop.submit(UserTurn(content="third"))
    await loop.wait_turn()
    path = [n.message.text() for n in await store.load_path("ses_fork")]
    assert path == ["first", "one", "third", "three"]
