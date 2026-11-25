"""A Stop while a permission or question card is going out leaves nothing pending.

native.md High #2: the future was registered, then the card published and the
Notification hooks awaited, before the try/finally that removes it began. A Stop
in that window left a pending future, and the watchdog took every later turn
for one waiting on a person, so it never caught a stall again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from fluid.engine.native import NativeLoop
from fluid.events import RespondPermission
from fluid.hooks.bus import NOTIFICATION, HookBus
from fluid.store.base import MemoryStore


class _IdleProvider:
    name = "anthropic"

    async def stream(self, request):  # pragma: no cover - never called
        if False:
            yield None


def _loop_with_hung_notification() -> tuple[NativeLoop, asyncio.Event]:
    entered = asyncio.Event()

    async def hung(_event: str, _payload: dict[str, Any]) -> None:
        entered.set()
        await asyncio.sleep(3600)

    hooks = HookBus()
    hooks.on(NOTIFICATION, hung)
    loop = NativeLoop(provider=_IdleProvider(), store=MemoryStore(), model="m", hooks=hooks)
    return loop, entered


def _ask_permission(loop: NativeLoop):
    return loop._await_permission("s", "t", "Bash", "tu1", {"command": "ls"}, "high")


def _ask_question(loop: NativeLoop):
    marker = SimpleNamespace(server="srv", requests={"r1": {"message": "which?"}})
    return loop._collect_input_responses("s", "t", "mcp__srv__t", "tu1", marker)


@pytest.mark.parametrize("ask", [_ask_permission, _ask_question])
async def test_cancel_during_the_notification_leaves_no_pending_wait(ask) -> None:
    loop, entered = _loop_with_hung_notification()
    task = asyncio.create_task(ask(loop))
    await asyncio.wait_for(entered.wait(), 5)
    assert loop._human_wait_pending()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not loop._pending_permissions and not loop._pending_questions
    assert not loop._human_wait_pending()


@pytest.mark.parametrize("ask", [_ask_permission, _ask_question])
async def test_interrupt_during_a_hung_notification_ends_the_wait(ask) -> None:
    loop, entered = _loop_with_hung_notification()
    task = asyncio.create_task(ask(loop))
    await asyncio.wait_for(entered.wait(), 5)

    loop._interrupted.set()
    result = await asyncio.wait_for(task, 5)  # was parked behind the hook
    assert result is None or result[0] == "deny"
    assert not loop._human_wait_pending()


async def test_an_answered_prompt_still_lets_its_notification_finish() -> None:
    """An answer that beats the hook is kept, and the hook is not cut short."""
    release = asyncio.Event()
    seen: list[str] = []

    async def slow(_event: str, payload: dict[str, Any]) -> None:
        seen.append(payload.get("notification_type"))
        await release.wait()

    hooks = HookBus()
    hooks.on(NOTIFICATION, slow)
    loop = NativeLoop(provider=_IdleProvider(), store=MemoryStore(), model="m", hooks=hooks)
    task = asyncio.create_task(_ask_permission(loop))
    while not seen:
        await asyncio.sleep(0)
    (request_id, fut), = loop._pending_permissions.items()
    fut.set_result(RespondPermission(request_id=request_id, decision="allow"))
    await asyncio.sleep(0.05)
    assert not task.done()  # the notice already under way finishes first
    release.set()
    verdict, _msg, _updated = await asyncio.wait_for(task, 5)
    assert verdict == "allow" and seen == ["permission_prompt"]
    assert not loop._human_wait_pending()
