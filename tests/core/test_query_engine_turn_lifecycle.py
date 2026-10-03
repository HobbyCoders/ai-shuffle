"""A chat turn's lifecycle through ``_stream_to_websocket_impl``.

Pool reuse and reconnect, refusals before spawn, the connect and mid-stream
failure paths, soft and hard interrupts, rate-limit / api_error frames, plugin
stderr, and the frames a user sees for API errors and slash commands. Every
test asserts what reaches the client (frames), what the engine hands to
persistence, and what it does to the pool and adapter.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fluid.engine.delegated import ClaudeCliMissingError

import app.core.query_engine as qe
from app.core.provider.events import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    UserMessage,
)
from app.core.query_options import WorktreeUnavailableError
from tests.core.engine_harness import collect, make_state, stream_response
from tests.core.query_engine_flow_harness import (
    RecordingPool,
    engine_patches,
    frames_of,
    fresh_adapter,
    scripted_events,
)

SID = "qe-lifecycle"


def _pooled_state(messages=(), *, has_approver=True, connect_model=None, connect_mode="default"):
    state = make_state()
    state.adapter.has_permission_callback = has_approver
    state.adapter.receive_events = stream_response(list(messages))
    state.adapter.set_model = AsyncMock()
    state.adapter.set_permission_mode = AsyncMock()
    state.connect_options = MagicMock(
        model=connect_model, permission_mode=connect_mode, aih_provider_id=None,
    )
    return state


async def _turn(stack, prompt="hi", **kwargs):
    return await collect(qe._stream_to_websocket_impl(prompt, SID, "claude-code", **kwargs))


def _result(**kw):
    return ResultMessage(subtype="success", **kw)


# ─── reuse, reconnect, dead client ──────────────────────────────────────────


async def test_interactive_turn_replaces_a_pooled_client_that_has_no_approver(tmp_path):
    """A client a headless path built has no permission callback and cannot
    get one; an interactive turn must not reuse it or its prompts would never
    reach the user (e19cad9f)."""
    headless = _pooled_state(has_approver=False)
    pool = RecordingPool(headless)
    with ExitStack() as stack:
        fresh = fresh_adapter([_result()])
        b = engine_patches(stack, pool=pool, cwd=str(tmp_path), adapter=fresh)
        events = await _turn(stack, broadcast_func=AsyncMock())

    assert pool.removed == [("remove", SID, True)]
    headless.adapter.send_query.assert_not_awaited()
    fresh.connect_with_sdk_options.assert_awaited_once_with(b.options)
    fresh.send_query.assert_awaited_once_with("hi")
    assert callable(b.options.can_use_tool)
    assert pool.state.adapter is fresh
    assert frames_of(events, "done")


async def test_headless_turn_keeps_reusing_a_client_without_an_approver(tmp_path):
    headless = _pooled_state([_result()], has_approver=False)
    pool = RecordingPool(headless)
    with ExitStack() as stack:
        b = engine_patches(stack, pool=pool, cwd=str(tmp_path))
        events = await _turn(stack)
    assert pool.removed == []
    b.make_adapter.assert_not_called()
    headless.adapter.send_query.assert_awaited_once_with("hi")
    assert frames_of(events, "done")


async def test_reuse_restamps_refs_and_pushes_mode_and_model_into_the_live_client(tmp_path):
    state = _pooled_state([_result()], connect_model="old", connect_mode="default")
    state.callback_refs.update({"attention_count": 3, "permission_mode": "default"})
    broadcast = AsyncMock()
    with ExitStack() as stack:
        b = engine_patches(stack, pool=RecordingPool(state), cwd=str(tmp_path))
        b.options.model = "new"
        b.options.permission_mode = "acceptEdits"
        events = await _turn(stack, overrides={"permission_mode": "acceptEdits"},
                             broadcast_func=broadcast, user_id="u-2")

    state.adapter.set_model.assert_awaited_once_with("new")
    state.adapter.set_permission_mode.assert_awaited_once_with("acceptEdits")
    assert ("update_session", (SID,), {"launched_model": "new"}) in b.run_sync_calls
    refs = state.callback_refs
    assert refs["permission_mode"] == "acceptEdits"
    assert refs["attention_count"] == 0
    assert refs["user_id"] == "u-2"
    assert refs["broadcast_func"] is None  # cleared again at turn end
    assert state.is_streaming is False
    assert frames_of(events, "done")


async def test_reuse_survives_a_failed_live_model_and_mode_switch(tmp_path):
    state = _pooled_state([_result()], connect_model="old", connect_mode="default")
    state.adapter.set_model = AsyncMock(side_effect=RuntimeError("no such model"))
    state.adapter.set_permission_mode = AsyncMock(side_effect=RuntimeError("nope"))
    with ExitStack() as stack:
        b = engine_patches(stack, pool=RecordingPool(state), cwd=str(tmp_path))
        b.options.model = "new"
        b.options.permission_mode = "acceptEdits"
        events = await _turn(stack)
    state.adapter.send_query.assert_awaited_once()
    assert not [c for c in b.run_sync_calls if c[2].get("launched_model") == "new"]
    assert frames_of(events, "done")
    assert state.is_streaming is False


async def test_changed_options_replace_the_pooled_client(tmp_path):
    state = _pooled_state()
    pool = RecordingPool(state)
    with ExitStack() as stack:
        fresh = fresh_adapter([_result()])
        engine_patches(stack, pool=pool, cwd=str(tmp_path), adapter=fresh, reconnect=True)
        await _turn(stack)
    assert pool.removed == [("remove", SID, True)]
    state.adapter.send_query.assert_not_awaited()
    fresh.send_query.assert_awaited_once()


async def test_dead_pooled_client_is_dropped_and_replaced(tmp_path):
    state = _pooled_state()
    pool = RecordingPool(state)
    with ExitStack() as stack:
        fresh = fresh_adapter([_result()])
        engine_patches(stack, pool=pool, cwd=str(tmp_path), adapter=fresh, alive=False)
        events = await _turn(stack)
    assert pool.removed == [("remove_dead", SID)]
    fresh.send_query.assert_awaited_once()
    assert frames_of(events, "done")


# ─── refusals before anything spawns ────────────────────────────────────────


async def test_pool_at_capacity_refuses_before_spawning(tmp_path):
    pool = RecordingPool(refusal="3 sessions running")
    with ExitStack() as stack:
        b = engine_patches(stack, pool=pool, cwd=str(tmp_path))
        events = await _turn(stack)
    assert events == [{
        "type": "error",
        "message": "Server is at capacity — 3 sessions running. Please retry shortly.",
    }]
    b.make_adapter.assert_not_called()
    assert pool.state is None
    b.persist.assert_not_awaited()


async def test_missing_profile_refuses(tmp_path):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), profile=None)
        events = await _turn(stack)
    assert events == [{"type": "error", "message": "Profile not found: claude-code"}]
    b.make_adapter.assert_not_called()


async def test_missing_project_refuses(tmp_path):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), project_row=None)
        events = await collect(qe._stream_to_websocket_impl(
            "hi", SID, "claude-code", project_id="proj-gone"))
    assert events == [{"type": "error", "message": "Project not found: proj-gone"}]
    b.make_adapter.assert_not_called()


@pytest.mark.parametrize("err", [
    WorktreeUnavailableError("worktree feature-x is gone"),
    ValueError("Working directory /gone does not exist"),
])
async def test_unbuildable_options_refuse_instead_of_running_elsewhere(tmp_path, err):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path))
        stack.enter_context(patch(
            "app.core.sdk_options.build_options_from_profile", side_effect=err,
        ))
        events = await _turn(stack)
    expected = {"type": "error", "message": str(err)}
    if isinstance(err, WorktreeUnavailableError):
        expected["code"] = "checkout_unavailable"
    assert events == [expected]
    b.make_adapter.assert_not_called()


async def test_credential_gate_refuses_with_the_setup_code(tmp_path):
    from app.core import credential_gate

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path))
        stack.enter_context(patch.object(
            credential_gate, "turn_refusal_async", new=AsyncMock(return_value="Sign in first"),
        ))
        events = await _turn(stack)
    assert events == [credential_gate.refusal_event("Sign in first")]
    b.make_adapter.assert_not_called()


async def test_loop_guard_holds_an_unattended_turn(tmp_path):
    from app.core import loop_guard

    turn_row = {"id": "turn-1", "payload": {}}
    hold = MagicMock()
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path))
        check = stack.enter_context(patch.object(loop_guard, "check", return_value="Hourly budget reached"))
        stack.enter_context(patch.object(loop_guard, "hold", new=hold))
        events = await _turn(stack, turn_source="automation", loop_guard_turn=turn_row)
    assert events == [{"type": "error", "message": "Hourly budget reached",
                       "code": loop_guard.HOLD_CODE}]
    assert check.call_args.kwargs == {"confirmed": False}
    hold.assert_called_once_with(turn_row, "Hourly budget reached")
    b.make_adapter.assert_not_called()


async def test_assistant_limit_refuses_the_turn(tmp_path):
    from app.core import assistant_limits

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path))
        gate = stack.enter_context(patch.object(assistant_limits, "turn_refusal", return_value="Limit reached"))
        events = await _turn(stack, turn_source="user")
    assert events == [{"type": "error", "message": "Limit reached", "code": "assistant_limit"}]
    assert gate.call_args.args[1] == "user"
    b.make_adapter.assert_not_called()


async def test_upload_materialization_failure_refuses(tmp_path):
    from app.core import upload_transfer

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path))
        stack.enter_context(patch.object(
            upload_transfer, "materialize_blobs_for_turn", side_effect=OSError("disk full"),
        ))
        events = await _turn(stack, upload_blob_ids=["blob-1"])
    assert events == [{"type": "error",
                       "message": "Couldn't prepare this message's attachments: disk full"}]
    b.make_adapter.assert_not_called()


async def test_uploads_are_materialized_and_the_prompt_rewritten(tmp_path):
    from app.core import upload_transfer

    with ExitStack() as stack:
        adapter = fresh_adapter([_result()])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        mat = stack.enter_context(patch.object(
            upload_transfer, "materialize_blobs_for_turn", return_value={"/far/a.png": "/near/a.png"},
        ))
        await _turn(stack, prompt="see /far/a.png", upload_blob_ids=["blob-1"])
    mat.assert_called_once_with(["blob-1"], str(tmp_path))
    adapter.send_query.assert_awaited_once_with("see /near/a.png")


# ─── connect failures ───────────────────────────────────────────────────────


async def test_missing_cli_surfaces_the_setup_message_without_pooling(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.connect_with_sdk_options = AsyncMock(
            side_effect=ClaudeCliMissingError("Install the Claude CLI"))
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    assert events == [{"type": "error", "message": "Install the Claude CLI"}]
    assert b.pool.state is None
    adapter.send_query.assert_not_awaited()


async def test_connect_crash_surfaces_an_error_without_pooling(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.connect_with_sdk_options = AsyncMock(side_effect=RuntimeError("boom"))
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        stack.enter_context(patch.object(
            qe, "_extract_connection_error", return_value="The agent failed to start: boom"))
        events = await _turn(stack)
    assert events == [{"type": "error", "message": "The agent failed to start: boom"}]
    assert b.pool.state is None
    b.persist.assert_not_awaited()


# ─── mid-stream failure and early end ───────────────────────────────────────


async def test_adapter_crash_mid_stream_errors_persists_and_drops_the_client(tmp_path):
    async def _boom():
        raise RuntimeError("Fatal error in message reader")

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
            AssistantMessage(content=[TextBlock(text="partial")], model="m"),
            _boom,
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)

    types = [e["type"] for e in events]
    assert types[-1] == "error"
    assert events[-1]["message"] == "Fatal error in message reader"
    assert "done" not in types and "interrupted" not in types
    # The partial turn and the SDK link still persist.
    b.persist.assert_awaited_once()
    assert b.persist.await_args.kwargs["sdk_session_id"] == "sdk-1"
    assert b.persist.await_args.kwargs["interrupted"] is False
    assert ("remove_dead", SID) in b.pool.removed
    assert ("update_session", (SID,), {"launched_model": None, "sdk_session_id": "sdk-1"}) \
        in b.run_sync_calls


async def test_turn_metadata_names_the_provider_leg_for_settlement(tmp_path):
    """Settlement attributes the ledger row to the binding the turn actually
    ran on (#1614), read off the options rather than the session row."""
    from app.core.query_persistence import USAGE_PROVIDER_KEY

    with ExitStack() as stack:
        adapter = fresh_adapter([_result(num_turns=1)])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        b.options.aih_provider_id = "p-codex"
        await _turn(stack)
    metadata = b.persist.await_args.kwargs["metadata"]
    assert metadata["usage_settlement_key"] == "settle-key"
    assert metadata[USAGE_PROVIDER_KEY] == "p-codex"


async def test_stream_that_ends_without_a_result_still_completes(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([AssistantMessage(content=[TextBlock(text="hi")], model="m")])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    assert events[-1]["type"] == "done"
    assert events[-1]["session_id"] == SID
    assert b.persist.await_args.kwargs["interrupted"] is False
    assert b.pool.state.is_streaming is False
    assert b.pool.state.is_connected is True


# ─── interrupts ─────────────────────────────────────────────────────────────


async def test_soft_interrupt_ends_the_turn_as_interrupted(tmp_path):
    """adapter.interrupt() makes receive_events end naturally; the engine must
    read the flag and report an interrupted turn, not a completed one."""
    state_box = {}

    async def _user_pressed_stop():
        state_box["pool"].state.interrupt_requested = True

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            AssistantMessage(content=[TextBlock(text="working")], model="m"),
            _user_pressed_stop,
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        state_box["pool"] = b.pool
        events = await _turn(stack)

    assert events[-1] == {
        "type": "interrupted", "session_id": SID, "message": "Query was interrupted",
        "metadata": {"terminal_reason": None},
    }
    assert not frames_of(events, "done")
    assert b.persist.await_args.kwargs["interrupted"] is True
    assert b.pool.state.interrupt_requested is False  # reset for the next turn


async def test_hard_cancel_mid_stream_ends_the_turn_as_interrupted(tmp_path):
    async def _cancelled():
        raise asyncio.CancelledError()

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            AssistantMessage(content=[TextBlock(text="working")], model="m"),
            _cancelled,
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    assert events[-1]["type"] == "interrupted"
    assert b.persist.await_args.kwargs["interrupted"] is True
    assert b.pool.state.is_streaming is False
    assert b.pool.state.is_connected is True


# ─── rate limit and api_error ───────────────────────────────────────────────


async def test_rate_limit_event_reaches_the_client_and_the_shared_snapshot(tmp_path):
    from app.core.config import settings
    from app.core.usage_limits import usage_limits_monitor

    info = RateLimitInfo(status="allowed_warning", resets_at=1700000000,
                         rate_limit_type="five_hour", utilization=0.91)
    with ExitStack() as stack:
        adapter = fresh_adapter([RateLimitEvent(rate_limit_info=info), _result()])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        live = stack.enter_context(patch.object(
            usage_limits_monitor, "apply_live_event_and_broadcast", new=AsyncMock()))
        events = await _turn(stack)
    [frame] = frames_of(events, "rate_limit")
    assert frame["status"] == "allowed_warning"
    assert frame["utilization"] == 0.91
    assert frame["session_id"] == SID
    assert frame["instance_id"] == settings.effective_instance_id
    live.assert_awaited_once_with("five_hour", 0.91, 1700000000, status="allowed_warning")
    assert frames_of(events, "done")


@pytest.mark.parametrize("status,notifies,message", [
    (503, True, "Service error: HTTP 503"),
    (400, True, "API error: HTTP 400"),
    (429, False, None),  # account_failover owns the 429 notification
])
async def test_api_error_status_frames_and_notifies(tmp_path, status, notifies, message):
    emit = AsyncMock()
    with ExitStack() as stack:
        adapter = fresh_adapter([_result(api_error_status=status)])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        stack.enter_context(patch("app.services.notification_service.emit_notification", new=emit))
        events = await _turn(stack, api_user_id="api-7")
    types = [e["type"] for e in events]
    assert types.index("api_error") < types.index("done")
    assert frames_of(events, "api_error") == [{"type": "api_error", "status": status, "session_id": SID}]
    assert b.persist.await_args.kwargs["metadata"]["api_error_status"] == status
    if notifies:
        emit.assert_awaited_once()
        assert emit.await_args.args == ("api-7",)
        kw = emit.await_args.kwargs
        assert kw["message"] == message
        assert kw["category"] == "api_error"
        assert kw["severity"] == "error"
        assert kw["action"] == {"kind": "fix_with_ai",
                                "payload": {"status_code": status, "session_id": SID}}
    else:
        emit.assert_not_awaited()


async def test_assistant_api_error_message_becomes_a_system_error_card(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            AssistantMessage(content=[TextBlock(text="API Error: overloaded")],
                             model="<synthetic>", error="rate_limit"),
            _result(),
        ])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    assert frames_of(events, "system") == [{
        "type": "system", "subtype": "api_error", "data": {"content": "API Error: overloaded"},
    }]
    assert not frames_of(events, "chunk")


# ─── plugin stderr ──────────────────────────────────────────────────────────


async def test_plugin_hook_stderr_is_forwarded_as_plugin_messages(tmp_path):
    box = {}

    async def _hook_writes_stderr():
        await box["b"].hooks["stderr_callback"]("lint-plugin", "warning: slow", "warning")

    async def _hook_writes_late():
        await box["b"].hooks["stderr_callback"]("lint-plugin", "done", "info")

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            _hook_writes_stderr,
            AssistantMessage(content=[TextBlock(text="ok")], model="m"),
            _hook_writes_late,
        ])
        box["b"] = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack, project_id=None)
    plugin = frames_of(events, "plugin_message")
    assert plugin == [
        {"type": "plugin_message", "plugin": "lint-plugin", "message": "warning: slow", "severity": "warning"},
        {"type": "plugin_message", "plugin": "lint-plugin", "message": "done", "severity": "info"},
    ]
    types = [e["type"] for e in events]
    # The first reaches the client before the text it preceded; the late one
    # is drained after the stream ends, before the terminal frame.
    assert types.index("plugin_message") < types.index("chunk")
    assert types[-2:] == ["plugin_message", "done"]


