"""V2 Phase-1 deletion (Commit 4): the background query path records to the
session event log via TurnRecorder instead of live_session_store/sync_engine
streaming broadcasts.

Drives ``_run_background_query`` end-to-end with a mocked pooled adapter
that yields provider-agnostic mirror message objects (from
``app.core.provider.events`` — same class names/fields as the
claude_agent_sdk message/block classes the streaming loops used to dispatch
on), and asserts against the event log written through the per-test SQLite
shim (autouse ``_auto_db_shim``). No SSE / ASGITransport anywhere (that
buffers infinite bodies and deadlocks).

Also covers ``app.core.turn_finalize.persist_turn_end_state`` — the durable
end-of-turn side effects extracted from ``live_session_store.end_streaming``.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.provider.events import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import app.core.query_background as qb
from app.core.session_pool import SessionState
from app.core.turn_finalize import persist_turn_end_state
from app.db import runs as runs_db
from app.db import session_events as se
from app.db import sessions as sessions_db


# ─── helpers ────────────────────────────────────────────────────────────────


def _result_message(session_id: str = "sdk-1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.01,
        usage={"input_tokens": 10, "output_tokens": 5},
        result="done",
        # SDK 0.2.126: terminal_reason + typed per-model usage now thread
        # through result metadata (query_background ResultMessage block).
        terminal_reason="completed",
        model_usage={"claude": {"provider": "firstParty"}},
    )


def _fake_state(messages=None, receive_error=None) -> SessionState:
    """Pooled-adapter stand-in whose receive_events yields mirror message objects."""
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()

    async def _response():
        for m in (messages or []):
            yield m
        if receive_error is not None:
            raise receive_error

    adapter.receive_events = MagicMock(side_effect=lambda: _response())
    return SessionState(adapter=adapter, is_connected=True)


class _StubPool:
    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self):
        return None

    async def evict_if_needed(self):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None


def _bg_patches(stack: ExitStack, pool: _StubPool, run_sync_calls=None):
    """Patch every _run_background_query boundary EXCEPT the recorder/DB."""

    async def _fake_run_sync(fn, *a, **k):
        if run_sync_calls is not None:
            run_sync_calls.append((fn, a, k))
        return {"id": "row-1"}

    stack.enter_context(patch.object(
        qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)
    ))
    sync = MagicMock()
    sync.broadcast_message_added = AsyncMock()
    sync._lock = asyncio.Lock()
    sync._session_locks = {}
    stack.enter_context(patch.object(qb, "sync_engine", new=sync))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qb, "direct_oauth", new=oauth))
    options = MagicMock()
    options.cwd = None
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(patch.object(qb, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qb, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qb, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qb, "credentials_rotated", return_value=False))
    stack.enter_context(patch(
        "app.core.hook_service.attach_hooks_to_options", new=MagicMock()
    ))
    # streaming_status.notify (fired on lease claim/release) schedules an async
    # DB re-check thread that shares the per-test ShimConnection; its commit can
    # interleave with append_events' in-flight statements and corrupt the shim's
    # transaction state (pre-existing pg_shim shared-SQLite hazard). Neutralize
    # it — these tests assert on the event log, not the board spinner.
    stack.enter_context(patch(
        "app.core.turn_recorder._notify_run_change", new=MagicMock()
    ))
    persist_results = AsyncMock()
    stack.enter_context(patch.object(qb, "_persist_turn_results", new=persist_results))
    stack.enter_context(patch.object(qb, "push_memory", new=AsyncMock()))
    finalize = AsyncMock()
    stack.enter_context(patch.object(qb, "persist_turn_end_state", new=finalize))
    return sync, persist_results, finalize


def _types(session_id):
    return [r["event_type"] for r in se.get_events(session_id)]


async def _run(session_id, prompt="hello"):
    await qb._run_background_query(
        session_id, prompt, {"id": "default"}, None, None, None, None, None,
    )


# ─── full-turn recording ────────────────────────────────────────────────────


async def test_background_turn_records_full_event_sequence(mock_db):
    sid = "bg-rec-1"
    sessions_db.create_session(sid, "default")
    state = _fake_state(messages=[
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        AssistantMessage(
            content=[TextBlock("hello "), ToolUseBlock(
                id="tu_1", name="Bash", input={"command": "ls"},
            )],
            model="claude",
            usage={"input_tokens": 7, "output_tokens": 3},
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="tu_1", content="file.txt", is_error=False,
        )]),
        AssistantMessage(content=[TextBlock("done.")], model="claude"),
        _result_message(),
    ])
    pool = _StubPool(state)
    run_sync_calls = []

    with ExitStack() as stack:
        sync, persist_results, finalize = _bg_patches(
            stack, pool, run_sync_calls=run_sync_calls
        )
        await _run(sid)

    rows = se.get_events(sid)
    types = [r["event_type"] for r in rows]
    assert types == [
        "turn.started",
        "message.user",
        "assistant.text_delta",
        "tool.use",
        "turn.usage",
        "tool.result",
        "assistant.text_delta",
        "turn.completed",
    ], types

    by_type = {t: r for t, r in zip(types, rows)}
    assert by_type["message.user"]["payload"]["content"] == "hello"
    tool_use = by_type["tool.use"]["payload"]
    assert tool_use["tool_use_id"] == "tu_1"
    assert tool_use["name"] == "Bash"
    assert tool_use["input"] == {"command": "ls"}
    tool_result = by_type["tool.result"]["payload"]
    assert tool_result["tool_use_id"] == "tu_1"
    assert tool_result["is_error"] is False
    assert "file.txt" in tool_result["content_preview"]
    assert by_type["turn.usage"]["payload"]["usage"]["input_tokens"] == 7
    completed = rows[-1]["payload"]
    assert completed["usage"]["tokens_in"] == 10
    assert completed["usage"]["tokens_out"] == 5
    # SDK 0.2.126: terminal_reason threads onto the turn.completed payload.
    assert completed["terminal_reason"] == "completed"

    # ...and the raw ResultMessage metadata (terminal_reason + typed
    # model_usage) reaches the durable turn-persist seam for future per-model
    # cost display.
    persist_meta = persist_results.await_args.kwargs["metadata"]
    assert persist_meta["terminal_reason"] == "completed"
    assert persist_meta["model_usage"] == {"claude": {"provider": "firstParty"}}

    # Every event this turn appended was FENCED by the claimed run lease...
    run_ids = {r["run_id"] for r in rows}
    assert run_ids != {None} and len(run_ids) == 1, run_ids
    # ...and the lease was released on close (a new claim succeeds).
    assert runs_db.claim_run(sid, "another-runner", 60) is not None

    # Legacy durable writes are KEPT: user message persisted + broadcast, and
    # the batched turn persist still runs.
    from app.db import database
    assert any(f is database.add_session_message for f, _, _ in run_sync_calls)
    sync.broadcast_message_added.assert_awaited_once()
    persist_results.assert_awaited_once()
    # No streaming broadcasts remain anywhere on the background path.
    sync.broadcast_stream_start.assert_not_called()
    sync.broadcast_stream_chunk.assert_not_called()
    sync.broadcast_stream_end.assert_not_called()

    # The durable end-of-turn helper ran.
    finalize.assert_awaited_once_with(sid)

    # The pooled client got exactly one prompt and is idle again.
    state.adapter.send_query.assert_awaited_once_with("hello")
    assert state.is_streaming is False


async def test_background_interrupt_records_turn_aborted_interrupt(mock_db):
    sid = "bg-rec-int"
    sessions_db.create_session(sid, "default")
    state = _fake_state(
        messages=[AssistantMessage(content=[TextBlock("partial")], model="claude")],
        receive_error=asyncio.CancelledError(),
    )
    pool = _StubPool(state)

    with ExitStack() as stack:
        _bg_patches(stack, pool)
        await _run(sid)

    types = _types(sid)
    assert types == [
        "turn.started", "message.user", "assistant.text_delta", "turn.aborted",
    ], types
    aborted = se.get_events(sid)[-1]["payload"]
    assert aborted["reason"] == "interrupt"


async def test_background_stream_error_records_turn_aborted_error(mock_db):
    sid = "bg-rec-err"
    sessions_db.create_session(sid, "default")
    state = _fake_state(messages=[], receive_error=RuntimeError("sdk exploded"))
    pool = _StubPool(state)

    with ExitStack() as stack:
        _bg_patches(stack, pool)
        await _run(sid)

    types = _types(sid)
    assert types == ["turn.started", "message.user", "turn.aborted"], types
    aborted = se.get_events(sid)[-1]["payload"]
    assert aborted["reason"] == "error"
    assert "sdk exploded" in (aborted.get("message") or "")


async def test_background_records_regardless_of_v2_flag(mock_db):
    # Un-gated (Commit 5): the recorder no longer reads v2_stream — every
    # session records a full turn (the flag is now always TRUE / vestigial).
    sid = "bg-rec-legacy"
    sessions_db.create_session(sid, "default")
    state = _fake_state(messages=[_result_message()])
    pool = _StubPool(state)

    with ExitStack() as stack:
        _bg_patches(stack, pool)
        await _run(sid)

    assert _types(sid) == ["turn.started", "message.user", "turn.completed"]
    # The turn itself still ran normally.
    state.adapter.send_query.assert_awaited_once()


# ─── launched_model stamp (issue #577) ──────────────────────────────────────


async def test_background_init_stamps_launched_model(mock_db):
    """Regression (#577): the background REST spawn path (POST
    /conversation/start -> _run_background_query) must persist the effective
    launched model to sessions.launched_model at init, like the other three
    spawn paths (execute_query / stream_query / stream_to_websocket) already do
    via #552. Before the fix this path never stamped launched_model, so every
    background session read back NULL and the Unraid host-op guard could not
    resolve its model authoritatively.
    """
    sid = "bg-launched-model"
    sessions_db.create_session(sid, "default")
    state = _fake_state(messages=[
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        AssistantMessage(content=[TextBlock("hi")], model="fable"),
        _result_message(),
    ])
    pool = _StubPool(state)
    run_sync_calls = []

    with ExitStack() as stack:
        _bg_patches(stack, pool, run_sync_calls=run_sync_calls)
        # The pinned-model profile resolves to an explicit options.model (the
        # worker's compounding-engineer profile pins `fable`). Override the
        # MagicMock options built by _bg_patches so getattr(options, "model")
        # is a real string, not an auto-created attribute.
        opts = MagicMock()
        opts.cwd = None
        opts.model = "fable"
        stack.enter_context(patch(
            "app.core.sdk_options.build_options_from_profile",
            return_value=(opts, {}),
        ))
        await _run(sid)

    from app.db import database

    launched_model_writes = [
        (a, k)
        for fn, a, k in run_sync_calls
        if fn is database.update_session and "launched_model" in k
    ]
    assert launched_model_writes, (
        "background spawn path never stamped launched_model — the session "
        "reads back NULL and the host-op guard cannot resolve its model (#577)"
    )
    assert all(k["launched_model"] == "fable" for _, k in launched_model_writes), (
        f"launched_model stamped with wrong value: {launched_model_writes}"
    )


# ─── build_session_options guard (FIX 5) ────────────────────────────────────


async def test_background_provider_config_error_records_aborted_no_escape(mock_db):
    """FIX 5: a misconfigured/deleted/disabled model provider raises
    ProviderConfigError (a ValueError) from build_session_options. Unguarded, it
    escapes the create_task'd coroutine → messageless turn.aborted + an
    unretrieved-exception log. The guard must surface it as a turn error event
    (turn.aborted reason=error carrying the message) and NOT let it escape."""
    from app.core.environment import ProviderConfigError

    sid = "bg-provider-config-err"
    sessions_db.create_session(sid, "default")
    pool = _StubPool(None)

    with ExitStack() as stack:
        _bg_patches(stack, pool)
        stack.enter_context(patch(
            "app.core.sdk_options.build_session_options",
            side_effect=ProviderConfigError("provider 'op' is disabled"),
        ))
        # Must not raise.
        await _run(sid)

    types = _types(sid)
    assert types == ["turn.started", "message.user", "turn.aborted"], types
    aborted = se.get_events(sid)[-1]["payload"]
    assert aborted["reason"] == "error"
    assert "provider 'op' is disabled" in (aborted.get("message") or "")


# ─── persist_turn_end_state (turn_finalize) ─────────────────────────────────


async def test_persist_turn_end_state_records_parked_kind():
    registry = MagicMock()
    registry.get_active_background_work = MagicMock(
        return_value=[{"kind": "shell"}]
    )
    set_kind = MagicMock()

    from app.core import task_registry as task_registry_module
    from app.db import database
    with patch.object(task_registry_module, "task_registry", new=registry), \
         patch.object(database, "set_session_parked_task_kind", new=set_kind):
        await persist_turn_end_state("sess-park")

    set_kind.assert_called_once_with("sess-park", "shell")


async def test_persist_turn_end_state_clears_marker_when_idle_and_never_raises():
    registry = MagicMock()
    registry.get_active_background_work = MagicMock(return_value=[])
    set_kind = MagicMock(side_effect=RuntimeError("boom"))

    from app.core import task_registry as task_registry_module
    from app.db import database
    with patch.object(task_registry_module, "task_registry", new=registry), \
         patch.object(database, "set_session_parked_task_kind", new=set_kind):
        # A persistence failure is swallowed (never breaks turn teardown).
        await persist_turn_end_state("sess-idle")

    set_kind.assert_called_once_with("sess-idle", None)
