"""Adapter-side coverage for issue #626 — expected-shutdown flagging on the
force-terminate teardown path of ``ClaudeAgentAdapter.disconnect``.

The adapter waits ``grace_period`` for a natural subprocess exit; on timeout it
must (1) flag the session as an expected shutdown BEFORE anything kills the
subprocess and (2) close the SDK client/reader before the manual force-terminate
(so the reader is cancelled cleanly rather than crashing on the ProcessError).

The SDK subprocess is simulated via a fake ``SdkProcessHandle`` so no CLI is
spawned; the fake records call order so we can assert client-close-before-kill.
"""

import asyncio

import pytest

from app.core.provider import claude_adapter
from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.expected_shutdown import consume_expected_shutdown, clear_expected_shutdown


class _FakeClient:
    def __init__(self, order):
        self._order = order
        self.disconnect_calls = 0

    async def disconnect(self):
        self.disconnect_calls += 1
        self._order.append("client.disconnect")


class _FakeHandle:
    """Simulates an alive subprocess whose grace wait always times out."""

    def __init__(self, client, order):
        self._order = order
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return not self.terminated

    @property
    def returncode(self):
        return None

    async def wait(self, timeout):
        # The initial grace wait (before terminate) times out; the post-terminate
        # 2s wait resolves cleanly.
        if not self.terminated:
            raise asyncio.TimeoutError
        return 0

    def terminate(self):
        self.terminated = True
        self._order.append("handle.terminate")
        return True

    def kill(self):
        self.killed = True
        self._order.append("handle.kill")
        return True


@pytest.fixture
def _order():
    return []


@pytest.fixture(autouse=True)
def _cleanup_marks():
    yield
    clear_expected_shutdown("sess-adapter")


async def test_timeout_path_marks_expected_shutdown_and_closes_before_terminate(
    monkeypatch, _order
):
    adapter = ClaudeAgentAdapter()
    client = _FakeClient(_order)
    adapter._client = client
    adapter._connected = True

    monkeypatch.setattr(
        claude_adapter, "SdkProcessHandle", lambda c: _FakeHandle(c, _order)
    )

    await adapter.disconnect(grace_period=0.01, session_id="sess-adapter")

    # (1) The session was flagged for expected shutdown (one-shot: consume it).
    assert consume_expected_shutdown("sess-adapter") is True
    # (2) The SDK client was closed BEFORE the manual force-terminate.
    assert "client.disconnect" in _order
    assert "handle.terminate" in _order
    assert _order.index("client.disconnect") < _order.index("handle.terminate")
    # Connection state is cleared.
    assert adapter._connected is False


async def test_natural_exit_does_not_flag(monkeypatch):
    """When the subprocess exits within grace, no terminate fires and no
    expected-shutdown flag is set (nothing to suppress)."""
    order = []

    class _CleanHandle(_FakeHandle):
        async def wait(self, timeout):
            return 0  # natural exit within grace

        def is_alive(self):
            return False  # already exited

    adapter = ClaudeAgentAdapter()
    client = _FakeClient(order)
    adapter._client = client
    adapter._connected = True

    monkeypatch.setattr(
        claude_adapter, "SdkProcessHandle", lambda c: _CleanHandle(c, order)
    )

    await adapter.disconnect(grace_period=5.0, session_id="sess-adapter")

    assert consume_expected_shutdown("sess-adapter") is False
    assert "handle.terminate" not in order
    assert adapter._connected is False


async def test_disconnect_without_client_is_noop():
    adapter = ClaudeAgentAdapter()
    # No client set — must return cleanly without touching the registry.
    await adapter.disconnect(session_id="sess-adapter")
    assert consume_expected_shutdown("sess-adapter") is False
