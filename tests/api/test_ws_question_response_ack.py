"""
Regression tests for the ``user_question_response`` ack contract in
``app/api/websocket.py``'s inline ``/ws/chat`` handler.

THE BUGS GUARDED HERE:

1. (cross-install) The pre-fix branch set ``result = True`` whenever the relay
   POST merely *reached* the owning install — even when the owner replied
   ``ok: false`` because it had NO pending question with that request_id
   (already answered on another device, or timed out). The frontend then
   dismissed the modal believing the answer landed, while nothing was waiting
   for it. ``cloud_relay.relay_question_response`` now returns the owner's
   verdict and the ack carries ``success`` accordingly.

2. (ack reason) A bare ``success: false`` is ambiguous: the frontend must keep
   the modal open ONLY when delivery failed and a retry could work
   (``reason: 'relay_offline'``), and dismiss it when the failure is
   authoritative (``reason: 'not_pending'`` — locally or from the owner).
   Without the reason, a stale modal latched open forever after its question
   timed out.

Reuses the scripted-frame fake-websocket harness from
``test_ws_pending_questions_relay`` and drives the REAL inline route.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.websocket as ws_mod

from tests.api.test_ws_pending_questions_relay import (
    _FakeWebSocket,
    _frames_of_type,
    _REMOTE_SESSION,
)


def _patch_route(stack: ExitStack, *, is_remote: bool, relay_mock=None, respond_mock=None):
    """Patch the boundaries the ``user_question_response`` branch touches."""
    stack.enter_context(
        patch.object(ws_mod, "authenticate_websocket", new=AsyncMock(return_value=(True, None)))
    )

    async def _noop_ping(*a, **k):
        return None

    stack.enter_context(patch.object(ws_mod, "ping_loop", new=_noop_ping))

    sync = stack.enter_context(patch.object(ws_mod, "sync_engine"))
    sync.register_user_device = AsyncMock()
    sync.unregister_user_device = AsyncMock()
    sync.register_device = AsyncMock()
    sync.unregister_device = AsyncMock()
    sync.is_device_watching_session = MagicMock(return_value=False)
    sync.get_user_streaming_sessions = MagicMock(return_value=[])
    sync.get_device_count = MagicMock(return_value=1)
    sync._connections = {}

    store = stack.enter_context(patch.object(ws_mod, "live_session_store"))
    store.get_session = MagicMock(return_value=None)
    store.has_session = MagicMock(return_value=False)
    store.is_streaming = MagicMock(return_value=False)
    store.cleanup_session = AsyncMock()

    uqh = stack.enter_context(patch.object(ws_mod, "user_question_handler"))
    uqh.respond = respond_mock or AsyncMock(return_value=True)

    db = stack.enter_context(patch.object(ws_mod, "database"))
    db.get_session = MagicMock(return_value=_REMOTE_SESSION)
    db.get_queued_messages = MagicMock(return_value=[])

    async def _fake_run_sync(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    stack.enter_context(patch.object(ws_mod, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    stack.enter_context(patch.object(ws_mod, "cleanup_session_handlers", new=AsyncMock()))
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))

    import app.core as core_pkg
    from app.core import cloud_relay as real_cloud_relay

    fake_relay = MagicMock()
    fake_relay.InstallOffline = real_cloud_relay.InstallOffline
    fake_relay.is_remote_session = MagicMock(return_value=is_remote)
    fake_relay.relay_question_response = relay_mock or AsyncMock(return_value=True)
    stack.enter_context(patch.object(core_pkg, "cloud_relay", fake_relay))
    stack.enter_context(patch.dict("sys.modules", {"app.core.cloud_relay": fake_relay}))


async def _drive(*, is_remote: bool, relay_mock=None, respond_mock=None):
    ws = _FakeWebSocket([
        {
            "type": "user_question_response",
            "session_id": "sess-remote",
            "request_id": "q-1",
            "answers": {"Pick one": "A"},
        },
    ])
    with ExitStack() as stack:
        _patch_route(stack, is_remote=is_remote, relay_mock=relay_mock, respond_mock=respond_mock)
        await ws_mod.chat_websocket(ws, token="tok", device_id="dev-1")
    acks = _frames_of_type(ws, "user_question_response_ack")
    assert len(acks) == 1, f"expected one ack, got {ws.sent}"
    return acks[0]


# ─── Cross-install (relayed) acks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_ok_true_acks_success_without_reason():
    ack = await _drive(is_remote=True, relay_mock=AsyncMock(return_value=True))
    assert ack["success"] is True
    assert "reason" not in ack


@pytest.mark.asyncio
async def test_relay_ok_false_acks_not_pending():
    """Owner reachable but had no such pending question → authoritative failure."""
    ack = await _drive(is_remote=True, relay_mock=AsyncMock(return_value=False))
    assert ack["success"] is False
    assert ack["reason"] == "not_pending"


@pytest.mark.asyncio
async def test_relay_offline_acks_relay_offline():
    from app.core import cloud_relay as real_cloud_relay
    ack = await _drive(
        is_remote=True,
        relay_mock=AsyncMock(side_effect=real_cloud_relay.InstallOffline("owner gone")),
    )
    assert ack["success"] is False
    assert ack["reason"] == "relay_offline"


@pytest.mark.asyncio
async def test_relay_generic_error_acks_relay_offline():
    ack = await _drive(is_remote=True, relay_mock=AsyncMock(side_effect=RuntimeError("boom")))
    assert ack["success"] is False
    assert ack["reason"] == "relay_offline"


# ─── Local acks ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_respond_true_acks_success_without_reason():
    ack = await _drive(is_remote=False, respond_mock=AsyncMock(return_value=True))
    assert ack["success"] is True
    assert "reason" not in ack


@pytest.mark.asyncio
async def test_local_respond_false_acks_not_pending():
    """No local waiter (answered elsewhere / timed out) → dismissible failure."""
    ack = await _drive(is_remote=False, respond_mock=AsyncMock(return_value=False))
    assert ack["success"] is False
    assert ack["reason"] == "not_pending"
