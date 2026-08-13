"""`SessionState.client` must tolerate adapters that have no SDK client.

The omni `HarnessAdapter` exposes an `engine`, not an `sdk_client`, so the
transitional `state.client` shim used to raise AttributeError on every omni
session — killing the between-turns idle drain and the per-task stop path.
It now degrades to None and callers handle that.
"""

from claude_agent_sdk import ClaudeAgentOptions

from app.core.session_pool import SessionState


class _AdapterWithoutSdkClient:
    """Stand-in for the omni harness adapter: engine only, no sdk_client."""

    def __init__(self):
        self.engine = object()

    def is_alive(self) -> bool:
        return True


class _AdapterWithSdkClient:
    def __init__(self, sdk_client):
        self.sdk_client = sdk_client


def _state(adapter) -> SessionState:
    return SessionState(
        adapter=adapter,
        connect_options=ClaudeAgentOptions(model="claude-sonnet-4-5"),
    )


def test_client_is_none_when_adapter_has_no_sdk_client():
    state = _state(_AdapterWithoutSdkClient())
    assert state.client is None


def test_client_returns_sdk_client_when_present():
    sentinel = object()
    state = _state(_AdapterWithSdkClient(sentinel))
    assert state.client is sentinel


def test_client_returns_none_when_sdk_client_is_none():
    state = _state(_AdapterWithSdkClient(None))
    assert state.client is None
