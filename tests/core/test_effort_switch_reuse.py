"""An effort change on a pooled session client reaches the engine.

Effort is fixed when the harness adapter connects: the delegated leg passes it
to the CLI as ``--effort`` at spawn, the native leg hands it to NativeLoop's
constructor (and to its Task children as ``app_caller_effort``). The reuse call
sites hot-swap only the model and the permission mode
(tests/core/test_model_switch_reuse.py, test_permission_mode_reuse.py), so an
effort change has to go through ``session_pool.reconnect_option_names``: the
pooled client is dropped (conversation preserved) and a fresh one connects with
the turn's effort. Before that, low -> high kept running at low.

These drive the real WebSocket turn (``_stream_to_websocket_impl``) with the
real reuse gate, a real HarnessAdapter per client, and only the engine build
inside ``HarnessAdapter.connect`` stubbed.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_engine as qe
from app.core.agent_types import AgentOptions
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.session_pool import (
    SessionState,
    reconnect_option_names,
    requires_reconnect,
)
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches

APP_SID = "sess-effort-reuse"

# Both legs read ``SessionOptions.effort`` once, inside connect():
# DelegatedLoop(effort=...) -> argv ``--effort``; NativeLoop(effort=...) ->
# every request's effort / reasoning param.
LEGS = ["delegated", "anthropic"]


def _sdk_options(cwd, effort, **extra) -> AgentOptions:
    """The options query_options builds for one turn (same session, same
    profile, only the per-turn picks varying)."""
    opts = AgentOptions(model="claude-opus-4-5", cwd=cwd, effort=effort)
    opts.permission_mode = "default"
    opts.aih_provider_id = None
    opts.aih_session_id = APP_SID
    for name, value in extra.items():
        setattr(opts, name, value)
    return opts


class _Engine:
    """The engine's shape as far as a reuse turn touches it."""

    def __init__(self, opts):
        self.effort = opts.effort
        self.model = opts.model
        self.permissions = SimpleNamespace(approval_policy=None)

    async def submit(self, op):
        # SetModel: the adapter then sets ``model`` by attribute (NativeLoop).
        raise NotImplementedError


def _fake_connect(leg, connected):
    """HarnessAdapter.connect with the engine build stubbed: the engine gets
    the effort connect() hands it, as both real legs do."""

    async def connect(self, opts):
        self._options = opts
        self._engine = _Engine(opts)
        self._connected = True
        self._is_delegated = leg == "delegated"
        self._provider_kind = leg
        connected.append(opts)

    return connect


def _connect_patches(stack, leg, connected):
    stack.enter_context(patch.object(HarnessAdapter, "connect", _fake_connect(leg, connected)))
    stack.enter_context(patch.object(
        HarnessAdapter, "prepare_background_completions", AsyncMock()))
    stack.enter_context(patch.object(HarnessAdapter, "reconcile_user_questions", AsyncMock()))
    stack.enter_context(patch(
        "app.core.pending_reconcile.reconcile_engine_pending", AsyncMock()))


def _live(adapter):
    """Stub the transport of a real adapter so a turn can run on it."""

    async def _no_events():
        if False:  # pragma: no cover — empty async generator
            yield None

    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()
    adapter.send_resume = AsyncMock()
    adapter.receive_events = MagicMock(side_effect=lambda: _no_events())
    return adapter


async def _pooled(leg, cwd, effort):
    """A client connected (for real, engine stubbed) with ``effort``."""
    connected = []
    adapter = HarnessAdapter()
    with ExitStack() as stack:
        _connect_patches(stack, leg, connected)
        sdk_options = _sdk_options(cwd, effort)
        await adapter.connect_with_sdk_options(sdk_options)
    _live(adapter)
    # A watched (WS) client: an approver is wired, so the turn may reuse it.
    adapter._can_use_tool = AsyncMock()
    return SessionState(adapter=adapter, is_connected=True, connect_options=sdk_options)


