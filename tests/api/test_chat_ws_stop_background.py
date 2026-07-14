"""
Tests for the "Stop, and also stop background agents" WebSocket path.

``ChatWebSocketHandler.handle_stop`` always interrupts the main turn. When the
inbound frame carries ``stop_background=True`` it ADDITIONALLY drives every
running background task through the existing per-task stop path
(``stop_all_background_tasks`` → ``stop_session_task`` → the SDK client's
``stop_task``). Absent/False → background sub-agents survive the Stop.

The SDK client and session pool are mocked exactly as
``tests/core/test_stop_session_task.py`` does; no Postgres / live SDK required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod
import app.core.cloud_relay as cloud_relay_mod
import app.mcp.builtin_tools as bt
import pytest
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _pin_legacy_turns_path(monkeypatch):
    """A6b (v2 turns cutover): pin the LEGACY (flag-off) handler path. Under
    ``AI_SHUFFLE_V2_TURNS`` (default ON) handle_stop delegates to
    ``turn_manager.interrupt`` (the stop_background opt-in gate is preserved and
    covered in tests/api/test_chat_ws_v2_turns.py). The legacy stop-background
    machinery still SHIPS behind the flag as the PR-A rollback path (this file is
    slated for PR-B deletion once the flag is removed)."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "v2_turns", False)


class _FakeWebSocket:
    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))


def _fake_pool_with_client():
    """A pool whose peek() yields a connected session with a stub SDK client."""
    fake_client = MagicMock()
    fake_client.stop_task = AsyncMock()
    fake_state = MagicMock(is_connected=True, client=fake_client)
    fake_pool = MagicMock()
    fake_pool.peek.return_value = fake_state
    return fake_pool, fake_client


class _StopHarness:
    """Patches the boundaries handle_stop + the stop path reach for a LOCAL
    session, leaving the real stop_all_background_tasks / stop_session_task in
    play so the per-task routing is genuinely exercised."""

    def __enter__(self):
        task_registry.reset_all()
        bt._background_tasks.clear()

        self._patches = []

        # Local (not cross-install) session.
        self._patches.append(
            patch.object(cloud_relay_mod, "is_remote_session", MagicMock(return_value=False))
        )

        # database.get_session via run_sync — return a bare local row.
        fake_db = MagicMock()
        fake_db.get_session = MagicMock(return_value={"id": "sess-1"})
        self._patches.append(patch.object(cwh, "database", fake_db))

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        self._patches.append(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync))
        )

        # Main-turn interrupt is stubbed; the real stop_all_background_tasks runs.
        self.interrupt_session = AsyncMock(return_value=True)
        self._patches.append(
            patch("app.core.query_engine.interrupt_session", self.interrupt_session)
        )

        # The stop_background opt-in is gated on the preserve-background flag AND
        # the patch actually being active; enable both so the per-task stop path
        # is genuinely exercised. (test_stop_background_true_but_flag_off_is_noop
        # overrides the flag to prove the gate.)
        self._patches.append(
            patch.object(cwh.settings, "preserve_background_on_interrupt", True)
        )
        self._patches.append(
            patch(
                "app.core.cli_interrupt_patch.is_patch_active",
                MagicMock(return_value=True),
            )
        )

        # Empty active-session registry → no safety-cancel task is spawned.
        self._patches.append(patch.object(ws_mod, "_active_chat_sessions", new={}))
        self._patches.append(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
        self._patches.append(patch.object(cwh, "_stopped_sessions", new=set()))

        # Session pool the per-task stop consults.
        self.pool, self.client = _fake_pool_with_client()
        self._patches.append(
            patch(
                "app.core.query_session_lifecycle.get_session_pool",
                return_value=self.pool,
            )
        )

        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        task_registry.reset_all()
        bt._background_tasks.clear()
        return False

    def make_handler(self):
        self.ws = _FakeWebSocket()
        return cwh.ChatWebSocketHandler(
            websocket=self.ws,  # type: ignore[arg-type]
            device_id="dev-1",
            user_id="admin",
            api_user_id=None,
        )


async def test_stop_background_true_stops_each_active_agent():
    with _StopHarness() as h:
        task_registry.start_task("sess-1", "agent-a", "agent", "bg a")
        task_registry.start_task("sess-1", "agent-b", "agent", "bg b")

        await h.make_handler().handle_stop(
            {"session_id": "sess-1", "stop_background": True}
        )

    # Main-turn interrupt still fired exactly once.
    h.interrupt_session.assert_awaited_once_with("sess-1")
    # Each running background sub-agent was routed to the SDK client's stop_task.
    stopped_ids = {c.args[0] for c in h.client.stop_task.await_args_list}
    assert stopped_ids == {"agent-a", "agent-b"}
    assert h.client.stop_task.await_count == 2


async def test_stop_without_flag_leaves_background_running():
    with _StopHarness() as h:
        task_registry.start_task("sess-1", "agent-a", "agent", "bg a")

        await h.make_handler().handle_stop({"session_id": "sess-1"})

        h.interrupt_session.assert_awaited_once_with("sess-1")
        # Default false → no per-task stop; the sub-agent survives (asserted
        # inside the with-block, before the harness resets the registry).
        h.client.stop_task.assert_not_awaited()
        assert task_registry.get_active("sess-1")  # still running


async def test_stop_background_false_is_noop_for_tasks():
    with _StopHarness() as h:
        task_registry.start_task("sess-1", "agent-a", "agent", "bg a")

        await h.make_handler().handle_stop(
            {"session_id": "sess-1", "stop_background": False}
        )

    h.interrupt_session.assert_awaited_once_with("sess-1")
    h.client.stop_task.assert_not_awaited()


async def test_stop_background_true_with_no_active_tasks_is_harmless():
    with _StopHarness() as h:
        await h.make_handler().handle_stop(
            {"session_id": "sess-1", "stop_background": True}
        )

    h.interrupt_session.assert_awaited_once_with("sess-1")
    h.client.stop_task.assert_not_awaited()


async def test_stop_background_true_but_flag_off_is_noop():
    """Flag OFF → the gate collapses stop_background to False even though the
    frontend always sends True, so the per-task stop is a genuine no-op."""
    with _StopHarness() as h:
        task_registry.start_task("sess-1", "agent-a", "agent", "bg a")
        with patch.object(cwh.settings, "preserve_background_on_interrupt", False):
            await h.make_handler().handle_stop(
                {"session_id": "sess-1", "stop_background": True}
            )
        h.interrupt_session.assert_awaited_once_with("sess-1")
        h.client.stop_task.assert_not_awaited()
        assert task_registry.get_active("sess-1")  # still running
