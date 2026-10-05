"""A reused session client follows every permission-mode change.

A pooled client is reused across turns, and each turn compares its permission
mode with ``SessionState.connect_options`` (the SDK options the adapter was
connected with) to decide whether to call ``adapter.set_permission_mode``. If
that record kept the connect-time mode, Ask -> Bypass -> Ask would compare the
final Ask with the original Ask, skip the call, and leave the engine in Bypass.
The adapter therefore writes every applied change back to those options.

Also: with nobody to answer, the delegated (Claude CLI) leg decides a prompt
by the same rule a watched session follows (``mode_approves_without_prompt``),
so Accept Edits and Auto deny the computer-use access request as Ask does.
"""

import asyncio
import dataclasses
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fluid.events import model as lm
from fluid.events.ops import RespondPermission, SetPermissionMode
from fluid.permissions.engine import PermissionEngine

import app.core.query_engine as qe
from app.core.provider.harness_adapter import _PERMISSION_POLICY, HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.session_modes import COMPUTER_USE_REQUEST_ACCESS
from app.core.session_pool import SessionState
from tests.core.engine_harness import StubPool, collect
from tests.core.test_busy_write_guard_engine import _chat_patches

FSID = "ses_reuse"
APP_SID = "sess-mode-reuse"


class _NativeEngine:
    """An engine without the ``SetPermissionMode`` op: the adapter falls back
    to setting the real ``PermissionEngine``'s policy and mode rules itself."""

    def __init__(self):
        self.permissions = PermissionEngine("on_write")
        self.ops: list = []
        self._events: list = []

    async def submit(self, op):
        if isinstance(op, SetPermissionMode):
            raise NotImplementedError
        self.ops.append(op)

    def script(self, events):
        self._events = [dataclasses.replace(e, seq=i + 1) for i, e in enumerate(events)]

    async def events(self, *, since_seq=0):
        for e in self._events:
            if e.seq > since_seq:
                yield e


async def _connected_adapter(engine, mode):
    """A real HarnessAdapter through ``connect_with_sdk_options``, with the
    engine build stubbed out. Returns (adapter, the connect-time SDK options)."""
    sdk_options = SimpleNamespace(
        model="gpt-5", permission_mode=mode, aih_provider_id=None, aih_session_id=APP_SID,
    )

    async def fake_connect(self, opts):
        self._options = opts
        self._engine = engine
        self._connected = True
        self._is_delegated = False
        self._provider_kind = "openai_responses"
        engine.permissions.approval_policy = _PERMISSION_POLICY.get(opts.permission_mode)

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


async def _turns(state, modes, *, expect_error=False, before_run=None):
    from app.db import database, turns

    if database.get_session(APP_SID) is None:
        database.create_session(APP_SID, None, permission_mode="default")
    from app.core import turn_manager

    pool = StubPool(state)
    for mode in modes:
        # The owner's own send, through the producer that stores its pick as
        # the session's binding before queuing it.
        turn = turns.get_turn(turn_manager.submit(
            APP_SID, "user", prompt="hi", explicit_permission=True,
            overrides={"permission_mode": mode}, no_kick=True))
        if before_run is not None:
            before_run()  # what the owner did while the message waited
        with ExitStack() as stack:
            options = _chat_patches(stack, pool)
            previous = qe.run_sync
            async def read_binding(fn, *args, **kwargs):
                if fn == database.get_session:
                    return database.get_session(*args, **kwargs)
                return await previous(fn, *args, **kwargs)
            stack.enter_context(patch.object(qe, "run_sync", side_effect=read_binding))
            options.permission_mode = mode
            options.model = "gpt-5"
            options.aih_provider_id = None
            events = await collect(qe._stream_to_websocket_impl(
                "hi", APP_SID, "claude-code", overrides=turn["overrides"], loop_guard_turn=turn))
        assert any(e.get("type") == "error" for e in events) == expect_error, events
        if expect_error:
            assert pool.state is None, "a client with a refused permission switch must be evicted"
        else:
            assert pool.state is state, "the client must be reused, not respawned"


async def _unwatched_prompt(adapter, engine, name="Write"):
    """Drive one prompted call with no approver; True when it surfaced as a
    prompt (Ask), False when the adapter approved it on the mode's say-so."""
    adapter._can_use_tool = None
    engine.ops.clear()
    engine.script([
        lm.PermissionRequest(
            session_id=FSID, request_id="perm_1", tool_use_id="tu1",
            name=name, input={"file_path": "/x"}, risk="write",
        ),
        lm.TurnComplete(session_id=FSID, stop_reason="end_turn", num_model_calls=1),
    ])
    adapter._event_cursor = 0
    got = [ev async for ev in HarnessAdapter.receive_events(adapter)]
    for _ in range(20):
        await asyncio.sleep(0)
    prompted = any(isinstance(e, PermissionRequestEvent) for e in got)
    answered = [op for op in engine.ops if isinstance(op, RespondPermission)]
    assert prompted != bool(answered)
    return prompted


# ─── Fix 1: the reuse path ────────────────────────────────────────────────


async def test_ask_bypass_ask_on_a_reused_client_ends_in_ask():
    engine = _NativeEngine()
    adapter, sdk_options = await _connected_adapter(engine, "default")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["bypassPermissions", "default"])

    assert adapter.permission_mode == "default"
    assert engine.permissions.approval_policy == _PERMISSION_POLICY["default"]
    assert state.connect_options.permission_mode == "default"
    # A call Ask puts to a person still prompts.
    assert await _unwatched_prompt(adapter, engine) is True


