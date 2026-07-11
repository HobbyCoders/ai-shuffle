"""Context-boundary auto-continue seam in ``ChatWebSocketHandler.run_query``.

When an autonomous turn is cut at the CLI's autocompact boundary
(``stop_reason == "tool_use"``) AND live context usage is over the threshold,
the handler must spawn EXACTLY ONE continuation ``run_query`` with
``is_auto_continue=True`` and ``deferred_delivery=True`` (so it streams live to
open tabs), persist the labeled ``[auto-continue]`` message, latch the one-shot
guard, and hand off ownership (register the successor, return).

Guard cases take the normal completion path (no successor): a natural
``end_turn``, the setting OFF, context below threshold, and — the anti-chaining
guard — a continuation turn that itself cuts at the boundary again.

Reuses the queue test's boundary harness (fake WebSocket + every collaborator
run_query touches mocked); mirrors tests/api/test_chat_ws_handler_failover.py.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.core.compact_continue as cc

from tests.api.test_chat_ws_handler_queue import _Harness


@pytest.fixture(autouse=True)
def _clear_guard():
    cc._auto_continued_sessions.clear()
    yield
    cc._auto_continued_sessions.clear()


def _staged_stream(*stages):
    """stream_to_websocket stand-in. Each call consumes the next stage dict:
      metadata -> yielded on the terminal ``done`` frame (drives final_metadata),
      mid/gate -> optional Event pair to hold the turn open for interleaving.
    Extra calls beyond ``stages`` complete immediately with empty metadata."""
    pending = list(stages)

    async def _gen(*args, **kwargs):
        yield {"type": "chunk", "content": "hi"}
        md = {}
        if pending:
            st = pending.pop(0)
            md = st.get("metadata", {})
            gate = st.get("gate")
            if gate is not None:
                st["mid"].set()
                await gate.wait()
        yield {"type": "done", "metadata": md}

    return _gen


async def _dispatch_setmeta(event, ectx, state):
    """dispatch_event stand-in: mirrors handle_done's final_metadata capture so
    the auto-continue decision sees the real turn-end stop_reason."""
    if event.get("type") in ("done", "interrupted"):
        state.final_metadata = event.get("metadata", {})


def _run_query_spy(handler):
    calls: list = []
    real = handler.run_query

    async def _spy(*a, **k):
        calls.append((a, k))
        return await real(*a, **k)

    handler.run_query = _spy
    return calls


async def test_boundary_cut_over_threshold_spawns_single_continuation():
    """tool_use + usage over threshold → exactly ONE continuation run_query
    (is_auto_continue=True, deferred_delivery=True); the labeled message is
    persisted, the guard is latched, ownership is handed off."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        calls = _run_query_spy(handler)

        gate = asyncio.Event(); mid = asyncio.Event()
        gate2 = asyncio.Event(); mid2 = asyncio.Event()

        stream = _staged_stream(
            {"metadata": {"stop_reason": "tool_use"}, "mid": mid, "gate": gate},
            {"metadata": {"stop_reason": "end_turn"}, "mid": mid2, "gate": gate2},
        )

        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(True, 150000))):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid.wait(), timeout=5.0)
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)

            # The continuation turn is registered and distinct from the original.
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer
            await asyncio.wait_for(mid2.wait(), timeout=5.0)
            gate2.set()
            await asyncio.wait_for(successor, timeout=5.0)

    # Exactly two run_query invocations: original + one continuation.
    assert len(calls) == 2
    assert calls[0][1].get("is_auto_continue", False) is False
    assert calls[1][1].get("is_auto_continue") is True
    assert calls[1][1].get("deferred_delivery") is True
    assert calls[1][0][0] == cc.AUTO_CONTINUE_PROMPT
    # The automatic message is persisted (labeled) and one-shot guard is latched.
    persisted = [c.kwargs for c in h.db.add_session_message.call_args_list]
    assert any(
        p.get("content") == cc.AUTO_CONTINUE_PROMPT and p.get("role") == "user"
        for p in persisted
    )
    assert cc.AUTO_CONTINUE_PROMPT.startswith("[auto-continue]")


