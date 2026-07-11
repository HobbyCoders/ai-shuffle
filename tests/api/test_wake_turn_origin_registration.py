"""Server-initiated wake turns must keep the origin device REGISTERED so they
render live (fix/wake-turn-origin-unregister).

Root cause recap: ``ChatWebSocketHandler.run_query`` unconditionally called
``sync_engine.unregister_device`` at the top of the turn (to avoid a
direct+broadcast duplicate on a normal user turn, where the origin socket gets
every event via ``send_json``) and re-registered it in the ``finally``. That is
correct for a USER-initiated turn (sender == viewer, holds the origin socket).

But a SERVER-initiated wake turn delivered by the drain pump / unpark path with
``deferred_delivery=True`` is NOT owned by the watching client — its only live
delivery channel is the ``sync_engine`` broadcast (dispatch_event →
broadcast_event, source_device_id=None) to REGISTERED devices. Unregistering the
origin can empty ``sync_engine._connections[session_id]`` (broadcast_event
silently no-ops on an empty map), so every stream_start/chunk/end of the wake
turn goes nowhere live and the turn only surfaces on a full reload.

The fix gates BOTH the top-of-turn unregister and the finally re-register on
``not deferred_delivery``. These tests drive the REAL run_query pipeline with the
shared boundary harness (mirrors test_chat_ws_handler_failover.py) and assert:

* a NORMAL turn still unregisters (and re-registers) — unchanged behavior,
* a DEFERRED wake turn NEVER unregisters and NEVER re-registers — the fix. This
  would FAIL against the old unconditional unregister.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import app.api.chat_ws_handler as cwh

from tests.api.test_chat_ws_handler_queue import _Harness, _gated_stream


async def _run_turn(h, handler, *, deferred_delivery: bool):
    """Drive one REAL run_query turn to completion with a fast fake stream."""
    with patch(
        "app.core.query_engine.stream_to_websocket", new=_gated_stream()
    ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
        await asyncio.wait_for(
            handler.run_query(
                "hello", "sess-1", "claude-code", None, None, None,
                deferred_delivery=deferred_delivery,
            ),
            timeout=5.0,
        )


async def test_normal_turn_unregisters_and_reregisters_origin():
    """Baseline (unchanged): a user-initiated turn unregisters the origin device
    at the start and re-registers it at the end."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        await _run_turn(h, handler, deferred_delivery=False)

    # The origin socket got direct sends, so it is unregistered mid-stream to
    # avoid a duplicate, then re-registered once streaming completes.
    h.sync.unregister_device.assert_awaited_once()
    h.sync.register_device.assert_awaited_once()


async def test_deferred_wake_turn_keeps_origin_registered():
    """THE fix: a server-initiated (deferred_delivery=True) wake turn must NOT
    unregister the origin device — otherwise the connection map empties and the
    turn's broadcast events reach no one, so it never renders live. It must also
    NOT re-register in the finally (register_device would close the still-live
    socket it deliberately kept)."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        await _run_turn(h, handler, deferred_delivery=True)

    # The device stays registered for the whole wake turn: neither unregister nor
    # re-register runs. (Against the old unconditional unregister this fails.)
    h.sync.unregister_device.assert_not_awaited()
    h.sync.register_device.assert_not_awaited()
