"""The REST streaming path (``stream_query``) and the headless one-shot path
(``execute_query``), plus the api_error notification funnel they share.

``stream_query`` serves POST /api/v1/query/stream; ``execute_query`` serves
automations, connectors and wake turns. Both run the same adapter vocabulary as
the chat path; these tests pin the frames/return value, the rows each writes
(through ``run_sync``) and the adapter lifecycle on success and on each
failure.
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
    ServerToolResultBlock,
    ServerToolUseBlock,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from app.core.task_registry import task_registry
from tests.core.engine_harness import collect, make_state, stream_response
from tests.core.query_engine_flow_harness import (
    RecordingPool,
    engine_patches,
    frames_of,
    fresh_adapter,
    scripted_events,
)

SID = "qe-rest"


@pytest.fixture(autouse=True)
def _clean_registry():
    task_registry.clear_session(SID)
    yield
    task_registry.clear_session(SID)


def _result(**kw):
    return ResultMessage(subtype="success", **kw)


def _stream(**kw):
    kw.setdefault("session_id", SID)
    return collect(qe.stream_query("hello", "claude-code", **kw))


# ─── stream_query ───────────────────────────────────────────────────────────


async def test_rest_stream_new_session_frames_rows_and_terminal(tmp_path):
    from app.core.usage_limits import usage_limits_monitor

    info = RateLimitInfo(status="allowed", rate_limit_type="seven_day", utilization=0.5)
    messages = [
        SystemMessage(subtype="init", data={"session_id": "sdk-r1"}),
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1", description="d"),
        TaskProgressMessage(subtype="task_progress", data={}, task_id="t1", description="d2"),
        TaskNotificationMessage(subtype="task_notification", data={}, task_id="t1",
                                status="failed", summary="s"),
        AssistantMessage(content=[
            TextBlock(text="Let me look."),
            ToolUseBlock(id="tu-1", name="Read", input={"file_path": "/a"}),
            ToolResultBlock(tool_use_id="tu-1", content="inline"),
            ServerToolUseBlock(id="srv-1", name="web_search", input={"q": "x"}),
            ServerToolResultBlock(tool_use_id="srv-1", content={"hits": 1}),
        ], model="claude-r", usage={"input_tokens": 12}),
        UserMessage(content=[ToolResultBlock(tool_use_id="tu-1", content="file body")]),
        RateLimitEvent(rate_limit_info=info),
        _result(usage={"input_tokens": 12, "output_tokens": 3}, api_error_status=502),
    ]
    emit = AsyncMock()
    with ExitStack() as stack:
        adapter = fresh_adapter(messages)
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        stack.enter_context(patch.object(
            usage_limits_monitor, "apply_live_event_and_broadcast", new=AsyncMock()))
        stack.enter_context(patch("app.services.notification_service.emit_notification", new=emit))
        events = await collect(qe.stream_query("hello there", "claude-code", device_id="dev-1"))

    new_sid = events[0]["session_id"]
    assert events[0] == {"type": "init", "session_id": new_sid}
    [created] = b.run_sync_named("create_session")
    assert created[2]["session_id"] == new_sid and created[2]["is_active"] is True
    assert created[2]["title"] == "hello there"
    assert b.run_sync_named("add_session_message")[0][2]["content"] == "hello there"
    b.sync_engine.broadcast_message_added.assert_awaited_once()
    assert b.sync_engine.broadcast_message_added.await_args.kwargs["source_device_id"] == "dev-1"
    assert [c[2]["event_type"] for c in b.run_sync_named("add_sync_log")] == ["message_added", "stream_start"]

    types = [e["type"] for e in events]
    assert types == [
        "init", "task_started", "task_progress", "task_notification",
        "text", "tool_use", "tool_result", "tool_use", "tool_result", "turn_usage",
        "tool_result", "rate_limit", "api_error", "done",
    ]
    assert frames_of(events, "task_notification")[0]["status"] == "failed"
    tool_results = frames_of(events, "tool_result")
    assert tool_results[0] == {"type": "tool_result", "name": "Read", "output": "inline"}
    assert tool_results[1]["server_side"] is True and tool_results[1]["name"] == "web_search"
    assert tool_results[2] == {"type": "tool_result", "name": "Read", "tool_use_id": "tu-1",
                               "output": "file body"}
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["message"] == "Service error: HTTP 502"

    adapter.send_query.assert_awaited_once_with("hello there")
    assert ("update_session", (new_sid,), {"launched_model": None, "sdk_session_id": "sdk-r1"}) \
        in b.run_sync_calls
    md = b.persist.await_args.kwargs["metadata"]
    assert md["tokens_in"] == 12 and md["last_call_tokens_in"] == 12
    assert md["model"] == "claude-r"
    [adhoc] = b.run_sync_named("record_adhoc_turn")
    assert adhoc[1] == (new_sid, "api_stream")
    assert adhoc[2]["outcome"] == "completed"
    assert b.pool.state.is_streaming is False
    assert task_registry.get_task(new_sid, "t1")["status"] == "error"
    task_registry.clear_session(new_sid)


async def test_rest_retry_does_not_duplicate_the_user_message(tmp_path):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID, "sdk_session_id": "sdk-0"},
                           adapter=fresh_adapter([_result()]))
        events = await _stream(persist_user_message=False)
    assert not b.run_sync_named("add_session_message")
    b.sync_engine.broadcast_message_added.assert_not_awaited()
    assert events[-1]["type"] == "done"
    # A resumed session keeps its sdk link: the init write is resume-gated.
    assert b.persist.await_args.kwargs["sdk_session_id"] == "sdk-0"


@pytest.mark.parametrize("kw,expected", [
    ({"profile": None}, "Profile not found: claude-code"),
    ({"session_row": None}, f"Session not found: {SID}"),
])
async def test_rest_refuses_unknown_profile_or_session(tmp_path, kw, expected):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), **kw)
        events = await _stream()
    assert events == [{"type": "error", "message": expected}]
    b.make_adapter.assert_not_called()


async def test_rest_refuses_unknown_project(tmp_path):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID})
        events = await _stream(project_id="p-x")
    assert events == [{"type": "error", "message": "Project not found: p-x"}]
    b.make_adapter.assert_not_called()


async def test_rest_refuses_unbuildable_options_and_missing_credentials(tmp_path):
    from app.core import credential_gate

    with ExitStack() as stack:
        engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID})
        stack.enter_context(patch("app.core.sdk_options.build_options_from_profile",
                                  side_effect=ValueError("cwd /gone does not exist")))
        events = await _stream()
    assert events[-1] == {"type": "error", "message": "cwd /gone does not exist"}

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID})
        stack.enter_context(patch.object(credential_gate, "refusal_for_options_async",
                                         new=AsyncMock(return_value="No key")))
        events = await _stream()
    assert events[-1] == credential_gate.refusal_event("No key")
    b.make_adapter.assert_not_called()


async def test_rest_admission_refusal(tmp_path):
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID},
                           pool=RecordingPool(refusal="RAM low"))
        events = await _stream()
    assert events[-1] == {"type": "error",
                          "message": "Server is at capacity — RAM low. Please retry shortly."}
    b.make_adapter.assert_not_called()


@pytest.mark.parametrize("exc,expected", [
    (asyncio.TimeoutError(), "Connection timed out — an MCP server may be unreachable"),
    (ClaudeCliMissingError("Install the CLI"), "Install the CLI"),
    (RuntimeError("spawn failed"), "extracted: spawn failed"),
])
async def test_rest_connect_failures(tmp_path, exc, expected):
    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.connect_with_sdk_options = AsyncMock(side_effect=exc)
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, adapter=adapter)
        stack.enter_context(patch.object(
            qe, "_extract_connection_error", side_effect=lambda e, lines: f"extracted: {e}"))
        events = await _stream()
    assert events[-1] == {"type": "error", "message": expected}
    assert b.pool.state is None
    b.persist.assert_not_awaited()


async def test_rest_mid_stream_crash_ends_on_error_not_done(tmp_path):
    """Regression: the REST stream is documented to end with exactly one of
    done / interrupted / error. A mid-stream crash yielded `error` and then a
    `done` as well, so an API client reading the terminal frame saw a clean
    completion for a turn that died."""

    async def _boom():
        raise RuntimeError("reader died")

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            AssistantMessage(content=[TextBlock(text="partial")], model="m"), _boom])
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, adapter=adapter)
        events = await _stream()
    assert events[-1] == {"type": "error", "message": "reader died"}
    assert [e["type"] for e in events] == ["init", "text", "error"]
    assert ("remove_dead", SID) in b.pool.removed
    [adhoc] = b.run_sync_named("record_adhoc_turn")
    assert adhoc[2]["outcome"] == "aborted"


async def test_rest_exception_group_is_unwrapped(tmp_path):
    async def _boom():
        raise ExceptionGroup("tg", [ValueError("inner cause")])

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([_boom])
        engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, adapter=adapter)
        events = await _stream()
    assert events[-1] == {"type": "error", "message": "inner cause"}
    assert not frames_of(events, "done")


async def test_rest_hard_cancel_reports_interrupted_once(tmp_path):
    async def _cancel():
        raise asyncio.CancelledError()

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([_cancel])
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, adapter=adapter)
        events = await _stream()
    assert [e["type"] for e in events] == ["init", "interrupted"]
    assert b.persist.await_args.kwargs["interrupted"] is True
    [adhoc] = b.run_sync_named("record_adhoc_turn")
    assert adhoc[2]["outcome"] == "aborted" and adhoc[2]["stop_reason"] == "interrupted"


async def test_rest_soft_interrupt_reports_interrupted(tmp_path):
    state = make_state()
    state.connect_options = None

    async def _stop():
        state.interrupt_requested = True

    state.adapter.receive_events = scripted_events([
        AssistantMessage(content=[TextBlock(text="x")], model="m"), _stop])
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID},
                           pool=RecordingPool(state))
        events = await _stream()
    assert events[-1] == {"type": "interrupted", "message": "Query was interrupted"}
    assert not frames_of(events, "done")
    assert b.persist.await_args.kwargs["interrupted"] is True


async def test_rest_reuse_pushes_model_and_mode_into_the_live_client(tmp_path):
    state = make_state()
    state.adapter.receive_events = stream_response([_result()])
    state.adapter.set_model = AsyncMock()
    state.adapter.set_permission_mode = AsyncMock(side_effect=RuntimeError("x"))
    state.connect_options = MagicMock(model="old", permission_mode="default", aih_provider_id=None)
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID},
                           pool=RecordingPool(state))
        b.options.model = "new"
        b.options.permission_mode = "acceptEdits"
        events = await _stream()
    state.adapter.set_model.assert_awaited_once_with("new")
    state.adapter.set_permission_mode.assert_awaited_once_with("acceptEdits")
    assert ("update_session", (SID,), {"launched_model": "new"}) in b.run_sync_calls
    b.make_adapter.assert_not_called()
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("reconnect,alive,removed", [
    (True, True, ("remove", SID, True)),
    (False, False, ("remove_dead", SID)),
])
async def test_rest_replaces_a_stale_or_dead_client(tmp_path, reconnect, alive, removed):
    state = make_state()
    pool = RecordingPool(state)
    with ExitStack() as stack:
        fresh = fresh_adapter([_result()])
        engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool,
                       adapter=fresh, reconnect=reconnect, alive=alive)
        events = await _stream()
    assert pool.removed == [removed]
    state.adapter.send_query.assert_not_awaited()
    fresh.send_query.assert_awaited_once()
    assert events[-1]["type"] == "done"


async def test_rest_busy_client_is_refused(tmp_path):
    state = make_state()
    state.is_streaming = True
    with ExitStack() as stack:
        engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=RecordingPool(state))
        events = await _stream()
    assert "busy" in events[-1]["message"]
    state.adapter.send_query.assert_not_awaited()
    assert state.is_streaming is True


# ─── execute_query (headless) ───────────────────────────────────────────────


def _headless_patches(stack, adapter, session_row=None, project_row=None):
    b = engine_patches(stack, cwd=None, session_row=session_row, project_row=project_row,
                       adapter=adapter)
    sync = b.sync_engine
    stack.enter_context(patch("app.core.sync_engine.sync_engine", new=sync))
    return b


async def test_headless_turn_returns_the_answer_and_records_an_automation_step():
    adapter = fresh_adapter([
        SystemMessage(subtype="init", data={"session_id": "sdk-h"}),
        AssistantMessage(content=[TextBlock(text="line 1"), TextBlock(text="line 2")],
                         model="claude-h", usage={"input_tokens": 30}),
        AssistantMessage(content=[TextBlock(text="sub")], model="claude-h",
                         parent_tool_use_id="tu-s", usage={"input_tokens": 9999}),
        _result(usage={"input_tokens": 30, "output_tokens": 4}, stop_reason="end_turn"),
    ])
    with ExitStack() as stack:
        b = _headless_patches(stack, adapter)
        # An API user may run Claude only on an API key, never the owner's plan.
        stack.enter_context(patch("app.core.credential_gate.claude_cli_uses_subscription", return_value=False))
        out = await qe.execute_query("do the thing", "claude-code", automation_id="auto-1",
                                     overrides={"model": "x"}, api_user_id="api-1")

    sid = out["session_id"]
    assert out["response"] == "line 1\nline 2\nsub"
    md = out["metadata"]
    assert md["model"] == "claude-h"
    assert md["last_call_tokens_in"] == 30
    assert (md["tokens_in"], md["tokens_out"]) == (30, 4)
    [created] = b.run_sync_named("create_session")
    assert created[2]["automation_id"] == "auto-1" and created[2]["is_active"] is False
    adapter.connect_with_sdk_options.assert_awaited_once()
    adapter.send_query.assert_awaited_once_with("do the thing")
    adapter.disconnect.assert_awaited_once()
    assert ("update_session", (sid,), {"launched_model": None, "sdk_session_id": "sdk-h"}) \
        in b.run_sync_calls
    [adhoc] = b.run_sync_named("record_adhoc_turn")
    assert adhoc[1] == (sid, "automation_step")
    assert adhoc[2]["outcome"] == "completed"
    assert adhoc[2]["payload"] == {"api_user_id": "api-1"}
    assert adhoc[2]["overrides"] == {"model": "x"}
    b.sync_engine.broadcast_user_session_created.assert_awaited_once()
    updated = b.sync_engine.broadcast_session_updated.await_args.kwargs
    assert updated["session_id"] == sid and updated["updates"]["has_new_messages"] is True


async def test_headless_error_result_is_recorded_as_aborted_and_notifies():
    adapter = fresh_adapter([
        RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", rate_limit_type="five_hour")),
        _result(is_error=True, errors=["overloaded"], api_error_status=529),
    ])
    live = AsyncMock()
    notify = AsyncMock()
    with ExitStack() as stack:
        b = _headless_patches(stack, adapter, session_row={"id": SID, "sdk_session_id": "sdk-old"})
        stack.enter_context(patch.object(qe, "_apply_rate_limit_live_event", new=live))
        stack.enter_context(patch.object(qe, "_emit_api_error_notification", new=notify))
        out = await qe.execute_query("x", "claude-code", session_id=SID, persist_user_message=False)
    assert out["metadata"]["errors"] == ["overloaded"]
    assert out["metadata"]["api_error_status"] == 529
    live.assert_awaited_once()
    notify.assert_awaited_once_with(None, SID, 529, delegated=False)
    assert not b.run_sync_named("add_session_message")
    [adhoc] = b.run_sync_named("record_adhoc_turn")
    assert adhoc[1] == (SID, "api_query")
    assert adhoc[2]["outcome"] == "aborted" and adhoc[2]["error"] == "['overloaded']"


@pytest.mark.parametrize("raised,expected", [
    (RuntimeError("adapter died"), RuntimeError),
    (ExceptionGroup("tg", [KeyError("inner")]), KeyError),
])
async def test_headless_adapter_failure_propagates_and_tears_down(raised, expected):
    async def _boom():
        raise raised

    adapter = fresh_adapter()
    adapter.receive_events = scripted_events([_boom])
    adapter.disconnect = AsyncMock(side_effect=RuntimeError("already gone"))
    with ExitStack() as stack:
        b = _headless_patches(stack, adapter, session_row={"id": SID})
        with pytest.raises(expected):
            await qe.execute_query("x", "claude-code", session_id=SID)
    adapter.disconnect.assert_awaited_once()
    b.persist.assert_not_awaited()
    assert not b.run_sync_named("record_adhoc_turn")


async def test_headless_refusals_raise_before_spawning():
    from app.core import credential_gate

    with ExitStack() as stack:
        b = _headless_patches(stack, fresh_adapter(), session_row={"id": SID})
        stack.enter_context(patch.object(credential_gate, "refusal_for_options_async",
                                         new=AsyncMock(return_value="No provider configured")))
        with pytest.raises(credential_gate.AgentSetupRequired, match="No provider configured"):
            await qe.execute_query("x", "claude-code", session_id=SID)
        b.make_adapter.assert_not_called()

    for kwargs, match in [
        ({"session_id": "missing"}, "Session not found"),
        ({"project_id": "p-missing"}, "Project not found"),
    ]:
        with ExitStack() as stack:
            _headless_patches(stack, fresh_adapter(), session_row=None, project_row=None)
            with pytest.raises(ValueError, match=match):
                await qe.execute_query("x", "claude-code", **kwargs)

    with ExitStack() as stack:
        engine_patches(stack, profile=None)
        with pytest.raises(ValueError, match="Profile not found"):
            await qe.execute_query("x", "claude-code")


# ─── the api_error notification funnel ─────────────────────────────────────


@pytest.mark.parametrize("status,message,payload", [
    ("oops", "An API error occurred.", {"session_id": "s"}),
    (None, "An API error occurred.", {"session_id": "s"}),
    ("500", "Service error: HTTP 500", {"status_code": 500, "session_id": "s"}),
])
async def test_api_error_notification_shapes(status, message, payload):
    emit = AsyncMock()
    with patch("app.services.notification_service.emit_notification", new=emit):
        await qe._emit_api_error_notification(None, "s", status)
    assert emit.await_args.args == ("admin",)
    assert emit.await_args.kwargs["message"] == message
    assert emit.await_args.kwargs["action"] == {"kind": "fix_with_ai", "payload": payload}
    assert emit.await_args.kwargs["persist"] is True


async def test_api_error_notification_never_raises():
    with patch("app.services.notification_service.emit_notification",
               new=AsyncMock(side_effect=RuntimeError("bell down"))):
        await qe._emit_api_error_notification("u", "s", 500)


# ─── small helpers the streaming paths lean on ─────────────────────────────


def test_task_output_file_is_read_and_capped(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("x" * 50)
    assert qe._read_task_output_file(str(f)) == "x" * 50
    assert qe._read_task_output_file("") == ""
    assert qe._read_task_output_file(str(tmp_path / "missing")) == ""
    assert "truncated" in qe._read_task_output_file(str(f), max_chars=10)
    with patch.object(qe, "truncate_large_payload", side_effect=RuntimeError):
        assert qe._read_task_output_file(str(f), max_chars=10) == "x" * 10 + "\n…[truncated]"


def test_task_messages_without_a_session_or_with_a_broken_registry_are_ignored():
    msg = TaskStartedMessage(subtype="task_started", data={}, task_id="t", tool_use_id="u")
    qe._record_task_message(None, msg)
    with patch.object(task_registry, "start_task", side_effect=RuntimeError("registry down")):
        qe._record_task_message(SID, msg)  # swallowed
    assert task_registry.get_active(SID) == []