async def test_natural_end_turn_no_continuation():
    """A clean end_turn completion never spawns a continuation."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        calls = _run_query_spy(handler)

        stream = _staged_stream({"metadata": {"stop_reason": "end_turn"}})
        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(True, 150000))) as over:
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    assert len(calls) == 1
    # end_turn short-circuits before the (expensive) context check.
    over.assert_not_awaited()
    assert h.registry.get("sess-1") is None or h.registry.get("sess-1").done()


async def test_setting_off_no_continuation():
    """auto_continue_on_compact OFF → no continuation even at the boundary."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        calls = _run_query_spy(handler)

        stream = _staged_stream({"metadata": {"stop_reason": "tool_use"}})
        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cwh.settings, "auto_continue_on_compact", False), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(True, 150000))):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    assert len(calls) == 1


async def test_below_threshold_no_continuation():
    """Boundary stop but context below threshold → no continuation."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        calls = _run_query_spy(handler)

        stream = _staged_stream({"metadata": {"stop_reason": "tool_use"}})
        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(False, 90000))):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    assert len(calls) == 1


async def test_queued_message_no_continuation():
    """A queued user message resumes the session on its own — the auto-continue
    seam must defer to it (no continuation, the queue drain owns the resume)."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        # Leave a queued message: the pre-close queue drain pops it and hands
        # off before the auto-continue block is ever reached.
        h.queue.append({"id": "q1", "content": "keep going pls",
                        "profile_id": None, "project_id": None})
        calls = _run_query_spy(handler)

        gate2 = asyncio.Event(); mid2 = asyncio.Event()
        stream = _staged_stream(
            {"metadata": {"stop_reason": "tool_use"}},
            {"metadata": {"stop_reason": "end_turn"}, "mid": mid2, "gate": gate2},
        )
        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(True, 150000))):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(outer, timeout=5.0)
            # A successor ran, but it is the QUEUE drain, not an auto-continue.
            successor = h.registry.get("sess-1")
            await asyncio.wait_for(mid2.wait(), timeout=5.0)
            gate2.set()
            await asyncio.wait_for(successor, timeout=5.0)

    # The successor prompt is the queued message, NOT the auto-continue prompt.
    assert calls[1][0][0] == "keep going pls"
    assert not calls[1][1].get("is_auto_continue", False)
    assert cc.has_auto_continued("sess-1") is False


async def test_continuation_cut_again_does_not_rechain(caplog):
    """Anti-chaining: the continuation turn (is_auto_continue) itself cuts at the
    boundary again. Compaction already ran at its start, so a second cut is a
    different problem — EXACTLY one continuation total, and a diagnosable log."""
    import logging

    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        calls = _run_query_spy(handler)

        gate = asyncio.Event(); mid = asyncio.Event()
        gate2 = asyncio.Event(); mid2 = asyncio.Event()
        stream = _staged_stream(
            {"metadata": {"stop_reason": "tool_use"}, "mid": mid, "gate": gate},
            # continuation ALSO cuts at the boundary
            {"metadata": {"stop_reason": "tool_use"}, "mid": mid2, "gate": gate2},
        )
        with patch("app.core.query_engine.stream_to_websocket", new=stream), \
             patch("app.api.ws_event_handlers.dispatch_event", new=_dispatch_setmeta), \
             patch.object(cc, "context_over_threshold",
                          new=AsyncMock(return_value=(True, 150000))):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid.wait(), timeout=5.0)
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer
            await asyncio.wait_for(mid2.wait(), timeout=5.0)
            with caplog.at_level(logging.INFO):
                gate2.set()
                await asyncio.wait_for(successor, timeout=5.0)

    # Exactly ONE continuation — the second cut is not re-nudged.
    assert len(calls) == 2
    assert calls[1][1].get("is_auto_continue") is True
    assert any("second cut" in r.getMessage() for r in caplog.records)