# ─── slash commands and stream frames ───────────────────────────────────────


async def test_slash_command_without_output_gets_a_completion_note(tmp_path):
    with ExitStack() as stack:
        engine_patches(stack, cwd=str(tmp_path), adapter=fresh_adapter([_result()]))
        events = await _turn(stack, prompt="/memory")
    assert frames_of(events, "system") == [{
        "type": "system", "subtype": "local_command",
        "data": {"content": "Command /memory completed (no output)."},
    }]


async def test_slash_command_output_is_forwarded_from_every_shape(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            AssistantMessage(content=[TextBlock(text="  Total cost: $0.01 ")], model="<synthetic>"),
            UserMessage(content="<local-command-stdout>ctx 40%</local-command-stdout>"),
            UserMessage(content=[TextBlock(text="Compacted.")]),
            UserMessage(content="No response requested."),
            _result(),
        ])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack, prompt="/cost")
    contents = [e["data"]["content"] for e in frames_of(events, "system")]
    assert contents == ["Total cost: $0.01", "ctx 40%", "Compacted."]


async def test_synthetic_output_is_hidden_for_a_normal_prompt(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            AssistantMessage(content=[TextBlock(text="local output")], model="<synthetic>"),
            UserMessage(content=[TextBlock(text="echoed")]),
            _result(),
        ])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack, prompt="hello")
    assert not frames_of(events, "system")
    assert not frames_of(events, "chunk")


