"""A delegated permission request always gets its answer in seconds (#105).

A host that answers at once (Bypass allows without asking anyone) could reply
while the request event was still being persisted, before the loop was
waiting for the reply. That answer was dropped and the call sat parked until
``PERMISSION_TIMEOUT`` (900 s) denied it.
"""

import asyncio
import time

from fluid.events.model import PermissionRequest, ToolResult, TurnComplete
from fluid.events.ops import RespondPermission, UserTurn
from fluid.store.base import MemoryStore

from tests.unit.test_delegated import _make_loop, _mapper, _spy_on_frames


class _SlowStore(MemoryStore):
    """Writes that yield to the event loop, as a database write does."""

    async def append_event(self, session_id, event):
        await asyncio.sleep(0.05)
        return await super().append_event(session_id, event)


async def test_an_immediate_answer_is_not_lost_while_the_request_is_saved(monkeypatch) -> None:
    from fluid.engine import delegated

    # A lost answer would park the call for this long; the turn must not.
    monkeypatch.setattr(delegated, "PERMISSION_TIMEOUT", 30.0)
    store = _SlowStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)

    async def bypass_host() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                await loop.submit(RespondPermission(request_id=event.request_id, decision="allow"))
                return

    host = asyncio.create_task(bypass_host())
    started = time.monotonic()
    try:
        await asyncio.sleep(0)
        await loop.submit(UserTurn(content="weather in Paris?"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        await asyncio.wait_for(host, 10)
        events = loop.bus.log(sid)
        result = next(e for e in events if isinstance(e, ToolResult))
        assert not result.is_error, result.output
        assert result.output == "Paris: sunny, 21C"
        assert isinstance(events[-1], TurnComplete)
        assert time.monotonic() - started < 10
        assert not loop._pending_permissions
    finally:
        host.cancel()
        await loop.aclose()


async def test_a_request_nobody_can_see_is_denied_quickly_with_the_reason(monkeypatch) -> None:
    from fluid.engine import delegated

    monkeypatch.setattr(delegated, "UNWATCHED_PERMISSION_TIMEOUT", 0.2)
    assert delegated.PERMISSION_TIMEOUT == 900.0
    loop = _make_loop("b")
    frames = _spy_on_frames(loop)
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        denial = next(
            f["response"]["response"] for f in frames
            if f.get("type") == "control_response" and f["response"]["request_id"] == "cli_req_1"
        )
        assert denial["behavior"] == "deny"
        assert "no one is attached to this session" in denial["message"]
        events = loop.bus.log(loop.session_id)
        assert next(e for e in events if isinstance(e, ToolResult)).is_error
    finally:
        await loop.aclose()


def test_the_clis_reason_for_asking_rides_the_request() -> None:
    reason = "This command removes a path computed at run time"
    mapped = _mapper().feed({
        "type": "control_request", "request_id": "cli_5",
        "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "tu_5",
            "input": {"command": "rm -rf $S/$r"}, "decision_reason": reason,
            "decision_reason_type": "safetyCheck",
        },
    })
    assert mapped.permission.reason == reason
    (event,) = mapped.events
    assert isinstance(event, PermissionRequest) and event.reason == reason


async def test_a_decision_for_no_open_request_is_logged_not_silent(caplog) -> None:
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake")
    with caplog.at_level("WARNING", logger="fluid.engine.delegated"):
        await loop.submit(RespondPermission(request_id="perm_gone", decision="allow"))
    assert "matched no open request" in caplog.text
