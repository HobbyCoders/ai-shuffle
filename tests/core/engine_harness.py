"""Shared doubles for driving one query-engine turn in unit tests.

A plain module: import what you need explicitly. Per-file boundary patches
(``_engine_patches`` and friends) stay with their tests because their
arguments differ.
"""

from unittest.mock import AsyncMock, MagicMock

from app.core.session_pool import SessionState


class StubPool:
    """In-memory stand-in for the session pool that holds a single state."""

    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self, lane="delegated"):
        return None

    async def evict_if_needed(self, incoming_lane="delegated"):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


def mock_adapter(**adapter_attrs):
    """A live MagicMock provider adapter.

    The coroutine methods the engine awaits on every send are AsyncMocks, as
    they are on every real adapter (``app.core.provider.protocol``):
    ``send_query``, and ``set_permission_mode``, which the engine awaits when
    it reconciles the turn's permission mode just before sending.
    ``adapter_attrs`` are set on top of these defaults.
    """
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()
    adapter.set_permission_mode = AsyncMock()
    for name, value in adapter_attrs.items():
        setattr(adapter, name, value)
    return adapter


def make_state(**adapter_attrs):
    """A connected SessionState around :func:`mock_adapter`."""
    return SessionState(adapter=mock_adapter(**adapter_attrs), is_connected=True)


def stream_response(messages):
    """Build a receive_response factory that yields the given SDK messages."""

    async def _gen():
        for m in messages:
            yield m

    return MagicMock(side_effect=lambda: _gen())


async def collect(agen):
    """Drain an async generator into a list."""
    events = []
    async for ev in agen:
        events.append(ev)
    return events
