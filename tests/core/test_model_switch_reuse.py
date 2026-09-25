"""A reused session client follows every model switch.

A pooled client is reused across turns, and each turn compares its model with
``SessionState.connect_options`` (the SDK options the adapter was connected
with) to decide whether to call ``adapter.set_model``. If that record kept the
connect-time model, A -> B -> A would compare the final A with the original A,
skip the call, and leave the engine on B. The adapter therefore writes every
applied model back to those options, as it does for the permission mode
(tests/core/test_permission_mode_reuse.py).

Only the model and the permission mode are hot-swapped at the reuse call
sites; effort, fast mode and the context window are not compared there.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.query_engine as qe
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.session_pool import SessionState
from fluid.events.ops import SetModel
from tests.core.engine_harness import StubPool, collect
from tests.core.test_busy_write_guard_engine import _chat_patches

APP_SID = "sess-model-reuse"


class _NativeEngine:
    """NativeLoop's shape for a model switch: ``SetModel`` is not a submit op
    there, the adapter sets ``model`` by attribute."""

    def __init__(self, model):
        self.model = model
        self.permissions = SimpleNamespace(approval_policy=None)

    async def submit(self, op):
        if isinstance(op, SetModel):
            raise NotImplementedError
        raise AssertionError(f"unexpected op {op!r}")


async def _connected_adapter(engine, model, provider_kind="openai_responses"):
    """A real HarnessAdapter through ``connect_with_sdk_options``, with the
    engine build stubbed out. Returns (adapter, the connect-time SDK options)."""
    sdk_options = SimpleNamespace(
        model=model, permission_mode="default", aih_provider_id=None,
        aih_session_id=APP_SID,
    )

    async def fake_connect(self, opts):
        self._options = opts
        self._engine = engine
        self._connected = True
        self._is_delegated = False
        self._provider_kind = provider_kind

    adapter = HarnessAdapter()
    with ExitStack() as stack:
        stack.enter_context(patch.object(HarnessAdapter, "connect", fake_connect))
        stack.enter_context(patch.object(
            HarnessAdapter, "prepare_background_completions", AsyncMock()))
        stack.enter_context(patch.object(HarnessAdapter, "reconcile_user_questions", AsyncMock()))
        stack.enter_context(patch(
            "app.core.pending_reconcile.reconcile_engine_pending", AsyncMock()))
        await adapter.connect_with_sdk_options(sdk_options)
    return adapter, sdk_options


def _pooled(adapter, sdk_options) -> SessionState:
    """The pool entry query_engine creates after a connect, ready for reuse."""

    async def _no_events():
        if False:  # pragma: no cover — empty async generator
            yield None

    # A watched (WS) client: an approver is wired, so the turn reuses it.
    adapter._can_use_tool = AsyncMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()
    adapter.receive_events = MagicMock(side_effect=lambda: _no_events())
    return SessionState(adapter=adapter, is_connected=True, connect_options=sdk_options)


async def _turns(state, models):
    pool = StubPool(state)
    for model in models:
        with ExitStack() as stack:
            options = _chat_patches(stack, pool)
            options.permission_mode = "default"
            options.model = model
            options.aih_provider_id = None
            events = await collect(qe._stream_to_websocket_impl("hi", APP_SID, "claude-code"))
        assert not any(e.get("type") == "error" for e in events), events
        assert pool.state is state, "the client must be reused, not respawned"


def _spy_set_model(adapter):
    applied = []
    real = adapter.set_model

    async def spy(model):
        applied.append(model)
        await real(model)

    adapter.set_model = spy
    return applied


async def test_a_b_a_on_a_reused_client_ends_on_a():
    engine = _NativeEngine("gpt-5")
    adapter, sdk_options = await _connected_adapter(engine, "gpt-5")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["gpt-5-mini", "gpt-5"])

    assert engine.model == "gpt-5"
    assert state.connect_options.model == "gpt-5"


async def test_b_a_b_on_a_reused_client_ends_on_b():
    engine = _NativeEngine("gpt-5")
    adapter, sdk_options = await _connected_adapter(engine, "gpt-5")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["gpt-5-mini", "gpt-5", "gpt-5-mini"])

    assert engine.model == "gpt-5-mini"
    assert state.connect_options.model == "gpt-5-mini"


async def test_an_unchanged_model_is_not_reapplied():
    engine = _NativeEngine("gpt-5")
    adapter, sdk_options = await _connected_adapter(engine, "gpt-5")
    state = _pooled(adapter, sdk_options)
    applied = _spy_set_model(adapter)

    await _turns(state, ["gpt-5", "gpt-5-mini", "gpt-5-mini", "gpt-5", "gpt-5"])

    assert applied == ["gpt-5-mini", "gpt-5"]
    assert engine.model == "gpt-5"


async def test_a_model_is_recorded_as_sent_not_as_canonicalized():
    """On the Anthropic leg the engine gets the canonical API id, but the
    record keeps the id the caller sent, so a session that keeps sending the
    same alias is not re-applied every turn."""
    engine = _NativeEngine("claude-opus")
    adapter, sdk_options = await _connected_adapter(
        engine, "opus", provider_kind="anthropic")
    with patch(
        "app.core.provider.harness_adapter.resolve_api_model_id",
        side_effect=lambda m: f"claude-{m}-canonical",
    ):
        await adapter.set_model("sonnet")
    assert engine.model == "claude-sonnet-canonical"
    assert sdk_options.model == "sonnet"


async def test_a_frozen_options_object_does_not_break_the_switch():
    engine = _NativeEngine("gpt-5")
    adapter, _ = await _connected_adapter(engine, "gpt-5")

    class _Frozen:
        model = "gpt-5"

        def __setattr__(self, name, value):
            raise AttributeError("frozen")

    adapter._sdk_options = _Frozen()
    await adapter.set_model("gpt-5-mini")
    assert engine.model == "gpt-5-mini"