async def test_bypass_ask_bypass_on_a_reused_client_ends_in_bypass():
    engine = _NativeEngine()
    adapter, sdk_options = await _connected_adapter(engine, "bypassPermissions")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["default", "bypassPermissions"])

    assert adapter.permission_mode == "bypassPermissions"
    assert engine.permissions.approval_policy == _PERMISSION_POLICY["bypassPermissions"]
    assert await _unwatched_prompt(adapter, engine) is False


async def test_message_queued_in_bypass_runs_at_ask_after_the_owner_tightens():
    """A later tightening wins over the pick a still-queued message carries."""
    from app.db import database

    engine = _NativeEngine()
    adapter, sdk_options = await _connected_adapter(engine, "bypassPermissions")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["bypassPermissions"], before_run=lambda: database.set_session_run_binding(
        APP_SID, permission_mode="default"))

    assert adapter.permission_mode == "default"
    assert engine.permissions.approval_policy == _PERMISSION_POLICY["default"]
    assert await _unwatched_prompt(adapter, engine) is True


async def test_an_unchanged_mode_is_not_reapplied():
    engine = _NativeEngine()
    adapter, sdk_options = await _connected_adapter(engine, "default")
    state = _pooled(adapter, sdk_options)
    applied = []
    real = adapter.set_permission_mode

    async def spy(mode):
        applied.append(mode)
        await real(mode)

    adapter.set_permission_mode = spy
    await _turns(state, ["default", "acceptEdits", "acceptEdits", "default", "default"])
    assert applied == ["acceptEdits", "default"]


async def test_a_switch_the_engine_refuses_is_not_recorded_and_is_retried():
    """R9-16: the delegated leg raises when the Claude CLI refuses (or does not
    acknowledge) a mode switch. The adapter must not record the mode as
    applied, so the next turn with the same mode asks again."""

    class _RefusingEngine(_NativeEngine):
        def __init__(self):
            super().__init__()
            self.refuse = True

        async def submit(self, op):
            if isinstance(op, SetPermissionMode):
                if self.refuse:
                    raise RuntimeError("the Claude CLI refused set_permission_mode: nope")
                self.ops.append(op)
                return
            await super().submit(op)

    engine = _RefusingEngine()
    adapter, sdk_options = await _connected_adapter(engine, "default")
    state = _pooled(adapter, sdk_options)

    await _turns(state, ["bypassPermissions"], expect_error=True)
    adapter.send_query.assert_not_awaited()
    assert state.connect_options.permission_mode == "default"
    assert not [op for op in engine.ops if isinstance(op, SetPermissionMode)]

    engine.refuse = False
    # The failed client was evicted and disconnected. Retry on a new client,
    # rather than resurrecting a client whose permission state is uncertain.
    adapter, sdk_options = await _connected_adapter(engine, "default")
    state = _pooled(adapter, sdk_options)
    await _turns(state, ["bypassPermissions"])
    # The native engine is sent the policy, never the mode name.
    assert [op.mode for op in engine.ops if isinstance(op, SetPermissionMode)] == ["never_ask"]
    assert state.connect_options.permission_mode == "bypassPermissions"


async def test_legacy_auto_is_recorded_as_asked_for():
    """The record holds the mode the caller asked for, so a native session
    that keeps sending the legacy ``auto`` is not re-applied every turn,
    while the engine runs it as Ask."""
    engine = _NativeEngine()
    adapter, sdk_options = await _connected_adapter(engine, "bypassPermissions")
    await adapter.set_permission_mode("auto")
    assert sdk_options.permission_mode == "auto"
    assert adapter.permission_mode == "default"
    assert engine.permissions.approval_policy == _PERMISSION_POLICY["default"]


# ─── Fix 2: the delegated leg with nobody to answer ──────────────────────


def _delegated(mode, alias=None):
    adapter = HarnessAdapter()
    adapter._is_delegated = True
    adapter._options = SessionOptions(model="fable", permission_mode=mode)
    registry = {alias: COMPUTER_USE_REQUEST_ACCESS} if alias else {}
    adapter._engine = SimpleNamespace(
        _registry_spec=lambda name: (
            SimpleNamespace(name=registry[name]) if name in registry else None
        ),
    )
    adapter.respond_to_permission = AsyncMock()
    return adapter


async def _decision(adapter, name):
    await adapter._answer_without_approver(
        PermissionRequestEvent(tool_use_id="t", input={}, name=name)
    )
    adapter.respond_to_permission.assert_awaited_once()
    return adapter.respond_to_permission.await_args.args[1]


@pytest.mark.parametrize("mode, tool, allow", [
    ("default", "Bash", False),
    ("plan", "Bash", False),
    (None, "Bash", False),
    ("acceptEdits", "Write", True),
    ("acceptEdits", "Bash", False),
    ("auto", "Bash", True),
    ("bypassPermissions", "Bash", True),
    ("default", COMPUTER_USE_REQUEST_ACCESS, False),
    ("acceptEdits", COMPUTER_USE_REQUEST_ACCESS, False),
    ("auto", COMPUTER_USE_REQUEST_ACCESS, False),
    ("bypassPermissions", COMPUTER_USE_REQUEST_ACCESS, True),
])
async def test_unattended_delegated_decision_follows_the_watched_rule(mode, tool, allow):
    assert await _decision(_delegated(mode), tool) is allow


@pytest.mark.parametrize("mode", ["acceptEdits", "auto"])
async def test_unattended_delegated_screen_control_under_its_bundled_alias_is_denied(mode):
    alias = "mcp__fluid_bundled__" + COMPUTER_USE_REQUEST_ACCESS
    adapter = _delegated(mode, alias=alias)
    assert await _decision(adapter, alias) is False
    assert adapter.respond_to_permission.await_args.kwargs["message"]