async def _turn(pool, leg, cwd, effort, **extra):
    """One WS turn with the real reuse gate. Returns (events, the adapter a
    respawn made or None, the SessionOptions it connected with)."""
    connected = []
    fresh = _live(HarnessAdapter())
    with ExitStack() as stack:
        b = engine_patches(stack, pool=pool, cwd=cwd, adapter=fresh)
        options = _sdk_options(cwd, effort, **extra)
        stack.enter_context(patch(
            "app.core.sdk_options.build_options_from_profile", return_value=(options, {})))
        # The real gate, not engine_patches' fixed answer.
        stack.enter_context(patch.object(qe, "requires_reconnect", requires_reconnect))
        stack.enter_context(patch.object(qe, "reconnect_option_names", reconnect_option_names))
        _connect_patches(stack, leg, connected)
        events = await collect(qe._stream_to_websocket_impl("hi", APP_SID, "claude-code"))
        made = fresh if b.make_adapter.called else None
    assert not any(e.get("type") == "error" for e in events), events
    return made, connected


@pytest.mark.parametrize("leg", LEGS)
async def test_low_to_high_on_a_pooled_client_runs_the_turn_at_high(leg, tmp_path):
    cwd = str(tmp_path)
    state = await _pooled(leg, cwd, "low")
    old = state.adapter
    assert old._engine.effort == "low"
    pool = RecordingPool(state)

    made, connected = await _turn(pool, leg, cwd, "high")

    assert made is not None, "an effort change must not reuse the low-effort client"
    assert pool.removed == [("remove", APP_SID, True)], "conversation must be preserved"
    assert [o.effort for o in connected] == ["high"]
    assert made._engine.effort == "high"
    made.send_query.assert_awaited_once()
    old.send_query.assert_not_awaited()
    assert pool.state.adapter is made
    assert pool.state.connect_options.effort == "high"


@pytest.mark.parametrize("leg", LEGS)
async def test_an_unchanged_effort_reuses_the_client(leg, tmp_path):
    cwd = str(tmp_path)
    state = await _pooled(leg, cwd, "high")
    pool = RecordingPool(state)

    made, connected = await _turn(pool, leg, cwd, "high")

    assert made is None and connected == []
    assert pool.removed == []
    state.adapter.send_query.assert_awaited_once()
    assert state.adapter._engine.effort == "high"


@pytest.mark.parametrize("leg", LEGS)
async def test_low_high_low_ends_at_low(leg, tmp_path):
    """The record compared against is the new client's, so a switch back is
    seen too (the A -> B -> A shape of the model / mode bugs)."""
    cwd = str(tmp_path)
    pool = RecordingPool(await _pooled(leg, cwd, "low"))

    await _turn(pool, leg, cwd, "high")
    made, connected = await _turn(pool, leg, cwd, "low")

    assert [o.effort for o in connected] == ["low"]
    assert made._engine.effort == "low"
    assert len(pool.removed) == 2


@pytest.mark.parametrize("leg", LEGS)
async def test_model_switch_with_same_effort_stays_a_hot_swap(leg, tmp_path):
    cwd = str(tmp_path)
    state = await _pooled(leg, cwd, "high")
    pool = RecordingPool(state)

    with patch(
        "app.core.provider.harness_adapter.resolve_api_model_id", side_effect=lambda m: m,
    ):
        made, _ = await _turn(pool, leg, cwd, "high", model="claude-sonnet-4-5")

    assert made is None
    assert state.adapter._engine.model == "claude-sonnet-4-5"
    assert state.adapter._engine.effort == "high"


# ─── The gate itself (also what stream_query / query_background consult) ───


@pytest.mark.parametrize("old,new,respawn", [
    ("low", "high", True),
    ("high", "low", True),
    (None, "high", True),
    ("high", None, True),
    ("high", "high", False),
    (None, None, False),
])
def test_effort_is_a_reconnect_reason(old, new, respawn):
    state = SessionState(adapter=MagicMock(), connect_options=AgentOptions(effort=old))
    names = reconnect_option_names(state, AgentOptions(effort=new))
    assert ("effort" in names) is respawn
    assert requires_reconnect(state, AgentOptions(effort=new)) is respawn


def test_model_and_permission_mode_stay_hot_swaps():
    state = SessionState(
        adapter=MagicMock(),
        connect_options=AgentOptions(model="a", permission_mode="default", effort="low"),
    )
    new = AgentOptions(model="b", permission_mode="bypassPermissions", effort="low")
    assert reconnect_option_names(state, new) == []