async def test_partial_stream_events_map_to_stream_frames(tmp_path):
    def ev(event, parent=None):
        return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=parent)

    with ExitStack() as stack:
        adapter = fresh_adapter([
            ev({"type": "message_start", "message": {"id": "m1"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
            ev({"type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hmm", "subtype": "reasoning_summary"}}),
            ev({"type": "content_block_stop", "index": 0}),
            ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}}),
            ev({"type": "content_block_delta", "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": "{\"a\"", "name": "Bash"}}),
            ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
            ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "sub"}}, parent="tu-sub"),
            ev({"type": "content_block_start", "content_block": {"type": "text"}}, parent="tu-sub"),
            AssistantMessage(content=[TextBlock(text="Hi")], model="m"),
            _result(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        b.options.include_partial_messages = True
        events = await _turn(stack)
    shaped = [e for e in events if e["type"].startswith("stream_") or e["type"] == "subagent_chunk"]
    assert shaped == [
        {"type": "stream_start", "message": {"id": "m1"}},
        {"type": "stream_block_start", "block_type": "thinking", "index": 0,
         "content_block": {"type": "thinking"}},
        {"type": "stream_delta", "delta_type": "thinking", "content": "hmm", "index": 0,
         "subtype": "reasoning_summary"},
        {"type": "stream_block_stop", "index": 0},
        {"type": "stream_delta", "delta_type": "text", "content": "Hi", "index": 1},
        {"type": "stream_delta", "delta_type": "tool_input", "content": "{\"a\"", "index": 2,
         "tool_name": "Bash"},
        {"type": "stream_message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 3}},
        {"type": "subagent_chunk", "agent_id": "tu-sub", "content": "sub"},
    ]
    # With partial messages on, the final text is not re-sent as a chunk.
    assert not frames_of(events, "chunk")


async def test_system_messages_forward_except_noise(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            SystemMessage(subtype="status", data={"status": "requesting"}),
            SystemMessage(subtype="status", data={"status": "compacting"}),
            SystemMessage(subtype="rate_limit_event", data={}),
            SystemMessage(subtype="skill_error", data={"parent_tool_use_id": "tu-sub"}),
            SystemMessage(subtype="skill_error", data={"message": "bad skill"}),
            object(),  # unknown frame: logged and skipped
            _result(),
        ])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    assert frames_of(events, "system") == [
        {"type": "system", "subtype": "status", "data": {"status": "compacting"}},
        {"type": "system", "subtype": "skill_error", "data": {"message": "bad skill"}},
    ]
    assert frames_of(events, "done")


# ─── usage settlement and model info ────────────────────────────────────────


async def test_usage_is_settled_from_the_last_main_lane_call(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            SystemMessage(subtype="init", data={"session_id": "sdk-9", "model": "claude-x"}),
            AssistantMessage(content=[TextBlock(text="a")], model="claude-x", uuid="asst-1",
                             usage={"input_tokens": 100, "cache_read_input_tokens": 10}),
            AssistantMessage(content=[TextBlock(text="sub")], model="claude-sub",
                             parent_tool_use_id="tu-sub", usage={"input_tokens": 99999}),
            AssistantMessage(content=[TextBlock(text="b")], model="claude-y", uuid="asst-2",
                             usage={"input_tokens": 250, "cache_creation_input_tokens": 5}),
            _result(usage={"input_tokens": 350, "output_tokens": 40}, total_cost_usd=0.02,
                    num_turns=2, model_usage={"claude-y": {"costUSD": 0.02}}),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        b.options.model = "claude-x"
        events = await _turn(stack)

    models = [e["data"]["model"] for e in frames_of(events, "system")
              if e["subtype"] == "model_info"]
    assert models == ["claude-x", "claude-y"]
    usages = [e["usage"]["input_tokens"] for e in frames_of(events, "turn_usage")]
    assert usages == [100, 250]  # the subagent's usage never reaches the main lane
    assert [e["agent_id"] for e in frames_of(events, "subagent_chunk")] == ["tu-sub"]
    md = b.persist.await_args.kwargs["metadata"]
    assert md["usage_settlement_key"] == "settle-key"
    assert md["last_call_tokens_in"] == 250
    assert md["last_call_cache_creation_tokens"] == 5
    assert md["last_call_cache_read_tokens"] == 0
    assert md["last_assistant_uuid"] == "asst-2"
    assert (md["tokens_in"], md["tokens_out"]) == (350, 40)
    assert md["total_cost_usd"] == 0.02
    assert md["model_usage"] == {"claude-y": {"costUSD": 0.02}}
    assert events[-1]["metadata"] is md


async def test_server_tools_and_screenshots_reach_the_right_lane(tmp_path):
    from app.core.provider.events import (
        ServerToolResultBlock,
        ServerToolUseBlock,
        ToolResultBlock,
    )

    png = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    with ExitStack() as stack:
        adapter = fresh_adapter([
            AssistantMessage(content=[
                ServerToolUseBlock(id="srv-1", name="web_search", input={"query": "q"}),
                ServerToolResultBlock(tool_use_id="srv-1", content={"results": []}),
            ], model="m"),
            AssistantMessage(content=[ToolResultBlock(tool_use_id="tu-s", content=[png])],
                             model="m", parent_tool_use_id="tu-p"),
            UserMessage(content=[ToolResultBlock(tool_use_id="tu-top", content=[png])]),
            _result(),
        ])
        engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _turn(stack)
    [use] = frames_of(events, "tool_use")
    assert use == {"type": "tool_use", "name": "web_search", "id": "srv-1",
                   "input": {"query": "q"}, "server_side": True}
    results = frames_of(events, "tool_result")
    assert results[0]["server_side"] is True and results[0]["name"] == "web_search"
    assert results[1]["tool_use_id"] == "tu-top"
    assert results[1]["screenshot"] == "data:image/png;base64,QUJD"
    [sub] = frames_of(events, "subagent_tool_result")
    assert sub["agent_id"] == "tu-p" and sub["screenshot"] == "data:image/png;base64,QUJD"
