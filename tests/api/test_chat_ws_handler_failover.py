"""Account auto-switch-on-limit retry seam in ``ChatWebSocketHandler.run_query``.

When a turn ends on a usage-limit 429 and ``attempt_failover`` says retry, the
handler must spawn EXACTLY ONE successor ``run_query`` with
``is_failover_retry=True`` and hand off ownership (register the successor under
the session lock, return). When the failover says no-retry, the turn takes the
normal completion path and no successor is spawned.

V2: run_query never persists the user message itself (handle_query owns that
row; the transcript is the recorder's event log), so a retry cannot duplicate
it by construction — pinned below by asserting run_query adds no user row.

Reuses the queue test's boundary harness (fake WebSocket + every collaborator
run_query touches mocked) and drives the REAL run_query pipeline with a gated
fake stream, mirroring tests/api/test_chat_ws_handler_queue.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.api.chat_ws_handler as cwh

from tests.api.test_chat_ws_handler_queue import _Harness, _gated_stream


def _decision(retry, switched=True, account_name="B", account_id="b"):
    return SimpleNamespace(
        retry=retry, switched=switched,
        account_name=account_name, account_id=account_id,
    )


def _patch_failover(is_limit_side_effect, attempt_return):
    """Patch cwh.account_failover with controllable classifier + attempt."""
    fake = MagicMock()
    fake.is_usage_limit_error = MagicMock(side_effect=is_limit_side_effect)
    fake.attempt_failover = AsyncMock(return_value=attempt_return)
    return patch.object(cwh, "account_failover", fake), fake


async def test_failover_retry_spawns_single_successor_no_dup_user_message():
    """retry=True → exactly one successor run_query(is_failover_retry=True); the
    successor skips add_user_message (no duplicate), and ownership is handed off
    (successor registered, original returns)."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        calls: list = []
        real_run_query = handler.run_query

        async def _spy(*a, **k):
            calls.append((a, k))
            return await real_run_query(*a, **k)

        handler.run_query = _spy

        # 429 on the FIRST turn's completion, then not-a-limit on the successor.
        fv_patch, fake = _patch_failover([True, False], _decision(retry=True))

        # Gate both turns: turn 1 so its completion is deterministic, turn 2 so
        # it stays live (and inspectable in the registry) after turn 1 returns.
        gate = asyncio.Event()
        mid = asyncio.Event()
        gate2 = asyncio.Event()
        mid2 = asyncio.Event()

        with fv_patch, \
             patch("app.core.query_engine.stream_to_websocket",
                   new=_gated_stream((mid, gate), (mid2, gate2))):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid.wait(), timeout=5.0)
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)
            await asyncio.wait_for(mid2.wait(), timeout=5.0)

            # The successor turn is registered and distinct from the original.
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer
            gate2.set()
            await asyncio.wait_for(successor, timeout=5.0)

    # attempt_failover consulted once (the successor's completion is classified
    # not-a-limit, so it never calls attempt_failover).
    fake.attempt_failover.assert_awaited_once()
    # Exactly two run_query invocations: original + one failover successor.
    assert len(calls) == 2
    assert calls[0][1].get("is_failover_retry", False) is False
    assert calls[1][1].get("is_failover_retry") is True
    # The retry carried the per-turn attempted set forward.
    assert "failover_attempted" in calls[1][1]
    # No duplicate user message: V2 run_query never persists the user row
    # (handle_query owns it), so neither turn added one.
    assert not any(
        c.kwargs.get("content") == "hello"
        for c in h.db.add_session_message.call_args_list
    )


async def test_no_failover_when_decision_declines():
    """retry=False → no successor; the turn finishes on the normal path and the
    user message was added exactly once."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        calls: list = []
        real_run_query = handler.run_query

        async def _spy(*a, **k):
            calls.append((a, k))
            return await real_run_query(*a, **k)

        handler.run_query = _spy

        fv_patch, fake = _patch_failover([True], _decision(retry=False))

        with fv_patch, \
             patch("app.core.query_engine.stream_to_websocket", new=_gated_stream()):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    fake.attempt_failover.assert_awaited_once()
    # No successor spawned — only the original run_query ran, and it completed
    # via the normal path (no failover retry registered).
    assert len(calls) == 1
    assert h.registry.get("sess-1") is None or h.registry.get("sess-1").done()


async def test_not_a_limit_error_skips_failover_entirely():
    """A clean (non-429) completion never consults attempt_failover."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        fv_patch, fake = _patch_failover([False], _decision(retry=False))

        with fv_patch, \
             patch("app.core.query_engine.stream_to_websocket", new=_gated_stream()):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    fake.attempt_failover.assert_not_awaited()


async def test_stop_during_failover_no_successor():
    """The user stops the session WHILE attempt_failover is awaiting (credential
    rotation can take seconds). Even though the decision says retry, the
    post-await stop re-check must suppress the successor — the turn takes the
    normal completion path and no second run_query is spawned."""
    with _Harness() as h:
        handler = h.make_handler()
        handler.send_json = AsyncMock(return_value=True)

        calls: list = []
        real_run_query = handler.run_query

        async def _spy(*a, **k):
            calls.append((a, k))
            return await real_run_query(*a, **k)

        handler.run_query = _spy

        async def _attempt(session_id, api_user_id, attempted):
            # Simulate a stop landing in the latch during the awaited rotation.
            h.stopped.add(session_id)
            return _decision(retry=True)

        fake = MagicMock()
        fake.is_usage_limit_error = MagicMock(side_effect=[True])
        fake.attempt_failover = AsyncMock(side_effect=_attempt)

        with patch.object(cwh, "account_failover", fake), \
             patch("app.core.query_engine.stream_to_websocket", new=_gated_stream()):
            await handler.run_query("hello", "sess-1", "claude-code", None, None, None)

    fake.attempt_failover.assert_awaited_once()
    # No successor: only the original run_query invocation happened.
    assert len(calls) == 1
    assert h.registry.get("sess-1") is None or h.registry.get("sess-1").done()


# DELETED (V2 Phase-1 deletion): test_failover_retry_readds_user_message_when_seed_missing_it.
# Its entire subject — FIX 5(b)'s live-store seed presence check (re-adding the
# user message to the live view when the ended-store snapshot was cleaned up in
# the hand-off window) — is deleted with the live session store: run_query no
# longer seeds or writes any live buffer, and the chat is read from the
# persisted transcript, where the user row persisted by handle_query cannot
# vanish in a hand-off window.
